"""Tasks that maintain something other than a CLI binary."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, override

from upkeep import layout
from upkeep.config import BusyPolicy
from upkeep.execute import CommandError
from upkeep.model import Probe, Skip, TaskInfo
from upkeep.procs import ProcMatcher, describe
from upkeep.sources import classify
from upkeep.tasks.base import Task, TaskContext, parse_version

if TYPE_CHECKING:
    from upkeep.host import Host

ACP_LOCK_BUSY = 75  # update_antigravity_acp.py: another updater holds the lock


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


class T3AntigravityAcp(Task):
    """The Antigravity ACP build T3 Code launches, via the bundled updater.

    Releases are installed side by side and activated by swapping a symlink;
    nothing in use is ever deleted, so running sessions are unaffected.
    """

    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="t3-antigravity-acp",
                title="t3 antigravity acp",
                summary="update_antigravity_acp.py --yes (T3 Code custom tool)",
                when_busy=BusyPolicy.ALLOW,
                busy_note="releases are side by side; activation swaps a symlink",
            )
        )

    @staticmethod
    def install_root(host: Host) -> Path:
        return host.home / ".t3" / "custom-tools"

    @override
    def probe(self, host: Host) -> Probe:
        if not layout.ACP_UPDATER.is_file():
            return Probe.missing(f"updater not found at {layout.ACP_UPDATER}")
        launcher = self.install_root(host) / "antigravity-acp"
        if not launcher.exists():
            return Probe.missing("not set up (run `upkeep install t3-antigravity-acp`)")
        return Probe.installed(launcher)

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        return ProcMatcher(dirs=(self.install_root(host) / "releases",))

    @override
    async def version(self, ctx: TaskContext) -> str | None:
        current = self.install_root(ctx.host) / "antigravity-acp-current"
        if not current.is_symlink():
            return None
        return parse_version(current.readlink().name.split("-", 1)[0])  # <version>-<platform>

    async def _updater(self, ctx: TaskContext) -> str | None:
        args = ["--yes"] if not ctx.dry_run else ["--check-only"]
        result = await ctx.run(
            sys.executable, layout.ACP_UPDATER, *args, check=False, mutates=False
        )
        if result.returncode == ACP_LOCK_BUSY:
            raise Skip("another ACP updater is running")
        if not result.ok:
            raise CommandError(result)
        return None

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        return await self._updater(ctx)

    @property
    @override
    def installable(self) -> bool:
        return layout.ACP_UPDATER.is_file()

    @override
    async def install(self, ctx: TaskContext) -> str | None:
        return await self._updater(ctx)


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
