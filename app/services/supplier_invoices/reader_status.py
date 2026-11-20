"""Heartbeat file the invoice worker keeps beside the database, so the admin page can tell if it runs.

A file and not a table: a beat every few seconds would otherwise be a database write that Litestream
backs up. The app and the worker share the database folder, in Docker as on a dev machine.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

STATUS_FILE_NAME = ".supplier-invoice-worker.json"
HEARTBEAT_SECONDS = 10
# Several missed beats before the page says the reader is not running.
STALE_SECONDS = 45
PROBLEM_CHARS_MAX = 300
# Problems that stop every photo, not just one. The upload goes back in line and the worker tries
# again after this long, so it carries on by itself once the login or the limit is fixed.
PROBLEM_RETRY_SECONDS = {"login": 300, "limit": 900, "busy": 120, "missing": 600}
PROBLEM_KINDS = tuple(PROBLEM_RETRY_SECONDS)
PROBLEM_TITLES = {
    "login": "The reader can't sign in to Claude",
    "limit": "Claude's usage limit is reached",
    "busy": "Claude is busy right now",
    "missing": "The reader has no claude program",
}


@dataclass(frozen=True)
class ReaderStatus:
    running: bool
    problem_kind: str
    problem: str


NOT_RUNNING = ReaderStatus(running=False, problem_kind="", problem="")


def status_path(db_path: Path) -> Path:
    """Return where the worker for this database keeps its heartbeat."""
    return db_path.parent / STATUS_FILE_NAME


def write_status(path: Path, problem_kind: str, problem: str, now_s: float) -> None:
    """Write one heartbeat atomically, so a reader never sees half a file."""
    if problem_kind not in ("", *PROBLEM_KINDS):
        raise ValueError(f"unknown problem kind {problem_kind!r}")
    if bool(problem_kind) != bool(problem):
        raise ValueError("a problem needs both a kind and a message")
    body = json.dumps({"beat_at": now_s, "problem_kind": problem_kind, "problem": problem[:PROBLEM_CHARS_MAX]})
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, path)


def read_status(path: Path, now_s: float) -> ReaderStatus:
    """Return the worker's state from its heartbeat; a missing, broken or old file means not running."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return NOT_RUNNING
    except (OSError, ValueError):
        logger.exception("could not read the invoice worker heartbeat %s", path)
        return NOT_RUNNING
    if not isinstance(data, dict):
        logger.error("invoice worker heartbeat %s is not a JSON object", path)
        return NOT_RUNNING
    beat_at = data.get("beat_at")
    kind = data.get("problem_kind")
    problem = data.get("problem")
    if not isinstance(beat_at, int | float) or not isinstance(kind, str) or not isinstance(problem, str):
        logger.error("invoice worker heartbeat %s has unexpected fields", path)
        return NOT_RUNNING
    if now_s - beat_at > STALE_SECONDS:
        return NOT_RUNNING
    if kind not in PROBLEM_KINDS:
        return ReaderStatus(running=True, problem_kind="", problem="")
    return ReaderStatus(running=True, problem_kind=kind, problem=problem[:PROBLEM_CHARS_MAX])


class Heartbeat:
    """Rewrite the heartbeat every HEARTBEAT_SECONDS from a thread, also while Claude reads a photo."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="invoice-worker-heartbeat", daemon=True)
        self._problem_kind = ""
        self._problem = ""

    def start(self) -> None:
        """Write the first beat now, then keep beating until stop."""
        self._beat()
        self._thread.start()

    def stop(self) -> None:
        """Stop beating and remove the file, so the page shows the reader stopped right away."""
        self._stopped.set()
        self._thread.join(timeout=HEARTBEAT_SECONDS)
        if self._thread.is_alive():
            raise RuntimeError("the heartbeat thread did not stop")
        self._path.unlink(missing_ok=True)

    def set_problem(self, kind: str, message: str) -> None:
        """Show a problem that stops every photo from being read, such as a missing Claude login."""
        if kind not in PROBLEM_KINDS:
            raise ValueError(f"unknown problem kind {kind!r}")
        with self._lock:
            self._problem_kind, self._problem = kind, " ".join(message.split()) or kind
        self._beat()

    def clear_problem(self) -> None:
        """Drop the problem once a photo is read again."""
        with self._lock:
            if not self._problem_kind:
                return
            self._problem_kind, self._problem = "", ""
        self._beat()

    def _beat(self) -> None:
        with self._lock:
            try:
                write_status(self._path, self._problem_kind, self._problem, time.time())
            except OSError:
                # Not fatal: the page then says the reader is not running, which shows something is wrong.
                logger.exception("could not write the invoice worker heartbeat %s", self._path)

    def _loop(self) -> None:
        while not self._stopped.wait(HEARTBEAT_SECONDS):
            self._beat()
