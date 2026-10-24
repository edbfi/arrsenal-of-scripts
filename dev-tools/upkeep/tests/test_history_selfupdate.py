import datetime as dt
import subprocess
from typing import TYPE_CHECKING

import pytest

from upkeep import history, layout, selfupdate
from upkeep.cli import parse_args
from upkeep.config import OutputMode
from upkeep.host import Host
from upkeep.model import Mode, Outcome, RunSummary, Status
from upkeep.selfupdate import State

if TYPE_CHECKING:
    from pathlib import Path


def test_lock_is_exclusive(tmp_path: Path) -> None:
    with (
        history.instance_lock(tmp_path),
        pytest.raises(history.AlreadyRunningError, match="pid"),
        history.instance_lock(tmp_path),
    ):
        pass
    with history.instance_lock(tmp_path):  # released again
        pass


def test_record_and_read_last_run(tmp_path: Path) -> None:
    summary = RunSummary(
        mode=Mode.UPDATE,
        started=dt.datetime(2026, 10, 24, 4, 37).astimezone(),
        elapsed=12.5,
        outcomes=(
            Outcome("uv", "uv", Status.UPDATED, before="1", after="2"),
            Outcome("codex", "codex", Status.FAILED, message="boom"),
        ),
    )
    history.record(tmp_path, summary, log=tmp_path / "x.log", scheduled=True)
    last = history.last_run(tmp_path)
    assert last is not None
    assert last.failed == ("codex",)
    assert last.scheduled
    assert last.when == "2026-10-24 04:37"


def test_log_rotation(tmp_path: Path) -> None:
    for i in range(history.KEEP_LOGS + 5):
        _ = (tmp_path / f"2026{i:04d}.log").write_text("")
    _ = history.new_log_path(tmp_path)
    assert len(list(tmp_path.glob("*.log"))) == history.KEEP_LOGS - 1


def test_parse_args_narrows() -> None:
    args = parse_args(
        ["run", "uv,ruff", "claude", "--skip", "brew", "-j", "2", "--output", "quiet"]
    )
    assert args.command == "run"
    assert args.tasks == ("uv", "ruff", "claude")
    assert args.skip == ("brew",)
    assert args.jobs == 2
    assert args.output is OutputMode.QUIET
    assert parse_args([]).command is None


# ----------------------------------------------------------- self-update


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An 'upstream' clone to push from and the 'installed' clone upkeep runs in."""
    origin = tmp_path / "origin.git"
    _ = subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    upstream = tmp_path / "upstream"
    _ = subprocess.run(["git", "clone", "-q", str(origin), str(upstream)], check=True)
    for repo in (upstream,):
        _ = git(repo, "config", "user.email", "t@example.com")
        _ = git(repo, "config", "user.name", "t")
        _ = git(repo, "config", "commit.gpgsign", "false")
    project = upstream / "dev-tools" / "upkeep"
    project.mkdir(parents=True)
    _ = (project / "pyproject.toml").write_text("[project]\n")
    _ = git(upstream, "add", "-A")
    _ = git(upstream, "commit", "-qm", "initial")
    _ = git(upstream, "push", "-q", "origin", "main")
    installed = tmp_path / "installed"
    _ = subprocess.run(["git", "clone", "-q", str(origin), str(installed)], check=True)
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        _ = git(installed, "config", key, value)
    monkeypatch.setattr(layout, "PROJECT_DIR", installed / "dev-tools" / "upkeep")
    monkeypatch.setattr(layout, "DEV_TOOLS_DIR", installed / "dev-tools")
    return upstream, installed


def push_change(upstream: Path, path: str, message: str) -> None:
    target = upstream / path
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_text(message)
    _ = git(upstream, "add", "-A")
    _ = git(upstream, "commit", "-qm", message)
    _ = git(upstream, "push", "-q", "origin", "main")


def check(installed: Path) -> selfupdate.Check:
    return selfupdate.check("origin", "main", start=installed)


def test_up_to_date(repos: tuple[Path, Path]) -> None:
    _, installed = repos
    assert check(installed).state is State.CURRENT


def test_fast_forward_and_code_change(repos: tuple[Path, Path], host: Host) -> None:
    upstream, installed = repos
    push_change(upstream, "README.md", "docs elsewhere")
    push_change(upstream, "dev-tools/upkeep/src/x.py", "upkeep change")
    result = check(installed)
    assert result.state is State.AVAILABLE
    assert result.behind == 2
    assert [c.split(" ", 1)[1] for c in result.changes] == ["upkeep change"]
    applied = selfupdate.apply(result, host)
    assert applied.code_changed
    assert git(installed, "rev-parse", "HEAD") == git(upstream, "rev-parse", "HEAD")


def test_unrelated_change_needs_no_restart(repos: tuple[Path, Path], host: Host) -> None:
    upstream, installed = repos
    push_change(upstream, "arr-scripts/x.sh", "other script")
    applied = selfupdate.apply(check(installed), host)
    assert not applied.code_changed


@pytest.mark.parametrize("blocker", ["dirty", "branch", "ahead"])
def test_blocked_checkout_is_left_alone(repos: tuple[Path, Path], blocker: str) -> None:
    upstream, installed = repos
    push_change(upstream, "dev-tools/upkeep/a.py", "new")
    match blocker:
        case "dirty":
            _ = (installed / "dev-tools/upkeep/pyproject.toml").write_text("edited\n")
        case "branch":
            _ = git(installed, "switch", "-qc", "feature")
        case _:
            _ = (installed / "local.txt").write_text("local\n")
            _ = git(installed, "add", "-A")
            _ = git(installed, "commit", "-qm", "local commit")
    result = check(installed)
    assert result.state is State.BLOCKED
    with pytest.raises(selfupdate.GitError):
        _ = selfupdate.apply(result, Host.detect())
