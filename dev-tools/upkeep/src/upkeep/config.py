"""User configuration: ``~/.config/upkeep/config.toml``.

Everything is optional; a missing file means defaults. The TOML is validated
once here and turned into frozen dataclasses, so nothing downstream handles
untyped values. Unknown keys are errors: a typo should not silently disable
a setting.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class ConfigError(ValueError):
    pass


class BusyPolicy(StrEnum):
    SKIP = "skip"  # leave the tool alone while it runs
    ALLOW = "allow"  # update anyway: the installer swaps files atomically


class OutputMode(StrEnum):
    FULL = "full"  # every task's complete output
    FAILURES = "failures"  # output of failed tasks only
    QUIET = "quiet"  # status lines only


class SelfUpdateMode(StrEnum):
    AUTO = "auto"
    NOTIFY = "notify"
    OFF = "off"


class ScheduleBackendName(StrEnum):
    AUTO = "auto"
    CRON = "cron"
    LAUNCHD = "launchd"


@dataclass(frozen=True, slots=True)
class TaskSettings:
    enabled: bool | None = None
    install: bool = False  # install the tool when missing (chosen in setup)
    when_busy: BusyPolicy | None = None
    timeout: float | None = None
    extra: Mapping[str, object] = field(default_factory=dict[str, object])

    def strings(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        if key not in self.extra:
            return default
        return _str_tuple(self.extra[key], key)


@dataclass(frozen=True, slots=True)
class CustomTask:
    id: str
    title: str
    summary: str
    argv: tuple[str, ...] = ()
    shell: str = ""
    after: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    busy_names: tuple[str, ...] = ()
    busy_dirs: tuple[str, ...] = ()
    when_busy: BusyPolicy = BusyPolicy.SKIP
    timeout: float = 900.0
    enabled: bool = True
    requires: str = ""  # only run when this path exists (~ expanded)


@dataclass(frozen=True, slots=True)
class Config:
    jobs: int = 4
    retries: int = 3
    output: OutputMode = OutputMode.FULL
    self_update: SelfUpdateMode = SelfUpdateMode.AUTO
    remote: str = "origin"
    branch: str = "main"
    schedule_backend: ScheduleBackendName = ScheduleBackendName.AUTO
    tasks: Mapping[str, TaskSettings] = field(default_factory=dict[str, TaskSettings])
    custom: tuple[CustomTask, ...] = ()

    def task(self, task_id: str) -> TaskSettings:
        return self.tasks.get(task_id, TaskSettings())


# ------------------------------------------------------------------ narrowing


def _table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    # tomllib tables always have string keys.
    return cast("dict[str, object]", value)


def _str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string")
    return value


def _int(value: object, where: str, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{where}: expected an integer >= {minimum}")
    return value


def _float(value: object, where: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where}: expected a positive number")
    return float(value)


def _bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false")
    return value


def _str_tuple(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list of strings")
    items = cast("list[object]", value)
    return tuple(_str(item, f"{where}[{i}]") for i, item in enumerate(items))


def _enum[E: StrEnum](kind: type[E], value: object, where: str) -> E:
    text = _str(value, where)
    try:
        return kind(text)
    except ValueError:
        choices = ", ".join(m.value for m in kind)
        raise ConfigError(f"{where}: expected one of {choices}") from None


def _check_keys(table: Mapping[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


# -------------------------------------------------------------------- parsing


TASK_KEYS = {"enabled", "install", "when_busy", "timeout"}


def _parse_task(raw: object, where: str) -> TaskSettings:
    table = _table(raw, where)
    extra = {k: v for k, v in table.items() if k not in TASK_KEYS}
    return TaskSettings(
        enabled=_bool(table["enabled"], f"{where}.enabled") if "enabled" in table else None,
        install=_bool(table.get("install", False), f"{where}.install"),
        when_busy=(
            _enum(BusyPolicy, table["when_busy"], f"{where}.when_busy")
            if "when_busy" in table
            else None
        ),
        timeout=_float(table["timeout"], f"{where}.timeout") if "timeout" in table else None,
        extra=extra,
    )


CUSTOM_KEYS = {
    "id",
    "title",
    "summary",
    "run",
    "shell",
    "after",
    "resources",
    "busy_names",
    "busy_dirs",
    "when_busy",
    "timeout",
    "enabled",
    "requires",
}


def _parse_custom(raw: object, where: str) -> CustomTask:
    t = _table(raw, where)
    _check_keys(t, CUSTOM_KEYS, where)
    if "id" not in t:
        raise ConfigError(f"{where}: 'id' is required")
    task_id = _str(t["id"], f"{where}.id")
    if not BARE_KEY.match(task_id):
        raise ConfigError(f"{where}.id: use letters, digits, '-' and '_' only")
    argv = _str_tuple(t["run"], f"{where}.run") if "run" in t else ()
    shell = _str(t["shell"], f"{where}.shell") if "shell" in t else ""
    if bool(argv) == bool(shell):
        raise ConfigError(f"{where}: set exactly one of 'run' (argv list) or 'shell'")

    def strings(key: str) -> tuple[str, ...]:
        return _str_tuple(t[key], f"{where}.{key}") if key in t else ()

    return CustomTask(
        id=task_id,
        title=_str(t.get("title", task_id), f"{where}.title"),
        summary=_str(t.get("summary", "custom task"), f"{where}.summary"),
        argv=argv,
        shell=shell,
        after=strings("after"),
        resources=strings("resources"),
        busy_names=strings("busy_names"),
        busy_dirs=strings("busy_dirs"),
        when_busy=_enum(BusyPolicy, t.get("when_busy", "skip"), f"{where}.when_busy"),
        timeout=_float(t.get("timeout", 900.0), f"{where}.timeout"),
        enabled=_bool(t.get("enabled", True), f"{where}.enabled"),
        requires=_str(t.get("requires", ""), f"{where}.requires"),
    )


def parse(document: Mapping[str, object]) -> Config:
    _check_keys(document, {"run", "self_update", "schedule", "tasks", "custom"}, "config")
    run = _table(document.get("run", {}), "run")
    _check_keys(run, {"jobs", "retries", "output"}, "run")
    selfupd = _table(document.get("self_update", {}), "self_update")
    _check_keys(selfupd, {"mode", "remote", "branch"}, "self_update")
    schedule = _table(document.get("schedule", {}), "schedule")
    _check_keys(schedule, {"backend"}, "schedule")
    tasks = _table(document.get("tasks", {}), "tasks")
    custom_raw = document.get("custom", [])
    if not isinstance(custom_raw, list):
        raise ConfigError("custom: expected an array of tables ([[custom]])")
    custom = tuple(
        _parse_custom(item, f"custom[{i}]")
        for i, item in enumerate(cast("list[object]", custom_raw))
    )
    ids = [c.id for c in custom]
    if len(set(ids)) != len(ids):
        raise ConfigError("custom: task ids must be unique")

    defaults = Config()
    return Config(
        jobs=_int(run.get("jobs", defaults.jobs), "run.jobs", 1),
        retries=_int(run.get("retries", defaults.retries), "run.retries", 1),
        output=_enum(OutputMode, run.get("output", defaults.output), "run.output"),
        self_update=_enum(
            SelfUpdateMode, selfupd.get("mode", defaults.self_update), "self_update.mode"
        ),
        remote=_str(selfupd.get("remote", defaults.remote), "self_update.remote"),
        branch=_str(selfupd.get("branch", defaults.branch), "self_update.branch"),
        schedule_backend=_enum(
            ScheduleBackendName,
            schedule.get("backend", defaults.schedule_backend),
            "schedule.backend",
        ),
        tasks={k: _parse_task(v, f"tasks.{k}") for k, v in tasks.items()},
        custom=custom,
    )


def load(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as fh:
            document = tomllib.load(fh)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    try:
        return parse(document)
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from None


TEMPLATE = """\
# upkeep configuration. Every setting is optional; shown values are defaults.

