"""Tests for the background worker, with transcription and pipeline faked."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_notes import worker
from meeting_notes.jobs import DONE, FAILED, PENDING, RUNNING, JobStore
from meeting_notes.note_maker import NoteMaker


class FakeTranscriber:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def load_model(self):
        pass

    def transcribe(self, audio_path):
        self.calls += 1
        if self.error:
            raise self.error
        segment = SimpleNamespace(start=0.0, end=61.0, text=" Hello there.")
        return SimpleNamespace(text="Hello there.", segments=[segment])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "meeting-notes").mkdir(parents=True)
    wav = tmp_path / "2026-10-21-204722.wav"
    wav.write_bytes(b"x")
    return SimpleNamespace(
        store=JobStore(tmp_path / "jobs.json"),
        wav=wav,
        note_maker=NoteMaker(str(tmp_path / "notes"), str(tmp_path / "transcripts"), ai_provider="none"),
        pipeline_calls=[],
        tmp=tmp_path,
    )


def run_job(env, transcriber):
    job = env.store.claim_next()
    worker.process_job(env.store, job, transcriber, env.note_maker)
    return env.store.all()[0]


def enable_pipeline(env, monkeypatch, error=None):
    def fake_pipeline(note_path, transcript_path):
        env.pipeline_calls.append((note_path, transcript_path))
        if error:
            raise error
    monkeypatch.setattr(worker, "run_pipeline", fake_pipeline)


def test_job_creates_note_from_queue_data(env):
    env.store.add(str(env.wav), "Planning", "my notes")
    job = run_job(env, FakeTranscriber())

    assert job.status == DONE
    note = Path(job.note_path).read_text()
    assert 'title: "Planning"' in note
    assert "my notes" in note
    assert 'recording_file: "2026-10-21-204722.wav"' in note
    assert "Hello there." in Path(job.transcript_path).read_text()


def test_note_is_dated_when_the_meeting_happened(env):
    env.store.add(str(env.wav))
    job = run_job(env, FakeTranscriber())
    assert Path(job.note_path).name.startswith("2026-10-21-204722-meeting-2026-10-21-2047")
    assert "date: 2026-10-21" in Path(job.note_path).read_text()


def test_pipeline_receives_note_and_transcript(env, monkeypatch):
    enable_pipeline(env, monkeypatch)
    env.store.add(str(env.wav))
    job = run_job(env, FakeTranscriber())
    assert env.pipeline_calls == [(job.note_path, job.transcript_path)]
    assert job.filed is True


def test_failed_transcription_is_recorded_and_keeps_the_job(env):
    env.store.add(str(env.wav))
    job = run_job(env, FakeTranscriber(error=RuntimeError("out of memory")))
    assert (job.status, job.error) == (FAILED, "out of memory")
    assert job.note_path == ""
    assert str(env.wav.resolve()) in env.store.unfinished_paths()


def test_missing_recording_fails_clearly(env):
    env.store.add(str(env.wav))
    env.wav.unlink()
    job = run_job(env, FakeTranscriber())
    assert job.status == FAILED
    assert "Recording not found" in job.error


def test_retry_after_pipeline_failure_does_not_transcribe_again(env, monkeypatch):
    enable_pipeline(env, monkeypatch, error=RuntimeError("Note pipeline failed"))
    env.store.add(str(env.wav))
    transcriber = FakeTranscriber()
    job = run_job(env, transcriber)
    assert job.status == FAILED
    assert job.note_path

    enable_pipeline(env, monkeypatch)
    env.store.retry_failed()
    job = run_job(env, transcriber)
    assert job.status == DONE
    assert transcriber.calls == 1


def test_finished_pipeline_is_not_repeated_on_a_later_run(env, monkeypatch):
    enable_pipeline(env, monkeypatch)
    env.store.add(str(env.wav))
    env.store.update(str(env.wav), status=PENDING, note_path="/note.md", transcript_path="/t.txt", filed=True)
    job = run_job(env, FakeTranscriber())
    assert job.status == DONE
    assert env.pipeline_calls == []


def test_pipeline_is_skipped_without_a_config_file(env, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("pipeline should not run")
    monkeypatch.setattr(worker.subprocess, "run", fail)
    worker.run_pipeline("/note.md", "/t.txt")


def test_pipeline_failure_raises_with_output(env, monkeypatch):
    (env.tmp / "config" / "meeting-notes" / "process-meeting.conf").write_text("")
    result = SimpleNamespace(returncode=1, stdout="⚠ Notion export failed", stderr="")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: result)
    with pytest.raises(RuntimeError, match="Notion export failed"):
        worker.run_pipeline("/note.md", "/t.txt")


def test_run_worker_processes_queue_and_recovers_interrupted_job(env, monkeypatch):
    monkeypatch.setattr(worker, "WhisperTranscriber", lambda *a, **k: FakeTranscriber())
    monkeypatch.setattr(worker, "build_note_maker", lambda config: env.note_maker)
    env.store.add(str(env.wav))
    assert env.store.claim_next().status == RUNNING

    worker.run_worker(env.store)

    job = env.store.all()[0]
    assert job.status == DONE
    assert job.attempts == 2


def test_only_one_worker_runs_at_a_time(env, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("second worker should not start")
    monkeypatch.setattr(worker, "drain", fail)
    assert worker.worker_running() is False
    fd = worker._acquire_worker_lock()
    try:
        assert worker.worker_running() is True
        worker.run_worker(env.store)
    finally:
        os.close(fd)
    assert worker.worker_running() is False


def test_add_recordings_skips_known_and_missing_files(env, capsys):
    env.store.add(str(env.wav))
    env.store.update(str(env.wav), status=DONE)
    other = env.tmp / "other.wav"
    other.write_bytes(b"x")
    added = worker.add_recordings([str(env.wav), str(other), "/missing.wav"], env.store)
    assert added == 1
    output = capsys.readouterr().out
    assert "Already known (done)" in output
    assert "Not found: /missing.wav" in output
