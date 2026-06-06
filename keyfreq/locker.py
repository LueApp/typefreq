"""Detects when a screen locker is active so we DON'T record the unlock password.

Why this exists
---------------
The tracker reads `/dev/input/event*` at the kernel level. Screen lockers
(kscreenlocker_greet, swaylock, …) grab the keyboard at the compositor/X
level — they hide the password from other GUI clients, but they CANNOT hide
it from anyone reading raw input devices. Our daemon is in the `input` group,
so we see every keystroke even when the screen is locked. That includes the
user's PC password — which we absolutely must not capture.

Approach
--------
A small background thread polls /proc every POLL_INTERVAL_S looking for any
process whose argv[0] basename matches a known screen-locker. While such a
process exists, `is_locked()` returns True and Tracker drops every keystroke
(and clears any partial word buffer).

Trade-off: there's a window of up to POLL_INTERVAL_S between the locker
appearing and us noticing. 500 ms is comfortably less than the time most
users take to focus the password field and start typing. If you want a
tighter guarantee, lower POLL_INTERVAL_S — at the cost of a few extra
/proc scans per second.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("keyfreq.locker")

# Process basenames matched against argv[0] from /proc/PID/cmdline. We match
# on argv[0]'s basename (not /proc/PID/comm, which is truncated to 15 chars
# and silently drops "kscreenlocker_greet" to "kscreenlocker_g").
LOCKER_NAMES: frozenset[str] = frozenset({
    "kscreenlocker_greet",  # KDE Plasma (X11 + Wayland)
    "gnome-screensaver",    # GNOME (older sessions)
    "swaylock",             # sway / wlroots
    "hyprlock",             # Hyprland
    "i3lock",
    "xscreensaver",
    "light-locker",
    "xsecurelock",
    "physlock",
    "slock",
    "betterlockscreen",
})


class LockerMonitor:
    """Polls /proc for known screen lockers. Thread-safe, lock-free reads."""

    POLL_INTERVAL_S = 0.5

    def __init__(self) -> None:
        self._locked = False
        self.poll_count = 0
        self.error_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # First reading inline so is_locked() is correct from the very first
        # keystroke (otherwise we'd have a one-poll-interval gap on startup).
        self._locked = self._scan()
        self._thread = threading.Thread(
            target=self._run, name="keyfreq-locker", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_INTERVAL_S):
            self.poll_count += 1
            try:
                new = self._scan()
            except Exception:
                self.error_count += 1
                log.exception("locker scan failed")
                continue
            if new != self._locked:
                log.info(
                    "screen locker %s",
                    "appeared — pausing key capture" if new else "gone — resuming key capture",
                )
            self._locked = new

    @staticmethod
    def _scan() -> bool:
        """Return True iff any /proc/PID/cmdline argv[0] basename is a known locker."""
        try:
            entries = os.listdir("/proc")
        except OSError:
            return False
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    data = f.read()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if not data:
                continue
            argv0 = data.split(b"\x00", 1)[0]
            name = argv0.rsplit(b"/", 1)[-1].decode("utf-8", errors="replace")
            if name in LOCKER_NAMES:
                return True
        return False

    def is_locked(self) -> bool:
        return self._locked

    # Generic "is this a secure context where keys must be dropped?" hook.
    # Lets Tracker iterate over a list of heterogeneous guards uniformly.
    def is_active(self) -> bool:
        return self._locked

    def stop(self) -> None:
        self._stop.set()

    def debug_state(self) -> dict:
        return {
            "locked": self._locked,
            "poll_count": self.poll_count,
            "error_count": self.error_count,
        }
