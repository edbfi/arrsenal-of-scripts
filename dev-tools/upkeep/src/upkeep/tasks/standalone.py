"""Standalone CLIs that live in ~/.local/bin (or ~/.bun/bin).

Updates use each tool's own updater (``uv self update``, ``claude update``,
``codex update``, ``agy update``, ...) and only for a copy the native
installer put there (see `BinaryTask.probe`). ruff has no updater, so its
installer is re-run.

Busy policies follow how each vendor replaces its files:

* uv, ruff, prek (cargo-dist): the new binary is written beside the old one
  and ``mv``-ed over it. A running process keeps the old inode.
* claude: versions live in ~/.local/share/claude/versions/<v>; the launcher
  symlink is swapped.
* codex: releases live in ~/.codex/packages/*/releases/<v>; ``current`` is
  swapped with an atomic ``mv -T`` of a symlink.
* agy: ``agy update`` renames the running binary to ``agy.<ts>.old``.
* bun: ``bun upgrade`` renames the new binary into place.

So all of them can update under a running session; the session keeps the
version it started with. Set ``when_busy = "skip"`` per task to be stricter.
"""

import contextlib
import fcntl
import os
import re
import shutil
from typing import TYPE_CHECKING, override

from upkeep.config import BusyPolicy
from upkeep.model import TaskInfo
from upkeep.procs import ProcessTable, ProcMatcher, describe
from upkeep.tasks.base import BinaryTask, TaskContext

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from upkeep.host import Host

ATOMIC = "installer swaps files atomically; running sessions keep their version"
CARGO_DIST = frozenset({"cargo-dist"})  # shared receipt dir and ~/.local/bin/env
CARGO_DIST_DIRS = (".local/bin", ".cargo/bin")  # the latter when CARGO_HOME was set


class CargoDistTask(BinaryTask):
    """uv and prek: ``<tool> self update`` needs the receipt the installer
    wrote; without one, re-running the installer updates and restores it."""

    def receipt(self, host: Host) -> Path:
        return host.config_dir.parent / self.binary / f"{self.binary}-receipt.json"

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        if not self.receipt(ctx.host).is_file():
            ctx.log(f"no install receipt at {self.receipt(ctx.host)}; re-running the installer")
            return await self.install(ctx)
        _ = await ctx.run(self.binary, "self", "update")
        return None


class Uv(CargoDistTask):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="uv",
                title="uv",
                summary="uv self update",
                resources=CARGO_DIST,
                when_busy=BusyPolicy.ALLOW,
                busy_note=ATOMIC,
            ),
            binary="uv",
            install_url="https://astral.sh/uv/install.sh",
            native_dirs=CARGO_DIST_DIRS,
        )


class Ruff(BinaryTask):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="ruff",
                title="ruff",
                summary="re-run the standalone installer",
                resources=CARGO_DIST,
                when_busy=BusyPolicy.ALLOW,
                busy_note=ATOMIC,
            ),
            binary="ruff",
            install_url="https://astral.sh/ruff/install.sh",
            native_dirs=CARGO_DIST_DIRS,
        )

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        return await self.install(ctx)


class Prek(CargoDistTask):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="prek",
                title="prek",
                summary="prek self update",
                after=("uv",),
                resources=CARGO_DIST,
                when_busy=BusyPolicy.ALLOW,
                busy_note=ATOMIC,
            ),
            binary="prek",
            install_url="https://github.com/j178/prek/releases/latest/download/prek-installer.sh",
            native_dirs=CARGO_DIST_DIRS,
        )


class Bun(BinaryTask):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="bun",
                title="bun",
                summary="bun upgrade",
                when_busy=BusyPolicy.ALLOW,
                busy_note="bun upgrade renames the new binary into place",
            ),
            binary="bun",
            install_url="https://bun.sh/install",
            interpreter="bash",
            native_dirs=(".bun/bin",),
        )

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        _ = await ctx.run("bun", "upgrade")
        return None


class Claude(BinaryTask):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="claude",
                title="claude",
                summary="claude update (native installer)",
                when_busy=BusyPolicy.ALLOW,
                busy_note="versions are side by side; the launcher symlink is swapped",
            ),
            binary="claude",
            install_url="https://claude.ai/install.sh",
            interpreter="bash",
            native_dirs=(".local/bin", ".local/share/claude"),
        )

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        # The exe is named after its version, so match on the versions dir too.
        return ProcMatcher(
            names=frozenset({"claude"}),
            dirs=(host.home / ".local" / "share" / "claude" / "versions",),
        )

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        _ = await ctx.run("claude", "update")
        return None


