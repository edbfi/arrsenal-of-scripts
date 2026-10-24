from typing import TYPE_CHECKING

import pytest

from upkeep.host import OS, Host

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def host(tmp_path: Path) -> Host:
    home = tmp_path / "home"
    home.mkdir()
    return Host(
        os=OS.LINUX,
        machine="x86_64",
        hostname="testhost",
        user="tester",
        home=home,
        brew_prefix=None,
        config_dir=home / ".config" / "upkeep",
        state_dir=home / ".local" / "state" / "upkeep",
    )
