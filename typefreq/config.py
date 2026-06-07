"""Runtime configuration. Override via env vars: TYPEFREQ_DB, TYPEFREQ_LANG, etc."""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_DATA_DIR = Path.home() / ".local/share/typefreq"
_LEGACY_DATA_DIR = Path.home() / ".local/share/keyfreq"


def _env(name: str, default: str | Path) -> str | Path:
    """Read TYPEFREQ_* values, falling back to the old KEYFREQ_* names."""
    return os.environ.get(f"TYPEFREQ_{name}", os.environ.get(f"KEYFREQ_{name}", default))


def _default_data_dir() -> Path:
    if _LEGACY_DATA_DIR.exists() and not _DEFAULT_DATA_DIR.exists():
        return _LEGACY_DATA_DIR
    return _DEFAULT_DATA_DIR


DATA_DIR = Path(_env("DATA", _default_data_dir()))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_DB_NAME = "keyfreq.db" if DATA_DIR == _LEGACY_DATA_DIR else "typefreq.db"
DB_PATH = Path(_env("DB", DATA_DIR / _DEFAULT_DB_NAME))

# Spell-check language. pyspellchecker supports en, es, fr, pt, de, ru, ar, eu, lv, nl.
LANG = _env("LANG", "en")

# Word filtering thresholds.
MIN_WORD_LEN = int(_env("MIN_WORD_LEN", "2"))
MAX_WORD_LEN = int(_env("MAX_WORD_LEN", "30"))

# Minimum word length to trigger typo notifications (avoids noise on short words).
TYPO_MIN_LEN = int(_env("TYPO_MIN_LEN", "5"))

# Don't notify the same misspelling twice within this many seconds.
TYPO_COOLDOWN_SEC = int(_env("TYPO_COOLDOWN", "300"))

# Global rate limit: at most one notification per N seconds.
TYPO_RATE_LIMIT_SEC = float(_env("TYPO_RATE_LIMIT", "8"))

# After a typo is recorded, the user has this many seconds to retract it by
# hitting backspace. A retraction removes the typo row from the DB, undoes
# the suggestion's word-count increment, and dismisses the still-visible
# toast (if any). Set to 0 to disable retraction entirely.
TYPO_RETRACT_WINDOW_S = float(_env("TYPO_RETRACT_WINDOW_S", "3.0"))

# HTTP server bind. Default to localhost only.
HTTP_HOST = _env("HOST", "127.0.0.1")
HTTP_PORT = int(_env("PORT", "8788"))

# Public web UI allowed to read this local service from the user's browser.
# Keep this restricted: any allowed origin can read the user's local typing
# analytics while the service is running.
PUBLIC_SITE_URL = str(_env("PUBLIC_SITE", "https://typefreq.lue-app.com")).rstrip("/")
_DEFAULT_ALLOWED_ORIGINS = ",".join(
    [
        PUBLIC_SITE_URL,
        "https://keyfreq.lue-app.com",
        "http://localhost:4321",
        "http://127.0.0.1:4321",
        "http://localhost:4325",
        "http://127.0.0.1:4325",
    ]
)
HTTP_ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in str(_env("ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS)).split(",")
    if origin.strip()
}

# Comma-separated paths to skip when scanning /dev/input devices.
DEVICE_BLOCKLIST = set(
    p.strip() for p in str(_env("DEVICE_BLOCKLIST", "")).split(",") if p.strip()
)

# If no keystroke has been seen for this many seconds, discard whatever
# partial word is in the buffer before processing the next key. Catches
# context switches we can't see directly (mouse clicks, window focus
# changes, thinking pauses). Default 1.5s: long enough that normal
# between-word pauses don't trigger it, short enough that any context
# switch resets state before it can pollute the next word.
IDLE_TIMEOUT_S = float(_env("IDLE_TIMEOUT_S", "1.5"))

# --- Overlay (typo notification) appearance ---------------------------------
# Where to anchor the toast. Options:
#   "cursor"        — near the mouse pointer (best proxy for "near typing").
#                      Requires xdotool for X11/XWayland; falls back to corner.
#   "bottom-right" "bottom-left" "top-right" "top-left" "bottom-center" "center"
OVERLAY_POSITION = _env("OVERLAY_POSITION", "cursor")

# Pixel offset from the anchor point.
OVERLAY_OFFSET_X = int(_env("OVERLAY_OFFSET_X", "16"))
OVERLAY_OFFSET_Y = int(_env("OVERLAY_OFFSET_Y", "20"))

# Visible duration before fade starts (ms).
OVERLAY_DURATION_MS = int(_env("OVERLAY_DURATION_MS", "3500"))

# Length of the fade-out animation (ms).
OVERLAY_FADE_MS = int(_env("OVERLAY_FADE_MS", "600"))

# Initial alpha (0.0 transparent, 1.0 opaque). Compositor must support alpha.
OVERLAY_ALPHA = float(_env("OVERLAY_ALPHA", "0.85"))

# Font size for the toast text.
OVERLAY_FONT_SIZE = int(_env("OVERLAY_FONT_SIZE", "12"))
