"""Privacy filters for raw word candidates.

The goal: drop anything that looks like a password, token, code identifier, or
random keypress noise, while keeping ordinary written words.

Scope: this filter only accepts ASCII letters (A–Z, a–z) plus internal hyphen
and apostrophe. CJK characters (Chinese, Japanese, Korean) and other non-Latin
scripts are intentionally rejected here, but in practice they never even reach
this filter — input methods (fcitx, ibus, rime, etc.) consume the raw key
events and emit composed text via the Wayland text-input protocol, which
bypasses /dev/input entirely. So Chinese typing is naturally excluded; this
comment is here so a reader doesn't go looking for special handling.
"""
from __future__ import annotations

import math
import re

from .config import MAX_WORD_LEN, MIN_WORD_LEN

# Anything that isn't a letter, digit, hyphen, or apostrophe terminates a word.
_WORD_OK = re.compile(r"^[A-Za-z][A-Za-z'\-]*[A-Za-z]$|^[A-Za-z]$")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def normalize(raw: str) -> str | None:
    """Apply length, charset, and entropy filters. Return canonical form or None."""
    if not raw:
        return None

    # Length gate.
    if len(raw) < MIN_WORD_LEN or len(raw) > MAX_WORD_LEN:
        return None

    # Strip leading/trailing hyphens and apostrophes (typing artifacts).
    stripped = raw.strip("-'")
    if not stripped:
        return None

    # Reject if it contains digits — likely an identifier, version, or password.
    if any(ch.isdigit() for ch in stripped):
        return None

    # Require the canonical word shape.
    if not _WORD_OK.match(stripped):
        return None

    # Entropy gate: real English words have entropy ~2.5–3.5 bits/char for length>=6.
    # Random strings of mixed case score much higher.
    if len(stripped) >= 8 and _shannon_entropy(stripped.lower()) > 3.8:
        return None

    return stripped.lower()
