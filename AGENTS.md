# AGENTS.md

This file provides guidance to AI coding agents when working with code in this
repository.

## Repository shape

Independent, self-contained sysadmin scripts — not a package. No build system, no root
dependency manifest and no shared library. `.github/workflows/ci.yml` now validates
all shell syntax, ShellCheck errors, backup tests and diagnostic self-tests, plus
production Python types. See `CI.md`. Several scripts
document being run straight off `raw.githubusercontent.com/.../main/<path>`
(`server-scripts/rclone/rclone-sync-script.sh:24`, `miscellaneous/hotio/hotio-support-script.sh:6`,
`miscellaneous/claude/claude-diag.py:6`).

Consequence: **never factor shared code into a common module** — a script must keep working when
that single file is downloaded in isolation. Duplicate instead. The only cross-file dependency in
the repo is `test_backup_script.py`, which loads the backup script by path via `importlib.util`.

Actively maintained:

| Path | |
| --- | --- |
| `server-scripts/backup/python/overengineered-backup-script.py` | 3.3k lines, 63 tests, type-checked |
| `miscellaneous/claude/claude-diag.py` | stdlib-only redacting diagnostic reporter, has `--self-test` |
| `miscellaneous/hotio/hotio-support-script.sh` | `set -euo pipefail`, hard dependency on `gum` |
| `server-scripts/rclone/rclone-sync-script.sh` | top-of-file "Configuration Section" constants |

Everything else was last touched in April 2025. Treat it as dormant: fix exactly what is asked,
do not modernize opportunistically (11 of the 13 shell scripts have no `set -e` — that is the
existing style here, not a bug to fix in passing). `server-scripts/backup/{7z,tar}-server-backup-script.sh`
are older standalone alternatives, **not** components of the Python backup script.

## Commands

From `server-scripts/backup/python/`:

```bash
uv run test_backup_script.py                                    # 63 tests, stdlib unittest
uv run test_backup_script.py TestConfigLoading                  # one class
uv run test_backup_script.py TestRotation.test_keeps_newest_n   # one test
uvx --from basedpyright==1.40.0 basedpyright                     # run at repo root; baseline guards new diagnostics
./overengineered-backup-script.py --dry-run --verbose           # full preflight preview
./overengineered-backup-script.py --print-default-config        # emit a commented example TOML
```

Both files there carry a PEP 723 `# /// script` header requiring Python >= 3.14; `uv run` resolves
`requests` and `uptime-kuma-api`. There is no `pyproject.toml`, `requirements.txt`, or lockfile
anywhere in the repo. Tests needing `age` or GNU `tar` skip themselves when the binary is missing,
so a green run on a machine without them is not full coverage.

`uvx basedpyright test_backup_script.py` reports 25 errors — all artifacts of the `importlib` load
(`mod.config = ...` is "cannot assign to attribute for class ModuleType"). Only the two production
scripts are held at 0 errors; do not chase the test file's.

Other checks:

```bash
uvx basedpyright miscellaneous/claude/claude-diag.py       # 0 errors
python3 miscellaneous/claude/claude-diag.py --self-test    # must end in "RESULT: OK"
bash miscellaneous/hotio/hotio-support-script.sh --dry-run
```

**Do not run `ruff check` or `ruff format`.** No ruff config is checked in and the code does not
conform (verified: 41 findings; all three `.py` files would be reformatted). Match the surrounding
style by hand. `pyrightconfig.json` scopes basedpyright to the two production scripts; a reviewed
baseline records 13 existing warnings under 1.40.0. New warnings/errors fail. The
CI script reads its pinned checker version from `ci.yml`. Do not regenerate the
baseline simply to accept new findings.

## Backup script

Single module, banner-delimited sections. Module-level globals set once at startup and read
everywhere: `config`, `dry_run_mode`, `backup_state`, `log`, `shutdown_requested`, `_lock_owned`.

Config precedence: `Config` dataclass defaults -> TOML (`/etc/backup-script.toml`) -> env secrets
(`BACKUP_UPTIME_KUMA_PASSWORD`, `BACKUP_DISCORD_WEBHOOK_URL`) -> CLI flags. Unknown TOML sections
or keys are a hard `ConfigError` typo guard, so the schema must be kept complete.