[run]
# jobs = 4            # tasks run in parallel (dependencies are respected)
# retries = 3         # attempts per task; rate-limit errors back off exponentially
# output = "full"     # full | failures | quiet

[self_update]
# mode = "auto"       # auto: fast-forward the checkout before running
#                     # notify: only report new commits  |  off
# remote = "origin"
# branch = "main"

[schedule]
# backend = "auto"    # auto (launchd on macOS, cron on Linux) | cron | launchd

# Per-task settings. Task ids: `upkeep list`.
# [tasks.claude]
# enabled = true
# when_busy = "skip"  # skip | allow  (see `upkeep list` for each default)
# timeout = 900
#
# [tasks.bun-globals]
# ensure = ["@agentclientprotocol/claude-agent-acp", "@agentclientprotocol/codex-acp"]
#
# [tasks.uv-tools]
# enabled = true      # off by default

# Host-specific extra steps. Use `run` (argv list) or `shell` (one string).
# [[custom]]
# id = "cloakbrowser"
# title = "cloakbrowser"
# run = ["~/scripts/cloakbrowser-update"]
# after = ["agent-browser"]
# requires = "~/scripts/cloakbrowser-update"
# busy_dirs = ["~/.cloakbrowser"]
"""


def write_template(path: Path) -> bool:
    """Create a commented config file; never overwrite an existing one."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(TEMPLATE)
    return True


