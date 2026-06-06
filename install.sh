#!/usr/bin/env bash
# Bootstrap keyfreq: venv, deps, systemd unit, autostart.
# Idempotent — safe to re-run.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/venv"
PYTHON="${PYTHON:-python3}"
USER_NAME="${USER:?}"
if [[ $# -gt 0 ]]; then
  KEYFREQ_PORT="$1"
fi
KEYFREQ_PORT="${KEYFREQ_PORT:-8788}"
KEYFREQ_PUBLIC_SITE="${KEYFREQ_PUBLIC_SITE:-https://keyfreq.lue-app.com}"
KEYFREQ_ALLOWED_ORIGINS="${KEYFREQ_ALLOWED_ORIGINS:-$KEYFREQ_PUBLIC_SITE,http://localhost:4321,http://127.0.0.1:4321,http://localhost:4325,http://127.0.0.1:4325}"
APT_PACKAGES=(
  python3-venv
  python3-dev
  python3-tk
  build-essential
  xdotool
  libnotify-bin
  python3-gi
  gir1.2-atspi-2.0
)

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

if ! [[ "$KEYFREQ_PORT" =~ ^[0-9]+$ ]] || (( KEYFREQ_PORT < 1024 || KEYFREQ_PORT > 65535 )); then
  die "KEYFREQ_PORT must be a number from 1024 to 65535. Example: ./install.sh 8789"
fi

run_as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

sed_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

install_ubuntu_packages() {
  if [[ "${KEYFREQ_SKIP_APT:-0}" == "1" ]]; then
    warn "Skipping apt dependency installation because KEYFREQ_SKIP_APT=1"
    return
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found. Continuing with local checks only."
    return
  fi
  say "Installing common Ubuntu dependencies"
  if ! run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update; then
    warn "apt-get update failed. Continuing with dependency checks."
    return
  fi
  if ! run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PACKAGES[@]}"; then
    warn "apt-get install failed. Continuing with dependency checks."
  fi
}

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "Do not run install.sh with sudo. Run it as your desktop user; the script will use sudo only for apt and group setup."
fi

install_ubuntu_packages

# 1. Check input group. The systemd unit wraps Python with `sg input`, so
# membership in /etc/group is enough; the current shell/systemd user-manager
# does not need to have refreshed its supplemental group list yet.
WILL_START=1
if id -nG | tr ' ' '\n' | grep -qx input; then
  : # (a) all good
elif getent group input | awk -F: -v u="$USER_NAME" 'BEGIN{r=1} {n=split($4,a,","); for(i=1;i<=n;i++) if (a[i]==u) r=0} END{exit r}'; then
  say "$USER_NAME is already listed in the input group"
elif getent group input >/dev/null 2>&1; then
  say "Adding $USER_NAME to the input group so keyfreq can read keyboard events"
  run_as_root usermod -aG input "$USER_NAME"
  say "Keyboard permission added. The service wrapper will use it immediately."
else
  die "The 'input' group does not exist on this system. keyfreq currently targets Ubuntu-style input permissions."
fi

# 2. Ensure pip, venv, Python dev headers (for evdev build), and tkinter (for the overlay).
if ! "$PYTHON" -m venv --help >/dev/null 2>&1; then
  die "python3-venv is missing. Install it with:  sudo apt install python3-venv"
fi
PYVER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! [[ -f "/usr/include/python${PYVER}/Python.h" ]] && \
   ! [[ -f "/usr/include/python${PYVER}m/Python.h" ]]; then
  die "Python headers missing — evdev cannot compile. Install with:
    sudo apt install python${PYVER}-dev  (or python3-dev)"
fi
if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
  die "tkinter is missing — the typo overlay needs it. Install with:
    sudo apt install python3-tk  (and possibly python${PYVER}-tk)"
fi
if ! command -v xdotool >/dev/null 2>&1; then
  warn "xdotool not found — the typo toast can't anchor near the mouse pointer"
  warn "  and will fall back to a screen corner. Install with:"
  warn "    sudo apt install xdotool"
fi
if ! command -v notify-send >/dev/null 2>&1; then
  warn "libnotify (notify-send) not found — used as the fallback if Tk fails."
  warn "    sudo apt install libnotify-bin"
fi
# AT-SPI for caret-aware positioning.
if ! "$PYTHON" -c "import gi; gi.require_version('Atspi','2.0'); from gi.repository import Atspi" >/dev/null 2>&1; then
  warn "AT-SPI bindings not found — typo toast won't follow your text caret."
  warn "Install with:  sudo apt install python3-gi gir1.2-atspi-2.0"
fi
if command -v gsettings >/dev/null 2>&1; then
  if [[ "$(gsettings get org.gnome.desktop.interface toolkit-accessibility 2>/dev/null)" != "true" ]]; then
    warn "GNOME 'toolkit-accessibility' is OFF. AT-SPI events won't fire until you:"
    warn "    gsettings set org.gnome.desktop.interface toolkit-accessibility true"
    warn "and then restart the apps you'll type in (so they pick up the bridge)."
  fi
fi
# IME (fcitx5) — used to gate tracking during pinyin composition.
if pgrep -x fcitx5 >/dev/null 2>&1; then
  say "fcitx5 detected — pinyin/IME composition will be skipped automatically"
elif pgrep -x ibus-daemon >/dev/null 2>&1; then
  warn "ibus detected — IME gating is currently only implemented for fcitx5"
fi

if [[ ! -d "$VENV" ]]; then
  say "Creating venv at $VENV"
  "$PYTHON" -m venv --system-site-packages "$VENV"
fi

say "Installing dependencies"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r "$DIR/requirements.txt"

# 3. Render and install the systemd user unit.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT_FILE="$UNIT_DIR/keyfreq.service"

say "Writing $UNIT_FILE"
sed \
  -e "s|__INSTALL_DIR__|$(sed_replacement "$DIR")|g" \
  -e "s|__KEYFREQ_PORT__|$(sed_replacement "$KEYFREQ_PORT")|g" \
  -e "s|__KEYFREQ_PUBLIC_SITE__|$(sed_replacement "$KEYFREQ_PUBLIC_SITE")|g" \
  -e "s|__KEYFREQ_ALLOWED_ORIGINS__|$(sed_replacement "$KEYFREQ_ALLOWED_ORIGINS")|g" \
  "$DIR/systemd/keyfreq.service" > "$UNIT_FILE"

# 4. Enable and (re)start.
say "Reloading systemd user units"
systemctl --user daemon-reload

say "Enabling autostart"
systemctl --user enable keyfreq.service >/dev/null

if [[ "$WILL_START" -eq 1 ]]; then
  say "Starting keyfreq"
  systemctl --user restart keyfreq.service
  sleep 1
  if systemctl --user is-active --quiet keyfreq.service; then
    say "keyfreq is running."
    say "Open the public dashboard: https://keyfreq.lue-app.com"
    say "Local fallback dashboard: http://127.0.0.1:$KEYFREQ_PORT"
    say "Logs:  journalctl --user -u keyfreq -f"
  else
    warn "keyfreq did not start. Inspect with:"
    warn "    journalctl --user -u keyfreq -n 50 --no-pager"
  fi
else
  say "Installed and enabled, NOT started."
  say "After your next login, the service will start automatically."
  say "Then open: https://keyfreq.lue-app.com"
  say "Configured local service port: $KEYFREQ_PORT"
fi
