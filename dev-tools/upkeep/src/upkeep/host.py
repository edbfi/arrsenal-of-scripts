"""The machine upkeep runs on: OS, user, standard locations and the child env."""

import getpass
import os
import platform
import shutil
import socket
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

APP = "upkeep"

# Prefixes Homebrew is installed under, in preference order.
BREW_PREFIXES = (
    Path("/opt/homebrew"),
    Path("/usr/local"),
    Path("/home/linuxbrew/.linuxbrew"),
)

SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

# Exported to every child process. Installers default to editing shell rc files
# and asking questions on /dev/tty; neither is acceptable from a scheduled run,
# and two installers appending to ~/.zshrc at once is a race.
CHILD_ENV = {
    "INSTALLER_NO_MODIFY_PATH": "1",  # cargo-dist installers: uv, ruff, prek
    "CODEX_NON_INTERACTIVE": "1",
    "NONINTERACTIVE": "1",  # Homebrew
    "HOMEBREW_NO_ENV_HINTS": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def _own_venv_bin() -> str | None:
    """upkeep's own virtualenv. `uv run` puts it first on PATH; it must not
    shadow the user's tools (it has its own ruff, for one)."""
    return str(Path(sys.prefix) / "bin") if sys.prefix != sys.base_prefix else None


class OS(StrEnum):
    MACOS = "darwin"
    LINUX = "linux"


class UnsupportedHostError(RuntimeError):
    pass


def _xdg_dir(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable, "")
    return Path(value) if value and Path(value).is_absolute() else fallback


@dataclass(frozen=True, slots=True)
class Host:
    os: OS
    machine: str
    hostname: str
    user: str
    home: Path
    brew_prefix: Path | None
    config_dir: Path
    state_dir: Path

    @classmethod
    def detect(cls) -> Host:
        system = platform.system().lower()
        if system not in OS:
            raise UnsupportedHostError(f"unsupported operating system: {system}")
        home = Path.home()
        return cls(
            os=OS(system),
            machine=platform.machine(),
            hostname=socket.gethostname().split(".")[0],
            user=getpass.getuser(),
            home=home,
            brew_prefix=next((p for p in BREW_PREFIXES if (p / "bin" / "brew").is_file()), None),
            config_dir=_xdg_dir("XDG_CONFIG_HOME", home / ".config") / APP,
            state_dir=_xdg_dir("XDG_STATE_HOME", home / ".local" / "state") / APP,
        )

    @property
    def local_bin(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def is_root(self) -> bool:
        return os.geteuid() == 0

    def bin_dirs(self) -> tuple[Path, ...]:
        """User tool directories, ahead of the inherited PATH."""
        dirs = [self.local_bin, self.home / ".bun" / "bin", self.home / ".cargo" / "bin"]
        if self.brew_prefix is not None:
            dirs += [self.brew_prefix / "bin", self.brew_prefix / "sbin"]
        return tuple(dirs)

    def search_path(self, inherited: str = "") -> str:
        """PATH with the user's tool dirs first, so cron's bare PATH still works."""
        entries = [str(p) for p in self.bin_dirs()]
        entries += [p for p in inherited.split(os.pathsep) if p]
        entries += SYSTEM_PATH
        own = _own_venv_bin()
        return os.pathsep.join(e for e in dict.fromkeys(entries) if e != own)

    def child_env(self, base: Mapping[str, str]) -> dict[str, str]:
        env = {k: v for k, v in base.items() if k not in {"VIRTUAL_ENV", "UV_RUN_RECURSION_DEPTH"}}
        env["PATH"] = self.search_path(base.get("PATH", ""))
        _ = env.setdefault("HOME", str(self.home))
        env.update(CHILD_ENV)
        return env

    def which(self, name: str) -> Path | None:
        found = shutil.which(name, path=self.search_path(os.environ.get("PATH", "")))
        return Path(found) if found else None

    def brew_owns(self, path: Path) -> bool:
        """Whether `path` resolves into the Homebrew prefix."""
        if self.brew_prefix is None:
            return False
        return path.resolve().is_relative_to(self.brew_prefix)

    def describe(self) -> str:
        return f"{self.user}@{self.hostname} ({self.os.value} {self.machine})"