class Codex(BinaryTask):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="codex",
                title="codex",
                summary="codex update, then prune unused releases",
                when_busy=BusyPolicy.ALLOW,
                busy_note="releases are side by side; `current` is swapped atomically",
            ),
            binary="codex",
            install_url="https://chatgpt.com/codex/install.sh",
            native_dirs=(".local/bin", ".codex/packages"),
        )

    @staticmethod
    def packages_dir(host: Host) -> Path:
        return host.home / ".codex" / "packages"

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        return ProcMatcher(names=frozenset({"codex"}), dirs=(self.packages_dir(host),))

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        _ = await ctx.run("codex", "update")
        return self.prune(ctx)

    def prune(self, ctx: TaskContext) -> str | None:
        """Delete releases `current` no longer points at, unless still running.

        The installer never prunes; each release is ~300 MB.
        """
        procs = ctx.processes()
        unplaced = [p for p in procs.matching(self.busy_matcher(ctx.host)) if not p.paths()]
        if unplaced:  # a codex we cannot tie to a release: it may use any of them
            ctx.log(f"keeping old releases: cannot tell which release {describe(unplaced)} runs")
            return None
        removed: list[str] = []
        for root in sorted(self.packages_dir(ctx.host).glob("*/")):
            current = root / "current"
            if not current.exists():
                continue
            with _try_lock(root / "install.lock") as locked:
                if not locked:
                    ctx.log(f"keeping {root.name} releases: its installer is running")
                    continue
                removed += self._prune_root(ctx, root, procs)
        return f"pruned {len(removed)} old release(s)" if removed else None

    def _prune_root(self, ctx: TaskContext, root: Path, procs: ProcessTable) -> list[str]:
        removed: list[str] = []
        active = (root / "current").resolve()
        for release in sorted((root / "releases").glob("*/")):
            if release.resolve() == active:
                continue
            users = procs.matching(ProcMatcher(dirs=(release, release.resolve())))
            if users:
                ctx.log(f"keeping {root.name}/{release.name}: still running")
                continue
            if ctx.dry_run:
                ctx.log(f"[dry-run] would remove {root.name}/{release.name}")
                continue
            ctx.log(f"removing old {root.name} release {release.name}")
            shutil.rmtree(release)
            removed.append(release.name)
        return removed


@contextlib.contextmanager
def _try_lock(path: Path) -> Generator[bool]:
    """Non-blocking flock on an installer's lock file; yields whether we got it."""
    if not path.exists():
        yield True
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


OLD_AGY = re.compile(r"^agy\.\d+\.old$")


class Agy(BinaryTask):
    def __init__(self) -> None:
        super().__init__(
            TaskInfo(
                id="agy",
                title="agy",
                summary="agy update, then remove superseded binaries",
                when_busy=BusyPolicy.ALLOW,
                busy_note="agy update renames the old binary aside",
            ),
            binary="agy",
            install_url="https://antigravity.google/cli/install.sh",
            interpreter="bash",
        )

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        _ = await ctx.run("agy", "update")
        return self.prune(ctx)

    def prune(self, ctx: TaskContext) -> str | None:
        """Remove the ``agy.<ts>.old`` files ``agy update`` leaves behind.

        macOS reports a renamed binary by its original path, so an old file
        cannot be tied to a process; keep all of them while any agy runs.
        """
        binary = ctx.host.which("agy")
        if binary is None:
            return None
        stale = sorted(p for p in binary.parent.iterdir() if OLD_AGY.match(p.name))
        if not stale:
            return None
        if ctx.processes().matching(self.busy_matcher(ctx.host)):
            ctx.log(f"keeping {len(stale)} old agy binaries: agy is running")
            return None
        for path in stale:
            if ctx.dry_run:
                ctx.log(f"[dry-run] would remove {path}")
            else:
                ctx.log(f"removing {path}")
                path.unlink(missing_ok=True)
        noun = "binary" if len(stale) == 1 else "binaries"
        return f"{'would remove' if ctx.dry_run else 'removed'} {len(stale)} old {noun}"
