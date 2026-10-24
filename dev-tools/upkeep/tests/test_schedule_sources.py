import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from upkeep import schedule
from upkeep.schedule import CRON_MARKER, At, Cron, Launchd, ScheduleError
from upkeep.sources import Source, classify, find_all
from upkeep.tasks.brew import parse_cask_apps, parse_outdated

if TYPE_CHECKING:
    from collections.abc import Sequence

    from upkeep.host import Host


def test_time_parsing() -> None:
    assert At.parse("4:05") == At(4, 5)
    assert str(At(4, 5)) == "04:05"
    with pytest.raises(ScheduleError):
        _ = At.parse("24:00")
    first = At.default_for("cvps")
    assert first == At.default_for("cvps")
    assert first.hour == 4


def test_cron_install_replaces_only_our_line(monkeypatch: pytest.MonkeyPatch) -> None:
    table = ["MAILTO=x", f"1 2 * * * old {CRON_MARKER}", "16 4 * * * /x/cloak # cloak managed"]

    def fake(argv: Sequence[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["crontab", "-l"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(table) + "\n", "")
        assert stdin is not None
        table[:] = stdin.splitlines()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(schedule, "_run", fake)
    cron = Cron()
    cron.install(At(4, 30), ["/h/.local/bin/upkeep", "run", "--scheduled"], Path("/h/s.log"), {})
    assert table[:2] == ["MAILTO=x", "16 4 * * * /x/cloak # cloak managed"]
    assert table[2].startswith("30 4 * * * /h/.local/bin/upkeep run --scheduled >> /h/s.log")
    current = cron.current()
    assert current is not None and current.at == At(4, 30)
    assert cron.remove()
    assert table == ["MAILTO=x", "16 4 * * * /x/cloak # cloak managed"]
    assert not cron.remove()


def test_launchd_document() -> None:
    doc = Launchd.document(
        At(5, 7), ["/u", "run", "--scheduled"], Path("/l"), {"XDG_STATE_HOME": "/s"}
    )
    assert doc["StartCalendarInterval"] == {"Hour": 5, "Minute": 7}
    assert doc["EnvironmentVariables"] == {"XDG_STATE_HOME": "/s"}
    entry = Cron.entry(At(1, 2), ["/u"], Path("/l"), {"XDG_STATE_HOME": "/a b"})
    assert entry.startswith("2 1 * * * XDG_STATE_HOME='/a b' /u >> /l 2>&1")
    assert doc["ProgramArguments"] == ["/u", "run", "--scheduled"]


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_classify_sources(host: Host, tmp_path: Path) -> None:
    native = (host.home / ".local" / "bin", host.home / ".local" / "share" / "claude")
    bin_dir = host.home / ".local" / "bin"

    real = _exe(host.home / ".local/share/claude/versions/2.1")
    bin_dir.mkdir(parents=True)
    (bin_dir / "claude").symlink_to(real)
    assert classify(host, bin_dir / "claude", native).source is Source.NATIVE

    tool = _exe(host.home / ".local/share/uv/tools/prek/bin/prek")
    (bin_dir / "prek").symlink_to(tool)
    assert classify(host, bin_dir / "prek", native).source is Source.UV_TOOL

    acp = _exe(host.home / ".bun/install/global/node_modules/x/cli.js")
    _ = _exe(host.home / ".bun/bin/placeholder")
    (host.home / ".bun/bin/x").symlink_to(acp)
    assert classify(host, host.home / ".bun/bin/x", native).source is Source.BUN

    prefix = tmp_path / "brew"
    brewed = _exe(prefix / "Cellar/ruff/1/bin/ruff")
    (prefix / "bin").mkdir()
    (prefix / "bin/ruff").symlink_to(brewed)
    brew_host = replace(host, brew_prefix=prefix)
    assert classify(brew_host, prefix / "bin/ruff", native).source is Source.BREW

    pip_entry = bin_dir / "ruff"
    _ = pip_entry.write_text("#!/usr/bin/python3\nfrom ruff import main\n")
    assert classify(host, pip_entry, native).source is Source.PYTHON

    odd = _exe(tmp_path / "elsewhere/tool")
    assert classify(host, odd, native).source is Source.UNKNOWN


def test_find_all_dedupes_by_real_path(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")  # only the fake home's dirs plus system dirs
    real = _exe(host.home / ".local/bin/agy")
    (host.home / ".cargo/bin").mkdir(parents=True)
    (host.home / ".cargo/bin/agy").symlink_to(real)
    found = find_all(host, "agy", (host.home / ".local/bin",))
    assert [f.real for f in found] == [real.resolve()]


def test_brew_json_parsing() -> None:
    outdated = parse_outdated(
        json.dumps(
            {
                "formulae": [
                    {
                        "name": "mole",
                        "installed_versions": ["1.62.0"],
                        "current_version": "1.63.0",
                        "pinned": False,
                    },
                    {
                        "name": "pinned",
                        "installed_versions": ["1"],
                        "current_version": "2",
                        "pinned": True,
                    },
                ],
                "casks": [
                    {"name": "ghostty", "installed_versions": "1.0", "current_version": "1.1"}
                ],
            }
        )
    )
    assert [(o.name, o.cask, o.pinned) for o in outdated] == [
        ("mole", False, False),
        ("pinned", False, True),
        ("ghostty", True, False),
    ]
    assert outdated[0].label() == "mole 1.62.0 → 1.63.0"
    apps = parse_cask_apps(
        json.dumps(
            {"casks": [{"token": "ghostty", "artifacts": [{"app": ["Ghostty.app"]}, {"zap": []}]}]}
        )
    )
    assert apps == {"ghostty": ("Ghostty.app",)}
