"""The task catalogue. To add a tool: write a `Task`, then list it here."""

from typing import TYPE_CHECKING

from upkeep.config import ConfigError
from upkeep.tasks.base import BinaryTask, Task, TaskContext
from upkeep.tasks.brew import Brew
from upkeep.tasks.bun import BunGlobals, Skills
from upkeep.tasks.custom import CustomCommandTask
from upkeep.tasks.extras import AgentBrowser, T3AntigravityAcp, UvTools
from upkeep.tasks.standalone import Agy, Bun, Claude, Codex, Prek, Ruff, Uv

if TYPE_CHECKING:
    from collections.abc import Sequence

    from upkeep.config import BusyPolicy, Config

__all__ = [
    "BinaryTask",
    "Task",
    "TaskContext",
    "UnknownTaskError",
    "all_tasks",
    "builtin_tasks",
    "check_config",
    "is_enabled",
    "select",
    "when_busy",
]


class UnknownTaskError(ValueError):
    pass


def builtin_tasks() -> list[Task]:
    # Order is display order and the tie-break for scheduling.
    return [
        Brew(),
        Uv(),
        Ruff(),
        Prek(),
        Bun(),
        Claude(),
        Codex(),
        Agy(),
        BunGlobals(),
        Skills(),
        AgentBrowser(),
        T3AntigravityAcp(),
        UvTools(),
    ]


def all_tasks(config: Config) -> list[Task]:
    tasks = builtin_tasks()
    taken = {t.info.id for t in tasks}
    for spec in config.custom:
        if spec.id in taken:
            raise UnknownTaskError(f"custom task id {spec.id!r} clashes with a built-in task")
        tasks.append(CustomCommandTask(spec))
    return tasks


def check_config(config: Config) -> None:
    """Reject [tasks.<id>] tables for unknown tasks and keys no task reads.

    The config parser only knows the keys every task shares; a misspelled
    task-specific key would otherwise be ignored without a word.
    """
    tasks = {t.info.id: t for t in all_tasks(config)}
    for task_id, settings in config.tasks.items():
        task = tasks.get(task_id)
        if task is None:
            known = ", ".join(sorted(tasks))
            raise ConfigError(f"tasks.{task_id}: unknown task (known: {known})")
        unknown = sorted(set(settings.extra) - task.info.options)
        if unknown:
            raise ConfigError(f"tasks.{task_id}: unknown key(s) {', '.join(unknown)}")


def is_enabled(task: Task, config: Config) -> bool:
    enabled = config.task(task.info.id).enabled
    return task.info.enabled_by_default if enabled is None else enabled


def when_busy(task: Task, config: Config) -> BusyPolicy:
    return config.task(task.info.id).when_busy or task.info.when_busy


def select(
    tasks: Sequence[Task],
    config: Config,
    *,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
) -> list[Task]:
    """Tasks for a run: the named ones (even if disabled), else all enabled."""
    known = {t.info.id for t in tasks}
    unknown = sorted((set(only) | set(skip)) - known)
    if unknown:
        raise UnknownTaskError(
            f"unknown task(s): {', '.join(unknown)} (known: {', '.join(sorted(known))})"
        )
    chosen = (
        [t for t in tasks if t.info.id in only]
        if only
        else [t for t in tasks if is_enabled(t, config)]
    )
    return [t for t in chosen if t.info.id not in skip]
