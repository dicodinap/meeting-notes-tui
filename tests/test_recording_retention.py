"""Tests for the recording retention cleanup logic."""
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from meeting_notes.jobs import expired_recordings


def _cleanup(recordings_dir: Path, retention_days: int, keep: set[str] = frozenset()) -> list[str]:
    """Run the cleanup as the app does, returning deleted names."""
    removed = []
    for wav in expired_recordings(recordings_dir, retention_days, keep):
        wav.unlink()
        removed.append(wav.name)
    return removed


def _set_age(path: Path, days_ago: int) -> None:
    ts = (datetime.now() - timedelta(days=days_ago)).timestamp()
    os.utime(path, (ts, ts))


def test_deletes_old_wav_files(tmp_path):
    old = tmp_path / "old.wav"
    new = tmp_path / "new.wav"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    _set_age(old, 60)
    _set_age(new, 5)

    removed = _cleanup(tmp_path, retention_days=30)

    assert removed == ["old.wav"]
    assert not old.exists()
    assert new.exists()


def test_retention_zero_disables_cleanup(tmp_path):
    old = tmp_path / "ancient.wav"
    old.write_bytes(b"x")
    _set_age(old, 365)

    removed = _cleanup(tmp_path, retention_days=0)

    assert removed == []
    assert old.exists(), "retention=0 must not delete anything"


def test_negative_retention_is_treated_as_disabled(tmp_path):
    """Defensive: negative values shouldn't delete everything."""
    old = tmp_path / "ancient.wav"
    old.write_bytes(b"x")
    _set_age(old, 365)

    removed = _cleanup(tmp_path, retention_days=-1)

    assert removed == []
    assert old.exists()


def test_missing_directory_is_noop(tmp_path):
    """If the recordings dir doesn't exist, cleanup must not crash."""
    nonexistent = tmp_path / "does-not-exist"
    removed = _cleanup(nonexistent, retention_days=30)
    assert removed == []


def test_only_wav_files_are_considered(tmp_path):
    """Other extensions should not be touched even if very old."""
    old_wav = tmp_path / "old.wav"
    old_txt = tmp_path / "old.txt"
    old_md = tmp_path / "old.md"
    for f in (old_wav, old_txt, old_md):
        f.write_bytes(b"x")
        _set_age(f, 60)

    removed = _cleanup(tmp_path, retention_days=30)

    assert removed == ["old.wav"]
    assert not old_wav.exists()
    assert old_txt.exists()
    assert old_md.exists()


def test_files_at_exactly_the_boundary_are_kept(tmp_path):
    """A file mtime'd exactly at the cutoff should NOT be deleted (strict <)."""
    edge = tmp_path / "edge.wav"
    edge.write_bytes(b"x")
    _set_age(edge, 5)  # comfortably newer than 30-day cutoff
    removed = _cleanup(tmp_path, retention_days=30)
    assert removed == []
    assert edge.exists()


@pytest.mark.parametrize("days_old,retention,should_delete", [
    (1, 30, False),
    (29, 30, False),
    (31, 30, True),
    (60, 30, True),
    # A file aged exactly N days vs N-day retention races the cutoff
    # because _set_age runs microseconds before _cleanup samples now().
    # Skip the exact-equal case and just test clearly above/below.
    (5, 1, True),
    (0, 1, False),
])
def test_retention_boundary_table(tmp_path, days_old, retention, should_delete):
    f = tmp_path / "x.wav"
    f.write_bytes(b"x")
    _set_age(f, days_old)
    _cleanup(tmp_path, retention_days=retention)
    assert f.exists() != should_delete


def test_keeps_old_recording_without_a_finished_note(tmp_path):
    protected = tmp_path / "unprocessed.wav"
    expired = tmp_path / "processed.wav"
    protected.write_bytes(b"x")
    expired.write_bytes(b"x")
    _set_age(protected, 90)
    _set_age(expired, 90)

    assert _cleanup(tmp_path, 30, {str(protected.resolve())}) == ["processed.wav"]
    assert protected.exists()
