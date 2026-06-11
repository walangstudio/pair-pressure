#!/usr/bin/env bash
# Bootstrap installer for pair-pressure on POSIX (macOS / Linux).
#
# Detects Python + a package installer (uv > pipx > pip), sources the
# pair-pressure code (uses an existing clone or clones from GitHub),
# installs the package, then runs the pp-setup wizard.
#
# Safe to re-run: if pair-pressure is already installed, routes to the
# upgrade flow in pp-setup.
#
# Flags:
#   --no-config           skip the wizard
#   --clone-to <path>     override default clone target (~/pair-pressure)
#   --installer uv|pipx|pip   force a specific installer
#   --bin-name <name>     installed binary name (default: pp)
#   --reinstall           force full fresh wizard (skip upgrade detection)
#   --uninstall           remove package + skill + slash commands + env vars
#   --keep-settings       with --uninstall, leave settings.local.json alone
#   --yes                 with --uninstall, skip the confirmation prompt
#   --dev                 editable install (keep source clone alive; only for
#                         contributors developing pair-pressure itself)
#
# Requires: bash, python3 (>=3.9), git. One of: uv, pipx, pip.

set -eu

NO_CONFIG=0
CLONE_TO=""
FORCE_INSTALLER=""
BIN_NAME="pp"
REINSTALL=0
UNINSTALL=0
KEEP_SETTINGS=0
YES=0
DEV=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-config)     NO_CONFIG=1; shift ;;
    --clone-to)      CLONE_TO="$2"; shift 2 ;;
    --installer)     FORCE_INSTALLER="$2"; shift 2 ;;
    --bin-name)      BIN_NAME="$2"; shift 2 ;;
    --reinstall)     REINSTALL=1; shift ;;
    --uninstall)     UNINSTALL=1; shift ;;
    --keep-settings) KEEP_SETTINGS=1; shift ;;
    --yes)           YES=1; shift ;;
    --dev)           DEV=1; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

# Find a python interpreter for the JSON edit during uninstall.
pick_python() {
  if   command -v python3 >/dev/null 2>&1; then echo python3
  elif command -v python  >/dev/null 2>&1; then echo python
  else echo ""; fi
}

uninstall_flow() {
  echo "==> pair-pressure uninstall"
  if [[ "$YES" -ne 1 ]]; then
    cat <<EOF
This will:
  - Uninstall the pair-pressure package via uv / pipx / pip (whichever owns it)
  - Remove the skill at $HOME/.claude/skills/pair-pressure
  - Remove slash commands at $HOME/.claude/commands/pp-chat
EOF
    if [[ "$KEEP_SETTINGS" -ne 1 ]]; then
      echo "  - Clear PAIR_PRESSURE_* env vars from settings.local.json (backed up to .bak)"
    fi
    cat <<EOF

It will NOT touch:
  - The tooling repo at $(cd "$(dirname "$0")" && pwd)
  - Your chat repo data (wherever PAIR_PRESSURE_REPO points)

EOF
    printf "Proceed? [y/N]: "
    read -r RESP
    case "$RESP" in
      y|Y|yes) ;;
      *) echo "Cancelled."; exit 0 ;;
    esac
  fi

  echo "==> uninstalling package"
  if have uv;   then uv tool uninstall pair-pressure   >/dev/null 2>&1 && echo "  uv tool: removed" || true; fi
  if have pipx; then pipx uninstall pair-pressure      >/dev/null 2>&1 && echo "  pipx:    removed" || true; fi
  if have pip; then
    pip uninstall -y pair-pressure >/dev/null 2>&1 && echo "  pip:     removed" || true
  else
    # Fall back to `<python> -m pip` when `pip` isn't directly on PATH
    # (otherwise the pip branch silently skips and orphans the install).
    local py_un
    py_un="$(pick_python)"
    if [[ -n "$py_un" ]]; then
      "$py_un" -m pip uninstall -y pair-pressure >/dev/null 2>&1 && echo "  pip (via $py_un -m): removed" || true
    fi
  fi

  echo "==> removing Claude Code wiring"
  local skill="$HOME/.claude/skills/pair-pressure"
  if [[ -L "$skill" ]] || [[ -e "$skill" ]]; then
    rm -rf "$skill"
    echo "  removed skill at $skill"
  fi
  local cmds="$HOME/.claude/commands/pp-chat"
  if [[ -d "$cmds" ]]; then
    rm -rf "$cmds"
    echo "  removed slash commands at $cmds"
  fi

  if [[ "$KEEP_SETTINGS" -ne 1 ]]; then
    echo "==> cleaning Claude Code settings + shell profiles"
    local py
    py="$(pick_python)"
    if [[ -n "$py" ]]; then
      # Clean both settings files in one Python invocation.
      "$py" - "$HOME/.claude/settings.local.json" "$HOME/.claude/settings.json" <<'PYEOF'
