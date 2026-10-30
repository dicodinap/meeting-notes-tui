"""Persistent queue of recordings waiting to become notes.

The queue is what lets a recording outlive the process that made it: a job is
written before any processing starts, along with the title and notes typed
during the meeting, and is only marked done once the note exists.

It lives in the config directory because recordings_dir can be relative, and
both the TUI and the worker process use it, so every change takes a file lock.
"""
import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set

from .config import get_config_path
from .logger import get_logger

logger = get_logger(__name__)

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Interrupted runs are retried on the next start, up to this many attempts, so
# a recording that kills the worker cannot do so on every launch.
MAX_ATTEMPTS = 3


@dataclass
class Job:
    """One recording and how far it has got."""
    audio_path: str
    recorded_at: str
    title: Optional[str] = None
    user_notes: str = ""
    status: str = PENDING
    attempts: int = 0
    error: str = ""
    note_path: str = ""
    transcript_path: str = ""
    filed: bool = False  # Went through the Obsidian/Notion pipeline


def _normalize(audio_path: str) -> str:
    return str(Path(audio_path).expanduser().resolve())


def recording_time(audio_path: str) -> datetime:
    """When a recording started: from its name, or its file time as a fallback."""
    path = Path(audio_path)
    try:
        return datetime.strptime(path.stem[:17], "%Y-%m-%d-%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime)


class JobStore:
    """The job queue, stored as one JSON file keyed by recording path."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or get_config_path().parent / "jobs.json"
        self.lock_path = self.path.with_suffix(".lock")

    def _read(self) -> Dict[str, Job]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            known = {field.name for field in fields(Job)}
            return {
                key: Job(**{k: v for k, v in value.items() if k in known})
                for key, value in data.items()
            }
        except (ValueError, TypeError, AttributeError) as e:
            # Keep the damaged file for recovery rather than overwriting it
            backup = self.path.with_suffix(".corrupt")
            logger.error(f"Could not read {self.path}, moving it to {backup}: {e}")
            os.replace(self.path, backup)
            return {}

    def _write(self, jobs: Dict[str, Job]) -> None:
        temp_path = self.path.with_suffix(".tmp")
        # Created private: jobs hold meeting titles and notes
        fd = os.open(temp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({key: asdict(job) for key, job in jobs.items()}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, self.path)

    @contextmanager
    def _transaction(self) -> Iterator[Dict[str, Job]]:
        with open(self.lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            jobs = self._read()
            yield jobs
            self._write(jobs)

    def all(self) -> List[Job]:
        with open(self.lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return list(self._read().values())

    def add(self, audio_path: str, title: Optional[str] = None, user_notes: str = "") -> Job:
        """Queue a recording. A recording already known is returned unchanged."""
        key = _normalize(audio_path)
        with self._transaction() as jobs:
            if key not in jobs:
                jobs[key] = Job(
                    audio_path=key,
                    recorded_at=recording_time(key).isoformat(),
                    title=title,
                    user_notes=user_notes,
                )
            return jobs[key]

    def update(self, audio_path: str, **changes) -> None:
        with self._transaction() as jobs:
            job = jobs[_normalize(audio_path)]
            for name, value in changes.items():
                setattr(job, name, value)

    def claim_next(self) -> Optional[Job]:
        """Mark the oldest pending job as running and return it."""
        with self._transaction() as jobs:
            pending = [job for job in jobs.values() if job.status == PENDING]
            if not pending:
                return None
            job = min(pending, key=lambda item: item.recorded_at)
            job.status = RUNNING
            job.attempts += 1
            return job

    def has_pending(self) -> bool:
        return any(job.status == PENDING for job in self.all())

    def requeue_interrupted(self) -> int:
        """Recover jobs left running by a dead worker.

        Only call this while holding the worker lock: a running job then
        cannot belong to a live worker.
        """
        count = 0
        with self._transaction() as jobs:
            for job in jobs.values():
                if job.status == RUNNING:
                    count += 1
                    if job.attempts >= MAX_ATTEMPTS:
                        job.status = FAILED
                        job.error = f"Interrupted {job.attempts} times"
                    else:
                        job.status = PENDING
        return count

    def retry_failed(self) -> int:
        """Queue failed jobs again. Finished steps are not repeated."""
        count = 0
        with self._transaction() as jobs:
            for job in jobs.values():
                if job.status == FAILED:
                    job.status = PENDING
                    job.attempts = 0
                    job.error = ""
                    count += 1
        return count

    def unfinished_paths(self) -> Set[str]:
        """Recordings that must be kept because they have no finished note."""
        return {job.audio_path for job in self.all() if job.status != DONE}


def expired_recordings(recordings_dir: Path, retention_days: int, keep: Set[str]) -> List[Path]:
    """Recordings past the retention period, excluding those in keep."""
    if retention_days <= 0 or not recordings_dir.is_dir():
        return []
    cutoff = datetime.now() - timedelta(days=retention_days)
    expired = []
    for wav_file in recordings_dir.glob("*.wav"):
        if str(wav_file.resolve()) in keep:
            continue
        if datetime.fromtimestamp(wav_file.stat().st_mtime) < cutoff:
            expired.append(wav_file)
    return expired
