"""Typing gate: forbid every basedpyright escape hatch except the one sanctioned kind.

The typing contract: basedpyright runs in ``recommended`` mode, warnings are
failures, and the only sanctioned suppression is a single-line, rule-scoped
``pyright: ignore[ruleName]`` followed on the same line by a short reason
comment, placed at a genuinely untyped third-party boundary.

This script fails (exit 1) when someone — human or AI agent — tries to
reintroduce a global or blanket escape hatch:

* keys in ``[tool.basedpyright]`` beyond the allowlist (``failOnWarnings``,
  ``report*``, ``exclude``, ``executionEnvironments``, ...)
* a ``typeCheckingMode`` other than ``recommended``
* dropping ``src`` / ``tests`` / ``tools`` from ``include``
* a ``[tool.pyright]`` table (basedpyright falls back to it)
* a ``pyrightconfig.json`` (silently takes precedence over pyproject.toml)
* a basedpyright baseline directory
* file-level pyright pragmas inside checked sources (``# pyright: basic``)
* blanket ``type: ignore`` comments in checked sources
* ``pyright: ignore`` without a rule, or without a same-line reason comment

It never edits anything. If this gate blocks you: fix the code, do not widen
the gate.

Usage::

    python typing_gate.py                 # find pyproject.toml upward from cwd
    python typing_gate.py --root PATH     # start the search from PATH
    python typing_gate.py --diff-ignores  # also list ignore lines added/removed vs git HEAD

Runs on Python 3.11+ and must itself pass the gate, since it lives in ``tools/``
when copied into a repo. Comment scanning is line-based; an ignore-shaped
string inside a string literal can produce a false positive. Rephrase the
string rather than widening this script.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

ALLOWED_KEYS = frozenset({"pythonVersion", "typeCheckingMode", "include"})
REQUIRED_MODE = "recommended"
REQUIRED_INCLUDE = frozenset({"src", "tests", "tools"})
FORBIDDEN_PATHS = ("pyrightconfig.json", ".basedpyright")

_PREFIX = r"#\s*(?:based)?pyright:"

# A standalone comment line that configures pyright for the whole file
# (``# pyright: basic``, ``# pyright: reportFoo=false``). The lookahead exempts
# a leading rule-scoped ignore, which is a suppression, not a configuration.
FILE_PRAGMA = re.compile(rf"^\s*{_PREFIX}(?!\s*ignore\[)")

# Any pyright ignore comment, anywhere on the line. Group 1 is the rule list
# (unmatched when the brackets are missing); whatever follows is the reason.
IGNORE_COMMENT = re.compile(rf"{_PREFIX}\s*ignore(?:\[([^\]]*)\])?")

# A same-line reason: a second comment with at least one non-space character.
REASON = re.compile(r"^\s*#\s*\S")

# Blanket mypy/pyright suppression. Not rule-scoped to pyright's rules and
# honoured by pyright by default, so it silences everything on the line.
TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore\b")

SELF = Path(__file__).resolve()


@dataclass
class Table:
    """The parsed ``[tool.basedpyright]`` table plus what we need around it."""

    present: bool
    entries: dict[str, object] = field(default_factory=dict)
    has_pyright_table: bool = False

    def included(self) -> set[str]:
        include = self.entries.get("include")
        if not isinstance(include, list):
            return set()
        items = cast("list[object]", include)
        return {item for item in items if isinstance(item, str)}


def find_root(start: Path) -> Path | None:
    """Walk upward from ``start`` to the first directory holding pyproject.toml."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def load_table(pyproject: Path) -> Table:
    with pyproject.open("rb") as fh:
        data: dict[str, object] = tomllib.load(fh)
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return Table(present=False)
    tool_table = cast("dict[str, object]", tool)
    based = tool_table.get("basedpyright")
    has_pyright = isinstance(tool_table.get("pyright"), dict)
    if not isinstance(based, dict):
        return Table(present=False, has_pyright_table=has_pyright)
    return Table(
        present=True,
        entries=cast("dict[str, object]", based),
        has_pyright_table=has_pyright,
    )


def check_table(table: Table) -> list[str]:
    problems: list[str] = []
    if table.has_pyright_table:
        problems.append(
            "[tool.pyright] table exists — basedpyright falls back to it, so it is "
            + "a second config surface; delete it"
        )
    if not table.present:
        problems.append("[tool.basedpyright] table is missing from pyproject.toml")
        return problems
    for key in sorted(set(table.entries) - ALLOWED_KEYS):
        problems.append(
            f"[tool.basedpyright] sets {key!r} — global diagnostic overrides are "
            + "forbidden; fix the code, or suppress one line with a justified "
            + "rule-scoped pyright ignore comment"
        )
    if table.entries.get("typeCheckingMode") != REQUIRED_MODE:
        problems.append(f"[tool.basedpyright] typeCheckingMode must be {REQUIRED_MODE!r}")
    for missing in sorted(REQUIRED_INCLUDE - table.included()):
        problems.append(f"[tool.basedpyright] include must keep {missing!r} type-checked")
    return problems


