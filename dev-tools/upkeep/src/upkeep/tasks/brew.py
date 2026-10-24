"""Homebrew: ``brew update``, then a busy-aware ``brew upgrade``.

Instead of a blanket ``brew upgrade``, outdated formulae and casks are listed
and any whose files a running process is using are held back. Upgrades run
with ``HOMEBREW_NO_INSTALL_CLEANUP`` so that dependencies upgraded along the
way keep their old keg for whoever still has it open (a running Python
lazily importing from its old Cellar path, say); only the packages that were
explicitly checked as idle are cleaned up afterwards.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast, override

from upkeep.host import OS, Host
from upkeep.model import Probe, TaskFailed, TaskInfo
from upkeep.procs import ProcessTable, ProcMatcher, describe
from upkeep.tasks.base import Task, TaskContext, parse_version

UPGRADE_ENV = {"HOMEBREW_NO_INSTALL_CLEANUP": "1", "HOMEBREW_NO_AUTO_UPDATE": "1"}


@dataclass(frozen=True, slots=True)
class Outdated:
    name: str
    cask: bool
    installed: str
    current: str
    pinned: bool = False

    def label(self) -> str:
        return f"{self.name} {self.installed} → {self.current}"


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object(item) for item in cast("list[object]", value)]


def _first_str(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        items = cast("list[object]", value)
        return next((i for i in items if isinstance(i, str)), "?")
    return "?"


def parse_outdated(raw: str) -> list[Outdated]:
    """Parse ``brew outdated --json=v2``."""
    document = _object(cast("object", json.loads(raw)))
    found: list[Outdated] = []
    for key, cask in (("formulae", False), ("casks", True)):
        for entry in _objects(document.get(key)):
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            found.append(
                Outdated(
                    name=name,
                    cask=cask,
                    installed=_first_str(entry.get("installed_versions")),
                    current=_first_str(entry.get("current_version")),
                    pinned=entry.get("pinned") is True,
                )
            )
    return found


def parse_cask_apps(raw: str) -> dict[str, tuple[str, ...]]:
    """Map cask token → ``.app`` bundle names from ``brew info --json=v2 --cask``."""
    apps: dict[str, tuple[str, ...]] = {}
    for cask in _objects(_object(cast("object", json.loads(raw))).get("casks")):
        token = cask.get("token")
        if not isinstance(token, str):
            continue
        bundles: list[str] = []
        for artifact in _objects(cask.get("artifacts")):
            app = artifact.get("app")
            if isinstance(app, list):
                bundles += [a for a in cast("list[object]", app) if isinstance(a, str)]
        apps[token] = tuple(bundles)
    return apps


class Brew(Task):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="brew",
                title="brew",
                summary="brew update + upgrade, holding back packages in use",
                resources=frozenset({"brew"}),
                timeout=3600.0,
                busy_note="decided per package: running formulae/casks are held back",
            )
        )

    @override
    def probe(self, host: Host) -> Probe:
        if host.brew_prefix is None:
            return Probe.missing()
        return Probe.installed(host.brew_prefix / "bin" / "brew")

    @override
    async def version(self, ctx: TaskContext) -> str | None:
        result = await ctx.query("brew", "--version")
        return parse_version(result.text) if result.ok else None

    def matcher(self, host: Host, item: Outdated, apps: tuple[str, ...]) -> ProcMatcher:
        prefix = host.brew_prefix or Path("/nonexistent")
        if not item.cask:
            dirs = (prefix / "Cellar" / item.name, prefix / "opt" / item.name)
            return ProcMatcher(dirs=dirs)
        dirs = [prefix / "Caskroom" / item.name]
        for app in apps:
            dirs += [Path("/Applications") / app, host.home / "Applications" / app]
        return ProcMatcher(dirs=tuple(dirs))

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        _ = await ctx.run("brew", "update")
        listing = await ctx.query("brew", "outdated", "--json=v2")
        if not listing.ok:
            raise TaskFailed("brew outdated failed: " + "\n".join(listing.stderr))
        outdated = [o for o in parse_outdated(listing.text) if not o.pinned]
        if not outdated:
            return "everything up to date"

        casks = [o.name for o in outdated if o.cask]
        apps: dict[str, tuple[str, ...]] = {}
        if casks and ctx.host.os is OS.MACOS:
            info = await ctx.query("brew", "info", "--json=v2", "--cask", *casks)
            apps = parse_cask_apps(info.text) if info.ok else {}

        procs: ProcessTable = ctx.processes()
        idle: list[Outdated] = []
        held: list[str] = []
        for item in outdated:
            users = procs.matching(self.matcher(ctx.host, item, apps.get(item.name, ())))
            if users and not ctx.force:
                ctx.log(f"holding back {item.label()}: in use by {describe(users)}")
                held.append(item.name)
            else:
                idle.append(item)

        formulae = [o.name for o in idle if not o.cask]
        idle_casks = [o.name for o in idle if o.cask]
        if formulae:
            _ = await ctx.run("brew", "upgrade", "--formula", *formulae, extra_env=UPGRADE_ENV)
        if idle_casks:
            _ = await ctx.run("brew", "upgrade", "--cask", *idle_casks, extra_env=UPGRADE_ENV)
        if formulae or idle_casks:
            _ = await ctx.run("brew", "cleanup", *formulae, *idle_casks, check=False)

        parts: list[str] = []
        if idle:
            verb = "would upgrade" if ctx.dry_run else "upgraded"
            parts.append(f"{verb} {len(idle)}: " + ", ".join(o.label() for o in idle))
        if held:
            parts.append(f"held back (in use): {', '.join(held)}")
        return "; ".join(parts)
