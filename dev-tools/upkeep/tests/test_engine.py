import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

import pytest

from upkeep.config import BusyPolicy, Config, TaskSettings
from upkeep.engine import CycleError, Engine, Options, Reporter
from upkeep.execute import CommandError, CommandResult
from upkeep.model import Mode, Outcome, Probe, RunSummary, Status, TaskFailed, TaskInfo
from upkeep.procs import ProcMatcher
from upkeep.tasks import Task, TaskContext

if TYPE_CHECKING:
    from collections.abc import Sequence

    from upkeep.host import Host


@dataclass
class Recorder(Reporter):
    events: list[str] = field(default_factory=list[str])
    outcomes: dict[str, Outcome] = field(default_factory=dict[str, Outcome])

    @override
    def begin(self, plan: Sequence[TaskInfo], *, mode: Mode, dry_run: bool) -> None:
        self.events.append("begin")

    @override
    def started(self, task_id: str) -> None:
        self.events.append(f"start {task_id}")

    @override
    def output(self, task_id: str, line: str) -> None:
        pass

    @override
    def finished(self, outcome: Outcome) -> None:
        self.events.append(f"end {outcome.task_id}")
        self.outcomes[outcome.task_id] = outcome

    @override
    def end(self, summary: RunSummary) -> None:
        self.events.append("finish")


class Fake(Task):
    """A task whose behaviour is scripted per test."""

    present: bool
    busy_name: str
    versions: list[str]
    failures: list[str]  # output of failed attempts, consumed in order
    crash: bool
    hold: float
    log: list[str]
    can_install: bool

    def __init__(
        self,
        task_id: str,
        log: list[str],
        *,
        after: tuple[str, ...] = (),
        resources: tuple[str, ...] = (),
        when_busy: BusyPolicy = BusyPolicy.SKIP,
        present: bool = True,
        busy_name: str = "",
        versions: Sequence[str] = (),
        failures: Sequence[str] = (),
        crash: bool = False,
        hold: float = 0.01,
        timeout: float = 5.0,
        can_install: bool = True,
    ) -> None:
        super().__init__(
            TaskInfo(
                id=task_id,
                title=task_id,
                summary="",
                after=after,
                resources=frozenset(resources),
                when_busy=when_busy,
                timeout=timeout,
            )
        )
        self.present = present
        self.busy_name = busy_name
        self.versions = list(versions)
        self.failures = list(failures)
        self.crash = crash
        self.hold = hold
        self.log = log
        self.can_install = can_install

    @override
    def probe(self, host: Host) -> Probe:
        return Probe.installed(None) if self.present else Probe.missing()

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        return ProcMatcher(names=frozenset({self.busy_name})) if self.busy_name else ProcMatcher()

    @override
    async def version(self, ctx: TaskContext) -> str | None:
        return self.versions.pop(0) if self.versions else None

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        self.log.append(f"+{self.info.id}")
        await asyncio.sleep(self.hold)
        self.log.append(f"-{self.info.id}")
        if self.crash:
            raise ZeroDivisionError("bug")
        if self.failures:
            raise CommandError(CommandResult(("x",), 1, (self.failures.pop(0),)))
        return None

    @property
    @override
    def installable(self) -> bool:
        return self.can_install

    @override
    async def install(self, ctx: TaskContext) -> str | None:
        self.log.append(f"install {self.info.id}")
        return "installed it"


def run(
    host: Host,
    tasks: Sequence[Task],
    *,
    config: Config | None = None,
    jobs: int = 4,
    retries: int = 3,
    force: bool = False,
) -> tuple[RunSummary, Recorder]:
    recorder = Recorder()
    opts = Options(jobs=jobs, retries=retries, force=force, backoff=lambda _attempt: 0.0)
    engine = Engine(host, config or Config(), recorder, opts)
    return asyncio.run(engine.run(tasks)), recorder


def status(summary: RunSummary, task_id: str) -> Status:
    return next(o.status for o in summary.outcomes if o.task_id == task_id)


def test_after_orders_tasks(host: Host) -> None:
    log: list[str] = []
    tasks = [Fake("b", log, after=("a",)), Fake("a", log)]
    _ = run(host, tasks)
    assert log.index("-a") < log.index("+b")


def test_after_ignores_tasks_not_in_run(host: Host) -> None:
    log: list[str] = []
    summary, _ = run(host, [Fake("b", log, after=("missing",))])
    assert status(summary, "b") is Status.DONE


