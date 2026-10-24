"""Find every copy of a tool on the host and work out who installed each.

A tool's own updater (``claude update``, ``agy update``, ...) is only used
when the copy the user actually runs (the first on PATH) came from the
vendor's native installer. A copy owned by Homebrew, apt, a bun global, a uv
tool and so on belongs to that manager, and upkeep leaves it to it.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from upkeep.host import Host


class Source(StrEnum):
    NATIVE = "native installer"
    BREW = "Homebrew"
    APT = "apt"
    RPM = "dnf/rpm"
    PACMAN = "pacman"
    SNAP = "snap"
    NIX = "nix"
    NPM = "npm global"
    BUN = "bun global"
    UV_TOOL = "uv tool"
    PYTHON = "pip/pipx"
    CARGO = "cargo"
    UNKNOWN = "unknown"

    @property
    def owner_hint(self) -> str:
        """Who updates a copy from this source, for messages."""
        return {
            Source.BREW: "the brew task updates it",
            Source.BUN: "the bun-globals task updates it",
            Source.UV_TOOL: "the uv-tools task updates it",
            Source.PYTHON: "update it with pip or pipx",
            Source.APT: "update it with apt (upkeep never uses sudo)",
            Source.RPM: "update it with dnf (upkeep never uses sudo)",
            Source.PACMAN: "update it with pacman (upkeep never uses sudo)",
            Source.SNAP: "snapd refreshes it",
            Source.NIX: "update it with nix",
            Source.NPM: "update it with npm",
            Source.CARGO: "update it with cargo install",
        }.get(self, "upkeep does not know its installer, so leaves it alone")


@dataclass(frozen=True, slots=True)
class Location:
    path: Path
    real: Path
    source: Source
    package: str = ""

    def describe(self) -> str:
        package = f" ({self.package})" if self.package else ""
        return f"{self.path} [{self.source.value}{package}]"


# Places tools end up besides PATH: cron and fresh shells often miss these.
EXTRA_DIRS = (
    ".local/bin",
    ".bun/bin",
    ".cargo/bin",
    ".npm-global/bin",
    ".nix-profile/bin",
    ".linuxbrew/bin",
)
SYSTEM_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/snap/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def search_dirs(host: Host) -> list[Path]:
    dirs = [Path(p) for p in host.search_path(os.environ.get("PATH", "")).split(os.pathsep) if p]
    dirs += [host.home / d for d in EXTRA_DIRS]
    dirs += [Path(d) for d in SYSTEM_DIRS]
    return list(dict.fromkeys(dirs))


def _under(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _system_package(path: Path) -> tuple[Source, str] | None:
    """Ask the distro package manager whether it owns `path`."""
    queries = (
        (Source.APT, ["dpkg-query", "-S"]),
        (Source.RPM, ["rpm", "-qf", "--qf", "%{NAME}"]),
        (Source.PACMAN, ["pacman", "-Qqo"]),
    )
    for source, argv in queries:
        if shutil.which(argv[0]) is None:
            continue
        try:
            result = subprocess.run(
                [*argv, str(path)], capture_output=True, text=True, timeout=10, check=False
            )
        except OSError, subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and result.stdout.strip():
            # dpkg-query prints "pkg[:arch]: /path"; the others print the name.
            return source, result.stdout.split(":", 1)[0].strip()
    return None


def uv_tool_dir(host: Host) -> Path:
    if value := os.environ.get("UV_TOOL_DIR"):
        return Path(value)
    data = os.environ.get("XDG_DATA_HOME") or str(host.home / ".local" / "share")
    return Path(data) / "uv" / "tools"


def _is_python_script(path: Path) -> bool:
    """pip/pipx entry points are small scripts with a python shebang; the
    vendor-installed tools here are all native executables."""
    try:
        with path.open("rb") as fh:
            head = fh.readline(256)
    except OSError:
        return False
    return head.startswith(b"#!") and b"python" in head


def classify(host: Host, path: Path, native: Sequence[Path]) -> Location:
    real = path.resolve()
    home = host.home
    managed = (
        (Source.UV_TOOL, [uv_tool_dir(host)]),
        (Source.PYTHON, [home / ".local" / "share" / "pipx", home / ".local" / "pipx"]),
        (Source.BUN, [home / ".bun" / "install" / "global"]),
        (Source.NIX, [Path("/nix"), home / ".nix-profile"]),
        (Source.SNAP, [Path("/snap")]),
    )
    for source, roots in managed:
        if _under(real, roots) or _under(path, roots):
            return Location(path, real, source)
    if host.brew_owns(path):
        return Location(path, real, Source.BREW)
    if "node_modules" in real.parts:
        return Location(path, real, Source.NPM)
    if _is_python_script(real):
        return Location(path, real, Source.PYTHON)
    if path.parent in native or _under(real, native):
        return Location(path, real, Source.NATIVE)
    if _under(real, [home / ".cargo"]):
        return Location(path, real, Source.CARGO)
    if _under(real, [Path("/usr"), Path("/bin"), Path("/sbin"), Path("/opt")]):
        owned = _system_package(real)
        if owned:
            return Location(path, real, owned[0], owned[1])
    return Location(path, real, Source.UNKNOWN)


def find_all(host: Host, binary: str, native: Sequence[Path]) -> list[Location]:
    """Every copy of `binary`, PATH order first, one entry per real file."""
    seen: set[Path] = set()
    found: list[Location] = []
    for directory in search_dirs(host):
        candidate = directory / binary
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        real = candidate.resolve()
        if real in seen:
            continue
        seen.add(real)
        found.append(classify(host, candidate, native))
    return found


def detected_managers(host: Host) -> Iterator[str]:
    """Package managers present on the host, for the setup screen."""
    if host.brew_prefix is not None:
        yield "brew"
    for name in ("apt", "dnf", "pacman", "snap", "nix", "bun", "npm", "uv", "cargo"):
        if host.which(name) is not None:
            yield name