import json, sys, shutil, pathlib
for arg in sys.argv[1:]:
    p = pathlib.Path(arg)
    if not p.is_file(): continue
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"  {p.name} is not valid JSON; skipping")
        continue
    env = data.get("env", {}) or {}
    changed = False
    for k in ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_AUTHOR",
              "PAIR_PRESSURE_ALIAS", "PAIR_PRESSURE_SERVER"):
        if k in env:
            env.pop(k); changed = True
    if changed:
        shutil.copy(p, str(p) + ".bak")
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  cleared PAIR_PRESSURE_* from {p.name} (backup: {p.name}.bak)")
PYEOF
    else
      echo "  python not found; settings files left as-is"
    fi

    # Strip the marker-wrapped block from shell rc files.
    local begin="# >>> pair-pressure env vars (pp-install) >>>"
    local end="# <<< pair-pressure env vars <<<"
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      [[ -f "$rc" ]] || continue
      if grep -qF "$begin" "$rc"; then
        cp "$rc" "$rc.bak"
        # sed in-place delete from begin marker through end marker
        # (POSIX-portable form: write to temp + mv).
        awk -v b="$begin" -v e="$end" '
          $0 ~ b { skip = 1; next }
          skip && $0 ~ e { skip = 0; next }
          !skip { print }
        ' "$rc.bak" > "$rc"
        echo "  removed pair-pressure block from $(basename "$rc") (backup: $(basename "$rc").bak)"
      fi
    done
  fi

  echo ""
  echo "Uninstall complete."
  echo "The tooling repo at $(cd "$(dirname "$0")" && pwd) is untouched -- delete manually if you want it gone too."
  exit 0
}

if [[ "$UNINSTALL" -eq 1 ]]; then uninstall_flow; fi

# ---- Phase 0: preflight ----
echo "==> pair-pressure installer (POSIX)"

if have python3; then PYTHON=python3
elif have python; then PYTHON=python
else
  echo "Python 3.9+ not found. Install from https://python.org and re-run." >&2
  exit 1
fi

if ! have git; then
  cat >&2 <<'EOF'
git not found on PATH. pair-pressure is a thin layer over git; every read
and write shells out, so git is a hard requirement.

Install one of:
  Debian/Ubuntu:  sudo apt install git
  Fedora/RHEL:    sudo dnf install git
  Arch:           sudo pacman -S git
  macOS:          brew install git   (or `xcode-select --install`)
  Source/docs:    https://git-scm.com/download

Then reopen this shell and re-run ./install.sh.
EOF
  exit 1
fi

PICKED="$FORCE_INSTALLER"
if [[ -z "$PICKED" ]]; then
  if have uv;   then PICKED=uv
  elif have pipx; then PICKED=pipx
  elif have pip;  then PICKED=pip
  else
    cat >&2 <<EOF
Need at least one of: uv (recommended), pipx, or pip.
Install uv:   curl -LsSf https://astral.sh/uv/install.sh | sh
Install pipx: $PYTHON -m pip install --user pipx
EOF
    exit 1
  fi
fi
echo "    python:    $PYTHON"
echo "    installer: $PICKED"

# ---- Phase 0.5: collision detection ----
if have pp; then
  EXISTING_PP="$(command -v pp)"
  VERSION_OUT="$(pp --version 2>&1 || true)"
  if ! echo "$VERSION_OUT" | grep -q 'pair-pressure'; then
    cat >&2 <<EOF

