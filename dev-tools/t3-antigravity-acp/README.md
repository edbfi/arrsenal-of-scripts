# T3 custom tools: Antigravity ACP updater

Source of truth for the updater that used to live loose in `~/.t3/custom-tools/`.
[upkeep](../upkeep/README.md) runs it as the `t3-antigravity-acp` task, so a
host with upkeep needs no copy of this script under `~/.t3`; it still runs
standalone as shown below. It installs *into* `~/.t3/custom-tools/` regardless
of where the script itself lives.

`update_antigravity_acp.py` installs the Antigravity ACP release listed in the
[ACP registry](https://raw.githubusercontent.com/agentclientprotocol/registry/refs/heads/main/antigravity-acp/agent.json)
for this machine's native platform. The same source is used on every host; the
binaries are always downloaded per platform and never copied between hosts.

Requires Python 3.11+ (standard library only). If the system `python3` is older,
use a user-managed interpreter: `uv run --python 3.11 --no-project python update_antigravity_acp.py ...`.

## Layout (under `~/.t3/custom-tools/`)

| Path | Purpose |
| --- | --- |
| `antigravity-acp` | Stable launcher. Point T3 Code's Antigravity *Binary path* here. |
| `localharness_external` | Symlink to the active harness; T3 requires it beside the binary path. |
| `antigravity-acp-current` | Symlink to the active `releases/<version>-<platform>` directory. |
| `antigravity-acp-previous` | Symlink to the release that was active before; used by `--rollback`. |
| `releases/<version>-<platform>/` | ACP, harness, release `launch` script, registry copy, `install-manifest.json`. |
| `antigravity-acp-<version>/` | Old pre-manifest layout. Reported, never reused, never pruned. |

The launcher resolves `antigravity-acp-current` at launch, then the release's
`launch` script pins `ANTIGRAVITY_HARNESS_PATH` to its own sibling harness,
adds registry arguments (Linux: `--uid=`) only when the caller did not pass them,
and `exec`s ACP. Neither script writes to stdout, so ACP's JSON-RPC stream is
untouched.

A release is promoted only after the archive matches the SHA-256 the registry
publishes for the platform, archive members are validated, both binaries are
confirmed to be the right format and CPU architecture, and an ACP `initialize`
request succeeds. Downloads are https only, redirects included. The manifest
records registry source and SHA-256, archive URL and SHA-256, file hashes,
formats and the smoke-test result. The file hashes are local provenance
records, not publisher signatures. A same-version directory without a
matching, verifiable manifest is never reused.

Credentials are not handled here. Configure API-key auth and environment
(`GOOGLE_GEMINI_BASE_URL`, `AGY_ACP_DEFAULT_MODEL`) in T3 Code's provider settings.

## Commands

```sh
cd dev-tools/t3-antigravity-acp   # in your arrsenal-of-scripts checkout
python3 update_antigravity_acp.py --check-only            # status only: no lock, no changes
python3 update_antigravity_acp.py --list                  # releases and links
python3 update_antigravity_acp.py --yes                   # install/update and activate (never deletes)
python3 update_antigravity_acp.py --yes --no-activate     # install and verify only
python3 update_antigravity_acp.py --registry ./agent.json --yes   # use a pinned registry snapshot
python3 update_antigravity_acp.py --activate 1.4.1-linux-x86_64   # switch to a specific release
python3 update_antigravity_acp.py --rollback              # reactivate the previous release
python3 update_antigravity_acp.py --prune                 # delete other releases, asking each time
python3 update_antigravity_acp.py --prune --yes           # same, without asking
```

`--force` installs a fresh copy into a new, timestamped release directory; the
active directory is never modified in place. A second updater for the same
install root exits with status 75 while the first holds the lock. Pruning only
considers `releases/` and always keeps the current and previous releases.

Scheduled updates come from upkeep (`upkeep schedule install`); the updater
itself never schedules anything.

## Development checks

```sh
python3 tools/typing_gate.py
uvx basedpyright@latest -p pyproject.toml   # 0 errors, 0 warnings; -p because the repo root has a pyrightconfig.json
python3 -m unittest discover -s tests -v
```
