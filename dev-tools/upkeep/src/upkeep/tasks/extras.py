"""Tasks that maintain something other than a CLI binary."""

from pathlib import Path
from typing import TYPE_CHECKING, override

from upkeep.model import Probe, TaskInfo
from upkeep.procs import ProcMatcher, describe
from upkeep.sources import classify
from upkeep.tasks.base import Task, TaskContext

if TYPE_CHECKING:
    from upkeep.host import Host


class AgentBrowser(Task):
    """Fetch the browser build matching the installed agent-browser.

    The CLI itself is updated by brew (or bun globals); this runs after them.
    Installing replaces files a running browser may still load, so it waits
    until no agent-browser daemon or browser is running.
    """

    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="agent-browser",
                title="agent-browser",
                summary="agent-browser install (browser for the current CLI)",
                after=("brew", "bun-globals"),
                busy_note="a running browser loads files from the install dir",
            )
        )

    @staticmethod
    def browsers_dir(host: Host) -> Path:
        return host.home / ".agent-browser" / "browsers"

    @override
    def probe(self, host: Host) -> Probe:
        path = host.which("agent-browser")
        if path is None:
            return Probe.missing()
        return Probe.installed(path, source=classify(host, path, (host.local_bin,)).source)

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        return ProcMatcher(names=frozenset({"agent-browser"}), dirs=(self.browsers_dir(host),))

    @override
    async def version(self, ctx: TaskContext) -> str | None:
        root = self.browsers_dir(ctx.host)
        builds = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        return builds[-1] if builds else None

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        _ = await ctx.run("agent-browser", "install")
        return None


class UvTools(Task):
    """``uv tool upgrade`` for each uv-installed tool that is not running.

    Off by default: a tool installed both with uv and a standalone installer
    (prek, say) would have its ~/.local/bin entry point re-linked by uv.
    """

    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="uv-tools",
                title="uv tools",
                summary="uv tool upgrade, per tool, skipping running ones",
                after=("uv",),
                resources=frozenset({"uv-tools"}),
                enabled_by_default=False,
                busy_note="decided per tool: each has its own environment",
            )
        )

    @override
    def probe(self, host: Host) -> Probe:
        uv = host.which("uv")
        return Probe.installed(uv) if uv else Probe.missing("uv is not installed")

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        result = await ctx.query("uv", "tool", "dir")
        tools_dir = Path(result.text.strip())
        if not result.ok or not tools_dir.is_dir():
            return "no uv tools installed"
        procs = ctx.processes()
        idle: list[str] = []
        held: list[str] = []
        for env in sorted(p for p in tools_dir.iterdir() if p.is_dir()):
            users = procs.matching(ProcMatcher(dirs=(env,)))
            if users and not ctx.force:
                ctx.log(f"skipping {env.name}: in use by {describe(users)}")
                held.append(env.name)
            else:
                idle.append(env.name)
        if idle:
            _ = await ctx.run("uv", "tool", "upgrade", *idle)
        message = f"checked {len(idle)} tool(s)"
        return f"{message}; held back (in use): {', '.join(held)}" if held else message
