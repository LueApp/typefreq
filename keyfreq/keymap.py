"""Mapping from evdev key codes to characters, with shift-state awareness.

We only care about letters, digits, and a few punctuation marks for word
boundary detection. Anything not in the map is treated as a non-word character
(which closes the current word).

Note on input methods: when an IME (fcitx5, ibus, rime, …) is active, the raw
keys you press are consumed by the IME and the composed result (e.g., Chinese
characters) is delivered to the focused app over a higher-level protocol.
Those composed characters never appear at /dev/input, so this map naturally
ignores all non-Latin typing — no special handling is required.
"""
from __future__ import annotations

# Lowercase character produced by the key when no modifier is held.
# Source: standard US QWERTY layout. evdev key names from linux/input-event-codes.h.
BASE: dict[str, str] = {
    "KEY_A": "a", "KEY_B": "b", "KEY_C": "c", "KEY_D": "d", "KEY_E": "e",
    "KEY_F": "f", "KEY_G": "g", "KEY_H": "h", "KEY_I": "i", "KEY_J": "j",
    "KEY_K": "k", "KEY_L": "l", "KEY_M": "m", "KEY_N": "n", "KEY_O": "o",
    "KEY_P": "p", "KEY_Q": "q", "KEY_R": "r", "KEY_S": "s", "KEY_T": "t",
    "KEY_U": "u", "KEY_V": "v", "KEY_W": "w", "KEY_X": "x", "KEY_Y": "y",
    "KEY_Z": "z",
    "KEY_1": "1", "KEY_2": "2", "KEY_3": "3", "KEY_4": "4", "KEY_5": "5",
    "KEY_6": "6", "KEY_7": "7", "KEY_8": "8", "KEY_9": "9", "KEY_0": "0",
    "KEY_MINUS": "-",  # in-word hyphen, e.g. "well-known"
    "KEY_APOSTROPHE": "'",  # contractions, e.g. "don't"
}

# Word boundary keys: typing one of these closes the current word.
BOUNDARY_KEYS = {
    "KEY_SPACE", "KEY_ENTER", "KEY_KPENTER",
    "KEY_DOT", "KEY_COMMA", "KEY_SEMICOLON", "KEY_SLASH",
    "KEY_LEFTBRACE", "KEY_RIGHTBRACE",
    "KEY_BACKSLASH", "KEY_GRAVE", "KEY_EQUAL",
    "KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN",
    "KEY_HOME", "KEY_END", "KEY_PAGEUP", "KEY_PAGEDOWN",
    "KEY_ESC",
}

BACKSPACE_KEYS = {"KEY_BACKSPACE"}
COMPLETION_KEYS = {"KEY_TAB"}
CANCEL_KEYS = {"KEY_C"}

# Arrow keys that, combined with Ctrl, move the caret by word (or paragraph
# for Up/Down in most editors). After such a chord, the next "word" the
# tracker assembles might just be an insertion in the middle of existing
# text, not a real standalone word — so we drop it.
WORD_NAV_KEYS = {"KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN"}

SHIFT_KEYS = {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}
CAPSLOCK_KEYS = {"KEY_CAPSLOCK"}

# Modifier keys whose presence we want to detect so we can ignore shortcuts.
CTRL_KEYS = {"KEY_LEFTCTRL", "KEY_RIGHTCTRL"}
ALT_KEYS = {"KEY_LEFTALT", "KEY_RIGHTALT"}
META_KEYS = {"KEY_LEFTMETA", "KEY_RIGHTMETA"}


def char_for(keyname: str, shift: bool, caps: bool) -> str | None:
    """Return the character for the key, applying shift/caps. None if not a letter/digit."""
    base = BASE.get(keyname)
    if base is None:
        return None
    if base.isalpha():
        # caps XOR shift inverts the case
        upper = caps ^ shift
        return base.upper() if upper else base
    return base
