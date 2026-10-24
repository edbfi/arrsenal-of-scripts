# upkeep

Keeps a host's AI and developer CLIs installed and up to date, on macOS and
Linux, without disturbing sessions that are using them. It replaces the
`update-ai` zsh function and does the same jobs, plus a few more:

| task | how it updates | while in use |
| --- | --- | --- |
| `brew` | `brew update`, then `brew upgrade` per package | packages in use are held back |
| `uv`, `prek` | `uv self update`, `prek self update` | updated (atomic `mv`) |
| `ruff` | re-runs the standalone installer (ruff has no self-update) | updated (atomic `mv`) |
| `bun` | `bun upgrade` | updated (rename) |
| `claude` | `claude update` | updated (versioned dirs + symlink) |
| `codex` | `codex update`, then deletes releases nothing runs | updated (versioned dirs + symlink) |
| `agy` | `agy update`, then deletes `agy.<ts>.old` leftovers | updated (old binary renamed aside) |
| `bun-globals` | installs the ACP adapters if missing, `bun update -g --latest` | **skipped** |
| `skills` | `bunx skills update -g` | n/a |
| `agent-browser` | `agent-browser install` (browser for the current CLI) | **skipped** |
| `t3-antigravity-acp` | [`../t3-antigravity-acp`](../t3-antigravity-acp/README.md) `--yes` | updated (versioned dirs + symlink) |
| `uv-tools` *(off by default)* | `uv tool upgrade`, per tool | each tool in use is skipped |

`upkeep list` prints the same table for the current config.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/edbfi/arrsenal-of-scripts/main/dev-tools/upkeep/install.sh | sh
```

This clones the repository to `~/.local/share/upkeep/arrsenal-of-scripts`,
installs uv if it is missing, writes the `~/.local/bin/upkeep` launcher and
starts `upkeep setup`. From an existing clone you can instead run
`uv run --project dev-tools/upkeep upkeep self install`; the launcher then points
at that clone.

Once every host runs upkeep, the `update-ai` / `codex-update` /
`_codex_prune_releases` functions can come out of `~/.zshrc` (radxa:
`~/.config/zsh/ai-tools.zsh`), and `~/.t3/custom-tools/update_antigravity_acp.py`
is no longer needed (keep the rest of `~/.t3/custom-tools`: that is the install).

## Use

```text
upkeep                     interactive menu
upkeep setup               choose tools, install missing ones, preferences, schedule
upkeep run                 update everything enabled          (-n: dry run)
upkeep run claude codex    only these         --skip brew     --force: even if in use
upkeep install codex       install a missing tool with its native installer
upkeep status              source, version and running sessions per tool
upkeep schedule install    daily run (--at 04:30); `schedule remove` to undo
upkeep self update         update upkeep itself now
upkeep logs                the last run's full transcript
upkeep config edit         open ~/.config/upkeep/config.toml (validated on save)
```

**Without setup**, upkeep updates whatever is already installed and enabled by
default. **Setup** writes `~/.config/upkeep/config.toml`. It records which tools to
keep updated, and marks tools you chose but don't have with `install = true`,
so later runs install them if they go missing. `upkeep config template` shows
every option, including `[[custom]]` tasks for host-specific steps (such as
radxa's cloakbrowser refresh after `agent-browser install`).

## How it behaves

**Native updaters, for native installs only.** Every copy of a tool is found
(PATH, `~/.local/bin`, `~/.bun/bin`, `~/.cargo/bin`, Homebrew, `/usr/bin`, snap,
nix, …) and classified: native installer, Homebrew, apt/dnf/pacman (asked via
`dpkg-query -S` / `rpm -qf` / `pacman -Qo`), bun/npm global, uv tool, cargo. A
tool's own updater runs only when the copy first on PATH came from its native
installer. Anything else is left to its owner, and `status` says which owner
that is and lists stray duplicate copies.

**Running sessions.** Before a task starts, upkeep takes a fresh process
snapshot. Each task declares what "in use" means (an executable name, or
files under a directory, which also catches `bun …/node_modules/x/cli.js`). The
default policy follows how each vendor replaces files (table above). Override it
per tool with `when_busy = "skip" | "allow"`, or for one run with `--force`.
Homebrew upgrades run with `HOMEBREW_NO_INSTALL_CLEANUP`, so a dependency
upgraded along the way keeps its old keg for whoever has it open. Only the
packages that were checked as idle are cleaned up.

**Races.** A run holds `~/.local/state/upkeep/upkeep.lock`, so a scheduled run
and a manual one never overlap (the second exits 75). Inside a run, tasks run
in parallel (`jobs`, default 4) but respect ordering (`after`) and shared
resources: the cargo-dist installers share a receipt dir, for example. Every
child gets `INSTALLER_NO_MODIFY_PATH=1`, `CODEX_NON_INTERACTIVE=1` and no
terminal, so no installer edits your shell rc or waits on a prompt. Timeouts
and Ctrl-C kill a task's whole process group.

**No sudo.** Every tool here installs under `$HOME` or into a user-owned
Homebrew prefix (checked on all hosts). Homebrew refuses to run as root and
Claude's installer refuses sudo, so a root crontab would only leave root-owned
files in your home. upkeep refuses to run as root, and scheduling uses your own
crontab on Linux and a LaunchAgent on macOS (launchd runs a job missed during
sleep at the next wake; cron skips it). `schedule.backend = "cron"` forces cron.
Scheduled runs pick a per-host minute after 04:00 so hosts don't all download at
once.

**Self-update.** upkeep runs from a git checkout. Before each run (`self_update.mode
= "auto"`), it fetches `origin/main`. If the checkout is clean, on `main` and
only behind, it fast-forwards and prints the commits that touched upkeep. It
re-syncs dependencies if the lockfile changed, and re-executes itself if its
own code changed. Any other state (local commits, another branch, uncommitted
edits) is reported and left alone, so a development clone is safe.

**Output.** In a terminal, a live dashboard shows each running task's latest
output line and an overall progress bar. Finished tasks print their full output
(`output = "full"`), only failures, or one line each. Every run also writes a
full transcript to `~/.local/state/upkeep/logs/` (newest 30 kept).

## Layout and extending

```text
src/upkeep/
  cli.py        argparse → typed Args → dispatch
  app.py        operations shared by the CLI, the menu and setup
  engine.py     scheduling: ordering, resources, parallelism, busy checks, retries
  tasks/        one class per tool; base.py holds the Task contract
  sources.py    where a binary came from
  procs.py      process snapshot and "in use" matching
  report.py     live dashboard and plain transcript
  selfupdate.py, schedule.py, launcher.py, history.py, config.py, inventory.py
  ui/           questionary prompts, menu, setup wizard
```

To add a tool, subclass `BinaryTask` (one binary with a vendor installer and
self-update) or `Task`, declare its `TaskInfo` (id, ordering, resources, busy
policy), and add it to `builtin_tasks()` in `tasks/__init__.py`. The engine,
status table, menu and setup pick it up from there. For a one-off on a single
host, add a `[[custom]]` task to the config.

## Development

```sh
cd dev-tools/upkeep
uv run upkeep …                                   # run from the checkout
python3 tools/typing_gate.py
uv run uvx basedpyright@latest -p pyproject.toml  # 0 errors, 0 warnings
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

`-p pyproject.toml` matters: pyright prefers the repository root's
`pyrightconfig.json` over this project's `pyproject.toml`. The same checks run
in prek and CI.
