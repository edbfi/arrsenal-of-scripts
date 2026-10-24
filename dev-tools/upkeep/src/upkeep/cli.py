"""Command line: `upkeep` alone opens the menu; subcommands script it."""

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from rich.console import Console
from rich.table import Table
from rich.text import Text

from upkeep import __version__, config, history, launcher, schedule, selfupdate
from upkeep.app import EXIT_LOCKED, App, RunRequest, load_config
from upkeep.config import Config, ConfigError, OutputMode
from upkeep.engine import CycleError
from upkeep.host import Host, UnsupportedHostError
from upkeep.model import Mode
from upkeep.tasks import UnknownTaskError, is_enabled, when_busy
from upkeep.ui.prompt import Cancelled

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT_REFUSAL = (
    "upkeep must not run as root: every tool it manages lives in your home "
    "directory or a user-owned Homebrew prefix, and Homebrew and the Claude "
    "installer refuse root. Run it as your normal user (--allow-root overrides)."
)


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number >= 1, got {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upkeep",
        description=(
            "Keep this host's AI and developer CLIs installed and up to date. "
            + "Run without arguments for the interactive menu."
        ),
    )
    _ = parser.add_argument("--version", action="version", version=f"upkeep {__version__}")
    _ = parser.add_argument("--allow-root", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = sub.add_parser("run", help="update installed tools (the default action)")
    _ = run.add_argument("tasks", nargs="*", metavar="TASK", help="only these tasks")
    _ = run.add_argument("--skip", action="append", default=[], metavar="TASK", help="skip a task")
    _ = run.add_argument("-n", "--dry-run", action="store_true", help="show what would run")
    _ = run.add_argument("--force", action="store_true", help="update tools even while in use")
    _ = run.add_argument("-j", "--jobs", type=_positive, help="parallel tasks")
    _ = run.add_argument("--retries", type=_positive, help="attempts per task")
    _ = run.add_argument("--output", choices=[m.value for m in OutputMode], help="output detail")
    _ = run.add_argument("--plain", action="store_true", help="plain text instead of the dashboard")
    _ = run.add_argument("--no-self-update", action="store_true", help="don't update upkeep first")
    _ = run.add_argument("--scheduled", action="store_true", help="for cron/launchd: quiet, logged")

    install = sub.add_parser("install", help="install missing tools with their native installers")
    _ = install.add_argument("tasks", nargs="*", metavar="TASK")
    _ = install.add_argument("-n", "--dry-run", action="store_true")

    _ = sub.add_parser("status", help="installed versions, sources and running sessions")
    _ = sub.add_parser("list", help="every task, its default and busy policy")
    _ = sub.add_parser("setup", help="choose tools, preferences and the schedule")

    sched = sub.add_parser("schedule", help="show, install or remove the daily run")
    _ = sched.add_argument(
        "action", nargs="?", choices=["show", "install", "remove"], default="show"
    )
    _ = sched.add_argument("--at", metavar="HH:MM", help="time of day (default 04:xx per host)")

    self_ = sub.add_parser("self", help="upkeep's own checkout and launcher")
    _ = self_.add_argument(
        "action", nargs="?", choices=["status", "update", "install"], default="status"
    )

    cfg = sub.add_parser("config", help="config file helpers")
    _ = cfg.add_argument(
        "action", nargs="?", choices=["path", "show", "template", "edit"], default="path"
    )

    logs = sub.add_parser("logs", help="print the latest run log")
    _ = logs.add_argument("--path", action="store_true", help="only print its path")
    return parser


@dataclass(frozen=True, slots=True)
class Args:
    command: str | None
    allow_root: bool
    tasks: tuple[str, ...]
    skip: tuple[str, ...]
    dry_run: bool
    force: bool
    jobs: int | None
    retries: int | None
    output: OutputMode | None
    plain: bool
    no_self_update: bool
    scheduled: bool
    action: str | None
    at: str | None
    path_only: bool


def parse_args(argv: Sequence[str]) -> Args:
    """argparse yields untyped attributes; narrow them all here."""
    ns = cast("dict[str, object]", vars(build_parser().parse_args(argv)))

    def text(key: str) -> str | None:
        value = ns.get(key)
        return value if isinstance(value, str) else None

    def flag(key: str) -> bool:
        return ns.get(key) is True

    def number(key: str) -> int | None:
        value = ns.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def words(key: str) -> tuple[str, ...]:
        value = ns.get(key)
        items = cast("list[object]", value) if isinstance(value, list) else []
        out: list[str] = []
        for item in items:  # accept "a,b" as well as "a b"
            if isinstance(item, str):
                out += [w for w in item.split(",") if w]
        return tuple(out)

    output = text("output")
    return Args(
        command=text("command"),
        allow_root=flag("allow_root"),
        tasks=words("tasks"),
        skip=words("skip"),
        dry_run=flag("dry_run"),
        force=flag("force"),
        jobs=number("jobs"),
        retries=number("retries"),
        output=OutputMode(output) if output else None,
        plain=flag("plain"),
        no_self_update=flag("no_self_update"),
        scheduled=flag("scheduled"),
        action=text("action"),
        at=text("at"),
        path_only=flag("path"),
    )


def cmd_list(app: App) -> int:
    table = Table(box=None, header_style="bold", pad_edge=False)
    for column in ("task", "on", "when in use", "what it does"):
        table.add_column(column)
    for task in app.tasks():
        info = task.info
        enabled = is_enabled(task, app.config)
        busy = when_busy(task, app.config).value
        table.add_row(
            Text(info.id, "bold"),
            Text("yes" if enabled else "no", "green" if enabled else "grey50"),
            Text(f"{busy}" + (f" ({info.busy_note})" if info.busy_note else ""), "grey50"),
            info.summary,
        )
    app.console.print(table)
    return 0


def cmd_schedule(app: App, args: Args) -> int:
    match args.action:
        case "install":
            at = (
                schedule.At.parse(args.at)
                if args.at
                else schedule.At.default_for(app.host.hostname)
            )
            backend = app.schedule_install(at)
            app.console.print(Text(f"✓ upkeep runs daily at {at} via {backend.name}", "green"))
            app.console.print(Text(f"  output: {app.host.state_dir / 'scheduled.log'}", "grey50"))
        case "remove":
            removed = schedule.remove_all(app.host)
            app.console.print(Text(f"removed: {', '.join(removed) or 'nothing was scheduled'}"))
        case _:
            current = schedule.current_any(app.host)
            app.console.print(current.describe() if current else "not scheduled")
            if current:
                app.console.print(Text(current.command, "grey50"))
    return 0


def cmd_self(app: App, args: Args) -> int:
    match args.action:
        case "install":
            target = launcher.install(app.host)
            app.console.print(Text(f"✓ wrote {target}", "green"))
            if str(app.host.local_bin) not in os.environ.get("PATH", "").split(os.pathsep):
                app.console.print(Text(f"  add {app.host.local_bin} to your PATH", "yellow"))
            return 0
        case "update":
            result = app.self_update_check()
            app.console.print(result.describe())
            for line in result.changes:
                app.console.print(Text(f"  {line}", "grey50"))
            match result.state:
                case selfupdate.State.CURRENT:
                    return 0
                case selfupdate.State.AVAILABLE:
                    return 0 if app.self_update_now(result) else EXIT_LOCKED
                case _:
                    return 1
        case _:
            app.console.print(app.header())
            return 0


def cmd_config(app: App, args: Args) -> int:
    path = app.host.config_file
    match args.action:
        case "template":
            app.console.print(config.TEMPLATE, markup=False, highlight=False)
        case "show":
            app.console.print(config.render(app.config), markup=False, highlight=False)
        case "edit":
            if config.write_template(path):
                app.console.print(Text(f"created {path}", "grey50"))
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
            _ = subprocess.run([*shlex.split(editor), str(path)], check=False)
            app.reload()  # validate what was written
            app.console.print(Text("✓ config is valid", "green"))
        case _:
            app.console.print(str(path), soft_wrap=True)
    return 0


def cmd_logs(app: App, args: Args) -> int:
    path = history.latest_log(app.host.log_dir)
    if path is None:
        app.console.print("no runs yet")
        return 1
    if args.path_only:
        app.console.print(str(path), soft_wrap=True)
    else:
        _ = sys.stdout.write(path.read_text(errors="replace"))
    return 0


def dispatch(app: App, args: Args) -> int:
    match args.command:
        case None:
            if not app.interactive:
                build_parser().print_help()
                return 2
            from upkeep.ui.menu import run_menu  # prompt_toolkit only when needed

            return run_menu(app)
        case "run":
            request = RunRequest(
                only=args.tasks,
                skip=args.skip,
                dry_run=args.dry_run,
                force=args.force,
                jobs=args.jobs,
                retries=args.retries,
                output=args.output,
                plain=args.plain,
                scheduled=args.scheduled,
                self_update=not args.no_self_update,
            )
            return app.run(request)
        case "install":
            if not args.tasks:
                app.console.print("name the tools to install, e.g. `upkeep install claude codex`")
                return 2
            return app.run(
                RunRequest(
                    only=args.tasks, mode=Mode.INSTALL, dry_run=args.dry_run, self_update=False
                )
            )
        case "status":
            app.status()
            return 0
        case "list":
            return cmd_list(app)
        case "setup":
            from upkeep.ui.setup import run_setup

            run_setup(app)
            return 0
        case "schedule":
            return cmd_schedule(app, args)
        case "self":
            return cmd_self(app, args)
        case "config":
            return cmd_config(app, args)
        case "logs":
            return cmd_logs(app, args)
        case other:
            raise AssertionError(f"unhandled command {other}")


# Commands that write nothing. Anything else run as root (say via sudo, which
# keeps HOME) could leave root-owned files in the user's home.
READ_ONLY = {"status", "list", "logs"}


def needs_valid_config(args: Args) -> bool:
    """Whether a broken config.toml stops this command.

    The commands that repair or bypass the file keep working on defaults.
    """
    match args.command:
        case "config":
            return args.action == "show"
        case "logs" | "self":
            return False
        case "schedule":
            return args.action != "remove"
        case _:
            return True


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw)
    console = Console(stderr=False, highlight=False)
    err = Console(stderr=True, highlight=False)
    try:
        host = Host.detect()
        if host.is_root and not args.allow_root and args.command not in READ_ONLY:
            err.print(Text(ROOT_REFUSAL, "red"))
            return 2
        try:
            loaded = load_config(host.config_file)
        except (ConfigError, UnknownTaskError) as error:
            if needs_valid_config(args):
                raise
            err.print(Text(f"upkeep: {error}\nupkeep: using the defaults", "yellow"))
            loaded = Config()
        app = App(host, loaded, console, raw)
        return dispatch(app, args)
    except (ConfigError, UnknownTaskError, UnsupportedHostError, CycleError) as error:
        err.print(Text(f"upkeep: {error}", "red"))
        return 2
    except (schedule.ScheduleError, selfupdate.GitError, launcher.LauncherError) as error:
        err.print(Text(f"upkeep: {error}", "red"))
        return 1
    except Cancelled, KeyboardInterrupt:
        err.print(Text("cancelled", "grey50"))
        return 130