**Adding or renaming a config option — four places, all in that one file:**

1. Field on the `Config` dataclass, under its `# [section]` comment. `_apply_toml` infers the
   expected TOML type from the default's runtime type, so the default must have the intended type.
2. The `"section" -> {"key": "attr"}` entry in `_TOML_SCHEMA`. Path-valued lists also go in
   `_PATH_LIST_ATTRS`.
3. The key in the `default_config_toml()` f-string template, in the same section.
4. `uv run test_backup_script.py TestConfigLoading`. Its
   `test_default_config_toml_round_trips_to_defaults` asserts the generated config re-parses to
   `Config()` exactly — that catches a wrong *value* in the template but not an *omitted* key (the
   default survives either way), so check step 3 by eye.

Removing a key: add a `(section, key)` entry to `_REMOVED_TOML_KEYS` with an upgrade hint, or
existing deployments get only a generic "unknown key" error. See `("encryption",
"legacy_password_file")` for the pattern.

**Calling an external binary** — resolve it first:
`run_command([resolve_command("docker"), "ps", "-q"], timeout=30)`. Under `sudo`, `secure_path`
strips Homebrew/Linuxbrew from `PATH`; `find_command()` falls back to those directories and caches
the lookup. A bare `"docker"` or `"rclone"` in an argv list works locally and fails in production.
Use `resolve_tar()` for tar — the pipeline uses GNU-only flags, gated by `tar_is_gnu()`.

**Spawning a subprocess** — go through `run_command()` / `run_pipeline()`, the only two callers of
`subprocess.Popen`; they register the child in `_active_processes`. A raw `Popen` is invisible to
`signal_handler()` and the overall-timeout watchdog, which can leave containers stopped for hours.

**Anything destructive** (delete, write, upload, container restart, maintenance window) must sit
behind the `dry_run_mode` global.

**New pre-flight validation** goes inside `pre_flight_checks()` and reports through the local
`problem()` helper, never `raise` — in dry-run mode `problem()` accumulates findings so one
`--dry-run` answers "will tonight's run work?" in a single pass. Tests named
`*_is_aggregated_not_raised` enforce this.

**A new module-level global** must also be reset in `ScriptTestCase.setUp` in the test file, or
tests leak state into each other.

## Gotchas

- The non-secret `Config` defaults are the author's real hostnames, users, and paths (`aplex`,
  `uptimekuma.cccp.ps`) — intentional. Do not extend that to credentials: `uptime_kuma_password`
  and `discord_webhook_url` default to `""` and come from the env vars above.
- `claude-diag.py --publish` uploads the report to PasteMyst, publicly. Any new field added to the
  report must pass through `Redactor`, and `SELF_TEST_FIXTURE` should gain a case for any new
  secret shape; `--self-test` fails loudly on leaked substrings.
- `arr-scripts/README.md` is stale: it documents `danishAudioRadarr.sh` / `danishAudioSonarr.sh`,
  renamed in `ce53824` to `radarr-check-for-danish-audio.sh` / `sonarr-check-for-danish-audio.sh`.
  Trust the filenames on disk; its Radarr/Sonarr connection setup steps are still current.
- `fail2ban-monitor.sh` and `fail2ban-monitor.zsh` are separate implementations, not one script
  with two shebangs. A behavioural fix to one does not propagate; state which variant you changed.
- Commit messages use Conventional Commits with the component as scope — `fix(backup):`,
  `chore(renovate):`, `feat(backup)!:` for breaking changes, `docs:`.

## Reference files

- `.agents/rules/python-3_14-core.md` — the Python 3.14 style reference the `.py` files are written
  against (PEP 695 generics, `TypeIs`, deferred annotations). Read before non-trivial Python work.
  Caveat: its `pyproject.toml` snippets for ruff/basedpyright are aspirational — none of that
  config exists here, so do not add it as part of an unrelated change.
- `server-scripts/backup/python/overengineered-backup-script.py` lines 10-43 — header documents the
  age post-quantum key setup and the exact CLI invocations. Read before touching encryption or
  restore.
- `arr-scripts/README.md` — Radarr/Sonarr Custom Script connection setup for the Danish-audio
  scripts. Read when wiring those two scripts into an 'arr app.
