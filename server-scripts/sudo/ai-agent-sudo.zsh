#!/usr/bin/env zsh
# ai-agent-sudo.zsh
# Temporary sudoers helper for AI coding agents on macOS and Linux (Ubuntu/sudo-rs).
# It installs a sudoers.d drop-in, validates it with visudo, and optionally
# schedules automatic removal (launchd on macOS, systemd-run on Linux, with a
# root background-sleep fallback on either).

emulate -L zsh
setopt NO_UNSET PIPE_FAIL EXTENDED_GLOB
unsetopt BEEP

typeset -gr SCRIPT_NAME="${0:t}"
typeset -gr VERSION="1.0.0"

typeset -gr OS_NAME="$(uname -s)"

is_macos() { [[ "$OS_NAME" == "Darwin" ]]; }

# gid 0 group is 'wheel' on macOS and 'root' on Linux.
typeset -g ROOT_GROUP="root"
is_macos && ROOT_GROUP="wheel"

# A representative privileged command, used only in help/menu examples.
typeset -g EXAMPLE_ALLOW_SPEC="/usr/bin/systemctl restart docker.service"
is_macos && EXAMPLE_ALLOW_SPEC="/bin/launchctl kickstart -k system/com.apple.mDNSResponder"

typeset -gi ASSUME_YES=0
typeset -gi QUIET=0
typeset -gi COLOR_OFF=0
typeset -gi DRY_RUN=0

typeset -g TARGET_USER=""
typeset -g TARGET_UID=""
typeset -g SAFE_TARGET_USER=""
typeset -g SUDOERS_FILE=""
typeset -g UNIT=""
typeset -g LAUNCHD_PLIST=""
typeset -g TMPFILE=""

typeset -ga ALLOW_SPECS
typeset -g DURATION_RAW="${AI_SUDO_DEFAULT_DURATION:-30m}"
typeset -gi UNTIL_DISABLED=0

typeset -gA STATE
STATE=()

# ---------- Paths ----------

cmd_path() {
  local fallback="$1"
  shift

  local candidate
  for candidate in "$@"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done

  print -r -- "$fallback"
}

typeset -g SUDO_CMD="$(cmd_path /usr/bin/sudo sudo)"
typeset -g VISUDO_CMD="$(cmd_path /usr/sbin/visudo visudo)"
typeset -g INSTALL_CMD="$(cmd_path /usr/bin/install install)"
typeset -g SYSTEMD_RUN_CMD="$(cmd_path /usr/bin/systemd-run systemd-run)"
typeset -g SYSTEMCTL_CMD="$(cmd_path /usr/bin/systemctl systemctl)"
typeset -g RM_CMD="$(cmd_path /bin/rm rm)"
typeset -g DATE_CMD="$(cmd_path /usr/bin/date date)"
typeset -g MKTEMP_CMD="$(cmd_path /usr/bin/mktemp mktemp)"
typeset -g LAUNCHCTL_CMD="$(cmd_path /bin/launchctl launchctl)"

typeset -g STATE_DIR="${AI_SUDO_STATE_DIR:-${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}/ai-agent-sudo}"
typeset -g STATE_FILE="${STATE_DIR}/state"
typeset -g LOG_FILE="${AI_SUDO_LOG_FILE:-${STATE_DIR}/ai-agent-sudo.log}"

# ---------- UI ----------

typeset -g C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN=""

setup_colors() {
  if (( COLOR_OFF )) || [[ -n "${NO_COLOR:-}" ]] || [[ ! -t 1 ]]; then
    return 0
  fi

  if command -v tput >/dev/null 2>&1; then
    C_RESET="$(tput sgr0 2>/dev/null || true)"
    C_BOLD="$(tput bold 2>/dev/null || true)"
    C_DIM="$(tput dim 2>/dev/null || true)"
    C_RED="$(tput setaf 1 2>/dev/null || true)"
    C_GREEN="$(tput setaf 2 2>/dev/null || true)"
    C_YELLOW="$(tput setaf 3 2>/dev/null || true)"
    C_BLUE="$(tput setaf 4 2>/dev/null || true)"
    C_CYAN="$(tput setaf 6 2>/dev/null || true)"
  fi
}

