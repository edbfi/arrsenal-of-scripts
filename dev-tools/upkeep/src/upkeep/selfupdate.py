"""Commit-based self-update of the checkout upkeep runs from.

upkeep runs from a git checkout of arrsenal-of-scripts. Updating it means:
fetch the tracked branch, and if the checkout is a clean fast-forward behind
it, ``git merge --ff-only``. Anything else (another branch checked out,
uncommitted changes, local commits) is reported and left alone, so a
development clone is never rewritten under its owner.

If the update touched upkeep's own files the process re-executes itself, so
the rest of the run uses the new code; dependency changes are synced first.
"""

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from upkeep import layout
from upkeep.host import CHILD_ENV, Host

REEXEC_MARKER = "UPKEEP_SELF_UPDATED"
GIT_TIMEOUT = 60.0


class GitError(RuntimeError):
    pass


class State(StrEnum):
    CURRENT = "up to date"
    AVAILABLE = "update available"
    BLOCKED = "update blocked"
    ERROR = "check failed"
    UNMANAGED = "not a git checkout"


@dataclass(frozen=True, slots=True)
class Checkout:
    root: Path
    branch: str | None
    head: str
    dirty: bool


@dataclass(frozen=True, slots=True)
class Check:
    state: State
    checkout: Checkout | None = None
    remote_head: str = ""
    behind: int = 0
    ahead: int = 0
    changes: tuple[str, ...] = ()  # one-line log of commits touching upkeep
    reason: str = ""

    def describe(self) -> str:
        head = self.checkout.head[:7] if self.checkout else "?"
        match self.state:
            case State.CURRENT:
                return f"{head}, up to date"
            case State.AVAILABLE:
                return f"{head}, {self.behind} new commit(s) available"
            case State.BLOCKED:
                return f"{head}, {self.behind} new commit(s); not applied: {self.reason}"
            case State.ERROR | State.UNMANAGED:
                return (
                    f"{head}, {self.state.value}: {self.reason}"
                    if self.reason
                    else self.state.value
                )


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str:
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f"git {args[0]}: {error}") from None
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def find_checkout(start: Path | None = None) -> Checkout | None:
    start = start or layout.PROJECT_DIR
    try:
        root = Path(_git(start, "rev-parse", "--show-toplevel"))
        head = _git(root, "rev-parse", "HEAD")
        dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    except GitError:
        return None
    try:
        branch: str | None = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    except GitError:
        branch = None  # detached HEAD
    return Checkout(root=root, branch=branch, head=head, dirty=dirty)


def owned_paths(checkout: Checkout) -> list[str]:
    return [str((layout.DEV_TOOLS_DIR / p).relative_to(checkout.root)) for p in layout.OWNED_PATHS]


def check(remote: str, branch: str, *, fetch: bool = True, start: Path | None = None) -> Check:
    checkout = find_checkout(start)
    if checkout is None:
        return Check(State.UNMANAGED)
    upstream = f"{remote}/{branch}"
    try:
        if fetch:
            _ = _git(checkout.root, "fetch", "--quiet", "--no-tags", remote, branch)
        remote_head = _git(checkout.root, "rev-parse", upstream)
        counts = _git(checkout.root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        ahead, behind = (int(n) for n in counts.split())
        changes = (
            _git(
                checkout.root,
                "log",
                "--format=%h %s",
                f"HEAD..{upstream}",
                "--",
                *owned_paths(checkout),
            ).splitlines()
            if behind
            else []
        )
    except (GitError, ValueError) as error:
        return Check(State.ERROR, checkout, reason=str(error))

    result = Check(State.CURRENT, checkout, remote_head, behind, ahead, tuple(changes))
    if behind == 0:
        return result
    blockers: list[str] = []
    if checkout.branch != branch:
        blockers.append(f"checkout is on {checkout.branch or 'a detached HEAD'}, not {branch}")
    if checkout.dirty:
        blockers.append("uncommitted changes")
    if ahead:
        blockers.append(f"{ahead} local commit(s) not on {upstream}")
    state = State.BLOCKED if blockers else State.AVAILABLE
    return Check(state, checkout, remote_head, behind, ahead, tuple(changes), "; ".join(blockers))


@dataclass(frozen=True, slots=True)
class Applied:
    old: str
    new: str
    code_changed: bool  # upkeep's own files changed: re-exec to use them


def apply(result: Check, host: Host) -> Applied:
    if result.state is not State.AVAILABLE or result.checkout is None:
        raise GitError(f"nothing to apply ({result.describe()})")
    root = result.checkout.root
    old = result.checkout.head
    _ = _git(root, "merge", "--ff-only", "--quiet", result.remote_head)
    changed = _git(
        root, "diff", "--name-only", old, result.remote_head, "--", *owned_paths(result.checkout)
    ).splitlines()
    project = str(layout.PROJECT_DIR.relative_to(root))
    if any(f in {f"{project}/uv.lock", f"{project}/pyproject.toml"} for f in changed):
        _sync_environment(host)
    return Applied(old=old, new=result.remote_head, code_changed=bool(changed))


def _sync_environment(host: Host) -> None:
    uv = host.which("uv")
    if uv is None:
        raise GitError("dependencies changed but uv was not found to sync them")
    env = os.environ | CHILD_ENV | {"PATH": host.search_path(os.environ.get("PATH", ""))}
    result = subprocess.run(
        [
            str(uv),
            "sync",
            "--frozen",
            "--no-dev",
            "--inexact",
            "--quiet",
            "--project",
            str(layout.PROJECT_DIR),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise GitError(f"uv sync failed: {result.stderr.strip()}")


def already_reexeced() -> bool:
    return REEXEC_MARKER in os.environ


def reexec(argv: list[str]) -> NoReturn:
    """Replace this process with the updated code, same arguments."""
    os.environ[REEXEC_MARKER] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "upkeep", *argv])