# ------------------------------------------------------------------ writing


BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object, where: str) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case int() | float():
            return repr(value)
        case str():
            # JSON string escapes are a subset of TOML basic-string escapes.
            return json.dumps(value, ensure_ascii=False)
        case list():
            items = cast("list[object]", value)
            return "[" + ", ".join(_toml_value(i, where) for i in items) + "]"
        case _:
            raise ConfigError(f"{where}: cannot write a {type(value).__name__} to TOML")


def render(config: Config) -> str:
    """The whole config as commented TOML; `parse(tomllib.loads(render(c))) == c`."""
    out = [
        "# upkeep configuration, written by `upkeep setup`. Hand edits are kept as long",
        "# as they are valid; comments are not. Reference: `upkeep config template`.",
        "",
        "[run]",
        f"jobs = {config.jobs}            # tasks run in parallel",
        f"retries = {config.retries}         # attempts per task",
        f"output = {_toml_value(config.output.value, 'run.output')}    # full | failures | quiet",
        "",
        "[self_update]",
        f"mode = {_toml_value(config.self_update.value, 'mode')}       # auto | notify | off",
        f"remote = {_toml_value(config.remote, 'remote')}",
        f"branch = {_toml_value(config.branch, 'branch')}",
        "",
        "[schedule]",
        f"backend = {_toml_value(config.schedule_backend.value, 'backend')}  # auto | cron | launchd",
    ]
    for task_id in sorted(config.tasks):
        s = config.tasks[task_id]
        out += ["", f"[tasks.{_toml_key(task_id)}]"]
        if s.enabled is not None:
            out.append(f"enabled = {_toml_value(s.enabled, task_id)}")
        if s.install:
            out.append("install = true  # install when missing")
        if s.when_busy is not None:
            out.append(f"when_busy = {_toml_value(s.when_busy.value, task_id)}")
        if s.timeout is not None:
            out.append(f"timeout = {_toml_value(s.timeout, task_id)}")
        out += [
            f"{_toml_key(k)} = {_toml_value(v, f'tasks.{task_id}.{k}')}" for k, v in s.extra.items()
        ]
    for c in config.custom:
        out += ["", "[[custom]]", f"id = {_toml_value(c.id, 'custom')}"]
        out.append(f"title = {_toml_value(c.title, c.id)}")
        out.append(f"summary = {_toml_value(c.summary, c.id)}")
        if c.argv:
            out.append(f"run = {_toml_value(list(c.argv), c.id)}")
        else:
            out.append(f"shell = {_toml_value(c.shell, c.id)}")
        lists = {
            "after": c.after,
            "resources": c.resources,
            "busy_names": c.busy_names,
            "busy_dirs": c.busy_dirs,
        }
        out += [f"{k} = {_toml_value(list(v), c.id)}" for k, v in lists.items() if v]
        out.append(f"when_busy = {_toml_value(c.when_busy.value, c.id)}")
        out.append(f"timeout = {_toml_value(c.timeout, c.id)}")
        out.append(f"enabled = {_toml_value(c.enabled, c.id)}")
        if c.requires:
            out.append(f"requires = {_toml_value(c.requires, c.id)}")
    return "\n".join(out) + "\n"


def save(config: Config, path: Path) -> None:
    text = render(config)
    _ = parse(tomllib.loads(text))  # never write a file we could not read back
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    _ = tmp.write_text(text)
    _ = tmp.replace(path)
