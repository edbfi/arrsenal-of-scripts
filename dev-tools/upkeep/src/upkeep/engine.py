"""Run tasks concurrently, in dependency order, without stepping on each other.

Scheduling rules:

* ``after``: a task waits until every task it names that is part of this run
  has finished (in any state; a failed ``brew`` should not block ``uv``).
* ``resources``: two tasks that share a resource never run at the same time
  (all cargo-dist installers write the same receipt dir, for example).
* at most ``jobs`` tasks run at once; ties go to catalogue order.

Per task: probe → busy check → version → update (with retries and a
timeout) → version, reported as one `Outcome`.
"""

import asyncio
import datetime as dt
import os
import re
import time
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from upkeep.config import BusyPolicy, Config
from upkeep.execute import CommandError
from upkeep.model import (
    Mode,
    Outcome,
    Presence,
    RunSummary,
    Skip,
    Status,
    TaskFailed,
    TaskInfo,
)
from upkeep.procs import ProcessTable, describe
from upkeep.tasks import Task, TaskContext, when_busy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from upkeep.host import Host

RATE_LIMITED = re.compile(
    r"rate.?limit|too many requests|\b429\b|\b503\b|retry.?after|quota", re.IGNORECASE
)


class Reporter(Protocol):
    def begin(self, plan: Sequence[TaskInfo], *, mode: Mode, dry_run: bool) -> None: ...
    def started(self, task_id: str) -> None: ...
    def output(self, task_id: str, line: str) -> None: ...
    def finished(self, outcome: Outcome) -> None: ...
    def end(self, summary: RunSummary) -> None: ...


def exponential_backoff(attempt: int) -> float:
    return float(1 << attempt)


class CycleError(ValueError):
    pass


def check_cycles(tasks: Sequence[Task]) -> None:
    graph = {t.info.id: list(t.info.after) for t in tasks}
    state: dict[str, int] = {}  # 1 visiting, 2 done

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 2 or node not in graph:
            return
        if state.get(node) == 1:
            raise CycleError("task dependency cycle: " + " → ".join([*path, node]))
        state[node] = 1
        for dep in graph[node]:
            visit(dep, [*path, node])
        state[node] = 2

    for node in graph:
        visit(node, [])


@dataclass(slots=True)
class Options:
    mode: Mode = Mode.UPDATE
    jobs: int = 4
    retries: int = 3
    dry_run: bool = False
    force: bool = False  # update tools even while they are in use
    backoff: Callable[[int], float] = exponential_backoff


