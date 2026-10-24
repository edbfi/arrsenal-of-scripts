"""Run logs and the single-instance lock, under ~/.local/state/upkeep/.

* ``logs/<timestamp>.log``: full transcript of every run (newest 30 kept)
* ``last-run.json``: summary of the latest run, for `status` and the menu
* ``upkeep.lock``: held for the whole run, so a cron run and a manual run
  never update the same tools at the same time
"""

import contextlib
import datetime as dt
import fcntl
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from upkeep.model import RunSummary

KEEP_LOGS = 30


class AlreadyRunningError(RuntimeError):
    pass


@contextlib.contextmanager
def instance_lock(state_dir: Path) -> Generator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "upkeep.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
            raise AlreadyRunningError(f"another upkeep run is in progress (pid {holder})") from None
        os.ftruncate(fd, 0)
        _ = os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def new_log_path(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    logs = sorted(log_dir.glob("*.log"))
    for old in logs[: max(0, len(logs) - KEEP_LOGS + 1)]:
        old.unlink(missing_ok=True)
    return log_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}.log"


def latest_log(log_dir: Path) -> Path | None:
    logs = sorted(log_dir.glob("*.log"))
    return logs[-1] if logs else None


@dataclass(frozen=True, slots=True)
class LastRun:
    started: str
    headline: str
    failed: tuple[str, ...]
    scheduled: bool
    log: str

    @property
    def when(self) -> str:
        try:
            return f"{dt.datetime.fromisoformat(self.started):%Y-%m-%d %H:%M}"
        except ValueError:
            return self.started


def record(state_dir: Path, summary: RunSummary, *, log: Path, scheduled: bool) -> None:
    payload = {
        "started": summary.started.isoformat(timespec="seconds"),
        "headline": summary.headline(),
        "failed": [o.task_id for o in summary.failed],
        "scheduled": scheduled,
        "log": str(log),
        "outcomes": {o.task_id: [o.status.value, o.describe()] for o in summary.outcomes},
    }
    target = state_dir / "last-run.json"
    tmp = target.with_suffix(".tmp")
    _ = tmp.write_text(json.dumps(payload, indent=2) + "\n")
    _ = tmp.replace(target)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in cast("list[object]", value) if isinstance(v, str))


def last_run(state_dir: Path) -> LastRun | None:
    try:
        raw = cast("object", json.loads((state_dir / "last-run.json").read_text()))
    except OSError, ValueError:
        return None
    match raw:
        case {
            "started": str(started),
            "headline": str(headline),
            "failed": object() as failed,
            "scheduled": bool(scheduled),
            "log": str(log),
        }:
            return LastRun(started, headline, _strings(failed), scheduled, log)
        case _:
            return None
