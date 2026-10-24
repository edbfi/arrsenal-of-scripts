"""Daily scheduled runs: the user's crontab, or a LaunchAgent on macOS.

No backend uses root. Every tool upkeep manages installs under $HOME or into
a user-owned Homebrew prefix: Homebrew refuses to run as root and Claude's
installer refuses sudo. A root crontab would leave root-owned files in the
user's home and break the next manual update.

launchd is the macOS default because it runs a job missed during sleep at
the next wake, while cron silently skips it.
"""

import os
import plistlib
import re
import shlex
import subprocess
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, override

from upkeep.config import ScheduleBackendName
from upkeep.host import OS, Host

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

CRON_MARKER = "# upkeep managed"
LAUNCHD_LABEL = "io.github.edbfi.upkeep"
TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class At:
    hour: int
    minute: int

    @classmethod
    def parse(cls, text: str) -> At:
        match = TIME.match(text.strip())
        if not match:
            raise ScheduleError(f"expected a time like 04:30, got {text!r}")
        return cls(int(match.group(1)), int(match.group(2)))

    @classmethod
    def default_for(cls, hostname: str) -> At:
        """04:xx, the minute derived from the hostname so hosts don't all hit
        the same download servers in the same minute."""
        return cls(4, zlib.crc32(hostname.encode()) % 60)

    @override
    def __str__(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True, slots=True)
class Installed:
    at: At | None
    command: str
    backend: str

    def describe(self) -> str:
        when = f"daily at {self.at}" if self.at else "custom timing"
        return f"{when} via {self.backend}"


class Backend(Protocol):
    name: str

    def current(self) -> Installed | None: ...
    def install(self, at: At, argv: Sequence[str], log: Path, env: Mapping[str, str]) -> None: ...
    def remove(self) -> bool: ...


def _run(argv: Sequence[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), input=stdin, capture_output=True, text=True, check=False)
    except OSError as error:
        raise ScheduleError(f"{argv[0]}: {error}") from None


class Cron(Backend):
    name: str = "cron"

    def _read(self) -> list[str]:
        result = _run(["crontab", "-l"])
        if result.returncode != 0:
            if "no crontab" in result.stderr.lower():
                return []
            raise ScheduleError(f"crontab -l: {result.stderr.strip()}")
        return result.stdout.splitlines()

    def _write(self, lines: list[str]) -> None:
        result = _run(["crontab", "-"], stdin="\n".join(lines) + "\n")
        if result.returncode != 0:
            raise ScheduleError(f"crontab: {result.stderr.strip()}")

    @override
    def current(self) -> Installed | None:
        for line in self._read():
            if line.rstrip().endswith(CRON_MARKER):
                fields = line.split(None, 5)
                at = None
                if len(fields) == 6 and fields[0].isdigit() and fields[1].isdigit():
                    at = At(int(fields[1]), int(fields[0]))
                return Installed(at, line, self.name)
        return None

    @staticmethod
    def entry(at: At, argv: Sequence[str], log: Path, env: Mapping[str, str]) -> str:
        prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
        command = f"{prefix}{shlex.join(argv)} >> {shlex.quote(str(log))} 2>&1"
        return f"{at.minute} {at.hour} * * * {command} {CRON_MARKER}"

    @override
    def install(self, at: At, argv: Sequence[str], log: Path, env: Mapping[str, str]) -> None:
        kept = [line for line in self._read() if not line.rstrip().endswith(CRON_MARKER)]
        self._write([*kept, self.entry(at, argv, log, env)])

    @override
    def remove(self) -> bool:
        lines = self._read()
        kept = [line for line in lines if not line.rstrip().endswith(CRON_MARKER)]
        if kept == lines:
            return False
        self._write(kept)
        return True


class Launchd(Backend):
    name: str = "launchd"
    plist: Path
    domain: str

    def __init__(self, host: Host) -> None:
        self.plist = host.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"

    @staticmethod
    def document(
        at: At, argv: Sequence[str], log: Path, env: Mapping[str, str]
    ) -> dict[str, object]:
        doc: dict[str, object] = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": list(argv),
            "StartCalendarInterval": {"Hour": at.hour, "Minute": at.minute},
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "RunAtLoad": False,
        }
        if env:
            doc["EnvironmentVariables"] = dict(env)
        return doc

    @override
    def current(self) -> Installed | None:
        if not self.plist.is_file():
            return None
        try:
            with self.plist.open("rb") as fh:
                loaded = cast("object", plistlib.load(fh))
        except (OSError, plistlib.InvalidFileException) as error:
            raise ScheduleError(f"{self.plist}: {error}") from None
        match loaded:
            case {
                "ProgramArguments": object() as args,
                "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
            }:
                items = cast("list[object]", args) if isinstance(args, list) else []
                words = [a for a in items if isinstance(a, str)]
                return Installed(At(hour, minute), shlex.join(words), self.name)
            case _:
                return Installed(None, str(self.plist), self.name)

    @override
    def install(self, at: At, argv: Sequence[str], log: Path, env: Mapping[str, str]) -> None:
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        _ = _run(["launchctl", "bootout", f"{self.domain}/{LAUNCHD_LABEL}"])
        with self.plist.open("wb") as fh:
            plistlib.dump(self.document(at, argv, log, env), fh)
        result = _run(["launchctl", "bootstrap", self.domain, str(self.plist)])
        if result.returncode != 0:
            raise ScheduleError(f"launchctl bootstrap: {result.stderr.strip()}")

    @override
    def remove(self) -> bool:
        if not self.plist.exists():
            return False
        _ = _run(["launchctl", "bootout", f"{self.domain}/{LAUNCHD_LABEL}"])
        self.plist.unlink()
        return True


def backend_for(host: Host, choice: ScheduleBackendName) -> Backend:
    if choice is ScheduleBackendName.LAUNCHD or (
        choice is ScheduleBackendName.AUTO and host.os is OS.MACOS
    ):
        if host.os is not OS.MACOS:
            raise ScheduleError("launchd is only available on macOS")
        return Launchd(host)
    return Cron()


# Locations a manual run honours; a scheduled run must see the same ones, or
# the two would use different lock files and configs.
CARRIED_ENV = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "UV_TOOL_DIR")


def scheduled_env() -> dict[str, str]:
    return {k: os.environ[k] for k in CARRIED_ENV if os.environ.get(k)}


def scheduled_argv(launcher: Path) -> list[str]:
    return [str(launcher), "run", "--scheduled"]


def current_any(host: Host) -> Installed | None:
    """Whatever upkeep schedule exists, under either backend."""
    backends: list[Backend] = [Cron()]
    if host.os is OS.MACOS:
        backends.insert(0, Launchd(host))
    for backend in backends:
        try:
            found = backend.current()
        except ScheduleError:
            continue
        if found:
            return found
    return None


def remove_all(host: Host) -> list[str]:
    removed: list[str] = []
    backends: list[Backend] = [Cron()]
    if host.os is OS.MACOS:
        backends.append(Launchd(host))
    for backend in backends:
        try:
            if backend.remove():
                removed.append(backend.name)
        except ScheduleError:
            continue
    return removed