log_msg() {
  local level="$1"
  shift

  mkdir -p -- "$STATE_DIR" 2>/dev/null || true
  print -r -- "$($DATE_CMD '+%Y-%m-%d %H:%M:%S%z' 2>/dev/null) [$level] $*" >> "$LOG_FILE" 2>/dev/null || true
}

say() {
  local icon="$1"
  local color="$2"
  shift 2

  (( QUIET )) && return 0
  print -r -- "${color}${icon}${C_RESET} $*"
}

info()    { say "ℹ" "$C_BLUE" "$@"; log_msg INFO "$*"; }
success() { say "✓" "$C_GREEN" "$@"; log_msg INFO "$*"; }
warn()    { say "⚠" "$C_YELLOW" "$@"; log_msg WARN "$*"; }
error()   { say "✕" "$C_RED" "$@"; log_msg ERROR "$*"; }

die() {
  error "$@"
  exit 1
}

banner() {
  (( QUIET )) && return 0
  print -r -- ""
  print -r -- "${C_CYAN}╭──────────────────────────────────────────────╮${C_RESET}"
  print -r -- "${C_CYAN}│${C_RESET} ${C_BOLD}AI Agent Temporary Sudo${C_RESET} ${C_DIM}v${VERSION}${C_RESET}             ${C_CYAN}│${C_RESET}"
  print -r -- "${C_CYAN}╰──────────────────────────────────────────────╯${C_RESET}"
}

row() {
  local key="$1"
  local value="$2"
  printf '  %-18s %s\n' "$key" "$value"
}

pause_menu() {
  (( QUIET )) && return 0
  print -rn -- "${C_DIM}Press Enter to continue...${C_RESET} "
  local _unused
  IFS= read -r _unused
}

confirm() {
  local prompt="$1"

  (( ASSUME_YES )) && return 0
  print -rn -- "${C_YELLOW}?${C_RESET} ${prompt} [y/N] "

  local reply=""
  IFS= read -r reply
  [[ "${reply:l}" == "y" || "${reply:l}" == "yes" ]]
}

# ---------- Lifecycle ----------

