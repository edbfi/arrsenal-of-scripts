"""Snapshot running processes to decide whether a tool is in use.

A process counts as using a tool when its executable, or any absolute path on
its command line, lies under one of the tool's directories, or when its
executable is named like the tool. The command-line check is what catches
interpreted programs such as ``bun ~/.bun/install/global/node_modules/x/cli.js``.

upkeep's own process tree is excluded so that an installer we spawned is never
mistaken for a user session.
"""

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

PROC = Path("/proc")


@dataclass(frozen=True, slots=True)
class Proc:
    pid: int
    ppid: int
    exe: str
    argv: tuple[str, ...]
    locations: tuple[str, ...] = ()  # absolute paths it uses, symlinks resolved

    @classmethod
    def create(cls, pid: int, ppid: int, exe: str, argv: tuple[str, ...]) -> Proc:
        return cls(pid, ppid, exe, argv, _locations(exe, argv))

    @property
    def name(self) -> str:
        return Path(self.exe or (self.argv[0] if self.argv else "")).name

    def names(self) -> set[str]:
        """Executable basename plus argv[0]'s: claude's exe is named by version."""
        found = {self.name}
        if self.argv:
            found.add(Path(self.argv[0]).name)
        return found

    def paths(self) -> tuple[str, ...]:
        return self.locations or _locations(self.exe, self.argv)

    def label(self) -> str:
        return f"{self.name}[{self.pid}]"


@dataclass(frozen=True, slots=True)
class ProcMatcher:
    """What "this tool is running" means for one tool."""

    names: frozenset[str] = frozenset()
    dirs: tuple[Path, ...] = ()

    def matches(self, proc: Proc) -> bool:
        if not self.names.isdisjoint(proc.names()):
            return True
        prefixes = [f"{d}/" for d in self.dirs]
        return any(p.startswith(prefix) for p in proc.paths() for prefix in prefixes)

    def __bool__(self) -> bool:
        return bool(self.names or self.dirs)


@dataclass(frozen=True, slots=True)
class ProcessTable:
    procs: tuple[Proc, ...] = field(default=())

    @classmethod
    def snapshot(cls, *, exclude_tree_of: int | None = None) -> ProcessTable:
        procs = list(_read_proc() if PROC.is_dir() else _read_ps())
        root = os.getpid() if exclude_tree_of is None else exclude_tree_of
        excluded = descendants(procs, root) | {root} | launcher_ancestors(procs, root)
        return cls(tuple(p for p in procs if p.pid not in excluded))

    def matching(self, matcher: ProcMatcher) -> list[Proc]:
        if not matcher:
            return []
        return [p for p in self.procs if matcher.matches(p)]


def _locations(exe: str, argv: tuple[str, ...]) -> tuple[str, ...]:
    """The exe and every absolute argv path, each also through symlinks: a bun
    ACP runs as ``bun ~/.bun/bin/x``, which links into node_modules."""
    raw = [exe, *argv] if exe.startswith("/") else list(argv)
    found: list[str] = []
    for item in raw:
        if item.startswith("/"):
            found += [item, os.path.realpath(item)]
    return tuple(dict.fromkeys(found))


def describe(procs: Iterable[Proc], limit: int = 4) -> str:
    procs = list(procs)
    shown = ", ".join(p.label() for p in procs[:limit])
    more = len(procs) - limit
    return f"{shown} +{more} more" if more > 0 else shown


def descendants(procs: Iterable[Proc], root: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)
    found: set[int] = set()
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def launcher_ancestors(procs: Iterable[Proc], root: int) -> set[int]:
    """Ancestors that only exist to start upkeep: the ``~/.local/bin/upkeep``
    shim and its ``uv run``. A real session further up (claude, say) still
    counts as in use."""
    by_pid = {p.pid: p for p in procs}
    found: set[int] = set()
    current = by_pid.get(root)
    while current is not None and (parent := by_pid.get(current.ppid)) is not None:
        if not any(Path(arg).name == "upkeep" for arg in parent.argv):
            break
        found.add(parent.pid)
        current = parent
    return found


def _read_proc() -> Iterator[Proc]:
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw_argv = (entry / "cmdline").read_bytes()
            stat = (entry / "stat").read_text()
        except OSError:  # exited mid-scan, or not ours to read
            continue
        if not raw_argv:  # kernel thread
            continue
        try:
            exe = str((entry / "exe").readlink()).removesuffix(" (deleted)")
        except OSError:
            exe = ""
        # The comm field is parenthesised and may itself contain ") ".
        fields = stat[stat.rfind(")") + 2 :].split()
        argv = tuple(a.decode(errors="replace") for a in raw_argv.split(b"\0") if a)
        yield Proc.create(int(entry.name), int(fields[1]), exe, argv)


def _ps(column: str) -> dict[int, list[str]]:
    out = subprocess.run(
        ["ps", "-axww", "-o", f"pid=,ppid=,{column}="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows: dict[int, list[str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows[int(parts[0])] = parts[1:]
    return rows


class _LibProc:
    """macOS ``proc_pidpath``: the real executable path. ``ps -o comm`` only
    shows the bare name of a program started by name from PATH."""

    _lib: ctypes.CDLL | None

    def __init__(self) -> None:
        try:
            self._lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        except OSError:
            self._lib = None

    def pidpath(self, pid: int) -> str:
        if self._lib is None:
            return ""
        buf = ctypes.create_string_buffer(4096)
        # ctypes calls are untyped; the C function returns the path length.
        length = cast("object", self._lib.proc_pidpath(pid, buf, len(buf)))
        if not isinstance(length, int) or length <= 0:
            return ""
        return buf.raw[:length].decode(errors="replace")


_LIBPROC = _LibProc() if sys.platform == "darwin" else None


def _read_ps() -> Iterator[Proc]:
    """macOS: no /proc. libproc for the exe, ``ps`` for the rest."""
    comms = _ps("comm")
    args = _ps("args")
    for pid, (ppid, comm) in comms.items():
        exe = _LIBPROC.pidpath(pid) if _LIBPROC else ""
        if not exe:  # another user's process: fall back to what ps says
            exe = os.path.realpath(comm) if comm.startswith("/") else comm
        argv = tuple(args.get(pid, ["", ""])[1].split())
        yield Proc.create(pid, int(ppid), exe, argv)
