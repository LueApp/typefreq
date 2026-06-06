#!/usr/bin/env bash
# Bootstrap keyfreq: venv, deps, systemd unit, autostart.
# Idempotent — safe to re-run.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/venv"
PYTHON="${PYTHON:-python3}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Check input group. Three states:
#    (a) effective member in current shell  -> ready to start now
#    (b) in /etc/group but not effective    -> need to relogin before start
#    (c) not in group at all                -> tell user to add + relogin
WILL_START=1
if id -nG | tr ' ' '\n' | grep -qx input; then
  : # (a) all good
elif getent group input | awk -F: -v u="$USER" 'BEGIN{r=1} {n=split($4,a,","); for(i=1;i<=n;i++) if (a[i]==u) r=0} END{exit r}'; then
  warn "You are in the 'input' group in /etc/group, but the current session"
  warn "hasn't picked it up yet. systemd --user is also stale. The unit will be"
  warn "installed and enabled, but the service won't be (re)started now."
  warn "Log out and back in (or run: loginctl terminate-user \"\$USER\") to activate."
  WILL_START=0
else
  warn "You are NOT in the 'input' group — evdev cannot read /dev/input/event*."
  warn "Run:  sudo usermod -aG input \"\$USER\""
  warn "Then log out and back in (or reboot), and re-run this script."
  read -r -p "Install the unit anyway (won't start it)? [y/N] " yn
  case "$yn" in [yY]*) WILL_START=0 ;; *) exit 1 ;; esac
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
  "$PYTHON" -m venv "$VENV"
fi

say "Installing dependencies"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r "$DIR/requirements.txt"

# 3. Render and install the systemd user unit.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT_FILE="$UNIT_DIR/keyfreq.service"

say "Writing $UNIT_FILE"
sed "s|__INSTALL_DIR__|$DIR|g" "$DIR/systemd/keyfreq.service" > "$UNIT_FILE"

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
    say "keyfreq is running. Dashboard: http://127.0.0.1:8788"
    say "Logs:  journalctl --user -u keyfreq -f"
  else
    warn "keyfreq did not start. Inspect with:"
    warn "    journalctl --user -u keyfreq -n 50 --no-pager"
  fi
else
  say "Installed and enabled, NOT started."
  say "After your next login, the service will start automatically on the"
  say "graphical session, and the dashboard will be at http://127.0.0.1:8788"
fi
