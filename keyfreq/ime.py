"""Detect when an IME (fcitx5) is actively composing input.

Why we need this: even though an IME consumes raw keys at the compositor
level, the kernel-level evdev events still fire — /dev/input/event* sees
every physical key. If we don't gate, pinyin syllables like "nihao" leak
into our English word stats and get flagged as typos.

How: poll fcitx5 via `gdbus call ...CurrentInputMethod` from a background
thread. If the current input method does NOT start with "keyboard-", we
treat the user as composing and skip evdev word capture.

Why polling, not D-Bus signals: on Wayland (KDE Plasma in this case),
fcitx5 talks to apps via the Wayland text-input protocol, not D-Bus. So
`gdbus monitor --dest org.fcitx.Fcitx5` never sees any traffic during
composition. The Controller1 D-Bus interface is still queryable for state,
just doesn't emit per-keystroke signals.

The poll interval is short (250 ms) so a toggle into pinyin mode is
detected before more than a syllable's worth of keys can slip through.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from threading import Event, Lock, Thread

log = logging.getLogger("keyfreq.ime")

POLL_INTERVAL_S = 0.25
# Treat the last successful poll as authoritative for this long. If polling
# stalls (e.g. fcitx5 restarted), we revert to "not composing" rather than
# wedging the tracker open or shut.
STALE_AFTER_S = 2.0


def _fcitx5_running() -> bool:
    try:
        subprocess.run(
            ["pgrep", "-x", "fcitx5"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.5,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


class IMEMonitor:
    """Background-polls fcitx5's CurrentInputMethod.

    Public surface:
      * `is_composing()`           -> bool   (safe to call on the hot path)
      * `shutdown()`                          (stops the polling thread)
      * `debug_state()`            -> dict   (diagnostics for /api/debug/ime)
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._stop = Event()
        self._composing = False
        self._current_im: str | None = None
        self._last_ok_at = 0.0
        self._poll_count = 0
        self._error_count = 0
        self._last_error: str | None = None
        self._gdbus = shutil.which("gdbus")
        self._available = self._gdbus is not None and _fcitx5_running()
        self._thread: Thread | None = None
        if self._available:
            log.info("fcitx5 detected — polling CurrentInputMethod for IME gating")
            self._thread = Thread(target=self._loop, name="keyfreq-ime-poll", daemon=True)
            self._thread.start()
        else:
            log.info("no fcitx5 detected — tracking all keystrokes")

    # --- public API -------------------------------------------------

    def is_composing(self) -> bool:
        if not self._available:
            return False
        with self._lock:
            if (time.monotonic() - self._last_ok_at) > STALE_AFTER_S:
                return False
            return self._composing

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def debug_state(self) -> dict:
        with self._lock:
            return {
                "available": self._available,
                "current_im": self._current_im,
                "composing": self._composing,
                "poll_count": self._poll_count,
                "error_count": self._error_count,
                "last_error": self._last_error,
                "stale_for_s": round(max(0.0, time.monotonic() - self._last_ok_at), 2),
            }

    # --- internals --------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                with self._lock:
                    self._error_count += 1
                    self._last_error = repr(e)[:160]
            self._stop.wait(POLL_INTERVAL_S)

    def _poll_once(self) -> None:
        # Short timeout: if fcitx5 hangs, don't block the polling loop.
        r = subprocess.run(
            [
                self._gdbus, "call", "--session",
                "--dest", "org.fcitx.Fcitx5",
                "--object-path", "/controller",
                "--method", "org.fcitx.Fcitx.Controller1.CurrentInputMethod",
            ],
            capture_output=True, text=True, timeout=0.5,
            env=os.environ.copy(),
        )
        if r.returncode != 0:
            with self._lock:
                self._error_count += 1
                self._last_error = (r.stderr or "").strip()[:160]
            return
        m = re.search(r"'([^']+)'", r.stdout)
        if not m:
            with self._lock:
                self._error_count += 1
                self._last_error = f"unparseable: {r.stdout!r}"[:160]
            return
        im = m.group(1)
        # "keyboard-us", "keyboard-cn", etc. all mean plain key passthrough.
        # Anything else (pinyin, anthy, hangul, …) means the IM may compose.
        composing = not im.startswith("keyboard-")
        with self._lock:
            self._current_im = im
            self._composing = composing
            self._last_ok_at = time.monotonic()
            self._poll_count += 1
