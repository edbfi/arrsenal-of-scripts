"""Value types shared by tasks, the engine and the reporters."""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from upkeep.config import BusyPolicy

if TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

    from upkeep.sources import Location, Source


class Presence(StrEnum):
    INSTALLED = "installed"
    MISSING = "missing"
    FOREIGN = "foreign"  # present, but owned by another updater (e.g. Homebrew)


@dataclass(frozen=True, slots=True)
class Probe:
    presence: Presence
    path: Path | None = None
    note: str = ""
    source: Source | None = None
    others: tuple[Location, ...] = ()  # further copies, e.g. a stale uv tool

    @classmethod
    def installed(
        cls,
        path: Path | None,
        note: str = "",
        source: Source | None = None,
        others: tuple[Location, ...] = (),
    ) -> Probe:
        return cls(Presence.INSTALLED, path, note, source, others)

    @classmethod
    def missing(cls, note: str = "not installed") -> Probe:
        return cls(Presence.MISSING, None, note)

    @classmethod
    def foreign(
        cls,
        path: Path | None,
        note: str,
        source: Source | None = None,
        others: tuple[Location, ...] = (),
    ) -> Probe:
        return cls(Presence.FOREIGN, path, note, source, others)


class Mode(StrEnum):
    UPDATE = "update"
    INSTALL = "install"


class Status(StrEnum):
    UPDATED = "updated"  # version changed
    CURRENT = "current"  # ran; version unchanged
    DONE = "done"  # ran; no version to compare
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def ok(self) -> bool:
        return self not in {Status.FAILED, Status.CANCELLED}


@dataclass(frozen=True, slots=True)
class TaskInfo:
    id: str
    title: str
    summary: str
    after: tuple[str, ...] = ()  # ordering only: wait for these if they are in the run
    resources: frozenset[str] = frozenset()  # never run two tasks sharing one at once
    when_busy: BusyPolicy = BusyPolicy.SKIP
    timeout: float = 900.0
    enabled_by_default: bool = True
    busy_note: str = ""  # why the default busy policy is what it is


class Skip(Exception):
    """Raised by a task to end without running anything."""


class TaskFailed(RuntimeError):
    """A task-level failure that retrying will not fix."""


@dataclass(frozen=True, slots=True)
class Outcome:
    task_id: str
    title: str
    status: Status
    message: str = ""
    before: str | None = None
    after: str | None = None
    elapsed: float = 0.0
    attempts: int = 0
    output: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.status is Status.UPDATED and self.before and self.after:
            change = f"{self.before} → {self.after}"
            return f"{change} · {self.message}" if self.message else change
        if self.message:
            return self.message
        if self.status is Status.CURRENT and self.after:
            return f"up to date ({self.after})"
        return self.status.value


@dataclass(frozen=True, slots=True)
class RunSummary:
    mode: Mode
    started: dt.datetime
    elapsed: float
    outcomes: tuple[Outcome, ...]
    dry_run: bool = False

    def count(self, *statuses: Status) -> int:
        return sum(1 for o in self.outcomes if o.status in statuses)

    @property
    def failed(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if not o.status.ok)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def headline(self) -> str:
        parts = [
            f"{self.count(Status.UPDATED)} updated",
            f"{self.count(Status.CURRENT, Status.DONE)} ok",
            f"{self.count(Status.SKIPPED)} skipped",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return " · ".join(parts) + f" · {self.elapsed:.1f}s"