def check_forbidden_paths(root: Path) -> list[str]:
    return [
        f"{root / name} exists — it overrides or baselines away the pyproject.toml "
        + "typing gate; delete it"
        for name in FORBIDDEN_PATHS
        if (root / name).exists()
    ]


def scan_line(line: str) -> list[str]:
    """Return the problems with one source line (empty when it is clean)."""
    problems: list[str] = []
    if FILE_PRAGMA.match(line):
        problems.append(
            "file-level pyright pragma is forbidden; only single-line rule-scoped "
            + "ignores are allowed"
        )
    if TYPE_IGNORE.search(line):
        problems.append(
            "blanket `type: ignore` is forbidden; use a rule-scoped pyright ignore "
            + "with a reason, or fix the code"
        )
    found = IGNORE_COMMENT.search(line)
    if found is not None:
        # Slice by span rather than Match.group(): group() is typed str | Any.
        rules = line[found.start(1) : found.end(1)] if found.start(1) != -1 else ""
        rest = line[found.end() :]
        if not rules.strip():
            problems.append("pyright ignore without a rule — scope it: ignore[ruleName]")
        elif REASON.match(rest) is None:
            problems.append(
                "pyright ignore without a same-line reason — add `# <why this "
                + "boundary is untyped>` after it"
            )
    return problems


def source_files(root: Path, roots: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for sub in sorted(set(roots)):
        base = root / sub
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return [path for path in files if path.resolve() != SELF]


def check_sources(root: Path, files: Iterable[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            for problem in scan_line(line):
                problems.append(f"{path.relative_to(root)}:{lineno}: {problem}")
    return problems


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def diff_ignores(root: Path, roots: Iterable[str]) -> tuple[list[str], list[str]]:
    """Ignore comments added and removed versus git HEAD, as ``file:line  text``."""
    subdirs = sorted(set(roots))
    diff = _git(root, "diff", "-U0", "HEAD", "--", *subdirs)
    if diff is None:
        return [], []
    added: list[str] = []
    removed: list[str] = []
    current = ""
    new_lineno = 0
    for raw in diff.splitlines():
        # A deleted file's new side is /dev/null; keep the old path for it.
        if raw.startswith("--- "):
            current = raw[4:].removeprefix("a/")
        elif raw.startswith("+++ "):
            if raw != "+++ /dev/null":
                current = raw[4:].removeprefix("b/")
        elif raw.startswith("@@"):
            hunk = re.search(r"\+(\d+)", raw)
            new_lineno = int(raw[hunk.start(1) : hunk.end(1)]) if hunk else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            if IGNORE_COMMENT.search(raw):
                added.append(f"{current}:{new_lineno}  {raw[1:].strip()}")
            new_lineno += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            if IGNORE_COMMENT.search(raw):
                removed.append(f"{current}  {raw[1:].strip()}")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", *subdirs)
    for rel in (untracked or "").splitlines():
        path = root / rel
        if path.suffix != ".py" or not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if IGNORE_COMMENT.search(line):
                added.append(f"{rel}:{lineno}  {line.strip()}  (new file)")
    return added, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    _ = parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="directory to start searching for pyproject.toml (default: cwd)",
    )
    _ = parser.add_argument(
        "--diff-ignores",
        action="store_true",
        help="also list pyright ignore comments added/removed versus git HEAD",
    )
    args = parser.parse_args(argv)
    start = cast("Path", args.root).resolve()
    show_diff = cast("bool", args.diff_ignores)

    root = find_root(start)
    if root is None:
        print(f"typing gate: no pyproject.toml found from {start} upward", file=sys.stderr)
        return 2

    table = load_table(root / "pyproject.toml")
    # Scan every configured include dir, not just the required trio, so a
    # pragma cannot hide in a newly added include entry.
    roots = REQUIRED_INCLUDE | table.included()
    problems = (
        check_table(table)
        + check_forbidden_paths(root)
        + check_sources(root, source_files(root, roots))
    )

    if problems:
        print(f"typing gate FAILED ({root}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
    else:
        print(f"typing gate OK ({root}): no global ignores, warnings stay fatal")

    if show_diff:
        added, removed = diff_ignores(root, roots)
        print(f"\nignores added vs HEAD ({len(added)}):")
        for item in added:
            print(f"  {item}")
        print(f"ignores removed vs HEAD ({len(removed)}):")
        for item in removed:
            print(f"  {item}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
