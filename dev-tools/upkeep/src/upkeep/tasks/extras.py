"""Tasks that maintain something other than a CLI binary."""

import hashlib
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, override

from upkeep import layout
from upkeep.config import BusyPolicy
from upkeep.execute import CommandError
from upkeep.model import Probe, Skip, TaskFailed, TaskInfo
from upkeep.procs import ProcMatcher, describe
from upkeep.sources import classify
from upkeep.tasks.base import Task, TaskContext, parse_version
from upkeep.tasks.standalone import try_lock

if TYPE_CHECKING:
    from upkeep.host import Host

ACP_LOCK_BUSY = 75  # update_antigravity_acp.py: another updater holds the lock
# Names the updater uses under its install root.
ACP_LOCK = ".antigravity-acp.lock"
ACP_CURRENT = "antigravity-acp-current"
ACP_PREVIOUS = "antigravity-acp-previous"
ACP_LAUNCHER = "antigravity-acp"
GATEWAY_NAME = "antigravity-acp-gateway"  # what antigravity_acp_gateway.py --install writes
# Tried in order when nothing is configured or installed. Stable names, not
# resolved paths, so a shebang survives the interpreter's own upgrades.
PYTHON_CANDIDATES = ("python3", "python3.14", "python3.13", "python3.12", "python3.11")
# A virtualenv interpreter could vanish with its venv (upkeep's own included).
USABLE_PYTHON = "import sys; print(sys.version_info >= (3, 11) and sys.prefix == sys.base_prefix)"


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
    nothing in use is ever deleted, so running sessions are unaffected. After
    updating, releases other than current and previous are pruned unless running.
    """

    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="t3-antigravity-acp",
                title="t3 antigravity acp",
                summary="update_antigravity_acp.py --yes, then prune unused releases",
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
        current = self.install_root(ctx.host) / ACP_CURRENT
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
        _ = await self._updater(ctx)
        return self.prune(ctx)

    def prune(self, ctx: TaskContext) -> str | None:
        """Delete releases other than current and previous, unless still running.

        The updater never prunes on its own, so every ACP version would stay.
        """
        root = self.install_root(ctx.host)
        removed: list[str] = []
        with try_lock(root / ACP_LOCK) as locked:
            if not locked:
                ctx.log("keeping old releases: an ACP updater is running")
                return None
            # Read under the lock: an updater may have just activated a release.
            if not (root / ACP_CURRENT).is_dir():
                ctx.log("keeping old releases: no active release")
                return None
            keep = {(root / link).resolve() for link in (ACP_CURRENT, ACP_PREVIOUS)}
            procs = ctx.processes()
            for release in sorted((root / "releases").glob("*/")):
                if (
                    release.name.startswith(".")
                    or release.is_symlink()
                    or release.resolve() in keep
                ):
                    continue
                if procs.matching(ProcMatcher(dirs=(release, release.resolve()))):
                    ctx.log(f"keeping {release.name}: still running")
                    continue
                if ctx.dry_run:
                    ctx.log(f"[dry-run] would remove {release.name}")
                    continue
                ctx.log(f"removing old release {release.name}")
                shutil.rmtree(release)
                removed.append(release.name)
        return f"pruned {len(removed)} old release(s)" if removed else None

    @property
    @override
    def installable(self) -> bool:
        return layout.ACP_UPDATER.is_file()

    @override
    async def install(self, ctx: TaskContext) -> str | None:
        return await self._updater(ctx)


class T3AntigravityAcpGateway(Task):
    """Keep the T3 Antigravity gateway shim installed beside the ACP launcher.

    The shim lives in this checkout, which self-update keeps current; this task
    reinstalls it when the checkout's copy differs from the installed one. Off
    by default: T3 uses it only once its Antigravity *Binary path* points at it.
    Installing replaces the file atomically, so running sessions keep theirs.
    """

    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="t3-antigravity-acp-gateway",
                title="t3 antigravity acp gateway",
                summary="reinstall antigravity_acp_gateway.py when this checkout's copy changes",
                after=("t3-antigravity-acp",),
                when_busy=BusyPolicy.ALLOW,
                enabled_by_default=False,
                busy_note="the installed file is replaced atomically",
                options=frozenset({"python"}),
            )
        )

    @staticmethod
    def target(host: Host) -> Path:
        return T3AntigravityAcp.install_root(host) / GATEWAY_NAME

    @override
    def probe(self, host: Host) -> Probe:
        if not layout.ACP_GATEWAY.is_file():
            return Probe.missing(f"shim not found at {layout.ACP_GATEWAY}")
        if not (T3AntigravityAcp.install_root(host) / ACP_LAUNCHER).exists():
            return Probe.missing("needs the ACP launcher (run `upkeep install t3-antigravity-acp`)")
        target = self.target(host)
        if not target.is_file():
            return Probe.missing(f"not set up (run `upkeep install {self.info.id}`)")
        return Probe.installed(target)

    @override
    async def version(self, ctx: TaskContext) -> str | None:
        target = self.target(ctx.host)
        if not target.is_file():
            return None
        # The interpreter counts too, so switching only it reports an update.
        interpreter = _shebang_interpreter(target) or ""
        return _digest(interpreter.encode() + b"\n" + _body(target))

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        target = self.target(ctx.host)
        interpreter = await self._interpreter(ctx)
        if (
            target.is_file()
            and _body(target) == _body(layout.ACP_GATEWAY)
            and _shebang_interpreter(target) == interpreter
        ):
            return None
        root = T3AntigravityAcp.install_root(ctx.host)
        _ = await ctx.run(
            interpreter,
            "-I",
            layout.ACP_GATEWAY,
            "--install",
            "--python",
            interpreter,
            "--install-root",
            root,
        )
        return None

    async def _interpreter(self, ctx: TaskContext) -> str:
        """The configured interpreter, else the installed one, else the first usable on PATH."""
        configured = ctx.settings.string("python")
        if configured:
            # --install writes the resolved path, so compare in that form.
            candidates = [str(ctx.host.which(configured) or configured)]
        else:
            installed = _shebang_interpreter(self.target(ctx.host))
            on_path = [str(p) for name in PYTHON_CANDIDATES if (p := ctx.host.which(name))]
            candidates = [*([installed] if installed else []), *on_path]
        for candidate in dict.fromkeys(candidates):
            result = await ctx.query(candidate, "-c", USABLE_PYTHON)
            if result.ok and result.text.strip() == "True":
                return candidate
        raise TaskFailed(
            f"no Python 3.11+ outside a virtualenv among {', '.join(candidates) or 'PATH'};"
            + f" set tasks.{self.info.id}.python"
        )

    @property
    @override
    def installable(self) -> bool:
        return layout.ACP_GATEWAY.is_file()

    @override
    async def install(self, ctx: TaskContext) -> str | None:
        if not (T3AntigravityAcp.install_root(ctx.host) / ACP_LAUNCHER).exists():
            raise Skip("install t3-antigravity-acp first")
        return await self.update(ctx)


def _body(path: Path) -> bytes:
    """The script without its shebang, which --install rewrites per host."""
    return path.read_bytes().partition(b"\n")[2]


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:12]


def _shebang_interpreter(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            first = handle.readline().decode("utf-8", "replace")
    except OSError:
        return None
    parts = first.removeprefix("#!").split()
    if not first.startswith("#!") or not parts or not Path(parts[0]).is_absolute():
        return None
    return parts[0]


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
