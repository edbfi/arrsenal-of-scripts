import asyncio
import os
import time
from pathlib import Path

import pytest

from upkeep import execute
from upkeep.execute import LineSplitter, strip_ansi
from upkeep.procs import Proc, ProcessTable, ProcMatcher, descendants, launcher_ancestors


def proc(pid: int, ppid: int, exe: str, *argv: str) -> Proc:
    return Proc(pid=pid, ppid=ppid, exe=exe, argv=argv or (exe,))


def test_matcher_by_name_and_argv0() -> None:
    matcher = ProcMatcher(names=frozenset({"claude"}))
    # claude's exe is named after its version; argv[0] still says claude
    assert matcher.matches(proc(1, 0, "/h/.local/share/claude/versions/2.1.3", "claude"))
    assert not matcher.matches(proc(2, 0, "/usr/bin/vim", "vim"))


def test_matcher_by_dir_in_exe_or_argv() -> None:
    root = Path("/h/.bun/install/global/node_modules")
    matcher = ProcMatcher(dirs=(root,))
    script = f"{root}/@agentclientprotocol/codex-acp/dist/index.js"
    assert matcher.matches(proc(1, 0, "/h/.bun/bin/bun", "bun", script))
    assert not matcher.matches(proc(2, 0, "/h/.bun/bin/bun", "bun", "/h/.bun/install/globalx/a"))
    assert not ProcMatcher().matches(proc(3, 0, "/x", "x"))


def test_own_tree_and_launchers_excluded() -> None:
    procs = [
        proc(1, 0, "/sbin/launchd"),
        proc(10, 1, "/h/.local/share/claude/versions/2", "claude"),
        proc(11, 10, "/bin/sh", "/bin/sh", "/h/.local/bin/upkeep", "run"),
        proc(12, 11, "/h/.local/bin/uv", "uv", "run", "--project", "/r/upkeep", "--", "upkeep"),
        proc(13, 12, "/venv/python", "python", "/venv/bin/upkeep"),  # us
        proc(14, 13, "/h/.local/bin/uv", "uv", "self", "update"),  # our child
    ]
    assert descendants(procs, 13) == {14}
    assert launcher_ancestors(procs, 13) == {11, 12}  # claude (10) is a real session: kept


def test_snapshot_sees_a_live_process() -> None:
    table = ProcessTable.snapshot(exclude_tree_of=2**30)  # no such pid
    assert any(p.pid == os.getpid() for p in table.procs)
    assert all(p.pid != os.getpid() for p in ProcessTable.snapshot().procs)


def test_line_splitter_keeps_final_carriage_return_state() -> None:
    lines: list[str] = []
    splitter = LineSplitter(lines.append)
    splitter.feed("downloading 10%\rdownloading 5")
    splitter.feed("0%\rdone\n\x1b[32mgreen\x1b[0m\nlast")
    splitter.close()
    assert lines == ["done", "green", "last"]
    assert strip_ansi("\x1b]8;;http://x\x07link") == "link"


def test_run_streams_and_separates_stderr() -> None:
    seen: list[str] = []
    result = asyncio.run(
        execute.run(
            ["sh", "-c", "echo out; echo err >&2; exit 3"],
            env=dict(os.environ),
            sink=seen.append,
            separate_stderr=True,
        )
    )
    assert (result.returncode, result.output, result.stderr) == (3, ("out",), ("err",))
    assert seen == ["out"]


def test_character_split_across_reads_is_decoded() -> None:
    # "█" is e2 96 88; the sleep makes the two halves arrive as separate reads.
    script = r"printf '\342\226'; sleep 0.3; printf '\210\n'"
    result = asyncio.run(execute.run(["sh", "-c", script], env=dict(os.environ), sink=print))
    assert result.output == ("█",)


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "survived"

    async def go() -> None:
        async with asyncio.timeout(0.3):
            _ = await execute.run(
                ["sh", "-c", f"(sleep 1; touch {marker}) & sleep 5"],
                env=dict(os.environ),
                sink=lambda _line: None,
            )

    with pytest.raises(TimeoutError):
        asyncio.run(go())
    time.sleep(1.3)
    assert not marker.exists()  # the background grandchild died with the group


def test_leftover_daemon_does_not_hang_a_finished_command() -> None:
    started = time.monotonic()
    result = asyncio.run(
        execute.run(
            ["sh", "-c", "sleep 30 & echo started"],  # the daemon keeps stdout open
            env=dict(os.environ),
            sink=lambda _line: None,
        )
    )
    assert result.output == ("started",)
    assert time.monotonic() - started < execute.DRAIN_SECONDS + 2


def test_timeout_after_leader_exit_kills_the_group(tmp_path: Path) -> None:
    marker = tmp_path / "survived"

    async def go() -> None:
        async with asyncio.timeout(0.3):  # fires while output is still draining
            _ = await execute.run(
                ["sh", "-c", f"(sleep 1; touch {marker}) & exit 0"],
                env=dict(os.environ),
                sink=lambda _line: None,
            )

    with pytest.raises(TimeoutError):
        asyncio.run(go())
    time.sleep(1.3)
    assert not marker.exists()


def test_symlinked_argv_paths_are_resolved(tmp_path: Path) -> None:
    target = tmp_path / "global" / "node_modules" / "acp" / "cli.js"
    target.parent.mkdir(parents=True)
    _ = target.write_text("")
    link = tmp_path / "bin" / "acp"
    link.parent.mkdir()
    link.symlink_to(target)
    proc = Proc.create(1, 0, "/h/.bun/bin/bun", ("bun", str(link)))
    matcher = ProcMatcher(dirs=(tmp_path.resolve() / "global" / "node_modules",))
    assert matcher.matches(proc)
