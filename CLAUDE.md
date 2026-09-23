# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository shape

Independent single-file scripts, not a package: no build system, no root manifest, no lockfile, no shared module. Some scripts are run straight off `raw.githubusercontent.com/edbfi/arrsenal-of-scripts/refs/heads/main/<path>` (`miscellaneous/claude/claude-diag.py`, `miscellaneous/hotio/hotio-support-script.sh`, `server-scripts/rclone/rclone-sync-script.sh`), so every script must work when downloaded alone.

- Don't factor shared code into a common module; duplicate it inside each script.
- Don't move or rename those three scripts: their path is a public URL, embedded in their own usage headers (and in `SCRIPT_URL` in `claude-diag.py`).
- Hostnames, usernames, paths and service defaults in scripts are the author's real values and are intentional; keep them. Credentials never get defaults: default to `""` and read an env var, as the backup script does.
- `miscellaneous/hotio/hotio-migrate-script.sh` intentionally matches `hotio/` and `engels74/` images; don't rewrite it to `edbfi`.
- Scripts untouched since 2025 (`arr-scripts/`, `game-servers-script/`, `server-scripts/fail2ban/`, `miscellaneous/dashboard-icons-script/`, `hotio-migrate-script.sh`, `7z-server-backup-script.sh`) are dormant: change exactly what is asked. Most shell scripts lack `set -e`; that is house style, not a bug to fix in passing.
- `fail2ban-monitor.sh` and `fail2ban-monitor.zsh` are separate implementations; a fix to one does not propagate to the other.
- `server-scripts/backup/{7z,tar}-server-backup-script.sh` are standalone alternatives, not parts of the Python backup script.

## Checks

Full CI suite, from the repo root: `bash .github/scripts/check.sh`. It needs Python 3.14, uv, ShellCheck, zsh, GNU tar as `tar`, and age >= 1.3 (`age-keygen -pq`).

| Target | Command (repo root) |
| --- | --- |
| Shell | `bash -n <f>.sh && shellcheck --severity=error <f>.sh`; `zsh -n <f>.zsh` |
| Backup tests (all) | `python3 server-scripts/backup/python/test_backup_script.py` |
| One class / one test | append `TestFormatHelpers` / `TestFormatHelpers.test_format_bytes` |
| claude-diag | `python3 miscellaneous/claude/claude-diag.py --self-test` (must print `RESULT: OK`) |
| Types | `uvx --from "basedpyright==<BASEDPYRIGHT_VERSION from ci.yml>" basedpyright --warnings` |

- `check.sh` asserts `tar --version` says GNU tar. On macOS the default bsdtar fails it even though the backup script itself finds Homebrew `gtar`; put GNU tar first on `PATH` as `tar` to run the suite locally.
- `check.sh` only lints scripts in the git index (`git ls-files`); an unstaged new script is silently skipped.
- The basedpyright version exists only as `BASEDPYRIGHT_VERSION` in `.github/workflows/ci.yml` (Renovate-managed); `check.sh` parses it from there. Change it there, nowhere else.
- basedpyright checks only the two files in `pyrightconfig.json` `include`, and `--warnings` makes warnings fatal. Existing warnings are recorded in `.basedpyright/baseline.json`: delete entries as you fix them, never regenerate it to absorb new findings. Fix the code or add a scoped `# pyright: ignore[<rule>]`.
- CI runs `git diff --exit-code HEAD` after the checks, so any tracked file the checks rewrite must be committed.

## Python: where `.agents/rules/python-3_14-core.md` does not apply

Use that rule for language and typing style. Its project and tooling sections conflict with this repo; follow this table instead:

| Rule says | This repo |
| --- | --- |
| `pyproject.toml` + `uv.lock`, `uv add` | Dependencies live in each script's PEP 723 `# /// script` header; no manifest or lockfile |
| `ruff format` / `ruff check` | Ruff is intentionally absent; don't run it or add config. Match surrounding style by hand |
| pytest | stdlib `unittest`, run the file directly |

## claude-diag.py

- Must stay stdlib-only with a `#!/usr/bin/env python3` shebang: users run it as `curl ... | python3 -`.
- `--publish` uploads the report publicly to PasteMyst. New report content must pass through `Redactor`. For a new secret shape, add a line to `SELF_TEST_FIXTURE`, its raw value to `forbidden` and its placeholder to `expected` in `self_test()`.

## Commits

Conventional Commits with the component as scope: `fix(backup):`, `feat(backup)!:`, `fix(ci):`, `chore(deps):`. PRs are gated by `.github/workflows/pr-policy.yml` (Conventional Commit title, author sign-off). Renovate owns dependency and action version bumps.

## Reference

- `CI.md`: what CI checks, baseline policy, Renovate and merge policy. Read before changing `.github/`, `renovate.json`, `pyrightconfig.json` or the baseline.
- `.agents/rules/python-3_14-core.md`: Python 3.14 typing and syntax style for basedpyright `recommended`. Read before non-trivial Python work, with the table above.
- `arr-scripts/README.md`: Radarr/Sonarr Custom Script wiring for the Danish-audio scripts. Read when changing those scripts.
