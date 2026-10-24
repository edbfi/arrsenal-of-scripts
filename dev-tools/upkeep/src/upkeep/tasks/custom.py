"""Host-specific steps declared in the config's ``[[custom]]`` tables."""

from pathlib import Path
from typing import TYPE_CHECKING, override

from upkeep.model import Probe, TaskInfo
from upkeep.procs import ProcMatcher
from upkeep.tasks.base import Task, TaskContext

if TYPE_CHECKING:
    from upkeep.config import CustomTask
    from upkeep.host import Host


def _expand(host: Host, text: str) -> str:
    return str(host.home) + text[1:] if text == "~" or text.startswith("~/") else text


class CustomCommandTask(Task):
    spec: CustomTask

    def __init__(self, spec: CustomTask) -> None:
        super().__init__(
            TaskInfo(
                id=spec.id,
                title=spec.title,
                summary=spec.summary,
                after=spec.after,
                resources=frozenset(spec.resources),
                when_busy=spec.when_busy,
                timeout=spec.timeout,
                enabled_by_default=spec.enabled,
                busy_note="custom task",
            )
        )
        self.spec = spec

    @override
    def probe(self, host: Host) -> Probe:
        if self.spec.requires:
            required = Path(_expand(host, self.spec.requires))
            if not required.exists():
                return Probe.missing(f"{required} does not exist")
            return Probe.installed(required)
        return Probe.installed(None)

    @override
    def busy_matcher(self, host: Host) -> ProcMatcher:
        return ProcMatcher(
            names=frozenset(self.spec.busy_names),
            dirs=tuple(Path(_expand(host, d)) for d in self.spec.busy_dirs),
        )

    @override
    async def update(self, ctx: TaskContext) -> str | None:
        if self.spec.shell:
            _ = await ctx.run("/bin/sh", "-c", self.spec.shell)
        else:
            _ = await ctx.run(*(_expand(ctx.host, a) for a in self.spec.argv))
        return None
