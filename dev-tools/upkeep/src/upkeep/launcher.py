"""The ``~/.local/bin/upkeep`` launcher pointing at this checkout."""

import shlex
from typing import TYPE_CHECKING

from upkeep import layout

if TYPE_CHECKING:
    from pathlib import Path

    from upkeep.host import Host

MARKER = "# upkeep launcher"


def path(host: Host) -> Path:
    return host.local_bin / "upkeep"


def render(uv: Path | None) -> str:
    """The shim. uv's path is recorded at install time because cron and
    launchd start it with a bare PATH (uv may live in a Homebrew prefix)."""
    project = shlex.quote(str(layout.PROJECT_DIR))
    known = shlex.quote(str(uv)) if uv else '"$HOME/.local/bin/uv"'
    return f"""#!/bin/sh
{MARKER}, written by `upkeep self install`; rewritten on reinstall.
# uv syncs the locked environment before each start, so a self-update that
# changes dependencies needs no extra step.
uv={known}
[ -x "$uv" ] || uv=$(command -v uv 2>/dev/null) || uv="$HOME/.local/bin/uv"
exec "$uv" run --frozen --no-dev --quiet --project {project} -- upkeep "$@"
"""


def is_ours(target: Path) -> bool:
    try:
        return MARKER in target.read_text(errors="replace")[:200]
    except OSError:
        return False


class LauncherError(RuntimeError):
    pass


def install(host: Host) -> Path:
    target = path(host)
    if target.exists() and not is_ours(target):
        raise LauncherError(f"{target} exists and was not written by upkeep; not overwriting it")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(".upkeep.tmp")
    _ = tmp.write_text(render(host.which("uv")))
    tmp.chmod(0o755)
    _ = tmp.replace(target)
    return target


def status(host: Host) -> str:
    target = path(host)
    if not target.exists():
        return "not installed (run `upkeep self install`)"
    if not is_ours(target):
        return f"{target} is not an upkeep launcher"
    if f"--project {shlex.quote(str(layout.PROJECT_DIR))} " not in target.read_text():
        return f"{target} points at another checkout (reinstall to use this one)"
    return str(target)
