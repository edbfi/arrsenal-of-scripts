"""The tasks that delete files or hold packages back, against a fake host."""

import asyncio
import fcntl
import json
import os
import tomllib
from dataclasses import replace
from typing import TYPE_CHECKING, override

import pytest

from upkeep import layout
from upkeep.config import TaskSettings, parse
from upkeep.execute import CommandResult
from upkeep.model import Presence, Skip, TaskFailed
from upkeep.procs import Proc, ProcessTable
from upkeep.tasks import builtin_tasks, check_config, is_enabled
from upkeep.tasks.base import TaskContext
from upkeep.tasks.brew import Brew
from upkeep.tasks.extras import USABLE_PYTHON, T3AntigravityAcp, T3AntigravityAcpGateway
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


# ----------------------------------------------------------- antigravity acp


def acp_root(host: Host, *releases: str, current: str, previous: str) -> Path:
    root = T3AntigravityAcp.install_root(host)
    for name in releases:
        (root / "releases" / name).mkdir(parents=True)
        _ = (root / "releases" / name / "agy_acp_server.par").write_text("")
    (root / "releases" / ".staging-1.6.0-abc").mkdir()
    (root / "antigravity-acp-current").symlink_to(f"releases/{current}")
    (root / "antigravity-acp-previous").symlink_to(f"releases/{previous}")
    return root


def test_acp_prune_keeps_current_previous_running_and_staging(host: Host) -> None:
    root = acp_root(
        host, "1.3.0-x", "1.4.0-x", "1.4.1-x", "1.5.0-x", current="1.5.0-x", previous="1.4.1-x"
    )
    ctx = FakeContext(host, procs=[running(root / "releases/1.4.0-x/agy_acp_server.par")])
    assert T3AntigravityAcp().prune(ctx) == "pruned 1 old release(s)"
    assert remaining(root) == [".staging-1.6.0-abc", "1.4.0-x", "1.4.1-x", "1.5.0-x"]


def test_acp_prune_keeps_everything_without_an_active_release(host: Host) -> None:
    root = acp_root(host, "1.4.0-x", "1.5.0-x", current="1.5.0-x", previous="1.4.0-x")
    (root / "releases" / "1.3.0-x").symlink_to(root / "releases" / "1.4.0-x")
    (root / "antigravity-acp-current").unlink()
    (root / "antigravity-acp-current").symlink_to("releases/gone")
    assert T3AntigravityAcp().prune(FakeContext(host)) is None
    assert remaining(root) == [".staging-1.6.0-abc", "1.3.0-x", "1.4.0-x", "1.5.0-x"]
    (root / "antigravity-acp-current").unlink()
    (root / "antigravity-acp-current").symlink_to("releases/1.4.0-x")
    assert T3AntigravityAcp().prune(FakeContext(host)) == "pruned 1 old release(s)"
    assert remaining(root) == [".staging-1.6.0-abc", "1.3.0-x", "1.4.0-x"]


