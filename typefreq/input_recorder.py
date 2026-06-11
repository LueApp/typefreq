"""Bounded debug log for input events.

The recorder is intentionally in-memory and disabled by default. It is for
short-lived diagnostics, not analytics storage.
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any


class InputRecorder:
    """Thread-safe bounded event recorder for keyboard/mouse debugging."""

    def __init__(self, limit: int = 2000) -> None:
        self.limit = max(1, int(limit))
        self._enabled = False
        self._entries: deque[dict[str, Any]] = deque(maxlen=self.limit)
        self._seq = 0
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = bool(value)

    def record(self, source: str, action: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._seq += 1
            self._entries.append(
                {
                    "seq": self._seq,
                    "ts": round(time.time(), 3),
                    "source": str(source),
                    "action": str(action),
                    "data": _json_safe(data or {}),
                }
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries)
            return {
                "enabled": self._enabled,
                "count": len(entries),
                "limit": self.limit,
                "max_seq": self._seq,
                "entries": entries,
            }

    def export_payload(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "generated_at": round(time.time(), 3),
            **snap,
        }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)
