#!/bin/sh
# Bootstrap upkeep on a host:
#   curl -fsSL https://raw.githubusercontent.com/edbfi/arrsenal-of-scripts/main/dev-tools/upkeep/install.sh | sh
#
# Clones arrsenal-of-scripts (upkeep self-updates from this checkout), installs
# uv if it is missing, writes ~/.local/bin/upkeep, then starts `upkeep setup`
# when a terminal is attached. Everything stays in your home directory; do not
# run this with sudo.
set -eu

REPO_URL=${UPKEEP_REPO_URL:-https://github.com/edbfi/arrsenal-of-scripts.git}
BRANCH=${UPKEEP_BRANCH:-main}
DEST=${UPKEEP_CHECKOUT:-${XDG_DATA_HOME:-$HOME/.local/share}/upkeep/arrsenal-of-scripts}

say() { printf 'upkeep: %s\n' "$*"; }
die() { printf 'upkeep: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root"
command -v git >/dev/null 2>&1 || die "git is required"
command -v curl >/dev/null 2>&1 || die "curl is required"

UV=$(command -v uv 2>/dev/null || true)
[ -n "$UV" ] || [ ! -x "$HOME/.local/bin/uv" ] || UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  say "installing uv (https://astral.sh/uv)"
  script=$(mktemp)
  trap 'rm -f "$script"' EXIT
  curl --proto '=https' --tlsv1.2 -fsSL -o "$script" https://astral.sh/uv/install.sh
  INSTALLER_NO_MODIFY_PATH=1 sh "$script"
  rm -f "$script"  # the exec below would skip the EXIT trap
  trap - EXIT
  UV="$HOME/.local/bin/uv"
fi

if [ -d "$DEST/.git" ]; then
  say "updating $DEST"
  git -C "$DEST" pull --ff-only --quiet origin "$BRANCH"
else
  say "cloning into $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DEST"
fi

PROJECT="$DEST/dev-tools/upkeep"
"$UV" sync --frozen --no-dev --quiet --project "$PROJECT"
"$UV" run --frozen --no-dev --quiet --project "$PROJECT" -- upkeep self install

if [ -t 1 ] && [ -r /dev/tty ]; then
  exec "$UV" run --frozen --no-dev --quiet --project "$PROJECT" -- upkeep setup </dev/tty
fi
say "installed. Run \`upkeep setup\` to choose tools and a schedule."