WARNING: a different \`pp\` is already on PATH at:
  $EXISTING_PP

It is NOT pair-pressure. Continuing will create a second \`pp\` (the one
that wins depends on PATH ordering).

To avoid the shadow, re-run with --bin-name pair-pp.
EOF
    printf "Proceed anyway? [y/N]: "
    read -r RESP
    case "$RESP" in
      y|Y|yes) ;;
      *) echo "Cancelled." ; exit 1 ;;
    esac
  fi
fi

# ---- Phase 1: source the code ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  REPO_ROOT="$SCRIPT_DIR"
  echo "    repo:      $REPO_ROOT (existing clone)"
else
  DEFAULT_DIR="${CLONE_TO:-$HOME/pair-pressure}"
  if [[ ! -d "$DEFAULT_DIR" ]]; then
    echo "==> cloning pair-pressure to $DEFAULT_DIR"
    git clone https://github.com/walangstudio/pair-pressure.git "$DEFAULT_DIR"
  else
    echo "    repo:      $DEFAULT_DIR (already cloned)"
  fi
  REPO_ROOT="$DEFAULT_DIR"
fi

# ---- Phase 2: install the package ----
echo "==> installing pair-pressure via $PICKED"
EDITABLE_FLAG=()
if [[ "$DEV" -eq 1 ]]; then
  EDITABLE_FLAG=(--editable)
fi
case "$PICKED" in
  uv)
    uv tool install "${EDITABLE_FLAG[@]}" "$REPO_ROOT" --reinstall
    uv tool update-shell >/dev/null 2>&1 || true
    ;;
  pipx)
    pipx install "${EDITABLE_FLAG[@]}" "$REPO_ROOT" --force
    pipx ensurepath >/dev/null 2>&1 || true
    ;;
  pip)
    "$PYTHON" -m pip install --user "${EDITABLE_FLAG[@]}" "$REPO_ROOT" --upgrade
    ;;
esac

# ---- Phase 2.5: locate the pp-setup wizard ----
# uv/pipx put the new console script in a bin dir that isn't necessarily on
# the current shell's PATH yet. Probe the known locations directly instead
# of relying on the shell to have rehashed.
WIZARD_BIN=""
case "$PICKED" in
  uv)
    # `uv tool dir` is the parent of all uv tool installs; the bin lives at
    # <dir>/pair-pressure/bin/pp-setup. Newer uv also symlinks into
    # `uv tool dir --bin` (typically ~/.local/bin).
    UV_TOOL_DIR="$(uv tool dir 2>/dev/null || true)"
    UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
    for cand in \
        "${UV_BIN_DIR%/}/pp-setup" \
        "${UV_TOOL_DIR%/}/pair-pressure/bin/pp-setup" \
        "$HOME/.local/bin/pp-setup"; do
      [[ -n "$cand" && -x "$cand" ]] && { WIZARD_BIN="$cand"; break; }
    done
    ;;
  pipx)
    for cand in "$HOME/.local/bin/pp-setup"; do
      [[ -x "$cand" ]] && { WIZARD_BIN="$cand"; break; }
    done
    ;;
  pip)
    USERBASE="$("$PYTHON" -m site --user-base 2>/dev/null || true)"
    for cand in "${USERBASE%/}/bin/pp-setup" "$HOME/.local/bin/pp-setup"; do
      [[ -n "$cand" && -x "$cand" ]] && { WIZARD_BIN="$cand"; break; }
    done
    ;;
esac

# If we couldn't find an absolute path, fall back to whatever's on PATH.
if [[ -z "$WIZARD_BIN" ]] && have pp-setup; then
  WIZARD_BIN="$(command -v pp-setup)"
fi
if [[ -z "$WIZARD_BIN" ]] && have pp-install; then
  WIZARD_BIN="$(command -v pp-install)"
fi

# ---- Phase 3: wizard ----
if [[ "$NO_CONFIG" -eq 1 ]]; then
  echo ""
  echo "Package installed. --no-config set; skipping wizard."
  echo "Run \`pp-setup\` later to configure env vars + skill + slash commands."
  exit 0
fi

if [[ -z "$WIZARD_BIN" ]]; then
  echo ""
  echo "Package installed but pp-setup isn't on PATH and couldn't be located."
  echo "Restart your shell and run \`pp-setup\` manually."
  exit 0
fi

echo ""
WIZARD_ARGS=()
[[ "$REINSTALL" -eq 1 ]] && WIZARD_ARGS+=('--reinstall')
if [[ "$BIN_NAME" != "pp" ]]; then WIZARD_ARGS+=('--bin-name' "$BIN_NAME"); fi

echo "==> launching pp-setup wizard ($WIZARD_BIN${WIZARD_ARGS[*]:+ }${WIZARD_ARGS[*]})"
exec "$WIZARD_BIN" "${WIZARD_ARGS[@]}"
