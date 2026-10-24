"""`upkeep setup`: choose tools, install missing ones, set preferences and
the daily schedule. Safe to re-run; it starts from the current config."""

import asyncio
from dataclasses import replace

from rich.panel import Panel
from rich.text import Text

from upkeep import inventory, launcher, schedule, sources
from upkeep.app import App, RunRequest, with_tasks
from upkeep.config import OutputMode, SelfUpdateMode, TaskSettings
from upkeep.model import Mode, Presence
from upkeep.ui import prompt
from upkeep.ui.prompt import Divider, Option


def _validate_time(value: str) -> str | None:
    try:
        _ = schedule.At.parse(value)
    except schedule.ScheduleError as error:
        return str(error)
    return None


def run_setup(app: App) -> None:
    console = app.console
    host = app.host
    first_time = not host.config_file.exists()
    managers = ", ".join(sources.detected_managers(host)) or "none found"
    console.print(
        Panel(
            Text.assemble(
                ("Welcome to upkeep" if first_time else "upkeep setup", "bold"),
                "\nKeeps this host's AI and developer CLIs installed and up to date, "
                + "using each tool's own updater, without disturbing running sessions.\n",
                (f"{host.describe()} · package managers: {managers}", "grey50"),
            ),
            border_style="cyan",
        )
    )
    with console.status("Looking for installed tools…", spinner="dots"):
        entries = asyncio.run(inventory.gather(host, app.config, app.tasks()))
    console.print(inventory.table(entries))
    console.print()

    # 1. Which tools to keep up to date (and install when missing).
    installed: list[prompt.Entry[str]] = [Divider("found on this host")]
    missing: list[prompt.Entry[str]] = [Divider("not installed")]
    for e in entries:
        info = e.task.info
        match e.presence:
            case Presence.INSTALLED:
                hint = " · ".join(x for x in (e.version, e.where()) if x)
                installed.append(Option(info.title, info.id, hint=hint, checked=e.enabled))
            case Presence.FOREIGN:
                source = e.probe.source.owner_hint if e.probe.source else e.probe.note
                installed.append(Option(info.title, info.id, disabled=f"{e.where()}: {source}"))
            case Presence.MISSING if e.task.installable:
                hint = "select to install"
                missing.append(
                    Option(info.title, info.id, hint=hint, checked=e.enabled and e.install)
                )
            case Presence.MISSING:
                missing.append(Option(info.title, info.id, disabled=e.probe.note))
    choices = installed + (missing if len(missing) > 1 else [])
    chosen = set(prompt.checkbox("Which tools should upkeep keep up to date?", choices))

    updates: dict[str, TaskSettings] = {}
    to_install: list[str] = []
    for e in entries:
        selectable = e.presence is Presence.INSTALLED or (
            e.presence is Presence.MISSING and e.task.installable
        )
        if not selectable:
            continue
        current = app.config.task(e.task.info.id)
        wanted = e.task.info.id in chosen
        install = wanted and e.presence is Presence.MISSING
        if install:
            to_install.append(e.task.info.id)
        updates[e.task.info.id] = replace(current, enabled=wanted, install=install)
    config = with_tasks(app.config, updates)

    # 2. Preferences.
    jobs = prompt.select(
        "How many tasks may run at the same time?",
        [Option(str(n), n, hint="default" if n == 4 else "") for n in (1, 2, 4, 6, 8)],
        default=config.jobs,
    )
    output = prompt.select(
        "How much output should a run print?",
        [
            Option("Everything", OutputMode.FULL, hint="each task's full output when it finishes"),
            Option("Failures only", OutputMode.FAILURES, hint="full output of failed tasks"),
            Option("Status lines", OutputMode.QUIET, hint="one line per task"),
        ],
        default=config.output,
    )
    self_update = prompt.select(
        "How should upkeep update itself?",
        [
            Option("Automatically", SelfUpdateMode.AUTO, hint="fast-forward before each run"),
            Option("Just tell me", SelfUpdateMode.NOTIFY, hint="report new commits"),
            Option("Never", SelfUpdateMode.OFF),
        ],
        default=config.self_update,
    )
    config = replace(config, jobs=jobs, output=output, self_update=self_update)
    app.save(config)
    console.print(Text(f"✓ saved {host.config_file}", "green"))

    # 3. Install what was chosen.
    if to_install and prompt.confirm(f"Install {', '.join(to_install)} now?"):
        _ = app.run(RunRequest(only=tuple(to_install), mode=Mode.INSTALL, self_update=False))
    elif to_install:
        console.print(Text("They will be installed on the next `upkeep run`.", "grey50"))

    # 4. Launcher and schedule.
    if launcher.status(host) != str(launcher.path(host)) and prompt.confirm(
        f"Install the `upkeep` command at {launcher.path(host)}?"
    ):
        console.print(Text(f"✓ wrote {launcher.install(host)}", "green"))

    existing = schedule.current_any(host)
    if prompt.confirm("Run upkeep automatically every day?", default=True):
        default_at = (
            existing.at if existing and existing.at else schedule.At.default_for(host.hostname)
        )
        at = schedule.At.parse(
            prompt.text(
                "At what time? (24h, HH:MM)", default=str(default_at), validate=_validate_time
            )
        )
        backend = app.schedule_install(at)
        console.print(Text(f"✓ scheduled daily at {at} via {backend.name}", "green"))
    elif existing and prompt.confirm(f"Remove the existing schedule ({existing.describe()})?"):
        removed = schedule.remove_all(host)
        console.print(Text(f"✓ removed {', '.join(removed)} schedule", "green"))

    console.print(
        Panel(
            Text.assemble(
                ("All set. ", "bold green"),
                "Run ",
                ("upkeep", "bold"),
                " for the menu, ",
                ("upkeep run", "bold"),
                " to update now, or ",
                ("upkeep status", "bold"),
                " to see where everything stands.",
            ),
            border_style="green",
        )
    )
