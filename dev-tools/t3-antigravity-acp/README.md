# T3 custom tools: Antigravity ACP updater and gateway shim

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
To choose models and thinking levels in T3 instead of pinning one model, see the
[gateway shim](#gateway-shim-models-and-thinking-levels-from-cliproxyapi).

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
itself never schedules anything. After each update, upkeep also deletes
releases other than the current and previous ones that no process is running
(about 300 MB each).

## Gateway shim: models and thinking levels from CLIProxyAPI

With *Gemini API key* sign-in pointed at CLIProxyAPI (CPA), ACP offers its
built-in model list, so T3 can only pin one CPA model with
`AGY_ACP_DEFAULT_MODEL`, and ACP still sends `thinkingLevel: high` whatever the
pinned id says. `antigravity_acp_gateway.py` sits between T3 and the stock
launcher and gives T3 a model picker plus a thinking-level picker, both built
from what the CPA instance reports:

- **Models.** At startup the shim lists CPA's models (`GET /v1beta/models`) and
  keeps those matching `AGY_GATEWAY_MODEL_PATTERN` (default: Gemini 4 and
  later). Ids that differ only by a level suffix (`-low`, `-xhigh`, ...) form one
  family.
- **Levels.** For each family it sends one request with an invalid level; CPA
  rejects it locally with the levels that family supports. If CPA does not know
  the bare family name (it may list only `<family>-high`), the listed ids are
  probed instead. A family without levels is offered as a plain model. If CPA
  cannot answer a probe (timeout, 429, 5xx), the refresh fails and the previous
  catalog stays in use.
- **Limit.** ACP accepts 32 gateway models; past that, whole families are
  dropped, never single levels, and the default model's family is kept.
- **Routing.** ACP runs in its gateway mode with one `family(level)` id per
  variant. CPA applies that suffix over the request's own thinking level.
  T3's `authenticate` is switched from `gemini-api-key` to `gateway`, and the
  model and thinking-level choices are mapped to and from those ids. All other
  ACP traffic passes through unchanged.

Nothing about specific models is built in: a model CPA starts listing appears
after the next catalog refresh (cached per T3 provider instance for six hours;
stale entries are reused if CPA is unreachable). With no gateway URL and key,
or no catalog, the shim runs the stock launcher unchanged.

Install it beside the launcher with an interpreter T3 can run (3.11+):

```sh
python3.14 antigravity_acp_gateway.py --install          # writes ~/.t3/custom-tools/antigravity-acp-gateway
python3.14 antigravity_acp_gateway.py --install --python /usr/bin/python3
```

Then, in T3's Antigravity provider settings, keep *Gemini API key* with the CPA
key, keep `GOOGLE_GEMINI_BASE_URL`, and set *Binary path* to
`~/.t3/custom-tools/antigravity-acp-gateway`. Optional environment:

| Variable | Effect |
| --- | --- |
| `AGY_ACP_DEFAULT_MODEL` | Starting model: a family (`<family>`), or one level (`<family>-high`, `<family>(high)`). Default: the first family at `high`, or its nearest level. |
| `AGY_GATEWAY_MODEL_PATTERN` | Regular expression selecting the CPA models to offer. |
| `AGY_LLM_GATEWAY_URL`, `AGY_LLM_GATEWAY_API_KEY` | Use these instead of the T3 base URL and key. |

`--catalog` prints what discovery finds for the current environment
(`--refresh` bypasses the cache). The installed copy does not update itself:
on a host with [upkeep](../upkeep/README.md), the opt-in
`t3-antigravity-acp-gateway` task reinstalls it whenever this file changes;
elsewhere, rerun `--install` after pulling changes. `--install` refuses a
virtualenv interpreter, whose removal would break the shebang.

## Development checks

```sh
python3 tools/typing_gate.py
uvx basedpyright@latest -p pyproject.toml   # 0 errors, 0 warnings; -p because the repo root has a pyrightconfig.json
python3 -m unittest discover -s tests -v
```
