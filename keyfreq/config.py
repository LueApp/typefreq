"""Runtime configuration. Override via env vars: KEYFREQ_DB, KEYFREQ_LANG, etc."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("KEYFREQ_DATA", Path.home() / ".local/share/keyfreq"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("KEYFREQ_DB", DATA_DIR / "keyfreq.db"))

# Spell-check language. pyspellchecker supports en, es, fr, pt, de, ru, ar, eu, lv, nl.
LANG = os.environ.get("KEYFREQ_LANG", "en")

# Word filtering thresholds.
MIN_WORD_LEN = int(os.environ.get("KEYFREQ_MIN_WORD_LEN", "2"))
MAX_WORD_LEN = int(os.environ.get("KEYFREQ_MAX_WORD_LEN", "30"))

# Minimum word length to trigger typo notifications (avoids noise on short words).
TYPO_MIN_LEN = int(os.environ.get("KEYFREQ_TYPO_MIN_LEN", "5"))

# Don't notify the same misspelling twice within this many seconds.
TYPO_COOLDOWN_SEC = int(os.environ.get("KEYFREQ_TYPO_COOLDOWN", "300"))

# Global rate limit: at most one notification per N seconds.
TYPO_RATE_LIMIT_SEC = float(os.environ.get("KEYFREQ_TYPO_RATE_LIMIT", "8"))

# After a typo is recorded, the user has this many seconds to retract it by
# hitting backspace. A retraction removes the typo row from the DB, undoes
# the suggestion's word-count increment, and dismisses the still-visible
# toast (if any). Set to 0 to disable retraction entirely.
TYPO_RETRACT_WINDOW_S = float(os.environ.get("KEYFREQ_TYPO_RETRACT_WINDOW_S", "3.0"))

# HTTP server bind. Default to localhost only.
HTTP_HOST = os.environ.get("KEYFREQ_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("KEYFREQ_PORT", "8788"))

# Comma-separated paths to skip when scanning /dev/input devices.
DEVICE_BLOCKLIST = set(
    p.strip() for p in os.environ.get("KEYFREQ_DEVICE_BLOCKLIST", "").split(",") if p.strip()
)

# If no keystroke has been seen for this many seconds, discard whatever
# partial word is in the buffer before processing the next key. Catches
# context switches we can't see directly (mouse clicks, window focus
# changes, thinking pauses). Default 1.5s: long enough that normal
# between-word pauses don't trigger it, short enough that any context
# switch resets state before it can pollute the next word.
IDLE_TIMEOUT_S = float(os.environ.get("KEYFREQ_IDLE_TIMEOUT_S", "1.5"))

# --- Overlay (typo notification) appearance ---------------------------------
# Where to anchor the toast. Options:
#   "cursor"        — near the mouse pointer (best proxy for "near typing").
#                      Requires xdotool for X11/XWayland; falls back to corner.
#   "bottom-right" "bottom-left" "top-right" "top-left" "bottom-center" "center"
OVERLAY_POSITION = os.environ.get("KEYFREQ_OVERLAY_POSITION", "cursor")

# Pixel offset from the anchor point.
OVERLAY_OFFSET_X = int(os.environ.get("KEYFREQ_OVERLAY_OFFSET_X", "16"))
OVERLAY_OFFSET_Y = int(os.environ.get("KEYFREQ_OVERLAY_OFFSET_Y", "20"))

# Visible duration before fade starts (ms).
OVERLAY_DURATION_MS = int(os.environ.get("KEYFREQ_OVERLAY_DURATION_MS", "3500"))

# Length of the fade-out animation (ms).
OVERLAY_FADE_MS = int(os.environ.get("KEYFREQ_OVERLAY_FADE_MS", "600"))

# Initial alpha (0.0 transparent, 1.0 opaque). Compositor must support alpha.
OVERLAY_ALPHA = float(os.environ.get("KEYFREQ_OVERLAY_ALPHA", "0.85"))

# Font size for the toast text.
OVERLAY_FONT_SIZE = int(os.environ.get("KEYFREQ_OVERLAY_FONT_SIZE", "12"))
