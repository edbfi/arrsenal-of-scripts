"""Where upkeep's own checkout is. upkeep runs from a git checkout of
arrsenal-of-scripts; self-update depends on that."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parents[1]  # dev-tools/upkeep (holds pyproject.toml)
DEV_TOOLS_DIR = PROJECT_DIR.parent

# Commits touching these count as upkeep updates (paths relative to DEV_TOOLS_DIR).
OWNED_PATHS = (PROJECT_DIR.name,)


def in_checkout() -> bool:
    return (PROJECT_DIR / "pyproject.toml").is_file()
