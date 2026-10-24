"""The tasks that delete files or hold packages back, against a fake host."""

import asyncio
import fcntl
import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING, override

from upkeep.config import TaskSettings
from upkeep.execute import CommandResult
from upkeep.procs import Proc, ProcessTable
from upkeep.tasks.base import TaskContext
from upkeep.tasks.brew import Brew
from upkeep.tasks.standalone import Agy, Codex

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from upkeep.host import Host


class FakeContext(TaskContext):
    """Records commands instead of running them; queries get canned output."""

    lines: list[str]
    commands: list[tuple[str, ...]]
    replies: dict[tuple[str, ...], str]
    table: ProcessTable

    def __init__(
        self,
        host: Host,
        *,
        procs: Sequence[Proc] = (),
        dry_run: bool = False,
        force: bool = False,
        replies: Mapping[tuple[str, ...], str] | None = None,
    ) -> None:
        self.lines = []
        self.commands = []
        self.replies = dict(replies or {})
        self.table = ProcessTable(tuple(procs))
        super().__init__(
            host=host,
            env={},
            settings=TaskSettings(),
            dry_run=dry_run,
            force=force,
            sink=self.lines.append,
            snapshot=lambda: self.table,
        )

    @override
    async def run(
        self,
        *argv: str | Path,
        check: bool = True,
        mutates: bool = True,
        extra_env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        args = tuple(str(a) for a in argv)
        self.commands.append(args)
        return CommandResult(args, 0, ())

    @override
    async def query(self, *argv: str | Path) -> CommandResult:
        args = tuple(str(a) for a in argv)
        return CommandResult(args, 0, tuple(self.replies.get(args, "").splitlines()))


def running(exe: Path) -> Proc:
    return Proc.create(pid=4242, ppid=1, exe=str(exe), argv=(str(exe),))


# ------------------------------------------------------------------ codex


def codex_root(host: Host, *releases: str, current: str) -> Path:
    root = Codex.packages_dir(host) / "standalone"
    for name in releases:
        (root / "releases" / name / "bin").mkdir(parents=True)
        _ = (root / "releases" / name / "bin" / "codex").write_text("")
    (root / "current").symlink_to(root / "releases" / current)
    return root


def remaining(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "releases").iterdir())


def test_codex_prune_keeps_current_and_releases_in_use(host: Host) -> None:
    root = codex_root(host, "0.1", "0.2", "0.3", current="0.3")
    ctx = FakeContext(host, procs=[running(root / "releases/0.2/bin/codex")])
    assert Codex().prune(ctx) == "pruned 1 old release(s)"
    assert remaining(root) == ["0.2", "0.3"]


def test_codex_prune_dry_run_removes_nothing(host: Host) -> None:
    root = codex_root(host, "0.1", "0.2", current="0.2")
    ctx = FakeContext(host, dry_run=True)
    assert Codex().prune(ctx) is None
    assert remaining(root) == ["0.1", "0.2"]
    assert any("would remove standalone/0.1" in line for line in ctx.lines)


def test_codex_prune_waits_for_an_unplaced_codex(host: Host) -> None:
    root = codex_root(host, "0.1", "0.2", current="0.2")
    pathless = Proc(pid=4242, ppid=1, exe="", argv=("codex",))  # no path to tie it to
    assert Codex().prune(FakeContext(host, procs=[pathless])) is None
    assert remaining(root) == ["0.1", "0.2"]


def test_codex_prune_skips_a_root_whose_installer_runs(host: Host) -> None:
    root = codex_root(host, "0.1", "0.2", current="0.2")
    lock = root / "install.lock"
    _ = lock.write_text("")
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert Codex().prune(FakeContext(host)) is None
    finally:
        os.close(fd)
    assert remaining(root) == ["0.1", "0.2"]
    assert Codex().prune(FakeContext(host)) == "pruned 1 old release(s)"


# -------------------------------------------------------------------- agy


def agy_bin(host: Host) -> Path:
    host.local_bin.mkdir(parents=True)
    agy = host.local_bin / "agy"
    _ = agy.write_text("#!/bin/sh\n")
    agy.chmod(0o755)
    for name in ("agy.1761000000.old", "agy.1762000000.old", "agy.notes"):
        _ = (host.local_bin / name).write_text("")
    return agy


def test_agy_prune_removes_only_old_binaries(host: Host) -> None:
    agy = agy_bin(host)
    assert Agy().prune(FakeContext(host)) == "removed 2 old binaries"
    assert sorted(p.name for p in agy.parent.iterdir()) == ["agy", "agy.notes"]


def test_agy_prune_keeps_everything_while_agy_runs(host: Host) -> None:
    agy = agy_bin(host)
    assert Agy().prune(FakeContext(host, procs=[running(agy)])) is None
    assert Agy().prune(FakeContext(host, dry_run=True)) == "would remove 2 old binaries"
    assert len(list(agy.parent.glob("agy.*.old"))) == 2


# ------------------------------------------------------------------- brew

OUTDATED = json.dumps(
    {
        "formulae": [
            {"name": n, "installed_versions": ["1"], "current_version": "2", "pinned": False}
            for n in ("busy", "idle")
        ],
        "casks": [],
    }
)


def brew_context(host: Host, tmp_path: Path, *, force: bool = False) -> FakeContext:
    prefix = tmp_path / "brew"
    exe = prefix / "Cellar" / "busy" / "1" / "bin" / "busy"
    exe.parent.mkdir(parents=True)
    return FakeContext(
        replace(host, brew_prefix=prefix),
        procs=[running(exe)],
        force=force,
        replies={("brew", "outdated", "--json=v2"): OUTDATED},
    )


def test_brew_holds_back_packages_in_use(host: Host, tmp_path: Path) -> None:
    ctx = brew_context(host, tmp_path)
    summary = asyncio.run(Brew().update(ctx))
    assert ctx.commands == [
        ("brew", "update"),
        ("brew", "upgrade", "--formula", "idle"),
        ("brew", "cleanup", "idle"),
    ]
    assert summary == "upgraded 1: idle 1 → 2; held back (in use): busy"


def test_brew_force_upgrades_packages_in_use(host: Host, tmp_path: Path) -> None:
    ctx = brew_context(host, tmp_path, force=True)
    _ = asyncio.run(Brew().update(ctx))
    assert ("brew", "upgrade", "--formula", "busy", "idle") in ctx.commands
