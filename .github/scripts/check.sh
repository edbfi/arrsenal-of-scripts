#!/usr/bin/env bash
set -euo pipefail
# Pin the local checker; callers may override it explicitly.
BASEDPYRIGHT_VERSION="${BASEDPYRIGHT_VERSION:-1.40.1}"
while IFS= read -r -d '' script; do
  case "$script" in
    *.zsh) zsh -n "$script" ;;
    *.sh) bash -n "$script"; shellcheck --severity=error "$script" ;;
  esac
done < <(git ls-files -z '*.sh' '*.zsh')
# Missing encryption support must fail instead of quietly skipping those tests.
command -v age
command -v age-keygen
age-keygen -pq > /dev/null
[[ "$(tar --version)" == *"GNU tar"* ]]
# The tests only use temporary paths, mocks and local encryption processes.
python3 server-scripts/backup/python/test_backup_script.py
python3 miscellaneous/claude/claude-diag.py --self-test
uvx --from "basedpyright==$BASEDPYRIGHT_VERSION" basedpyright --warnings
