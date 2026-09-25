# Development CI

Every PR and default-branch push runs checks for these independent scripts:
Bash/Zsh parsing, ShellCheck errors for Bash scripts, all 63 backup tests,
the diagnostic self-test and basedpyright for the two production Python scripts.
Run `bash .github/scripts/check.sh` at the repository root with Python 3.14,
uv, ShellCheck, Zsh, GNU tar and age with post-quantum support. Missing encryption
or GNU tar support fails before tests can silently skip those cases.

The check never runs a backup, mover, notification, migration or support script
against a live service. The existing tests use temporary directories, mocks and
local age/tar operations. The diagnostic self-test covers redaction and now also
protects against hostname/username collisions damaging path aliases. A ShellCheck
error in legacy backup logging was corrected without changing backup behavior.

The two Python scripts remain downloadable single files. No shared runtime
module, package framework or mass reformat is introduced. Ruff is intentionally
absent to preserve the existing project convention. basedpyright 1.40.0 reports
zero errors but 13 historical warnings; its supported baseline records those
specific findings, so new warnings and errors fail. The dynamic-import unittest
file is executed but excluded from type checking, as before. Remove baseline
entries as warnings are fixed; never regenerate it to conceal new failures.

CI versions are explicit and Renovate inherits the shared preset plus the official
GitHub Actions environment-version manager. Its native PEP 723 manager explicitly
scans the backup scripts' inline metadata. Existing unbounded script dependencies
remain a reproducibility gap: updates may resolve without a manifest change.
Renovate does not yet maintain uv script lockfiles, so no unmaintainable script
lockfile is introduced as an apparent merge guarantee. Runtime dependency and
live service compatibility remain manual validation requirements.

Shared actions, workflows and presets use immutable `v4.0.0` references.
Renovate is the sole ongoing dependency merge owner. Through the shared
`automerge.json` preset it arms GitHub auto-merge with the rebase strategy, and
GitHub merges only after every required CI and policy check passes on the
current head. Shared Renovate policy updates remain manual;
release-age rules, holds and repository-specific updater ownership still apply.
The legacy Actions merger and its comment commands are retired.

The separate PR policy workflow verifies Conventional Commit titles, genuine
matching author sign-offs, Renovate provenance, holds, outstanding review requests
and unresolved changes requests. After a pass, policy re-runs the other event's
older failed verdict for the same head (`actions: write`), so a withdrawn
objection clears without a manual re-run. Require its actual emitted policy context alongside
all existing application/content checks, pinned to GitHub Actions, with strict
up-to-date branch protection. Preserve stronger review requirements. Explicit CI
dispatches do not substitute for a missing metadata policy result. Review exact
head/base, full diffs and all required results before a bootstrap merge, then
verify resulting default-branch CI. Repository-specific updater ownership and
manual publication or delivery controls remain unchanged.
