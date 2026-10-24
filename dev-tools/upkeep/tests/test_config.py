import tomllib
from typing import TYPE_CHECKING

import pytest

from upkeep.config import (
    BusyPolicy,
    Config,
    ConfigError,
    CustomTask,
    OutputMode,
    TaskSettings,
    load,
    parse,
    render,
    save,
    write_template,
)
from upkeep.tasks import check_config

if TYPE_CHECKING:
    from pathlib import Path


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load(tmp_path / "absent.toml") == Config()


def test_template_parses_to_defaults() -> None:
    from upkeep.config import TEMPLATE

    assert parse(tomllib.loads(TEMPLATE)) == Config()


def test_parses_settings() -> None:
    config = parse(
        tomllib.loads(
            """
            [run]
            jobs = 2
            output = "failures"
            [tasks.claude]
            when_busy = "skip"
            [tasks.bun-globals]
            ensure = ["a", "b"]
            """
        )
    )
    assert config.jobs == 2
    assert config.output is OutputMode.FAILURES
    assert config.task("claude").when_busy is BusyPolicy.SKIP
    assert config.task("bun-globals").strings("ensure", ()) == ("a", "b")
    assert config.task("unknown") == TaskSettings()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("[run]\njbos = 3", "unknown key"),
        ("[run]\njobs = 0", "run.jobs"),
        ("[run]\njobs = true", "run.jobs"),
        ('[run]\noutput = "loud"', "expected one of"),
        ('[tasks.claude]\nwhen_busy = "maybe"', "tasks.claude.when_busy"),
        ('[[custom]]\nid = "x"', "exactly one of"),
        ('[[custom]]\nid = "my tool"\nrun = ["a"]', "letters, digits"),
        ('[[custom]]\nid = "x"\nrun = ["a"]\nshell = "b"', "exactly one of"),
        ('[[custom]]\nid = "x"\nrun = ["a"]\n[[custom]]\nid = "x"\nshell = "b"', "unique"),
    ],
)
def test_rejects_invalid(document: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _ = parse(tomllib.loads(document))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ('[tasks.claude]\nwhen-busy = "skip"', r"tasks\.claude: unknown key\(s\) when-busy"),
        ("[tasks.bun-globals]\nenable = false", r"tasks\.bun-globals: unknown key\(s\) enable"),
        ('[tasks.brew]\nensure = ["x"]', "unknown key"),
        ("[tasks.cluade]\nenabled = false", "tasks.cluade: unknown task"),
    ],
)
def test_task_tables_are_checked_against_the_catalogue(document: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        check_config(parse(tomllib.loads(document)))


def test_known_task_tables_pass_the_check() -> None:
    document = """
        [tasks.bun-globals]
        ensure = ["a"]
        [tasks.cloak]
        enabled = false
        [[custom]]
        id = "cloak"
        run = ["true"]
    """
    check_config(parse(tomllib.loads(document)))


def test_render_round_trips() -> None:
    config = Config(
        jobs=6,
        output=OutputMode.QUIET,
        tasks={
            "claude": TaskSettings(enabled=True, when_busy=BusyPolicy.SKIP),
            "codex": TaskSettings(enabled=True, install=True, timeout=120.0),
            "bun-globals": TaskSettings(extra={"ensure": ["x", 'quote"d'], "odd key.x": 1}),
            "dotted.id": TaskSettings(enabled=False),
        },
        custom=(
            CustomTask(id="c1", title="One", summary="s", argv=("~/bin/x", "--y"), after=("brew",)),
            CustomTask(id="c2", title="Two", summary="s", shell="echo 'hi' | cat", requires="~/x"),
        ),
    )
    assert parse(tomllib.loads(render(config))) == config


def test_save_and_template_never_clobber(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "config.toml"
    assert write_template(path)
    assert not write_template(path)
    save(Config(jobs=3), path)
    assert load(path).jobs == 3
