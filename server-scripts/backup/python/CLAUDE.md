# CLAUDE.md — server-scripts/backup/python

`overengineered-backup-script.py` is one module; `test_backup_script.py` loads it by path with `importlib` (the hyphenated name can't be imported normally). `requests` and `uptime-kuma-api` are declared only in the PEP 723 header and imported inside `try/except ImportError` so a plain interpreter still runs. A new third-party dependency goes in the header and gets the same optional import.

The header comment (lines 10-43) documents the age post-quantum key setup and every CLI invocation. Read it before touching encryption or restore.

Module-level globals set at startup and read everywhere: `config`, `dry_run_mode`, `backup_state`, `shutdown_requested`, `_lock_owned`. A new mutable global must also be reset in `ScriptTestCase.setUp` in the test file, or state leaks between tests.

## Adding or renaming a config option

Precedence: `Config` defaults, then TOML (`/etc/backup-script.toml`), then env secrets (`BACKUP_UPTIME_KUMA_PASSWORD`, `BACKUP_DISCORD_WEBHOOK_URL`), then CLI flags. Unknown TOML sections or keys raise `ConfigError`, so the schema must stay complete.

1. Add the field to `Config` under its `# [section]` comment. `_apply_toml` infers the TOML type from the default's runtime type (Path, bool, int, str, list), so the default must have the intended type.
2. Add `"key": "attr"` under the section in `_TOML_SCHEMA`. Path-valued lists also go in `_PATH_LIST_ATTRS`.
3. Add the key to the `default_config_toml()` template in the same section.
4. Run `python3 test_backup_script.py TestConfigLoading`. `test_default_config_toml_round_trips_to_defaults` catches a wrong value in the template but not an omitted key, so check step 3 by eye.

Removing a key: add `(section, key)` with an upgrade hint to `_REMOVED_TOML_KEYS` (see `("encryption", "legacy_password_file")`), or existing configs get only a generic unknown-key error.

## Invariants

- Call external binaries through `resolve_command("docker")` (or `find_command()` when the tool is optional), never by bare name. Under `sudo`, `secure_path` drops Homebrew/Linuxbrew from `PATH` and only `find_command()` falls back to those directories. For tar use `resolve_tar()` and gate on `tar_is_gnu()`.
- Spawn subprocesses only via `run_command()` or `run_pipeline()`. They register children in `_active_processes`, which the signal handler and timeout watchdog terminate; a raw `subprocess.Popen` survives an abort and can leave containers stopped.
- Gate anything destructive (delete, write, upload, container stop/start, maintenance window) on `dry_run_mode`.
- New pre-flight validation goes in `pre_flight_checks()` and reports through its local `problem()`, never `raise`: in dry-run mode it accumulates every problem. Tests named `*_is_aggregated_not_raised` enforce this.

## Tests

- `TestRestoreRoundTrip`, `TestCreateBackupEndToEnd` and `TestPostQuantumRoundTrip` skip themselves without age, GNU tar or `age-keygen -pq`, so a green local run can be partial. `.github/scripts/check.sh` refuses to run without those tools.
- The test file is excluded from basedpyright (`pyrightconfig.json`); its type errors come from the dynamic import and are not to be fixed.
