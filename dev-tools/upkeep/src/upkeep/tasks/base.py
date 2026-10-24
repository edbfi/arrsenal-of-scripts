"""The task contract and the context tasks run their commands through.

Adding a tool means subclassing `Task` (or `BinaryTask` for the common
"one binary with a self-update command" case) and listing it in
`upkeep.tasks.builtin_tasks`. A task only describes *what* to run; the engine
owns ordering, parallelism, busy checks, retries, timeouts and reporting.
"""

import re
import shlex
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, override

from upkeep import execute
from upkeep.execute import CommandError, CommandResult, LineSink
from upkeep.model import Probe, Skip, TaskInfo
from upkeep.procs import ProcessTable, ProcMatcher
from upkeep.sources import Source, find_all

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from upkeep.config import TaskSettings
    from upkeep.host import Host

VERSION = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.\-]+)?")


def parse_version(text: str) -> str | None:
    match = VERSION.search(text)
    return match.group(0) if match else None


def _quiet(_line: str) -> None:
    pass


class TaskContext:
    """Everything a running task may touch."""

    host: Host
    env: dict[str, str]
    settings: TaskSettings
    dry_run: bool
    force: bool  # update even what is in use
    _sink: LineSink
    _snapshot: Callable[[], ProcessTable]

    def __init__(
        self,
        *,
        host: Host,
        env: dict[str, str],
        settings: TaskSettings,
        dry_run: bool,
        force: bool,
        sink: LineSink,
        snapshot: Callable[[], ProcessTable],
    ) -> None:
        self.host = host
        self.env = env
        self.settings = settings
        self.dry_run = dry_run
        self.force = force
        self._sink = sink
        self._snapshot = snapshot

    def log(self, message: str) -> None:
        self._sink(message)

    def processes(self) -> ProcessTable:
        """A fresh snapshot; take it as late as possible before acting."""
        return self._snapshot()

    async def run(
        self,
        *argv: str | Path,
        check: bool = True,
        mutates: bool = True,
        extra_env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        """Run a command, streaming its output into the task log.

        In a dry run, commands that change the system (`mutates=True`) are
        logged instead of executed; read-only queries still run so the plan
        reflects reality.
        """
        args = tuple(str(a) for a in argv)
        if self.dry_run and mutates:
            self.log(f"[dry-run] would run: {shlex.join(args)}")
            return CommandResult(args, 0, ())
        self.log(f"$ {shlex.join(args)}")
        env = self.env | dict(extra_env or {})
        result = await execute.run(args, env=env, sink=self._sink, cwd=cwd)
        if check and not result.ok:
            raise CommandError(result)
        return result

    async def query(self, *argv: str | Path) -> CommandResult:
        """Run a read-only command without echoing its output into the log."""
        args = tuple(str(a) for a in argv)
        return await execute.run(args, env=self.env, sink=_quiet, separate_stderr=True)

    async def run_installer(
        self,
        url: str,
        *,
        interpreter: str = "sh",
        args: tuple[str, ...] = (),
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Download an install script, then run it: a failed download fails
        loudly instead of piping an error page into a shell."""
        with tempfile.TemporaryDirectory(prefix="upkeep-") as tmp:
            script = Path(tmp) / "install.sh"
            _ = await self.run(
                "curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "-fsSL",
                "--retry",
                "3",
                "-o",
                script,
                url,
            )
            return await self.run(interpreter, script, *args, extra_env=extra_env)


class Task(ABC):
    info: TaskInfo

    def __init__(self, info: TaskInfo) -> None:
        self.info = info

    @abstractmethod
    def probe(self, host: Host) -> Probe:
        """Is the tool here, and is it ours to update? Must be fast and offline."""

    def busy_matcher(self, host: Host) -> ProcMatcher:
        """Processes that mean the tool is in use. Empty: never busy."""
        _ = host
        return ProcMatcher()

    async def version(self, ctx: TaskContext) -> str | None:
        """Installed version, for before/after reporting."""
        _ = ctx
        return None

    @abstractmethod
    async def update(self, ctx: TaskContext) -> str | None:
        """Bring the tool up to date. Return an optional one-line result."""

    @property
    def installable(self) -> bool:
        return False

    async def install(self, ctx: TaskContext) -> str | None:
        _ = ctx
        raise Skip("upkeep cannot install this tool")


class BinaryTask(Task, ABC):
    """A single executable on PATH, installed by a vendor install script."""

    binary: str
    install_url: str | None
    interpreter: str
    native_dirs: tuple[str, ...]

    def __init__(
        self,
        info: TaskInfo,
        *,
        binary: str,
        install_url: str | None = None,
        interpreter: str = "sh",
        native_dirs: tuple[str, ...] = (".local/bin",),
    ) -> None:
        super().__init__(info)
        self.binary = binary
        self.install_url = install_url
        self.interpreter = interpreter
        self.native_dirs = native_dirs

    def native(self, host: Host) -> tuple[Path, ...]:
        """Where the vendor's installer puts the binary (or its real target),
        relative to $HOME."""
        return tuple(host.home / d for d in self.native_dirs)

    @override
    def probe(self, host: Host) -> Probe:
        """Classify the copy on PATH: only a native install is ours to update."""
        copies = find_all(host, self.binary, self.native(host))
        if not copies:
            return Probe.missing()
        active = host.which(self.binary)
        primary = next((c for c in copies if c.path == active), copies[0])
        others = tuple(c for c in copies if c is not primary)
        if primary.source is Source.NATIVE:
            return Probe.installed(primary.path, source=primary.source, others=others)
        note = f"installed via {primary.source.value}; {primary.source.owner_hint}"
        return Probe.foreign(primary.path, note, source=primary.source, others=others)

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        return ProcMatcher(names=frozenset({self.binary}))

    @override
    async def version(self, ctx: TaskContext) -> str | None:
        path = ctx.host.which(self.binary)
        if path is None:
            return None
        result = await ctx.query(path, "--version")
        return parse_version(result.text) if result.ok else None

    @property
    @override
    def installable(self) -> bool:
        return self.install_url is not None

    @override
    async def install(self, ctx: TaskContext) -> str | None:
        if self.install_url is None:
            return await super().install(ctx)
        _ = await ctx.run_installer(self.install_url, interpreter=self.interpreter)
        return None
