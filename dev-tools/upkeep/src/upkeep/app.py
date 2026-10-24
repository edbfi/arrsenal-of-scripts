"""Operations behind every command and menu entry."""

import asyncio
import signal
import sys
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.text import Text

from upkeep import config as config_mod
from upkeep import history, inventory, launcher, schedule, selfupdate
from upkeep.config import Config, OutputMode, SelfUpdateMode
from upkeep.engine import Engine, Options, Reporter
from upkeep.model import Mode, RunSummary
from upkeep.report import Dashboard, Tee, Transcript
from upkeep.tasks import Task, all_tasks, select

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from rich.console import Console

    from upkeep.host import Host

EXIT_LOCKED = 75  # EX_TEMPFAIL: another run holds the lock


@dataclass(frozen=True, slots=True)
class RunRequest:
    only: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    mode: Mode = Mode.UPDATE
    dry_run: bool = False
    force: bool = False
    jobs: int | None = None
    retries: int | None = None
    output: OutputMode | None = None
    plain: bool = False
    scheduled: bool = False
    self_update: bool = True


async def _with_signals[T](work: Coroutine[object, object, T]) -> T:
    """SIGTERM/SIGHUP (launchd stopping the job, a closed terminal) cancel the
    run like Ctrl-C does, so child process groups are killed, not orphaned."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(work)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            _ = loop.remove_signal_handler(sig)


class App:
    host: Host
    config: Config
    console: Console
    argv: list[str]  # for re-exec after a self-update

    def __init__(self, host: Host, config: Config, console: Console, argv: list[str]) -> None:
        self.host = host
        self.config = config
        self.console = console
        self.argv = argv

    @property
    def interactive(self) -> bool:
        return self.console.is_terminal and sys.stdin.isatty()

    def tasks(self) -> list[Task]:
        return all_tasks(self.config)

    def reload(self) -> None:
        self.config = config_mod.load(self.host.config_file)

    def save(self, new: Config) -> None:
        config_mod.save(new, self.host.config_file)
        self.config = new

    # ------------------------------------------------------------- running

    def run(self, request: RunRequest) -> int:
        try:
            with history.instance_lock(self.host.state_dir):
                if request.self_update and not request.dry_run:
                    self.self_update_before_run(scheduled=request.scheduled)
                summary = self._run_locked(request)
        except history.AlreadyRunningError as error:
            self.console.print(Text(f"upkeep: {error}", "yellow"))
            return EXIT_LOCKED
        except KeyboardInterrupt, asyncio.CancelledError:
            self.console.print(Text("\ncancelled", "yellow"))
            return 130
        return summary.exit_code

    def _run_locked(self, request: RunRequest) -> RunSummary:
        tasks = select(self.tasks(), self.config, only=request.only, skip=request.skip)
        output = request.output or self.config.output
        log_path = history.new_log_path(self.host.log_dir)
        title = f"upkeep · {self.host.describe()}"
        with log_path.open("w") as log:
            reporters: list[Reporter] = [Transcript(log, OutputMode.FULL, title)]
            if request.scheduled:
                reporters.append(Transcript(sys.stdout, OutputMode.QUIET, title))
            elif self.console.is_terminal and not request.plain:
                reporters.append(Dashboard(self.console, output, title))
            else:
                reporters.append(Transcript(sys.stdout, output, title))
            options = Options(
                mode=request.mode,
                jobs=request.jobs or self.config.jobs,
                retries=request.retries or self.config.retries,
                dry_run=request.dry_run,
                force=request.force,
            )
            engine = Engine(self.host, self.config, Tee(*reporters), options)
            summary = asyncio.run(_with_signals(engine.run(tasks)))
        if not request.dry_run:
            history.record(self.host.state_dir, summary, log=log_path, scheduled=request.scheduled)
        if not request.scheduled:
            self.console.print(Text(f"log: {log_path}", "grey50"))
        return summary

    # --------------------------------------------------------- self-update

    def self_update_check(self, *, fetch: bool = True) -> selfupdate.Check:
        return selfupdate.check(self.config.remote, self.config.branch, fetch=fetch)

    def self_update_before_run(self, *, scheduled: bool) -> None:
        mode = self.config.self_update
        if mode is SelfUpdateMode.OFF or selfupdate.already_reexeced():
            return
        result = self.self_update_check()
        if result.state is selfupdate.State.AVAILABLE and mode is SelfUpdateMode.AUTO:
            try:
                self.apply_self_update(result)
            except selfupdate.GitError as error:  # update the tools anyway
                self.console.print(Text(f"upkeep self-update failed: {error}", "yellow"))
        elif result.state not in {selfupdate.State.CURRENT, selfupdate.State.UNMANAGED}:
            prefix = "upkeep self-update: " if scheduled else ""
            self.console.print(Text(f"{prefix}{result.describe()}", "yellow"))

    def apply_self_update(self, result: selfupdate.Check) -> None:
        applied = selfupdate.apply(result, self.host)
        self.console.print(Text(f"upkeep updated {applied.old[:7]} → {applied.new[:7]}", "green"))
        for line in result.changes:
            self.console.print(Text(f"  {line}", "grey50"))
        if applied.code_changed:
            self.console.print(Text("restarting with the new version…", "grey50"))
            selfupdate.reexec(self.argv)

    # -------------------------------------------------------------- views

    def status(self) -> None:
        entries = asyncio.run(inventory.gather(self.host, self.config, self.tasks()))
        self.console.print(self.header())
        self.console.print(inventory.table(entries))
        self.console.print(
            Text("● updated by upkeep   ○ left alone · `upkeep setup` to change", "grey50")
        )

    def header(self) -> Panel:
        check = self.self_update_check(fetch=False)
        installed = schedule.current_any(self.host)
        last = history.last_run(self.host.state_dir)
        lines = [
            ("host", self.host.describe()),
            ("upkeep", check.describe() if check.checkout else check.state.value),
            ("launcher", launcher.status(self.host)),
            ("schedule", installed.describe() if installed else "not scheduled"),
            ("last run", f"{last.when} · {last.headline}" if last else "never"),
            (
                "config",
                str(self.host.config_file)
                + ("" if self.host.config_file.exists() else " (defaults; run `upkeep setup`)"),
            ),
        ]
        body = Text()
        for i, (key, value) in enumerate(lines):
            _ = body.append(f"{key:<9}", "grey50")
            _ = body.append(value)
            if i < len(lines) - 1:
                _ = body.append("\n")
        return Panel(body, title="[bold]upkeep[/]", title_align="left", border_style="cyan")

    # ----------------------------------------------------------- schedule

    def schedule_install(self, at: schedule.At) -> schedule.Backend:
        target = launcher.install(self.host)
        backend = schedule.backend_for(self.host, self.config.schedule_backend)
        for name in schedule.remove_all(self.host):  # never two schedules at once
            if name != backend.name:
                self.console.print(Text(f"removed the previous {name} schedule", "grey50"))
        self.host.state_dir.mkdir(parents=True, exist_ok=True)
        backend.install(
            at,
            schedule.scheduled_argv(target),
            self.host.state_dir / "scheduled.log",
            schedule.scheduled_env(),
        )
        return backend


def with_tasks(config: Config, updates: dict[str, config_mod.TaskSettings]) -> Config:
    tasks = dict(config.tasks)
    tasks.update(updates)
    return replace(config, tasks=tasks)
