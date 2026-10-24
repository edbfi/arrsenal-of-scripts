"""Reporters turn engine events into output.

* `Dashboard`: a live rich panel for terminals: one row per task with a
  spinner, elapsed time and the task's latest output line, plus an overall
  progress bar. Finished tasks' output is printed above the panel.
* `Transcript`: timestamped plain lines, for log files and cron.
* `Tee`: fan one event stream out to several reporters.
"""

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TextIO, override

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from upkeep.config import OutputMode
from upkeep.engine import Reporter
from upkeep.model import Mode, Outcome, RunSummary, Status, TaskInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

ICONS: dict[Status, tuple[str, str]] = {
    Status.UPDATED: ("↑", "bold green"),
    Status.CURRENT: ("✓", "green"),
    Status.DONE: ("✓", "green"),
    Status.SKIPPED: ("–", "yellow"),
    Status.FAILED: ("✗", "bold red"),
    Status.CANCELLED: ("⊘", "dim"),
}


def _wants_output(mode: OutputMode, outcome: Outcome) -> bool:
    if not outcome.output:
        return False
    return mode is OutputMode.FULL or (mode is OutputMode.FAILURES and not outcome.status.ok)


def headline(summary: RunSummary) -> str:
    verb = "Would update" if summary.dry_run else "Updated"
    if summary.mode is Mode.INSTALL:
        verb = "Would install" if summary.dry_run else "Installed"
    lead = "✗ Finished with errors" if summary.failed else f"✓ {verb}"
    return f"{lead} · {summary.headline()}"


# --------------------------------------------------------------- dashboard


@dataclass(slots=True)
class _Row:
    info: TaskInfo
    started: float | None = None
    last: str = ""
    outcome: Outcome | None = None
    spinner: Spinner = field(default_factory=lambda: Spinner("dots", style="cyan"))


class Dashboard(Reporter):
    console: Console
    output_mode: OutputMode
    title: str
    _rows: dict[str, _Row]
    _live: Live | None
    _t0: float

    def __init__(self, console: Console, output_mode: OutputMode, title: str) -> None:
        self.console = console
        self.output_mode = output_mode
        self.title = title
        self._rows = {}
        self._live = None
        self._t0 = time.monotonic()

    @override
    def begin(self, plan: Sequence[TaskInfo], *, mode: Mode, dry_run: bool) -> None:
        self._rows = {info.id: _Row(info) for info in plan}
        self._t0 = time.monotonic()
        note = " · dry run" if dry_run else ""
        self.console.print(Text.assemble((self.title, "bold"), (note, "yellow")))
        self._live = Live(self, console=self.console, refresh_per_second=10, transient=True)
        self._live.start()

    @override
    def started(self, task_id: str) -> None:
        self._rows[task_id].started = time.monotonic()

    @override
    def output(self, task_id: str, line: str) -> None:
        if line.strip():
            self._rows[task_id].last = line.strip()

    @override
    def finished(self, outcome: Outcome) -> None:
        row = self._rows[outcome.task_id]
        row.outcome = outcome
        if _wants_output(self.output_mode, outcome):
            self.console.print(
                Rule(Text(f" {outcome.title} ", "bold"), align="left", style="grey50")
            )
            for line in outcome.output:
                self.console.print(Text("  " + line), highlight=False, soft_wrap=True)
        if self.output_mode is not OutputMode.QUIET or not outcome.status.ok:
            self.console.print(self._result_line(outcome))

    @override
    def end(self, summary: RunSummary) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        style = "bold red" if summary.failed else "bold green"
        self.console.print()
        self.console.print(Text(headline(summary), style))
        for outcome in summary.failed:
            self.console.print(Text(f"  {outcome.title}: {outcome.describe()}", "red"))

    def _result_line(self, outcome: Outcome) -> Text:
        icon, style = ICONS[outcome.status]
        return Text.assemble(
            (f" {icon} ", style),
            (f"{outcome.title:<20}", "bold"),
            (f"{outcome.elapsed:6.1f}s  ", "grey50"),
            (outcome.describe(), "dim" if outcome.status is Status.SKIPPED else ""),
        )

    def _waiting_on(self, row: _Row) -> str:
        blockers = [
            self._rows[d].info.title
            for d in row.info.after
            if d in self._rows and self._rows[d].outcome is None
        ]
        return f"waiting for {', '.join(blockers)}" if blockers else "queued"

    def __rich__(self) -> RenderableType:
        now = time.monotonic()
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)
        table.add_column(min_width=20, no_wrap=True)
        table.add_column(justify="right", width=7, style="grey50")
        table.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
        for row in self._rows.values():
            if row.outcome is not None:
                continue
            if row.started is None:
                table.add_row(
                    Text("◌", "grey50"),
                    Text(row.info.title, "grey50"),
                    "",
                    Text(self._waiting_on(row), "grey50"),
                )
            else:
                table.add_row(
                    row.spinner,
                    Text(row.info.title, "bold"),
                    f"{now - row.started:5.1f}s",
                    Text(row.last, "dim"),
                )
        total = len(self._rows)
        finished = sum(1 for r in self._rows.values() if r.outcome is not None)
        bar = Table.grid(padding=(0, 1))
        bar.add_row(
            ProgressBar(total=max(total, 1), completed=finished, width=40),
            Text(f"{finished}/{total} · {now - self._t0:.0f}s", "grey50"),
        )
        return Group(table, bar)


