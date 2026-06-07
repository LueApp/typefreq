"""Detects when a privilege-escalation prompt might be capturing a password.

Why
---
Polkit GUI dialogs (`polkit-kde-authentication-agent-1`, the GNOME equivalent,
…) and TTY/console prompts (`sudo`, `pkexec`, `pkttyagent`, `*-askpass`) all
read the user's PC password from the keyboard. Since our tracker reads
`/dev/input/event*` at the kernel level, it sees those keystrokes too — and
we must NOT record them.

Two complementary signals
-------------------------
The polkit GUI agent runs continuously, so "is the agent process alive?"
isn't useful. Instead we combine:

1. **Process scan** (every POLL_INTERVAL_S): looks for processes whose argv[0]
   basename matches a known auth helper that ONLY exists during/around
   password entry — `sudo`, `pkexec`, `pkttyagent`, `polkit-agent-helper-1`,
   the `askpass` family. Catches all TTY-based auth and the moment a GUI
   dialog submits a password.

2. **D-Bus subscription** (background `dbus-monitor` subprocess): listens
   on the system bus for method calls on the
   `org.freedesktop.PolicyKit1.AuthenticationAgent` interface. polkitd
   sends `BeginAuthentication` to the registered agent the moment a
   privileged action triggers a dialog. Catches the GUI dialog while it's
   open (which the process scan can't, because the agent is always running).

Either signal sets a single `active_until` timestamp, refreshed on every
observation. `is_active()` returns True while the timestamp is in the future.
DBUS_GRACE_S is generous so the lock survives the time the user spends
focusing the password field, typing, and submitting.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("typefreq.polkit")

# Process basenames that strongly suggest a password is being typed RIGHT NOW.
# These are short-lived helpers / wrappers, distinct from always-running agents.
AUTH_PROCESS_NAMES: frozenset[str] = frozenset({
    "sudo",
    "pkexec",
    "pkttyagent",
    "polkit-agent-helper-1",
    # Askpass variants across desktop environments.
    "ksshaskpass",
    "ssh-askpass",
    "gnome-ssh-askpass",
    "lxqt-openssh-askpass",
    "seahorse-askpass",
    "x11-ssh-askpass",
    "git-credential-manager",  # may prompt for credentials
})

# How long after the last observed auth signal to keep the guard active.
# Long enough that a user can focus the dialog, type, and submit — short
# enough that real typing isn't blocked for ages after a quick cancel.
GRACE_S = 30.0


class PolkitMonitor:
    """Detects polkit dialogs and TTY auth helpers. Thread-safe, lock-free reads."""

    POLL_INTERVAL_S = 0.25

    def __init__(self) -> None:
        self._active_until = 0.0  # monotonic deadline
        self.proc_match_count = 0
        self.dbus_match_count = 0
        self.poll_count = 0
        self.error_count = 0
        self._proc_active = False
        self._stop = threading.Event()
        self._proc_thread: threading.Thread | None = None
        self._dbus_thread: threading.Thread | None = None
        self._dbus_proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self._proc_thread is not None:
            return
        # Inline first reading so is_active() is correct from t=0.
        self._proc_active = self._scan_processes()
        if self._proc_active:
            self._active_until = time.monotonic() + GRACE_S
        self._proc_thread = threading.Thread(
            target=self._proc_loop, name="typefreq-polkit-proc", daemon=True,
        )
        self._proc_thread.start()
        self._dbus_thread = threading.Thread(
            target=self._dbus_loop, name="typefreq-polkit-dbus", daemon=True,
        )
        self._dbus_thread.start()

    # --- process scanning ---------------------------------------------------

    def _proc_loop(self) -> None:
        while not self._stop.wait(self.POLL_INTERVAL_S):
            self.poll_count += 1
            try:
                hit = self._scan_processes()
            except Exception:
                self.error_count += 1
                log.exception("polkit process scan failed")
                continue
            if hit:
                self._active_until = time.monotonic() + GRACE_S
                if not self._proc_active:
                    self.proc_match_count += 1
                    log.info("auth helper process detected — pausing key capture")
            elif self._proc_active:
                log.info("auth helper process gone (guard held by grace window)")
            self._proc_active = hit

    @staticmethod
    def _scan_processes() -> bool:
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
            if name in AUTH_PROCESS_NAMES:
                return True
        return False

    # --- D-Bus subscription ------------------------------------------------

    def _dbus_loop(self) -> None:
        """Watch the system bus for polkit AuthenticationAgent method calls.

        polkitd calls `BeginAuthentication` on the registered agent the
        moment a privileged action needs a password. Observing such a call
        tells us a dialog is about to appear, even though the agent process
        was already running.
        """
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._dbus_proc = subprocess.Popen(
                    [
                        "dbus-monitor", "--system",
                        "interface=org.freedesktop.PolicyKit1.AuthenticationAgent",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                backoff = 1.0
                if self._dbus_proc.stdout is None:
                    raise RuntimeError("dbus-monitor stdout missing")
                for line in self._dbus_proc.stdout:
                    if self._stop.is_set():
                        break
                    # We treat any AuthenticationAgent traffic as evidence
                    # of a live auth flow — BeginAuthentication starts it,
                    # CancelAuthentication ends it, but both deserve the
                    # same guard while we don't have precise close-tracking.
                    if (
                        "BeginAuthentication" in line
                        or "CancelAuthentication" in line
                    ):
                        self._active_until = time.monotonic() + GRACE_S
                        self.dbus_match_count += 1
                        log.info("polkit auth event observed — pausing key capture")
            except FileNotFoundError:
                log.warning(
                    "dbus-monitor not installed — polkit GUI dialogs won't be detected "
                    "via D-Bus (process scan still active for sudo/pkexec/pkttyagent)."
                )
                return
            except Exception:
                self.error_count += 1
                log.exception("polkit dbus monitor failed; retry in %.1fs", backoff)
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    # --- public surface -----------------------------------------------------

    def is_active(self) -> bool:
        return time.monotonic() < self._active_until

    def stop(self) -> None:
        self._stop.set()
        if self._dbus_proc is not None:
            try:
                self._dbus_proc.terminate()
            except Exception:
                pass

    def debug_state(self) -> dict:
        now = time.monotonic()
        return {
            "active": now < self._active_until,
            "active_for_s": max(0.0, self._active_until - now),
            "proc_active": self._proc_active,
            "proc_match_count": self.proc_match_count,
            "dbus_match_count": self.dbus_match_count,
            "poll_count": self.poll_count,
            "error_count": self.error_count,
        }