def test_shared_resource_serialises(host: Host) -> None:
    log: list[str] = []
    shared = ("r",)
    _ = run(host, [Fake("a", log, resources=shared), Fake("b", log, resources=shared)])
    assert log in (["+a", "-a", "+b", "-b"], ["+b", "-b", "+a", "-a"])


def test_jobs_cap(host: Host) -> None:
    log: list[str] = []
    _ = run(host, [Fake(n, log) for n in "abc"], jobs=1)
    assert log == ["+a", "-a", "+b", "-b", "+c", "-c"]


def test_independent_tasks_overlap(host: Host) -> None:
    log: list[str] = []
    _ = run(host, [Fake("a", log, hold=0.05), Fake("b", log, hold=0.05)], jobs=2)
    assert log[:2] == ["+a", "+b"]


def test_missing_tool_is_skipped(host: Host) -> None:
    summary, _ = run(host, [Fake("a", [], present=False)])
    assert status(summary, "a") is Status.SKIPPED


def test_missing_tool_installed_when_configured(host: Host) -> None:
    log: list[str] = []
    config = Config(tasks={"a": TaskSettings(install=True)})
    summary, _ = run(host, [Fake("a", log, present=False, versions=["1.0"])], config=config)
    assert log == ["install a"]
    assert status(summary, "a") is Status.UPDATED


def test_busy_tool_skipped_or_allowed(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    from upkeep import procs

    me = procs.Proc(pid=999_999, ppid=1, exe="/x/sess", argv=("/x/sess",))

    def snapshot(*, exclude_tree_of: int | None = None) -> procs.ProcessTable:
        _ = exclude_tree_of
        return procs.ProcessTable((me,))

    monkeypatch.setattr(procs.ProcessTable, "snapshot", snapshot)
    log: list[str] = []
    tasks = [
        Fake("strict", log, busy_name="sess"),
        Fake("relaxed", log, busy_name="sess", when_busy=BusyPolicy.ALLOW),
    ]
    summary, _ = run(host, tasks)
    assert status(summary, "strict") is Status.SKIPPED
    assert status(summary, "relaxed") is Status.DONE
    summary, _ = run(host, [Fake("strict", log, busy_name="sess")], force=True)
    assert status(summary, "strict") is Status.DONE


def test_retries_then_succeeds(host: Host) -> None:
    log: list[str] = []
    summary, _ = run(host, [Fake("a", log, failures=["HTTP 429 Too Many Requests"])])
    outcome = summary.outcomes[0]
    assert outcome.status is Status.DONE
    assert outcome.attempts == 2


def test_gives_up_after_retries(host: Host) -> None:
    summary, _ = run(host, [Fake("a", [], failures=["boom"] * 5)], retries=2)
    assert summary.outcomes[0].status is Status.FAILED
    assert summary.exit_code == 1


def test_version_change_is_reported(host: Host) -> None:
    summary, _ = run(host, [Fake("a", [], versions=["1.0", "1.1"])])
    outcome = summary.outcomes[0]
    assert (outcome.status, outcome.before, outcome.after) == (Status.UPDATED, "1.0", "1.1")


def test_bug_in_one_task_does_not_stop_others(host: Host) -> None:
    summary, _ = run(host, [Fake("bad", [], crash=True), Fake("good", [])])
    assert status(summary, "bad") is Status.FAILED
    assert "internal error" in summary.outcomes[0].message
    assert status(summary, "good") is Status.DONE


def test_timeout_fails_task(host: Host) -> None:
    summary, _ = run(host, [Fake("slow", [], hold=2.0, timeout=0.05)])
    assert status(summary, "slow") is Status.FAILED
    assert "timed out" in summary.outcomes[0].message


def test_task_failed_is_not_retried(host: Host) -> None:
    class Hard(Fake):
        @override
        async def update(self, ctx: TaskContext) -> str | None:
            self.log.append("try")
            raise TaskFailed("no")

    log: list[str] = []
    summary, _ = run(host, [Hard("a", log)])
    assert log == ["try"]
    assert summary.exit_code == 1


def test_cycle_detected(host: Host) -> None:
    with pytest.raises(CycleError):
        _ = run(host, [Fake("a", [], after=("b",)), Fake("b", [], after=("a",))])


def test_each_task_reported_once(host: Host) -> None:
    _, recorder = run(host, [Fake("a", []), Fake("b", [], after=("a",))])
    assert recorder.events.count("end a") == 1
    assert recorder.events[0] == "begin"
    assert recorder.events[-1] == "finish"
