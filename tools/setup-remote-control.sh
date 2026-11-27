#!/usr/bin/env bash
# One-time setup so Claude projects can start Remote Control sessions on this
# Linux machine without anyone at the keyboard. Safe to run again.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/ausboss/comfy-agent/main/tools/setup-remote-control.sh)
#   ssh -t OTHER-BOX 'bash <(curl -fsSL https://raw.githubusercontent.com/ausboss/comfy-agent/main/tools/setup-remote-control.sh)'
#
# What it does (each step is skipped when already done):
#   1. Names the machine after its GPU when the hostname is a copy from another
#      box (an "arch5090" with an RTX 3090 becomes "arch3090"). Claude shows each
#      computer under its hostname, so two boxes with one name look like one.
#   2. Comments out variables that turn Remote Control off or get in its way
#      (DISABLE_TELEMETRY, DO_NOT_TRACK, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC,
#      CLAUDE_CODE_DISABLE_FEATURE_FLAGS, DISABLE_GROWTHBOOK) in shell profiles,
#      environment.d and ~/.claude/settings.json. Every edited file is backed up.
#   3. Adds allow rules to ~/.claude/settings.json so sessions don't stop to ask
#      before each command, and marks the folder trusted for Remote Control.
#   4. Installs a systemd user service that runs `claude remote-control` in the
#      folder, starts at boot (linger) and restarts if it dies.
#   5. Prints a short check of all of the above.
#
# Options:
#   --dir PATH     folder to serve (default: ~/code/ausboss)
#   --name NAME    hostname to use instead of the GPU-based guess
#   --no-rename    never change the hostname
#   --dry-run      show what would change, change nothing
#   --uninstall    stop and remove the service (backups and settings stay)

set -uo pipefail

DIR="$HOME/code/ausboss"
WANT_NAME=""
NO_RENAME=0
DRY=0
UNINSTALL=0
UNIT_NAME="claude-remote-control.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$UNIT_NAME"
STAMP="$(date +%Y%m%d-%H%M%S)"
MARK="# disabled by setup-remote-control $STAMP:"

# Variables that stop or limit Remote Control. Cleared from profiles.
BLOCKERS=(DISABLE_TELEMETRY DO_NOT_TRACK CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
          CLAUDE_CODE_DISABLE_FEATURE_FLAGS DISABLE_GROWTHBOOK)
# Variables that switch Claude to another backend or a limited token. Left alone
# in profiles (a local model setup may need them) but kept out of the service.
AUTH_OVERRIDES=(ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
                CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX
                CLAUDE_CODE_USE_FOUNDRY)

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --dir=*) DIR="${1#*=}"; shift ;;
    --name) WANT_NAME="$2"; shift 2 ;;
    --name=*) WANT_NAME="${1#*=}"; shift ;;
    --no-rename) NO_RENAME=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) echo "usage: setup-remote-control.sh [--dir PATH] [--name NAME] [--no-rename] [--dry-run] [--uninstall]"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
