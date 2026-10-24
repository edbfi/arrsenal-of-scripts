"""Bun global packages (the ACP adapters live here) and agent skills."""

import json
from typing import TYPE_CHECKING, cast, override

from upkeep.model import Probe, TaskInfo
from upkeep.procs import ProcMatcher
from upkeep.tasks.base import Task, TaskContext

if TYPE_CHECKING:
    from pathlib import Path

    from upkeep.host import Host

DEFAULT_GLOBALS = (
    "@agentclientprotocol/claude-agent-acp",
    "@agentclientprotocol/codex-acp",
)


def _global_dir(host: Host) -> Path:
    return host.home / ".bun" / "install" / "global"


def _read_json(path: Path) -> dict[str, object]:
    try:
        loaded = cast("object", json.loads(path.read_text()))
    except OSError, ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast("dict[str, object]", loaded)  # JSON object keys are strings


def installed_globals(host: Host) -> dict[str, str]:
    """Top-level global packages and their installed versions."""
    root = _global_dir(host)
    deps = _read_json(root / "package.json").get("dependencies", {})
    if not isinstance(deps, dict):
        return {}
    versions: dict[str, str] = {}
    for name in cast("dict[str, object]", deps):
        version = _read_json(root / "node_modules" / name / "package.json").get("version")
        versions[name] = version if isinstance(version, str) else "?"
    return versions


class BunGlobals(Task):
    """``bun update -g --latest``, after making sure the ACPs are installed.

    Every global shares one hoisted node_modules, rewritten in place, so a
    running ACP can lose a lazily-required file mid-session. This task therefore
    skips entirely while any global package is running.
    """

    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="bun-globals",
                title="bun globals",
                summary="install the ACP adapters, then bun update -g --latest",
                after=("bun",),
                resources=frozenset({"bun-global"}),
                busy_note="node_modules is rewritten in place under running ACPs",
            )
        )

    @override
    def probe(self, host: Host) -> Probe:
        bun = host.which("bun")
        return Probe.installed(bun) if bun else Probe.missing("bun is not installed")

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        return ProcMatcher(dirs=(_global_dir(host) / "node_modules",))

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        wanted = ctx.settings.strings("ensure", DEFAULT_GLOBALS)
        before = installed_globals(ctx.host)
        missing = [name for name in wanted if name not in before]
        if missing:
            _ = await ctx.run("bun", "add", "-g", *missing)
        _ = await ctx.run("bun", "update", "-g", "--latest")
        after = installed_globals(ctx.host)
        changed = [
            f"{name.rsplit('/', 1)[-1]} {before[name]} → {version}"
            for name, version in sorted(after.items())
            if name in before and before[name] != version
        ]
        added = [name.rsplit("/", 1)[-1] for name in missing if name in after]
        parts: list[str] = []
        if added:
            parts.append("added " + ", ".join(added))
        if changed:
            parts.append(", ".join(changed))
        return "; ".join(parts) or "all globals up to date"


class Skills(Task):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="skills",
                title="skills",
                summary="bunx skills update -g",
                after=("bun",),
                busy_note="skills are read when a session starts",
            )
        )

    @override
    def probe(self, host: Host) -> Probe:
        bun = host.which("bunx")
        return Probe.installed(bun) if bun else Probe.missing("bun is not installed")

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        _ = await ctx.run("bunx", "skills", "update", "-g")
        return None
