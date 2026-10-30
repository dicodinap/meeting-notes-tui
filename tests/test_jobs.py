"""Tests for the persistent job queue."""
import json
import multiprocessing
from datetime import datetime

import pytest

from meeting_notes.jobs import DONE, FAILED, MAX_ATTEMPTS, PENDING, RUNNING, JobStore, recording_time


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.json")


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "2026-10-21-204722.wav"
    path.write_bytes(b"x")
    return path


def test_recording_time_comes_from_the_filename(wav):
    assert recording_time(str(wav)) == datetime(2026, 10, 21, 20, 47, 22)


def test_recording_time_falls_back_to_file_time(tmp_path):
    path = tmp_path / "renamed.wav"
    path.write_bytes(b"x")
    assert recording_time(str(path)).year >= 2026


def test_job_keeps_title_and_notes_across_instances(store, wav):
    store.add(str(wav), "Planning", "my notes")
    job = JobStore(store.path).all()[0]
    assert (job.title, job.user_notes, job.status) == ("Planning", "my notes", PENDING)
    assert job.audio_path == str(wav.resolve())


def test_adding_twice_does_not_reset_a_job(store, wav):
    store.add(str(wav), "First")
    store.update(str(wav), status=DONE)
    assert store.add(str(wav), "Second").status == DONE
    assert len(store.all()) == 1


def test_claim_takes_the_oldest_recording(store, tmp_path):
    newer = tmp_path / "2026-10-22-090000.wav"
    older = tmp_path / "2026-10-20-090000.wav"
    for path in (newer, older):
        path.write_bytes(b"x")
        store.add(str(path))
    job = store.claim_next()
    assert job.audio_path == str(older.resolve())
    assert (job.status, job.attempts) == (RUNNING, 1)
    assert store.claim_next().audio_path == str(newer.resolve())
    assert store.claim_next() is None


def test_interrupted_job_is_requeued(store, wav):
    store.add(str(wav))
    store.claim_next()
    assert store.requeue_interrupted() == 1
    assert store.all()[0].status == PENDING


def test_job_that_keeps_getting_interrupted_stops_retrying(store, wav):
    store.add(str(wav))
    for _ in range(MAX_ATTEMPTS):
        store.claim_next()
        store.requeue_interrupted()
    job = store.all()[0]
    assert job.status == FAILED
    assert "Interrupted" in job.error


def test_retry_keeps_finished_steps(store, wav):
    store.add(str(wav))
    store.update(str(wav), status=FAILED, error="boom", attempts=2, note_path="/note.md")
    assert store.retry_failed() == 1
    job = store.all()[0]
    assert (job.status, job.error, job.attempts, job.note_path) == (PENDING, "", 0, "/note.md")


def test_only_done_jobs_can_be_cleaned_up(store, tmp_path):
    for name, status in [("a", PENDING), ("b", RUNNING), ("c", FAILED), ("d", DONE)]:
        path = tmp_path / f"{name}.wav"
        path.write_bytes(b"x")
        store.add(str(path))
        store.update(str(path), status=status)
    assert sorted(p.rsplit("/", 1)[1] for p in store.unfinished_paths()) == ["a.wav", "b.wav", "c.wav"]


def test_store_file_is_private(store, wav):
    store.add(str(wav), "Confidential", "notes")
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_unknown_fields_are_ignored_for_forward_compatibility(store, wav):
    store.add(str(wav))
    data = json.loads(store.path.read_text())
    data[str(wav.resolve())]["future_field"] = 1
    store.path.write_text(json.dumps(data))
    assert len(store.all()) == 1


def test_corrupt_store_is_preserved_not_overwritten(store, wav):
    store.path.write_text("{not json")
    store.add(str(wav))
    assert store.path.with_suffix(".corrupt").read_text() == "{not json"
    assert len(store.all()) == 1


def _add_many(path, directory, prefix):
    store = JobStore(path)
    for i in range(25):
        wav = f"{directory}/{prefix}-{i}.wav"
        open(wav, "wb").close()
        store.add(wav)


def test_concurrent_writers_do_not_lose_jobs(store, tmp_path):
    processes = [
        multiprocessing.Process(target=_add_many, args=(store.path, tmp_path, name))
        for name in "abcd"
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    assert len(store.all()) == 100