cleanup() {
  if [[ -n "${TMPFILE:-}" && -f "$TMPFILE" ]]; then
    rm -f -- "$TMPFILE" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap 'print -r -- ""; error "Interrupted"; exit 130' INT
trap 'error "Terminated"; exit 143' TERM

# ---------- State and context ----------

default_target_user() {
  if (( EUID == 0 )) && [[ -n "${SUDO_USER:-}" && "${SUDO_USER:-}" != "root" ]]; then
    print -r -- "$SUDO_USER"
    return 0
  fi

  if [[ -n "${USER:-}" && "${USER:-}" != "root" ]]; then
    print -r -- "$USER"
    return 0
  fi

  id -un 2>/dev/null || print -r -- "root"
}

validate_target_user() {
  local user="$1"
  local re='^[A-Za-z_][A-Za-z0-9_.-]*[$]?$'

  [[ -n "$user" ]] || die "Target user is empty. Use --user <name>."
  [[ "$user" =~ "$re" ]] || die "Unsafe target username: $user"
}

safe_unit_part() {
  local raw="$1"
  local safe="${raw//[^A-Za-z0-9_]/_}"
  print -r -- "$safe"
}

refresh_context() {
  [[ -n "$TARGET_USER" ]] || TARGET_USER="$(default_target_user)"
  validate_target_user "$TARGET_USER"

  TARGET_UID="$(id -u "$TARGET_USER" 2>/dev/null || print -r -- unknown)"
  SAFE_TARGET_USER="$(safe_unit_part "$TARGET_USER")"
  SUDOERS_FILE="/etc/sudoers.d/90-ai-agent-temp-${TARGET_USER}"

  if is_macos; then
    # launchd label (reverse-DNS style) doubles as the state 'unit' value.
    UNIT="com.ai-agent-sudo.remove.${SAFE_TARGET_USER}.${TARGET_UID}"
    LAUNCHD_PLIST="/Library/LaunchDaemons/${UNIT}.plist"
  else
    UNIT="ai-agent-temp-sudo-remove-${SAFE_TARGET_USER}-${TARGET_UID}"
    LAUNCHD_PLIST=""
  fi

  STATE_FILE="${STATE_DIR}/state-${SAFE_TARGET_USER}-${TARGET_UID}"
}

load_state() {
  STATE=()
  [[ -r "$STATE_FILE" ]] || return 1

  local key value
  while IFS='=' read -r key value; do
    [[ -n "$key" ]] || continue
    STATE[$key]="$value"
  done < "$STATE_FILE"

  return 0
}

save_state() {
  local expires_epoch="${1:-}"
  local mode="${2:-timed}"

  mkdir -p -- "$STATE_DIR" || die "Could not create state directory: $STATE_DIR"

  {
    print -r -- "version=$VERSION"
    print -r -- "target_user=$TARGET_USER"
    print -r -- "sudoers_file=$SUDOERS_FILE"
    print -r -- "unit=$UNIT"
    print -r -- "enabled_epoch=$($DATE_CMD +%s)"
    print -r -- "expires_epoch=$expires_epoch"
    print -r -- "mode=$mode"
  } >| "$STATE_FILE"
}

clear_state() {
  rm -f -- "$STATE_FILE" 2>/dev/null || true
}

# ---------- Utilities ----------

run_sudo() {
  log_msg DEBUG "+ sudo $*"
  "$SUDO_CMD" "$@"
  local rc=$?

  if (( rc != 0 )); then
    log_msg ERROR "sudo command failed with exit $rc: $*"
  fi

  return $rc
}

duration_to_minutes() {
  local raw="${1:l}"
  raw="${raw//[[:space:]]/}"

  local n=0
  local mult=1

  if [[ "$raw" == <-> ]]; then
    n="$raw"
    mult=1
  elif [[ "$raw" == <->m ]]; then
    n="${raw%m}"
    mult=1
  elif [[ "$raw" == <->min ]]; then
    n="${raw%min}"
    mult=1
  elif [[ "$raw" == <->mins ]]; then
    n="${raw%mins}"
    mult=1
  elif [[ "$raw" == <->h ]]; then
    n="${raw%h}"
    mult=60
  elif [[ "$raw" == <->hr ]]; then
    n="${raw%hr}"
    mult=60
  elif [[ "$raw" == <->hrs ]]; then
    n="${raw%hrs}"
    mult=60
  elif [[ "$raw" == <->d ]]; then
    n="${raw%d}"
    mult=1440
  else
    die "Invalid duration: '$1'. Use examples like 15m, 30m, 2h, or 1d."
  fi

  local minutes=$(( n * mult ))
  (( minutes > 0 )) || die "Duration must be greater than zero."
  (( minutes <= 10080 )) || die "Duration is too large. Maximum is 7 days."

  print -r -- "$minutes"
}

format_duration() {
  local minutes="$1"

  if (( minutes % 1440 == 0 )); then
    print -r -- "$(( minutes / 1440 ))d"
  elif (( minutes % 60 == 0 )); then
    print -r -- "$(( minutes / 60 ))h"
  else
    print -r -- "${minutes}m"
  fi
}

date_human() {
  local epoch="${1:-}"
  [[ -n "$epoch" ]] || { print -r -- "-"; return 0; }

  if is_macos; then
    "$DATE_CMD" -r "$epoch" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || print -r -- "$epoch"
  else
    "$DATE_CMD" -d "@$epoch" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || print -r -- "$epoch"
  fi
}

build_sudoers_rule() {
  local joined="${(j:, :)ALLOW_SPECS}"
  print -r -- "$TARGET_USER ALL=(root) NOPASSWD: $joined"
}

validate_allow_specs() {
  local spec
  for spec in "$@"; do
    [[ -n "$spec" ]] || die "Empty sudo command specification."
    [[ "$spec" != *$'\n'* ]] || die "Sudo command specifications cannot contain newlines."
  done
}

# ---------- Core actions ----------

cancel_existing_timer_quietly() {
  load_state >/dev/null 2>&1 || true

  local unit="${STATE[unit]:-$UNIT}"
  [[ -n "$unit" ]] || return 0

  if is_macos; then
    "$SUDO_CMD" "$LAUNCHCTL_CMD" bootout "system/${unit}" >/dev/null 2>&1 || true
    "$SUDO_CMD" "$RM_CMD" -f -- "/Library/LaunchDaemons/${unit}.plist" >/dev/null 2>&1 || true
  else
    "$SUDO_CMD" "$SYSTEMCTL_CMD" stop "${unit}.timer" "${unit}.service" >/dev/null 2>&1 || true
    "$SUDO_CMD" "$SYSTEMCTL_CMD" reset-failed "${unit}.timer" "${unit}.service" >/dev/null 2>&1 || true
  fi
}

install_sudoers_rule() {
  local rule="$(build_sudoers_rule)"

  if (( DRY_RUN )); then
    banner
    info "Dry run only. Would install this sudoers rule:"
    print -r -- ""
    print -r -- "$rule"
    print -r -- ""
    return 0
  fi

  local tmpl="${TMPDIR:-/tmp}"
  TMPFILE="$($MKTEMP_CMD "${tmpl%/}/ai-agent-sudo.XXXXXX")" || die "Could not create temporary file."
  print -r -- "$rule" >| "$TMPFILE" || die "Could not write temporary sudoers file."

  info "Validating sudoers rule with visudo..."
  run_sudo "$VISUDO_CMD" -cf "$TMPFILE" >/dev/null || die "visudo rejected the generated sudoers rule. Nothing was installed."

  info "Installing temporary sudoers drop-in..."
  run_sudo "$INSTALL_CMD" -o root -g "$ROOT_GROUP" -m 0440 "$TMPFILE" "$SUDOERS_FILE" || die "Could not install $SUDOERS_FILE"

  if ! run_sudo "$VISUDO_CMD" -c >/dev/null; then
    run_sudo "$RM_CMD" -f -- "$SUDOERS_FILE" >/dev/null 2>&1 || true
    die "Full sudoers validation failed. Removed temporary rule."
  fi

  rm -f -- "$TMPFILE" 2>/dev/null || true
  TMPFILE=""
}

schedule_removal_fallback() {
  local minutes="$1"
  local seconds=$(( minutes * 60 ))

  warn "Using a root background sleep as the removal fallback."
  warn "If the machine reboots or the process is killed, run '$SCRIPT_NAME disable' manually."

  # SUDOERS_FILE is generated from a validated local username, so this simple
  # single-quoted shell fragment is safe for the controlled path we use.
  "$SUDO_CMD" /bin/sh -c "sleep $seconds; rm -f -- '$SUDOERS_FILE'" >/dev/null 2>&1 &!

  return 0
}

schedule_removal_launchd() {
  local expires_epoch="$1"
  local label="$UNIT"
  local plist="$LAUNCHD_PLIST"

  # Calendar components of the expiry moment (BSD date). Base-10 arithmetic
  # strips the leading zeros that would otherwise produce invalid plist ints.
  local cal_min cal_hour cal_day cal_mon
  cal_min=$(( 10#$("$DATE_CMD" -r "$expires_epoch" '+%M') ))
  cal_hour=$(( 10#$("$DATE_CMD" -r "$expires_epoch" '+%H') ))
  cal_day=$(( 10#$("$DATE_CMD" -r "$expires_epoch" '+%d') ))
  cal_mon=$(( 10#$("$DATE_CMD" -r "$expires_epoch" '+%m') ))

  local tmpl="${TMPDIR:-/tmp}"
  local plist_tmp
  plist_tmp="$("$MKTEMP_CMD" "${tmpl%/}/ai-agent-sudo-plist.XXXXXX")" || return 1

  # Paths embedded below come from a validated username, so they are safe to
  # single-quote in the removal command and contain no XML metacharacters.
  cat >| "$plist_tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>rm -f -- '${SUDOERS_FILE}'; rm -f -- '${plist}'; /bin/launchctl bootout system/${label}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Month</key>
        <integer>${cal_mon}</integer>
        <key>Day</key>
        <integer>${cal_day}</integer>
        <key>Hour</key>
        <integer>${cal_hour}</integer>
        <key>Minute</key>
        <integer>${cal_min}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>AbandonProcessGroup</key>
    <true/>
</dict>
</plist>
PLIST

  # Replace any previous daemon before installing the fresh one.
  "$SUDO_CMD" "$LAUNCHCTL_CMD" bootout "system/${label}" >/dev/null 2>&1 || true
  "$SUDO_CMD" "$RM_CMD" -f -- "$plist" >/dev/null 2>&1 || true

  if ! run_sudo "$INSTALL_CMD" -o root -g "$ROOT_GROUP" -m 0644 "$plist_tmp" "$plist"; then
    rm -f -- "$plist_tmp" 2>/dev/null || true
    return 1
  fi
  rm -f -- "$plist_tmp" 2>/dev/null || true

  # Load it (modern bootstrap API, with legacy load -w as backup).
  if ! "$SUDO_CMD" "$LAUNCHCTL_CMD" bootstrap system "$plist" >/dev/null 2>&1; then
    "$SUDO_CMD" "$LAUNCHCTL_CMD" load -w "$plist" >/dev/null 2>&1 || return 1
  fi

  return 0
}

schedule_removal() {
  local minutes="$1"
  local expires_epoch=$(( $($DATE_CMD +%s) + minutes * 60 ))

  (( DRY_RUN )) && return 0

  cancel_existing_timer_quietly

  if is_macos; then
    info "Scheduling automatic removal in $(format_duration "$minutes")..."
    if schedule_removal_launchd "$expires_epoch"; then
      save_state "$expires_epoch" "timed-launchd"
      success "Auto-removal scheduled for $(date_human "$expires_epoch")."
      return 0
    fi
    warn "launchd scheduling failed. Using a root background sleep instead."
  elif [[ -x "$SYSTEMD_RUN_CMD" ]]; then
    info "Scheduling automatic removal in $(format_duration "$minutes")..."

    if "$SUDO_CMD" "$SYSTEMD_RUN_CMD" \
      --quiet \
      --unit="$UNIT" \
      --on-active="${minutes}m" \
      --collect \
      "$RM_CMD" -f -- "$SUDOERS_FILE"; then
      save_state "$expires_epoch" "timed-systemd"
      success "Auto-removal scheduled for $(date_human "$expires_epoch")."
      return 0
    fi
  fi

  schedule_removal_fallback "$minutes"
  save_state "$expires_epoch" "timed-fallback"
  success "Fallback auto-removal scheduled for $(date_human "$expires_epoch")."
}

do_enable() {
  DURATION_RAW="${AI_SUDO_DEFAULT_DURATION:-30m}"
  UNTIL_DISABLED=0
  ALLOW_SPECS=("ALL")

  local saw_allow=0

  while (( $# > 0 )); do
    case "$1" in
      -m|--minutes|-d|--duration)
        [[ $# -ge 2 ]] || die "$1 needs a value. Example: --duration 30m"
        DURATION_RAW="$2"
        shift 2
        ;;
      --allow)
        [[ $# -ge 2 ]] || die "--allow needs a sudoers command spec."
        if (( ! saw_allow )); then
          ALLOW_SPECS=()
          saw_allow=1
        fi
        ALLOW_SPECS+=("$2")
        shift 2
        ;;
      --all)
        ALLOW_SPECS=("ALL")
        saw_allow=0
        shift
        ;;
      --user)
        [[ $# -ge 2 ]] || die "--user needs a username."
        TARGET_USER="$2"
        shift 2
        ;;
      --until-disabled|--forever|--no-timer)
        UNTIL_DISABLED=1
        shift
        ;;
      -y|--yes)
        ASSUME_YES=1
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --quiet)
        QUIET=1
        shift
        ;;
      --no-color)
        COLOR_OFF=1
        setup_colors
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown enable option: $1"
        ;;
    esac
  done

  refresh_context
  validate_allow_specs "${ALLOW_SPECS[@]}"

  local minutes="$(duration_to_minutes "$DURATION_RAW")"

  banner
  warn "This grants passwordless sudo to '$TARGET_USER' for: ${(j:, :)ALLOW_SPECS}"

  if (( UNTIL_DISABLED )); then
    confirm "Enable without an auto-removal timer?" || die "Cancelled."
  elif (( minutes > 240 )); then
    confirm "Enable for $(format_duration "$minutes")? That is longer than 4 hours." || die "Cancelled."
  fi

  install_sudoers_rule

  if (( DRY_RUN )); then
    return 0
  fi

  if (( UNTIL_DISABLED )); then
    cancel_existing_timer_quietly
    save_state "" "until-disabled"
    warn "Enabled until you run: $SCRIPT_NAME disable"
  else
    schedule_removal "$minutes"
  fi

  "$SUDO_CMD" -k >/dev/null 2>&1 || true

  if [[ "${(j:, :)ALLOW_SPECS}" == "ALL" ]]; then
    if "$SUDO_CMD" -n true >/dev/null 2>&1; then
      success "Passwordless sudo is active for '$TARGET_USER'."
    else
      warn "Rule installed, but 'sudo -n true' did not pass. Run '$SCRIPT_NAME doctor'."
    fi
  else
    success "Passwordless sudo allowlist is active for '$TARGET_USER'."
  fi

  row "Sudoers file" "$SUDOERS_FILE"
  row "Log file" "$LOG_FILE"
  info "Tell Claude/Codex: use 'sudo -n' and never ask for my sudo password."
}

do_disable() {
  while (( $# > 0 )); do
    case "$1" in
      --user)
        [[ $# -ge 2 ]] || die "--user needs a username."
        TARGET_USER="$2"
        shift 2
        ;;
      --quiet)
        QUIET=1
        shift
        ;;
      --no-color)
        COLOR_OFF=1
        setup_colors
        shift
        ;;
      *)
        die "Unknown disable option: $1"
        ;;
    esac
  done

  refresh_context
  load_state >/dev/null 2>&1 || true

  local unit="${STATE[unit]:-$UNIT}"

  banner
  info "Disabling temporary sudo for '$TARGET_USER'..."

  if [[ -n "$unit" ]]; then
    if is_macos; then
      "$SUDO_CMD" "$LAUNCHCTL_CMD" bootout "system/${unit}" >/dev/null 2>&1 || true
      "$SUDO_CMD" "$RM_CMD" -f -- "/Library/LaunchDaemons/${unit}.plist" >/dev/null 2>&1 || true
    else
      "$SUDO_CMD" "$SYSTEMCTL_CMD" stop "${unit}.timer" "${unit}.service" >/dev/null 2>&1 || true
      "$SUDO_CMD" "$SYSTEMCTL_CMD" reset-failed "${unit}.timer" "${unit}.service" >/dev/null 2>&1 || true
    fi
  fi

  if [[ -e "$SUDOERS_FILE" ]]; then
    run_sudo "$RM_CMD" -f -- "$SUDOERS_FILE" || die "Could not remove $SUDOERS_FILE"
  else
    info "No active sudoers drop-in found at $SUDOERS_FILE."
  fi

  if ! run_sudo "$VISUDO_CMD" -c >/dev/null; then
    die "sudoers validation failed after removal. Check sudoers manually."
  fi

  "$SUDO_CMD" -k >/dev/null 2>&1 || true
  clear_state

  success "Disabled. 'sudo -n' should now fail unless another rule/cache allows it."
}

do_status() {
  while (( $# > 0 )); do
    case "$1" in
      --user)
        [[ $# -ge 2 ]] || die "--user needs a username."
        TARGET_USER="$2"
        shift 2
        ;;
      --no-color)
        COLOR_OFF=1
        setup_colors
        shift
        ;;
      *)
        die "Unknown status option: $1"
        ;;
    esac
  done

  refresh_context
  load_state >/dev/null 2>&1 || true

  local active="no"
  [[ -e "$SUDOERS_FILE" ]] && active="yes"

  local mode="${STATE[mode]:-unknown}"
  local expires="${STATE[expires_epoch]:-}"
  local unit="${STATE[unit]:-$UNIT}"
  local timer_state="unknown"

  if is_macos; then
    if [[ -n "$unit" && -e "/Library/LaunchDaemons/${unit}.plist" ]]; then
      timer_state="scheduled"
    else
      timer_state="inactive"
    fi
  elif [[ -x "$SYSTEMCTL_CMD" && -n "$unit" ]]; then
    timer_state="$($SYSTEMCTL_CMD is-active "${unit}.timer" 2>/dev/null || print -r -- inactive)"
  fi

  local sudo_n="requires password / not allowed"
  if "$SUDO_CMD" -n true >/dev/null 2>&1; then
    sudo_n="works now"
  fi

  banner
  row "Target user" "$TARGET_USER"
  row "Drop-in active" "$active"
  row "Sudo -n check" "$sudo_n"
  row "Mode" "$mode"
  row "Timer" "$timer_state"
  row "Expires" "$(date_human "$expires")"
  row "Sudoers file" "$SUDOERS_FILE"
  row "State file" "$STATE_FILE"
  row "Log file" "$LOG_FILE"
}

do_logs() {
  local lines=80

  while (( $# > 0 )); do
    case "$1" in
      -n|--lines)
        [[ $# -ge 2 ]] || die "$1 needs a number."
        lines="$2"
        shift 2
        ;;
      --no-color)
        COLOR_OFF=1
        setup_colors
        shift
        ;;
      *)
        die "Unknown logs option: $1"
        ;;
    esac
  done

  [[ "$lines" == <-> ]] || die "--lines must be a number."

  banner
  if [[ ! -r "$LOG_FILE" ]]; then
    warn "No log file yet: $LOG_FILE"
    return 0
  fi

  tail -n "$lines" "$LOG_FILE"
}

do_doctor() {
  refresh_context
  banner
  row "Script" "$SCRIPT_NAME v$VERSION"
  row "OS" "$OS_NAME"
  row "Shell" "${ZSH_VERSION:-unknown zsh}"
  row "User" "$TARGET_USER"
  row "UID" "$TARGET_UID"
  row "State dir" "$STATE_DIR"
  row "Sudoers file" "$SUDOERS_FILE"
  print -r -- ""

  local missing=0
  local name path
  local -A checks=(
    sudo "$SUDO_CMD"
    visudo "$VISUDO_CMD"
    install "$INSTALL_CMD"
    rm "$RM_CMD"
    date "$DATE_CMD"
    mktemp "$MKTEMP_CMD"
  )

  if is_macos; then
    checks[launchctl]="$LAUNCHCTL_CMD"
  else
    checks[systemd-run]="$SYSTEMD_RUN_CMD"
    checks[systemctl]="$SYSTEMCTL_CMD"
  fi

  for name in ${(k)checks}; do
    path="${checks[$name]}"
    if [[ -x "$path" ]]; then
      row "$name" "${C_GREEN}ok${C_RESET}  $path"
    else
      row "$name" "${C_RED}missing${C_RESET}  expected $path"
      missing=1
    fi
  done

  print -r -- ""

  if [[ -d /etc/sudoers.d ]]; then
    row "/etc/sudoers.d" "${C_GREEN}ok${C_RESET}"
  else
    row "/etc/sudoers.d" "${C_RED}missing${C_RESET}"
    missing=1
  fi

  if "$SUDO_CMD" -n true >/dev/null 2>&1; then
    row "sudo -n" "${C_GREEN}works now${C_RESET}"
  else
    row "sudo -n" "requires password or no matching NOPASSWD rule"
  fi

  print -r -- ""

  if (( missing )); then
    if is_macos; then
      warn "Doctor found missing tools. Timed mode needs launchctl or the fallback sleeper."
    else
      warn "Doctor found missing tools. Timed mode needs systemd-run or the fallback sleeper."
    fi
    return 1
  fi

  success "Doctor checks look good."
}

# ---------- Menu ----------

menu_custom_duration() {
  print -rn -- "Duration ${C_DIM}(examples: 15m, 30m, 2h, 1d)${C_RESET}: "
  local raw=""
  IFS= read -r raw
  [[ -n "$raw" ]] || { warn "Cancelled."; return 0; }
  do_enable --duration "$raw"
}

menu_allowlist() {
  print -r -- ""
  print -r -- "${C_BOLD}Allowlist mode${C_RESET} grants only one sudoers command spec."
  print -r -- "Example: ${EXAMPLE_ALLOW_SPEC}"
  print -rn -- "Command spec: "

  local spec=""
  IFS= read -r spec
  [[ -n "$spec" ]] || { warn "Cancelled."; return 0; }

  print -rn -- "Duration ${C_DIM}(default 30m)${C_RESET}: "
  local raw=""
  IFS= read -r raw
  [[ -n "$raw" ]] || raw="30m"

  do_enable --duration "$raw" --allow "$spec"
}

menu_loop() {
  while true; do
    banner
    print -r -- "  ${C_BOLD}1${C_RESET}) Enable broad sudo for 15 minutes"
    print -r -- "  ${C_BOLD}2${C_RESET}) Enable broad sudo for 30 minutes"
    print -r -- "  ${C_BOLD}3${C_RESET}) Enable broad sudo for custom duration"
    print -r -- "  ${C_BOLD}4${C_RESET}) Enable allowlisted sudo command"
    print -r -- "  ${C_BOLD}5${C_RESET}) Enable broad sudo until disabled"
    print -r -- "  ${C_BOLD}6${C_RESET}) Disable now"
    print -r -- "  ${C_BOLD}7${C_RESET}) Status"
    print -r -- "  ${C_BOLD}8${C_RESET}) Show logs"
    print -r -- "  ${C_BOLD}9${C_RESET}) Doctor"
    print -r -- "  ${C_BOLD}0${C_RESET}) Quit"
    print -r -- ""
    print -rn -- "${C_BOLD}Choose:${C_RESET} "

    local choice=""
    IFS= read -r choice

    case "$choice" in
      1) do_enable --duration 15m; pause_menu ;;
      2) do_enable --duration 30m; pause_menu ;;
      3) menu_custom_duration; pause_menu ;;
      4) menu_allowlist; pause_menu ;;
      5) do_enable --until-disabled; pause_menu ;;
      6) do_disable; pause_menu ;;
      7) do_status; pause_menu ;;
      8) do_logs; pause_menu ;;
      9) do_doctor; pause_menu ;;
      0|q|Q|quit|exit) print -r -- "Bye."; return 0 ;;
      *) warn "Unknown option: $choice"; pause_menu ;;
    esac
  done
}

