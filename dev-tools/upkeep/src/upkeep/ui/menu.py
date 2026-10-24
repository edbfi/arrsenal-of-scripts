"""The interactive menu: what `upkeep` shows when started in a terminal."""

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING

from rich.rule import Rule
from rich.text import Text

from upkeep import history, inventory, schedule, selfupdate
from upkeep.app import App, RunRequest
from upkeep.model import Mode, Presence
from upkeep.ui import prompt
from upkeep.ui.prompt import Divider, Option
from upkeep.ui.setup import run_setup

if TYPE_CHECKING:
    from collections.abc import Callable


class Action(StrEnum):
    RUN = "run"
    PICK = "pick"
    DRY = "dry"
    STATUS = "status"
    INSTALL = "install"
    SETUP = "setup"
    SCHEDULE = "schedule"
    SELF = "self"
    LOG = "log"
    QUIT = "quit"


def run_menu(app: App) -> int:
    console = app.console
    if not app.host.config_file.exists():
        console.print(Text("No configuration yet: upkeep updates whatever is installed.", "grey50"))
        try:
            if prompt.confirm("Run the first-time setup now?"):
                run_setup(app)
        except prompt.Cancelled:
            pass

    handlers: dict[Action, Callable[[App], None]] = {
        Action.RUN: lambda a: _run(a, RunRequest()),
        Action.PICK: _pick,
        Action.DRY: lambda a: _run(a, RunRequest(dry_run=True)),
        Action.STATUS: lambda a: a.status(),
        Action.INSTALL: _install,
        Action.SETUP: run_setup,
        Action.SCHEDULE: _schedule,
        Action.SELF: _self_update,
        Action.LOG: _log,
    }
    console.print(app.header())
    while True:
        installed = schedule.current_any(app.host)
        try:
            action = prompt.select(
                "What would you like to do?",
                [
                    Option("Update everything", Action.RUN, hint="all enabled tools"),
                    Option("Update selected tools…", Action.PICK),
                    Option("Preview a run", Action.DRY, hint="dry run: nothing changes"),
                    Option("Status", Action.STATUS, hint="versions, sources, sessions"),
                    Divider(),
                    Option("Install tools…", Action.INSTALL),
                    Option("Setup & preferences…", Action.SETUP),
                    Option(
                        "Schedule…",
                        Action.SCHEDULE,
                        hint=installed.describe() if installed else "not scheduled",
                    ),
                    Option("Update upkeep itself…", Action.SELF),
                    Option("Show the last run's log", Action.LOG),
                    Divider(),
                    Option("Quit", Action.QUIT),
                ],
            )
        except prompt.Cancelled:
            return 0
        if action is Action.QUIT:
            return 0
        console.print(Rule(style="grey35"))
        try:
            handlers[action](app)
        except prompt.Cancelled:
            console.print(Text("cancelled", "grey50"))
        console.print(Rule(style="grey35"))


def _run(app: App, request: RunRequest) -> None:
    _ = app.run(request)


def _pick(app: App) -> None:
    with app.console.status("Checking tools…"):
        entries = asyncio.run(inventory.gather(app.host, app.config, app.tasks()))
    options: list[prompt.Entry[str]] = [
        Option(
            e.task.info.title,
            e.task.info.id,
            hint=" · ".join(
                x for x in (e.version, f"{len(e.busy)} running" if e.busy else "") if x
            ),
        )
        for e in entries
        if e.presence is Presence.INSTALLED
    ]
    chosen = prompt.checkbox("Update which tools?", options)
    if chosen:
        force = any(e.busy for e in entries if e.task.info.id in chosen) and prompt.confirm(
            "Some are in use. Update them anyway (normally they are skipped)?", default=False
        )
        _run(app, RunRequest(only=tuple(chosen), force=force))


def _install(app: App) -> None:
    with app.console.status("Looking for missing tools…"):
        entries = asyncio.run(inventory.gather(app.host, app.config, app.tasks()))
    options: list[prompt.Entry[str]] = [
        Option(e.task.info.title, e.task.info.id, hint=e.task.info.summary)
        for e in entries
        if e.presence is Presence.MISSING and e.task.installable
    ]
    if not options:
        app.console.print(Text("Every tool upkeep can install is already installed.", "green"))
        return
    chosen = prompt.checkbox("Install which tools?", options)
    if chosen:
        _run(app, RunRequest(only=tuple(chosen), mode=Mode.INSTALL))


def _schedule(app: App) -> None:
    current = schedule.current_any(app.host)
    app.console.print(Text(f"Current: {current.describe() if current else 'not scheduled'}"))
    choice = prompt.select(
        "Schedule",
        [
            Option("Run daily at…" if not current else "Change the time…", "set"),
            Option(
                "Remove the schedule", "remove", disabled=None if current else "nothing scheduled"
            ),
            Option("Back", "back"),
        ],
    )
    if choice == "set":
        default = (
            current.at if current and current.at else schedule.At.default_for(app.host.hostname)
        )
        text = prompt.text("Time (24h, HH:MM)", default=str(default))
        at = schedule.At.parse(text)
        backend = app.schedule_install(at)
        app.console.print(Text(f"✓ scheduled daily at {at} via {backend.name}", "green"))
    elif choice == "remove":
        removed = schedule.remove_all(app.host)
        app.console.print(Text(f"✓ removed {', '.join(removed) or 'nothing'}", "green"))


def _self_update(app: App) -> None:
    with app.console.status("Checking for a newer upkeep…"):
        result = app.self_update_check()
    app.console.print(Text(result.describe()))
    for line in result.changes:
        app.console.print(Text(f"  {line}", "grey50"))
    if result.state is selfupdate.State.AVAILABLE and prompt.confirm("Update now?"):
        app.apply_self_update(result)
        app.console.print(Text("✓ up to date", "green"))


def _log(app: App) -> None:
    path = history.latest_log(app.host.log_dir)
    if path is None:
        app.console.print(Text("No runs yet.", "grey50"))
        return
    with app.console.pager():
        app.console.print(Text(path.read_text(errors="replace")))
