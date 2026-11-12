"""Run child processes with streamed, line-buffered output.

Each child gets its own session (so it has no controlling terminal and cannot
block on a /dev/tty prompt) and its own process group, so a timeout or Ctrl-C
takes down the whole pipeline it started, not just the first process.
"""

import asyncio
import codecs
import os
import re
import signal
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

type LineSink = Callable[[str], None]

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
KILL_GRACE_SECONDS = 5.0
DRAIN_SECONDS = 3.0


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    output: tuple[str, ...]
    stderr: tuple[str, ...] = ()  # only when run with separate_stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class CommandError(RuntimeError):
    result: CommandResult

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(f"{Path(result.argv[0]).name} exited with status {result.returncode}")


class LineSplitter:
    """Turn a byte stream into lines; a carriage return rewrites the line.

    Progress bars redraw with ``\\r``. Only the final state of each line is
    emitted, so a 300-frame download bar becomes one log line.
    """

    def __init__(self, sink: LineSink) -> None:
        self._sink: LineSink = sink
        self._pending: str = ""

    def feed(self, chunk: str) -> None:
        *complete, self._pending = (self._pending + chunk).split("\n")
        for line in complete:
            self._emit(line)

    def close(self) -> None:
        if self._pending:
            self._emit(self._pending)
            self._pending = ""

    def _emit(self, line: str) -> None:
        final = line.rstrip("\r").rsplit("\r", 1)[-1]
        self._sink(strip_ansi(final).rstrip())


async def run(
    argv: Sequence[str | Path],
    *,
    env: Mapping[str, str],
    sink: LineSink,
    cwd: Path | None = None,
    stdin: bytes | None = None,
    separate_stderr: bool = False,
) -> CommandResult:
    """Run `argv`; every output line goes to `sink`.

    stdout and stderr are merged, unless `separate_stderr`: then only stdout
    reaches `sink`/`output` (for parsing machine output) and stderr is kept
    in `stderr`.

    Wrap the call in ``asyncio.timeout`` to bound it; cancellation kills the
    child's process group.
    """
    args = tuple(str(a) for a in argv)
    lines: list[str] = []
    errors: list[str] = []

    def collect(line: str) -> None:
        lines.append(line)
        sink(line)

    # Own the output pipes separately: on older Python releases, proc.wait()
    # waits for PIPE EOF as well as process exit, so an inherited daemon pipe
    # would prevent us from ever starting the bounded drain below.
    async with AsyncExitStack() as pipes:
        stdout, stdout_writer = await pipes.enter_async_context(_output_pipe())
        stderr: asyncio.StreamReader | None = None
        stderr_writer: BinaryIO | int = asyncio.subprocess.STDOUT
        if separate_stderr:
            stderr, stderr_writer = await pipes.enter_async_context(_output_pipe())
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=stdout_writer,
            stderr=stderr_writer,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
        stdout_writer.close()
        if not isinstance(stderr_writer, int):
            stderr_writer.close()
        try:
            if stdin is not None and proc.stdin is not None:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            readers = [asyncio.create_task(_pump(stdout, LineSplitter(collect)))]
            if stderr is not None:
                readers.append(asyncio.create_task(_pump(stderr, LineSplitter(errors.append))))
            try:
                returncode = await proc.wait()
                # A daemon the command left behind may keep the pipe open forever;
                # collect what is buffered, then stop listening (it keeps running).
                _ = await asyncio.wait(readers, timeout=DRAIN_SECONDS)
            finally:
                for reader in readers:
                    _ = reader.cancel()
                _ = await asyncio.gather(*readers, return_exceptions=True)
        except BaseException:
            await _terminate(proc)
            raise
    return CommandResult(args, returncode, tuple(lines), tuple(errors))


@asynccontextmanager
async def _output_pipe() -> AsyncGenerator[tuple[asyncio.StreamReader, BinaryIO]]:
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb", buffering=0) as read_pipe,
        os.fdopen(write_fd, "wb", buffering=0) as write_pipe,
    ):
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), read_pipe
        )
        try:
            yield reader, write_pipe
        finally:
            transport.close()


async def _pump(stream: asyncio.StreamReader, splitter: LineSplitter) -> None:
    # Incremental, so a multi-byte character split across two reads survives.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while chunk := await stream.read(65536):
        splitter.feed(decoder.decode(chunk))
    splitter.feed(decoder.decode(b"", final=True))
    splitter.close()


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole group, even when its leader has already exited."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return  # group is gone
        if proc.returncode is not None:
            await asyncio.sleep(0.2)
            continue
        try:
            async with asyncio.timeout(KILL_GRACE_SECONDS):
                _ = await proc.wait()
            return
        except TimeoutError:
            continue