# -------------------------------------------------------------- transcript


class Transcript(Reporter):
    """Timestamped plain text. Safe for files, pipes and cron mail."""

    stream: TextIO
    output_mode: OutputMode
    title: str
    _buffered: dict[str, list[str]]

    def __init__(self, stream: TextIO, output_mode: OutputMode, title: str) -> None:
        self.stream = stream
        self.output_mode = output_mode
        self.title = title
        self._buffered = {}

    def _write(self, text: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        _ = self.stream.write(f"[{stamp}] {text}\n")
        self.stream.flush()

    @override
    def begin(self, plan: Sequence[TaskInfo], *, mode: Mode, dry_run: bool) -> None:
        note = " (dry run)" if dry_run else ""
        self._write(f"{self.title}: {mode.value} {len(plan)} task(s){note}")

    @override
    def started(self, task_id: str) -> None:
        self._write(f"▶ {task_id}")

    @override
    def output(self, task_id: str, line: str) -> None:
        if self.output_mode is OutputMode.FULL:
            self._write(f"  {task_id} │ {line}")
        elif self.output_mode is OutputMode.FAILURES:
            self._buffered.setdefault(task_id, []).append(line)

    @override
    def finished(self, outcome: Outcome) -> None:
        if self.output_mode is OutputMode.FAILURES and not outcome.status.ok:
            for line in self._buffered.get(outcome.task_id, []):
                self._write(f"  {outcome.task_id} │ {line}")
        _ = self._buffered.pop(outcome.task_id, None)
        icon, _style = ICONS[outcome.status]
        self._write(f"{icon} {outcome.task_id}: {outcome.describe()} ({outcome.elapsed:.1f}s)")

    @override
    def end(self, summary: RunSummary) -> None:
        self._write(headline(summary))


class Tee(Reporter):
    reporters: tuple[Reporter, ...]

    def __init__(self, *reporters: Reporter) -> None:
        self.reporters = reporters

    @override
    def begin(self, plan: Sequence[TaskInfo], *, mode: Mode, dry_run: bool) -> None:
        for r in self.reporters:
            r.begin(plan, mode=mode, dry_run=dry_run)

    @override
    def started(self, task_id: str) -> None:
        for r in self.reporters:
            r.started(task_id)

    @override
    def output(self, task_id: str, line: str) -> None:
        for r in self.reporters:
            r.output(task_id, line)

    @override
    def finished(self, outcome: Outcome) -> None:
        for r in self.reporters:
            r.finished(outcome)

    @override
    def end(self, summary: RunSummary) -> None:
        for r in self.reporters:
            r.end(summary)