DIR="$(cd "$DIR" 2>/dev/null && pwd -P || echo "$DIR")"

ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
did()  { printf '  \033[36mdone\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; WARNINGS=$((WARNINGS+1)); }
fail() { printf '  \033[31mfail\033[0m  %s\n' "$*"; FAILS=$((FAILS+1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
WARNINGS=0; FAILS=0
[ "$DRY" = 1 ] && echo "(dry run: nothing will be changed)"

# sudo only when needed, asked at most once.
SUDO_OK=""
need_sudo() {
  [ "$(id -u)" = 0 ] && return 0
  [ "$SUDO_OK" = 1 ] && return 0
  [ "$SUDO_OK" = 0 ] && return 1
  if sudo -n true 2>/dev/null || sudo -v; then SUDO_OK=1; return 0; fi
  SUDO_OK=0; return 1
}
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

find_claude() {
  local c
  c="$(command -v claude 2>/dev/null)"
  for c in "$c" "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /usr/local/bin/claude /usr/bin/claude; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
  step "Removing the Remote Control service"
  if [ "$DRY" = 1 ]; then echo "  would disable and delete $UNIT"; exit 0; fi
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null && did "stopped and disabled $UNIT_NAME"
  rm -f "$UNIT" && systemctl --user daemon-reload && did "removed $UNIT"
  echo "  Settings and *.bak-rc-* backups were left in place."
  exit 0
fi

# ---------------------------------------------------------------- preflight
step "Checking this machine"
CLAUDE="$(find_claude)" || { fail "claude is not installed or not on PATH"; exit 1; }
ok "claude: $CLAUDE ($("$CLAUDE" --version 2>/dev/null | head -1))"
command -v python3 >/dev/null || { fail "python3 is needed to edit settings files"; exit 1; }
command -v systemctl >/dev/null || { fail "systemd is needed for the background service"; exit 1; }
[ -d "$DIR" ] || { fail "folder $DIR does not exist (use --dir)"; exit 1; }
ok "folder: $DIR"

GPU="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | sed 's/^ *//;s/ *$//')"
[ -n "$GPU" ] && ok "GPU: $GPU" || warn "nvidia-smi found no GPU (naming falls back to the hostname)"

# Sign-in check without the overrides the service won't see either.
UNSET_ARGS=(); for v in "${BLOCKERS[@]}" "${AUTH_OVERRIDES[@]}"; do UNSET_ARGS+=(-u "$v"); done
AUTH_JSON="$(env "${UNSET_ARGS[@]}" "$CLAUDE" auth status 2>/dev/null)"
AUTH_LINE="$(printf '%s' "$AUTH_JSON" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("unknown"); sys.exit()
print(("in" if d.get("loggedIn") else "out")+" "+str(d.get("authMethod","?")))' 2>/dev/null)"
case "$AUTH_LINE" in
  "in oauth_token"|"in api_key"*|"in apiKey"*)
    fail "signed in with a token or API key ($AUTH_LINE); Remote Control needs a claude.ai login: run 'claude auth login'" ;;
  in*) ok "signed in (${AUTH_LINE#in })" ;;
  out*) fail "not signed in: run 'claude auth login' once, then run this again" ;;
  *) warn "could not read 'claude auth status'; check you are signed in" ;;
esac

# ---------------------------------------------------------------- 1. name
step "1. Machine name"
CUR="$(hostnamectl --static 2>/dev/null || hostname)"
NEW="$CUR"
if [ -n "$WANT_NAME" ]; then
  NEW="$WANT_NAME"
elif [ -n "$GPU" ]; then
  GPU_NUM="$(printf '%s' "$GPU" | grep -oE '[0-9]{4}' | head -1)"
  # Only fix names that follow the <prefix><gpu number> pattern, e.g. arch5090.
  if [ -n "$GPU_NUM" ] && [[ "$CUR" =~ ^(.*[^0-9])([0-9]{4})$ ]] && [ "${BASH_REMATCH[2]}" != "$GPU_NUM" ]; then
    NEW="${BASH_REMATCH[1]}${GPU_NUM}"
  fi
fi
if [ "$NEW" = "$CUR" ]; then
  ok "hostname '$CUR' already fits this machine"
elif [ "$NO_RENAME" = 1 ]; then
  warn "hostname '$CUR' looks wrong for a $GPU (expected '$NEW'); left as is (--no-rename)"
elif [ "$DRY" = 1 ]; then
  echo "  would rename '$CUR' -> '$NEW' (hostnamectl, plus 127.x lines in /etc/hosts)"
elif need_sudo; then
  as_root hostnamectl set-hostname "$NEW" && did "hostname '$CUR' -> '$NEW'"
  # Only loopback lines: a line pointing at the other machine must keep its name.
  if grep -qE "^(127\.|::1).*[[:space:]]$CUR([[:space:]]|$)" /etc/hosts 2>/dev/null; then
    as_root cp -p /etc/hosts "/etc/hosts.bak-rc-$STAMP"
    as_root sed -i -E "/^(127\.|::1)/ s/([[:space:]])$CUR([[:space:]]|$)/\1$NEW\2/g" /etc/hosts
    did "/etc/hosts loopback entry updated (backup /etc/hosts.bak-rc-$STAMP)"
  fi
  RENAMED=1
else
  fail "could not get sudo to rename '$CUR' -> '$NEW'"
fi

# ---------------------------------------------------------------- 2. env vars
step "2. Variables that block Remote Control"
BL_RE="$(IFS='|'; echo "${BLOCKERS[*]}")"
AO_RE="$(IFS='|'; echo "${AUTH_OVERRIDES[*]}")"
# Matches: VAR=..  export VAR=..  set -gx VAR ..  VAR DEFAULT=.. (pam_environment)
line_re() { echo "^[[:space:]]*(export[[:space:]]+|set[[:space:]]+-[A-Za-z]+[[:space:]]+|setenv[[:space:]]+)?($1)([=[:space:]]|$)"; }

USER_FILES=("$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"
            "$HOME/.zshrc" "$HOME/.zshenv" "$HOME/.zprofile" "$HOME/.zlogin" "$HOME/.xprofile"
            "$HOME/.pam_environment" "$HOME/.config/fish/config.fish")
for f in "$HOME"/.config/fish/conf.d/*.fish "$HOME"/.config/environment.d/*.conf "$HOME"/.config/plasma-workspace/env/*.sh; do
  [ -f "$f" ] && USER_FILES+=("$f")
done
SYS_FILES=(/etc/environment)
for f in /etc/profile.d/*.sh /etc/environment.d/*.conf; do [ -f "$f" ] && SYS_FILES+=("$f"); done

fix_file() {  # $1 file, $2 "user"|"root"
  local f="$1" who="$2" re hits
  [ -f "$f" ] || return 0
  re="$(line_re "$BL_RE")"
  hits="$(grep -nE "$re" "$f" 2>/dev/null)" || return 0
  if [ "$DRY" = 1 ]; then echo "  would comment out in $f:"; printf '%s\n' "$hits" | sed 's/^/      /'; return 0; fi
  local tmp; tmp="$(mktemp)"
  awk -v re="$re" -v mark="$MARK" '$0 ~ re { print mark " " $0; next } { print }' "$f" > "$tmp" || { rm -f "$tmp"; fail "could not edit $f"; return 0; }
  if [ "$who" = root ]; then
    need_sudo || { rm -f "$tmp"; fail "$f sets a blocker but sudo is unavailable"; return 0; }
    as_root cp -p "$f" "$f.bak-rc-$STAMP" && as_root sh -c 'cat "$1" > "$2"' _ "$tmp" "$f"
  else
    cp -p "$f" "$f.bak-rc-$STAMP" && cat "$tmp" > "$f"
  fi
  rm -f "$tmp"
  did "commented out in $f (backup $f.bak-rc-$STAMP):"
  printf '%s\n' "$hits" | sed 's/^/      /'
  CHANGED_ENV=1
}
CHANGED_ENV=0
for f in "${USER_FILES[@]}"; do fix_file "$f" user; done
for f in "${SYS_FILES[@]}"; do fix_file "$f" root; done
# The systemd user manager may hold a copy imported at login; the desktop app inherits it.
for v in "${BLOCKERS[@]}"; do
  if systemctl --user show-environment 2>/dev/null | grep -q "^$v="; then
    if [ "$DRY" = 1 ]; then echo "  would unset $v in the systemd user environment"
    else systemctl --user unset-environment "$v" && did "unset $v in the systemd user environment"; CHANGED_ENV=1; fi
  fi
done
[ "$CHANGED_ENV" = 0 ] && [ "$DRY" = 0 ] && ok "none set in profiles, environment.d or the systemd user environment"
# Report, don't touch: these may be there on purpose for local models.
for f in "${USER_FILES[@]}" "${SYS_FILES[@]}"; do
  [ -f "$f" ] || continue
  while IFS= read -r l; do
    warn "$f:${l%%:*} sets ${l#*:} (left as is; the service ignores it, but an interactive 'claude' with it can't use Remote Control)"
  done < <(grep -nE "$(line_re "$AO_RE")" "$f" 2>/dev/null)
done
STILL=""; for v in "${BLOCKERS[@]}"; do [ -n "${!v:-}" ] && STILL="$STILL $v"; done
[ -n "$STILL" ] && warn "this terminal still has$STILL set: open a new terminal (or log out and in) before running claude by hand"

# ---------------------------------------------------------------- 3. settings
step "3. Claude settings (allow rules, trust)"
DRY="$DRY" STAMP="$STAMP" DIR="$DIR" BLOCKERS="${BLOCKERS[*]}" python3 - <<'PY'
import json, os, shutil, sys, tempfile

home = os.path.expanduser("~")
dry = os.environ["DRY"] == "1"
stamp = os.environ["STAMP"]
folder = os.environ["DIR"]
blockers = os.environ["BLOCKERS"].split()

ALLOW = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit",
         "WebFetch", "WebSearch", "TodoWrite", "Task", "Agent"]
# A short safety net: these never run without a person approving.
DENY = ["Bash(rm -rf /)", "Bash(rm -rf /*)", "Bash(rm -rf ~)", "Bash(rm -rf ~/*)",
        "Bash(rm -rf $HOME)", "Bash(rm -rf $HOME/*)",
        "Bash(git push --force *)", "Bash(git push -f *)",
        "Bash(mkfs *)", "Bash(dd * of=/dev/*)"]

def load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        text = fh.read()
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  \033[31mfail\033[0m  {path} is not valid JSON ({e}); not touching it")
        sys.exit(0)

def save(path, data):
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-rc-{stamp}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rc-")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)

# ~/.claude/settings.json: allow/deny rules, no blocker vars in its env block.
p = os.path.join(home, ".claude", "settings.json")
s = load(p)
perms = s.setdefault("permissions", {})
changes = []
for key, rules in (("allow", ALLOW), ("deny", DENY)):
    cur = perms.setdefault(key, [])
    add = [r for r in rules if r not in cur]
    if add:
        cur.extend(add)
        changes.append(f"{len(add)} {key} rule(s)")
env = s.get("env") or {}
gone = [k for k in blockers if k in env]
for k in gone:
    del env[k]
if gone:
    changes.append("removed " + ", ".join(gone) + " from its env block")
for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
    if k in env:
        print(f"  \033[33mwarn\033[0m  {p} env block sets {k}: Remote Control won't work while it is set (left as is)")
if not changes:
    print(f"  \033[32mok\033[0m    {p} already has the rules")
elif dry:
    print(f"  would update {p}: " + "; ".join(changes))
else:
    save(p, s)
    print(f"  \033[36mdone\033[0m  {p}: " + "; ".join(changes) + f" (backup .bak-rc-{stamp})")

# ~/.claude.json: folder trusted, "Enable Remote Control?" answered, so the
# service never stops at a prompt nobody can see.
p = os.path.join(home, ".claude.json")
c = load(p)
changes = []
if not c.get("remoteDialogSeen"):
    c["remoteDialogSeen"] = True
    changes.append("Remote Control enabled")
proj = c.setdefault("projects", {}).setdefault(folder, {})
if not proj.get("hasTrustDialogAccepted"):
    proj["hasTrustDialogAccepted"] = True
    changes.append(f"{folder} trusted")
if not changes:
    print(f"  \033[32mok\033[0m    {p} already trusts {folder}")
elif dry:
    print(f"  would update {p}: " + "; ".join(changes))
else:
    save(p, c)
    print(f"  \033[36mdone\033[0m  {p}: " + "; ".join(changes) + f" (backup .bak-rc-{stamp})")
PY

# ---------------------------------------------------------------- 4. service
step "4. Background service"
# Another 'claude remote-control' already serving this folder makes the service
# fail with "already served". Stop terminal copies; the service replaces them.
for pid in $(pgrep -u "$(id -u)" -f 'claude( .*)? (remote-control|rc)( |$)' 2>/dev/null); do
  [ "$pid" = "$$" ] && continue
  MAINPID="$(systemctl --user show -p MainPID --value "$UNIT_NAME" 2>/dev/null)"
  [ "$pid" = "$MAINPID" ] && continue
  cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)"
  if [ "$cwd" = "$DIR" ]; then
    if [ "$DRY" = 1 ]; then echo "  would stop 'claude remote-control' (pid $pid) started by hand in $DIR"
    else kill "$pid" 2>/dev/null && did "stopped 'claude remote-control' (pid $pid) started by hand in $DIR; the service takes over"; fi
  fi
done

# The session's commands (nvidia-smi, git, python, ssh...) need your normal PATH.
SERVICE_PATH="$(printf '%s' "$(dirname "$CLAUDE"):$PATH" | awk -v RS=: -v ORS=: '!seen[$0]++' | sed 's/:$//; s/%/%%/g')"
UNIT_TEXT="[Unit]
Description=Claude Code Remote Control for $DIR
Documentation=https://code.claude.com/docs/en/remote-control
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=\"PATH=$SERVICE_PATH\"
UnsetEnvironment=${BLOCKERS[*]} ${AUTH_OVERRIDES[*]}
ExecStart=$CLAUDE remote-control
StandardInput=null
Restart=always
RestartSec=30
TimeoutStopSec=20

[Install]
WantedBy=default.target
"
if [ -f "$UNIT" ] && [ "$(cat "$UNIT")" = "$(printf '%s' "$UNIT_TEXT")" ]; then
  ok "$UNIT unchanged"; UNIT_CHANGED=0
elif [ "$DRY" = 1 ]; then
  echo "  would write $UNIT:"; printf '%s\n' "$UNIT_TEXT" | sed 's/^/      /'; UNIT_CHANGED=1
else
  mkdir -p "$UNIT_DIR"
  [ -f "$UNIT" ] && cp -p "$UNIT" "$UNIT.bak-rc-$STAMP"
  printf '%s' "$UNIT_TEXT" > "$UNIT"
  did "wrote $UNIT"; UNIT_CHANGED=1
fi

LINGER="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)"
if [ "$LINGER" = yes ]; then ok "linger on: the service starts at boot without a login"
elif [ "$DRY" = 1 ]; then echo "  would turn on linger for $USER"
elif loginctl enable-linger "$USER" 2>/dev/null || { need_sudo && as_root loginctl enable-linger "$USER"; }; then
  did "linger on: the service starts at boot without a login"
else fail "could not turn on linger; the service only runs while you are logged in"; fi

if [ "$DRY" = 0 ]; then
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT_NAME" >/dev/null 2>&1 && ok "enabled at boot"
  if [ "$UNIT_CHANGED" = 1 ] || [ "${RENAMED:-0}" = 1 ] || ! systemctl --user is-active -q "$UNIT_NAME"; then
    systemctl --user restart "$UNIT_NAME" && did "(re)started $UNIT_NAME"
  else
    ok "$UNIT_NAME already running"
  fi
fi

# ---------------------------------------------------------------- 5. check
step "5. Check"
if [ "$DRY" = 1 ]; then echo "  dry run: skipped"; exit 0; fi
STATE=""; LOG=""
for _ in $(seq 1 20); do
  sleep 1
  LOG="$(journalctl --user -u "$UNIT_NAME" --since "-2min" -o cat --no-pager 2>/dev/null | tail -40)"
  if printf '%s' "$LOG" | grep -qi 'already served'; then STATE=conflict; break; fi
  if printf '%s' "$LOG" | grep -qE 'Error|not trusted|requires a|not available|disabled by'; then STATE=error; break; fi
  systemctl --user is-active -q "$UNIT_NAME" || { STATE=error; break; }
done
[ -z "$STATE" ] && STATE=up
NAME_NOW="$(hostnamectl --static 2>/dev/null || hostname)"
ok "name: $NAME_NOW${GPU:+ ($GPU)}"
if systemctl --user is-active -q "$UNIT_NAME"; then ok "service running: $UNIT_NAME"; else fail "service not running"; fi
case "$STATE" in
  up) ok "Remote Control running for 20s with no errors: projects should list '$NAME_NOW ($(basename "$DIR"))'" ;;
  conflict)
    warn "another Claude (most likely the desktop app) already serves $DIR."
    echo "        In the desktop app: Settings > Claude Code > Remote Control, remove $DIR."
    echo "        The service retries every 30s and takes over as soon as the app lets go." ;;
  error) fail "Remote Control refused to start. Last lines:"; printf '%s\n' "$LOG" | tail -8 | sed 's/^/        /' ;;
esac
[ "${RENAMED:-0}" = 1 ] && echo "  note: renamed, so the old '$CUR' entry in the project goes offline; Tailscale keeps its own name until you rename it there."
[ "${CHANGED_ENV:-0}" = 1 ] && echo "  note: restart the Claude desktop app (or log out and in) so it drops the old variables."
echo
if [ "$FAILS" -gt 0 ]; then echo "Finished with $FAILS problem(s) above."; exit 1; fi
[ "$WARNINGS" -gt 0 ] && W=" ($WARNINGS warning(s) above)" || W=""
echo "All set$W. Logs: journalctl --user -u $UNIT_NAME -f   Remove: add --uninstall"
