"""What is installed, where from, which version, and whether it is in use."""

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text

from upkeep.config import BusyPolicy, Config
from upkeep.model import Presence, Probe
from upkeep.procs import Proc, ProcessTable, describe
from upkeep.tasks import Task, TaskContext, is_enabled, when_busy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from upkeep.host import Host


@dataclass(frozen=True, slots=True)
class Entry:
    task: Task
    probe: Probe
    version: str | None
    busy: tuple[Proc, ...]
    enabled: bool
    install: bool
    policy: BusyPolicy

    @property
    def presence(self) -> Presence:
        return self.probe.presence

    def where(self) -> str:
        match self.presence:
            case Presence.INSTALLED:
                if self.probe.source is not None:
                    return self.probe.source.value
                return f"via {self.probe.path.name}" if self.probe.path else "present"
            case Presence.FOREIGN:
                return self.probe.source.value if self.probe.source else "other installer"
            case Presence.MISSING:
                return "not installed"


def _quiet(_line: str) -> None:
    pass


async def gather(host: Host, config: Config, tasks: Sequence[Task]) -> list[Entry]:
    procs = ProcessTable.snapshot()
    env = host.child_env(os.environ)

    async def one(task: Task) -> Entry:
        probe = await asyncio.to_thread(task.probe, host)
        version: str | None = None
        if probe.presence is not Presence.MISSING:
            ctx = TaskContext(
                host=host,
                env=env,
                settings=config.task(task.info.id),
                dry_run=True,
                force=False,
                sink=_quiet,
                snapshot=lambda: procs,
            )
            try:
                async with asyncio.timeout(20):
                    version = await task.version(ctx)
            except OSError, TimeoutError:
                version = None
        return Entry(
            task=task,
            probe=probe,
            version=version,
            busy=tuple(procs.matching(task.busy_matcher(host))),
            enabled=is_enabled(task, config),
            install=config.task(task.info.id).install,
            policy=when_busy(task, config),
        )

    return list(await asyncio.gather(*(one(t) for t in tasks)))


def table(entries: Sequence[Entry]) -> Table:
    grid = Table(box=None, header_style="bold", pad_edge=False, expand=False)
    grid.add_column("", width=1)
    grid.add_column("tool", style="bold", no_wrap=True)
    grid.add_column("installed via", no_wrap=True)
    grid.add_column("version", no_wrap=True)
    grid.add_column("in use", no_wrap=True)
    grid.add_column("notes", overflow="fold")
    for e in entries:
        if not e.enabled:
            mark = Text("○", "grey50")
        elif e.presence is Presence.INSTALLED or (e.presence is Presence.MISSING and e.install):
            mark = Text("●", "green")
        else:
            mark = Text("○", "yellow")
        where = Text(e.where(), "grey50" if e.presence is Presence.MISSING else "")
        if e.busy:
            verdict = "kept" if e.policy is BusyPolicy.SKIP else "updated anyway"
            in_use = Text(f"{len(e.busy)} proc · {verdict}", "yellow")
        else:
            in_use = Text("—", "grey50")
        notes: list[str] = []
        if not e.enabled:
            notes.append("disabled")
        elif e.presence is Presence.MISSING and e.install:
            notes.append("will be installed")
        if e.presence is Presence.FOREIGN:
            notes.append(e.probe.note)
        for other in e.probe.others:
            notes.append(f"also {other.describe()}")
        grid.add_row(
            mark, e.task.info.title, where, e.version or "", in_use, Text("; ".join(notes), "dim")
        )
    return grid


def busy_detail(entry: Entry) -> str:
    return describe(entry.busy) if entry.busy else ""
