"""Background worker that turns queued recordings into notes.

Transcribing can take longer than the app stays open, so it runs in its own
process, detached from the terminal. The worker holds a lock for its whole
life: at most one runs at a time, and the kernel releases the lock if it dies,
which is how an interrupted job is told apart from a running one.
"""
import argparse
import fcntl
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import AppConfig, get_config_path, load_config
from .jobs import DONE, FAILED, PENDING, Job, JobStore
from .logger import get_logger, setup_logging
from .note_maker import NoteMaker
from .transcriber import WhisperTranscriber

logger = get_logger(__name__)

REPO_DIR = Path(__file__).resolve().parent.parent
PIPELINE_TIMEOUT = 1800  # Seconds


def _lock_path() -> Path:
    return get_config_path().parent / "worker.lock"


def _acquire_worker_lock() -> Optional[int]:
    fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def worker_running() -> bool:
    fd = _acquire_worker_lock()
    if fd is None:
        return True
    os.close(fd)
    return False


def start_worker() -> None:
    """Start a detached worker. It exits on its own if one is already running."""
    # Started through run.py by absolute path so that imports never resolve
    # against the caller's working directory, as they would with python -m.
    subprocess.Popen(
        [sys.executable, str(REPO_DIR / "run.py"), "--worker"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def build_note_maker(config: AppConfig) -> NoteMaker:
    # Check config first, then env vars
    api_key = None
    if config.ai_provider == "openai":
        api_key = config.openai_api_key or os.getenv("OPENAI_API_KEY")
    elif config.ai_provider == "anthropic":
        api_key = config.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
    elif config.ai_provider == "openrouter":
        api_key = config.openrouter_api_key or os.getenv("OPENROUTER_API_KEY")

    return NoteMaker(
        output_dir=config.notes_dir,
        transcripts_dir=config.transcripts_dir,
        ai_provider=config.ai_provider,
        ai_model=config.ai_model,
        api_key=api_key,
    )


def run_pipeline(note_path: str, transcript_path: str) -> None:
    """File a note with process-meeting.sh, if the user configured it."""
    if not (get_config_path().parent / "process-meeting.conf").exists():
        return
    logger.info(f"Running note pipeline for {note_path}")
    result = subprocess.run(
        [str(REPO_DIR / "process-meeting.sh"), note_path, transcript_path],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=PIPELINE_TIMEOUT,
    )
    logger.info(f"Note pipeline output: {result.stdout}")
    if result.returncode != 0:
        raise RuntimeError(f"Note pipeline failed: {(result.stdout + result.stderr).strip()[-300:]}")


def _create_note(job: Job, transcriber: WhisperTranscriber, note_maker: NoteMaker) -> tuple[str, str]:
    if not Path(job.audio_path).exists():
        raise FileNotFoundError(f"Recording not found: {job.audio_path}")

    transcriber.load_model()
    logger.info(f"Starting transcription: {job.audio_path}")
    result = transcriber.transcribe(job.audio_path)
    logger.info(f"Transcription complete: {len(result.text.split())} words")

    formatted = '\n\n'.join([
        f'**[{int(seg.start // 60):02d}:{int(seg.start % 60):02d}]** {seg.text.strip()}'
        for seg in result.segments
    ])
    duration = result.segments[-1].end if result.segments else 0
    note_path, transcript_path, ai_error = note_maker.create_note(
        transcript_text=result.text,
        formatted_transcript=formatted,
        duration=duration,
        title=job.title,
        metadata={'recording_file': Path(job.audio_path).name},
        user_notes=job.user_notes,
        date=datetime.fromisoformat(job.recorded_at),
    )
    if ai_error:
        logger.warning(f"Note created but AI summarization failed: {ai_error}")
    logger.info(f"Note created: {note_path}, transcript: {transcript_path}")
    return str(Path(note_path).resolve()), str(Path(transcript_path).resolve())


def process_job(store: JobStore, job: Job, transcriber: WhisperTranscriber, note_maker: NoteMaker) -> None:
    """Run the steps a job still needs. Finished steps are skipped on a retry."""
    try:
        note_path, transcript_path = job.note_path, job.transcript_path
        if not note_path:
            note_path, transcript_path = _create_note(job, transcriber, note_maker)
            store.update(job.audio_path, note_path=note_path, transcript_path=transcript_path)
        if not job.filed:
            run_pipeline(note_path, transcript_path)
            store.update(job.audio_path, filed=True)
        store.update(job.audio_path, status=DONE, error="")
    except Exception as e:
        logger.error(f"Error processing {job.audio_path}: {e}", exc_info=True)
        store.update(job.audio_path, status=FAILED, error=str(e))


def drain(store: JobStore) -> None:
    """Process every pending job, oldest first."""
    interrupted = store.requeue_interrupted()
    if interrupted:
        logger.warning(f"Resuming {interrupted} interrupted job(s)")
    config = load_config()
    transcriber = WhisperTranscriber(config.whisper_model, device=config.whisper_device)
    note_maker = build_note_maker(config)
    while (job := store.claim_next()) is not None:
        process_job(store, job, transcriber, note_maker)


def run_worker(store: Optional[JobStore] = None) -> None:
    store = store or JobStore()
    while True:
        fd = _acquire_worker_lock()
        if fd is None:
            return
        try:
            drain(store)
        finally:
            os.close(fd)
        # A job added just as the lock was released found a worker still
        # running, so nothing else will pick it up.
        if not store.has_pending():
            return


def add_recordings(paths: List[str], store: Optional[JobStore] = None) -> int:
    """Queue existing recordings by hand. Returns how many were added."""
    store = store or JobStore()
    added = 0
    for path in paths:
        if not Path(path).is_file():
            print(f"Not found: {path}")
            continue
        job = store.add(path)
        if job.attempts or job.status != PENDING:
            print(f"Already known ({job.status}): {path}")
        else:
            print(f"Queued: {path}")
            added += 1
    return added


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Process queued meeting recordings.")
    parser.add_argument("--worker", action="store_true", help="Process the queue, then exit")
    parser.add_argument("--add", nargs="+", metavar="WAV", help="Queue existing recordings")
    args = parser.parse_args(argv)

    setup_logging()
    if args.add:
        if add_recordings(args.add):
            start_worker()
    else:
        run_worker()
    return 0