class Engine:
    host: Host
    config: Config
    reporter: Reporter
    options: Options
    _env: dict[str, str]
    _outcomes: dict[str, Outcome]

    def __init__(self, host: Host, config: Config, reporter: Reporter, options: Options) -> None:
        self.host = host
        self.config = config
        self.reporter = reporter
        self.options = options
        self._env = host.child_env(os.environ)
        self._outcomes = {}

    async def run(self, tasks: Sequence[Task]) -> RunSummary:
        check_cycles(tasks)
        started = dt.datetime.now().astimezone()
        t0 = time.monotonic()
        self.reporter.begin(
            [t.info for t in tasks], mode=self.options.mode, dry_run=self.options.dry_run
        )
        outcomes: dict[str, Outcome] = {}
        self._outcomes = outcomes
        pending = list(tasks)
        planned = {t.info.id for t in tasks}
        running: dict[asyncio.Task[Outcome], Task] = {}
        held: set[str] = set()
        try:
            while pending or running:
                for task in list(pending):
                    if len(running) >= self.options.jobs:
                        break
                    waiting = any(d in planned and d not in outcomes for d in task.info.after)
                    if waiting or task.info.resources & held:
                        continue
                    pending.remove(task)
                    held |= task.info.resources
                    running[asyncio.create_task(self._execute(task))] = task
                if not running:  # unreachable after check_cycles; guards a spin
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for future in done:
                    held -= running.pop(future).info.resources
        except asyncio.CancelledError:
            for future in running:
                _ = future.cancel()
            _ = await asyncio.gather(*running, return_exceptions=True)
            for task in [*running.values(), *pending]:
                if task.info.id not in outcomes:
                    _ = self._finish(task, Status.CANCELLED, "cancelled", 0.0)
            raise
        finally:
            summary = RunSummary(
                mode=self.options.mode,
                started=started,
                elapsed=time.monotonic() - t0,
                outcomes=tuple(outcomes[t.info.id] for t in tasks if t.info.id in outcomes),
                dry_run=self.options.dry_run,
            )
            self.reporter.end(summary)
        return summary

    def _finish(
        self,
        task: Task,
        status: Status,
        message: str,
        elapsed: float,
        *,
        before: str | None = None,
        after: str | None = None,
        attempts: int = 0,
        output: Sequence[str] = (),
    ) -> Outcome:
        outcome = Outcome(
            task_id=task.info.id,
            title=task.info.title,
            status=status,
            message=message,
            before=before,
            after=after,
            elapsed=elapsed,
            attempts=attempts,
            output=tuple(output),
        )
        self._outcomes[task.info.id] = outcome
        self.reporter.finished(outcome)
        return outcome

    def _context(self, task: Task, lines: list[str]) -> TaskContext:
        def sink(line: str) -> None:
            lines.append(line)
            self.reporter.output(task.info.id, line)

        return TaskContext(
            host=self.host,
            env=self._env,
            settings=self.config.task(task.info.id),
            dry_run=self.options.dry_run,
            force=self.options.force,
            sink=sink,
            snapshot=ProcessTable.snapshot,
        )

    async def _execute(self, task: Task) -> Outcome:
        t0 = time.monotonic()
        lines: list[str] = []
        ctx = self._context(task, lines)
        self.reporter.started(task.info.id)
        before: str | None = None
        attempts = 0
        action = Mode.UPDATE
        try:
            action = self._preflight(task, ctx)
            if action is Mode.INSTALL:
                ctx.log(f"{task.info.title} is not installed; installing it")
            else:
                before = await self._version(task, ctx)
            attempts, message = await self._attempt(task, ctx, action)
            after = await self._version(task, ctx)
        except Skip as skip:
            return self._finish(
                task, Status.SKIPPED, str(skip), time.monotonic() - t0, output=lines
            )
        except asyncio.CancelledError:
            _ = self._finish(
                task, Status.CANCELLED, "cancelled", time.monotonic() - t0, output=lines
            )
            raise
        except TimeoutError:
            message = f"timed out after {self._timeout(task):.0f}s"
            return self._finish(
                task,
                Status.FAILED,
                message,
                time.monotonic() - t0,
                before=before,
                attempts=attempts,
                output=lines,
            )
        except (CommandError, TaskFailed, OSError) as error:
            return self._finish(
                task,
                Status.FAILED,
                str(error),
                time.monotonic() - t0,
                before=before,
                attempts=max(attempts, 1),
                output=lines,
            )
        except Exception as error:  # a bug in one task must not abort the run
            lines.extend(traceback.format_exception(error))
            return self._finish(
                task,
                Status.FAILED,
                f"internal error: {error!r}",
                time.monotonic() - t0,
                before=before,
                attempts=max(attempts, 1),
                output=lines,
            )

        if self.options.dry_run:
            status = Status.DONE
            message = message or "would run"
        elif action is Mode.INSTALL:
            status = Status.UPDATED if after else Status.DONE
            message = message or (f"installed {after}" if after else "installed")
        elif before and after and before != after:
            status = Status.UPDATED
        elif before and after:
            status = Status.CURRENT
        else:
            status = Status.DONE
        return self._finish(
            task,
            status,
            message or "",
            time.monotonic() - t0,
            before=before,
            after=after,
            attempts=attempts,
            output=lines,
        )

    def _preflight(self, task: Task, ctx: TaskContext) -> Mode:
        """Decide whether to install or update; raise `Skip` to do neither.

        A missing tool is only installed when asked for: `upkeep install`, or
        ``install = true`` for it in the config (what setup writes).
        """
        probe = task.probe(self.host)
        wants_install = self.options.mode is Mode.INSTALL or ctx.settings.install
        if probe.presence is Presence.MISSING and wants_install:
            if not task.installable:
                raise Skip(f"{probe.note}; upkeep has no installer for it")
            return Mode.INSTALL
        if self.options.mode is Mode.INSTALL:
            raise Skip("already installed" if probe.presence is Presence.INSTALLED else probe.note)
        if probe.presence is not Presence.INSTALLED:
            raise Skip(probe.note)

        users = ctx.processes().matching(task.busy_matcher(self.host))
        if not users:
            return Mode.UPDATE
        if when_busy(task, self.config) is BusyPolicy.SKIP and not self.options.force:
            raise Skip(f"in use by {describe(users)}")
        ctx.log(f"in use by {describe(users)}; those sessions keep their current version")
        return Mode.UPDATE

    def _timeout(self, task: Task) -> float:
        return self.config.task(task.info.id).timeout or task.info.timeout

    async def _version(self, task: Task, ctx: TaskContext) -> str | None:
        try:
            async with asyncio.timeout(30):
                return await task.version(ctx)
        except OSError, TimeoutError, CommandError:
            return None

    async def _attempt(self, task: Task, ctx: TaskContext, mode: Mode) -> tuple[int, str | None]:
        """Run the update (or install), retrying command failures."""
        action = task.install if mode is Mode.INSTALL else task.update
        attempt = 1
        while True:
            try:
                async with asyncio.timeout(self._timeout(task)):
                    return attempt, await action(ctx)
            except CommandError as error:
                if attempt >= self.options.retries:
                    raise
                if RATE_LIMITED.search(error.result.text):
                    delay = self.options.backoff(attempt)
                    ctx.log(f"rate limited; backing off {delay:.0f}s (attempt {attempt})")
                else:
                    delay = 1.0
                    ctx.log(f"{error}; retrying (attempt {attempt + 1}/{self.options.retries})")
                await asyncio.sleep(delay)
                attempt += 1