# ---------- Help and main ----------

usage() {
  cat <<USAGE
${SCRIPT_NAME} v${VERSION}

Temporary passwordless sudo for AI coding agents on macOS and Linux (Ubuntu/sudo-rs).

Usage:
  ${SCRIPT_NAME}                       Open the menu
  ${SCRIPT_NAME} menu                  Open the menu
  ${SCRIPT_NAME} enable [options]      Enable temporary sudo
  ${SCRIPT_NAME} timed <duration>      Enable broad sudo for a duration
  ${SCRIPT_NAME} disable               Remove the temporary rule now
  ${SCRIPT_NAME} status                Show status
  ${SCRIPT_NAME} logs [-n 80]          Show logs
  ${SCRIPT_NAME} doctor                Check dependencies

Enable options:
  -d, --duration <time>        Duration: 15m, 30m, 2h, 1d. Default: 30m
  -m, --minutes <time>         Alias for --duration
      --allow <sudoers-spec>   Allowlist one exact sudoers command spec
      --all                    Use broad NOPASSWD: ALL, the default
      --until-disabled         No timer; you must run disable manually
      --user <name>            Target a different local user
  -y, --yes                    Skip confirmation prompts
      --dry-run                Show what would happen without changing sudoers
      --no-color               Disable color output
      --quiet                  Reduce output

Examples:
  ${SCRIPT_NAME} enable --duration 30m
  ${SCRIPT_NAME} timed 15m
  ${SCRIPT_NAME} enable --duration 2h --yes
  ${SCRIPT_NAME} enable --allow "${EXAMPLE_ALLOW_SPEC}" --duration 30m
  ${SCRIPT_NAME} disable

Agent instruction to use after enabling:
  Use sudo -n for privileged commands. Never ask for or handle my sudo password.
USAGE
}

main() {
  while (( $# > 0 )); do
    case "$1" in
      --no-color)
        COLOR_OFF=1
        shift
        ;;
      --quiet)
        QUIET=1
        shift
        ;;
      -y|--yes)
        ASSUME_YES=1
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --)
        shift
        break
        ;;
      *)
        break
        ;;
    esac
  done

  setup_colors

  local cmd="${1:-menu}"
  if (( $# > 0 )); then
    shift
  fi

  case "$cmd" in
    menu) menu_loop "$@" ;;
    enable|on) do_enable "$@" ;;
    timed)
      local duration="${1:-30m}"
      if (( $# > 0 )); then
        shift
      fi
      do_enable --duration "$duration" "$@"
      ;;
    disable|off) do_disable "$@" ;;
    status) do_status "$@" ;;
    logs|log) do_logs "$@" ;;
    doctor) do_doctor "$@" ;;
    help|-h|--help) usage ;;
    *)
      error "Unknown command: $cmd"
      usage
      exit 2
      ;;
  esac
}

main "$@"