def test_acp_prune_waits_for_a_running_updater(host: Host) -> None:
    root = acp_root(host, "1.4.0-x", "1.4.1-x", "1.5.0-x", current="1.5.0-x", previous="1.4.1-x")
    fd = os.open(root / ".antigravity-acp.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert T3AntigravityAcp().prune(FakeContext(host)) is None
    finally:
        os.close(fd)
    assert "1.4.0-x" in remaining(root)
    ctx = FakeContext(host, dry_run=True)
    assert T3AntigravityAcp().prune(ctx) is None
    assert any("would remove 1.4.0-x" in line for line in ctx.lines)
    assert "1.4.0-x" in remaining(root)


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


# ------------------------------------------------------- antigravity gateway

SHIM_BODY = "print('shim v2')\n"
PYTHON = "/opt/python/bin/python3"


@pytest.fixture
def shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The checkout's copy of the gateway shim."""
    source = tmp_path / "antigravity_acp_gateway.py"
    _ = source.write_text("#!/usr/bin/env python3\n" + SHIM_BODY)
    monkeypatch.setattr(layout, "ACP_GATEWAY", source)
    return source


def gateway_host(host: Host, *, installed: str | None = None) -> Path:
    """An ACP install root, optionally with a shim already installed."""
    root = T3AntigravityAcp.install_root(host)
    root.mkdir(parents=True)
    _ = (root / "antigravity-acp").write_text("#!/bin/sh\n")
    if installed is not None:
        _ = (root / "antigravity-acp-gateway").write_text(installed)
    return root


def gateway_context(host: Host, *usable: str, python: str | None = None) -> FakeContext:
    ctx = FakeContext(host, replies={(p, "-c", USABLE_PYTHON): "True" for p in usable})
    if python is not None:
        ctx.settings = TaskSettings(extra={"python": python})
    return ctx


def install_command(source: Path, root: Path, python: str) -> tuple[str, ...]:
    return (python, "-I", str(source), "--install", "--python", python, "--install-root", str(root))


def test_gateway_probe_reports_each_missing_piece(host: Host, shim: Path) -> None:
    task = T3AntigravityAcpGateway()
    assert "ACP launcher" in task.probe(host).note
    root = gateway_host(host)
    probe = task.probe(host)
    assert probe.presence is Presence.MISSING
    assert "upkeep install t3-antigravity-acp-gateway" in probe.note
    _ = (root / "antigravity-acp-gateway").write_text("#!/x\n")
    assert task.probe(host).presence is Presence.INSTALLED
    shim.unlink()
    assert "shim not found" in task.probe(host).note


def test_gateway_installs_with_the_configured_python(host: Host, shim: Path) -> None:
    root = gateway_host(host)
    ctx = gateway_context(host, PYTHON, python=PYTHON)
    _ = asyncio.run(T3AntigravityAcpGateway().install(ctx))
    assert ctx.commands == [install_command(shim, root, PYTHON)]


@pytest.mark.usefixtures("shim")
def test_gateway_skips_reinstall_when_current(host: Host) -> None:
    _ = gateway_host(host, installed=f"#!{PYTHON} -I\n{SHIM_BODY}")
    ctx = gateway_context(host, PYTHON)
    _ = asyncio.run(T3AntigravityAcpGateway().update(ctx))
    assert ctx.commands == []


def test_gateway_reinstalls_when_the_checkout_changed(host: Host, shim: Path) -> None:
    root = gateway_host(host, installed=f"#!{PYTHON} -I\nprint('shim v1')\n")
    ctx = gateway_context(host, PYTHON)
    _ = asyncio.run(T3AntigravityAcpGateway().update(ctx))
    assert ctx.commands == [install_command(shim, root, PYTHON)]


def test_gateway_reinstalls_when_the_configured_python_changed(host: Host, shim: Path) -> None:
    root = gateway_host(host, installed=f"#!{PYTHON} -I\n{SHIM_BODY}")
    ctx = gateway_context(host, PYTHON, "/usr/bin/python3", python="/usr/bin/python3")
    _ = asyncio.run(T3AntigravityAcpGateway().update(ctx))
    assert ctx.commands == [install_command(shim, root, "/usr/bin/python3")]


@pytest.mark.usefixtures("shim")
def test_gateway_resolves_a_bare_configured_name(host: Host) -> None:
    host.local_bin.mkdir(parents=True)
    python = host.local_bin / "python3"
    _ = python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    _ = gateway_host(host, installed=f"#!{python} -I\n{SHIM_BODY}")
    ctx = gateway_context(host, str(python), python="python3")
    _ = asyncio.run(T3AntigravityAcpGateway().update(ctx))
    assert ctx.commands == []


@pytest.mark.usefixtures("shim")
def test_gateway_fails_without_a_usable_python(host: Host) -> None:
    _ = gateway_host(host, installed=f"#!{PYTHON} -I\nold\n")
    ctx = gateway_context(host)  # nothing answers as a 3.11+ non-venv Python
    with pytest.raises(TaskFailed, match=r"set tasks\.t3-antigravity-acp-gateway\.python"):
        _ = asyncio.run(T3AntigravityAcpGateway().update(ctx))
    assert ctx.commands == []


@pytest.mark.usefixtures("shim")
def test_gateway_install_needs_the_acp_launcher(host: Host) -> None:
    with pytest.raises(Skip, match="install t3-antigravity-acp first"):
        _ = asyncio.run(
            T3AntigravityAcpGateway().install(gateway_context(host, PYTHON, python=PYTHON))
        )


def test_gateway_is_off_by_default_and_takes_a_python_option() -> None:
    task = next(t for t in builtin_tasks() if t.info.id == "t3-antigravity-acp-gateway")
    assert not is_enabled(task, parse({}))
    config = parse(
        tomllib.loads(
            '[tasks.t3-antigravity-acp-gateway]\nenabled = true\npython = "/usr/bin/python3"'
        )
    )
    check_config(config)
    assert is_enabled(task, config)
    assert config.task("t3-antigravity-acp-gateway").string("python") == "/usr/bin/python3"
