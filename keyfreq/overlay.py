"""Custom transparent toast overlay for typo notifications.

Why not notify-send? It hands the message to the desktop's notification daemon
(GNOME Shell, mako, dunst, …) which controls placement and styling — we can't
ask for "near where the user is typing" or for a custom transparent look.

What we do here:
  * Use tkinter to create a borderless, semi-transparent, always-on-top toast.
  * Anchor it near the mouse pointer (best proxy for "where you're typing"
    — the actual text caret position is not exposed to other clients on
    Wayland; you'd need a compositor-specific hack to get it).
  * Auto-dismiss with a smooth alpha fade.

Threading: tkinter's Tk.mainloop must run on the main thread of the process.
Other threads push notification requests into a queue.Queue; the Tk loop
drains that queue periodically via after().

Fallback: if Tk cannot be initialised (no display, no python3-tk, …) we log
loudly and degrade to notify-send if available, else stderr.
"""
from __future__ import annotations

import logging
import queue
import re
import shutil
import subprocess
import time
from threading import Event

from .config import (
    OVERLAY_ALPHA,
    OVERLAY_DURATION_MS,
    OVERLAY_FADE_MS,
    OVERLAY_FONT_SIZE,
    OVERLAY_OFFSET_X,
    OVERLAY_OFFSET_Y,
    OVERLAY_POSITION,
)

log = logging.getLogger("keyfreq.overlay")

# Tkinter is imported lazily so a missing python3-tk doesn't break import-time.
_tk = None
_TclError = Exception  # placeholder until tkinter is loaded


def _import_tk():
    global _tk, _TclError
    if _tk is not None:
        return _tk
    import tkinter as tk
    _tk = tk
    _TclError = tk.TclError
    return _tk


def _mouse_position(default: tuple[int, int]) -> tuple[int, int]:
    """Return (x, y) of the mouse pointer, or `default` if it can't be determined."""
    xdo = shutil.which("xdotool")
    if xdo is None:
        return default
    try:
        out = subprocess.check_output(
            [xdo, "getmouselocation", "--shell"],
            text=True,
            timeout=0.3,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return default
    x = y = None
    for line in out.splitlines():
        if line.startswith("X="):
            x = int(line[2:])
        elif line.startswith("Y="):
            y = int(line[2:])
    if x is None or y is None:
        return default
    return x, y


# Monitor list cached for 60 s — re-detected occasionally so plug/unplug is picked up
# without restarting the daemon.
_MONITOR_TTL_SEC = 60.0
_monitor_cache_at = 0.0
_monitor_cache: list[dict] = []

# Format from `xrandr --listactivemonitors`:
#     " 0: +*eDP-1 2560/344x1600/215+2304+2304  eDP-1"
_MONITOR_LINE = re.compile(
    r"^\s*\d+:\s*[+*]+\S+\s+(?P<w>\d+)/\d+x(?P<h>\d+)/\d+\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
)


def _get_monitors() -> list[dict]:
    """Return list of dicts with keys x,y,w,h. Cached for _MONITOR_TTL_SEC."""
    global _monitor_cache_at, _monitor_cache
    now = time.monotonic()
    if _monitor_cache and (now - _monitor_cache_at) < _MONITOR_TTL_SEC:
        return _monitor_cache
    xr = shutil.which("xrandr")
    if xr is None:
        _monitor_cache_at, _monitor_cache = now, []
        return _monitor_cache
    try:
        out = subprocess.check_output(
            [xr, "--listactivemonitors"], text=True, timeout=0.5, stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        _monitor_cache_at, _monitor_cache = now, []
        return _monitor_cache
    monitors: list[dict] = []
    for line in out.splitlines():
        m = _MONITOR_LINE.match(line)
        if m:
            monitors.append({
                "x": int(m["x"]), "y": int(m["y"]),
                "w": int(m["w"]), "h": int(m["h"]),
            })
    _monitor_cache_at, _monitor_cache = now, monitors
    return monitors


def _monitor_containing(x: int, y: int, monitors: list[dict]) -> dict | None:
    for m in monitors:
        if m["x"] <= x < m["x"] + m["w"] and m["y"] <= y < m["y"] + m["h"]:
            return m
    return None


def _compute_origin(position: str, screen_w: int, screen_h: int,
                    win_w: int, win_h: int, ox: int, oy: int,
                    caret_pos: tuple[int, int] | None = None) -> tuple[int, int]:
    """Compute the top-left coordinate for the toast given a named position.

    For "cursor": prefers AT-SPI caret position when provided, else falls back
    to the mouse pointer. Constrained to the monitor the anchor point is on,
    and flipped above/left of the anchor if the natural placement would fall
    off that monitor.
    """
    pad = 24
    if position == "cursor":
        if caret_pos is not None:
            cx, cy = caret_pos
        else:
            cx, cy = _mouse_position(default=(screen_w - win_w - pad, screen_h - win_h - pad))
        monitors = _get_monitors()
        mon = _monitor_containing(cx, cy, monitors) or {
            "x": 0, "y": 0, "w": screen_w, "h": screen_h,
        }
        # Vertical: place below cursor if there's room, otherwise above.
        if cy + oy + win_h <= mon["y"] + mon["h"] - 4:
            y = cy + oy
        else:
            y = cy - win_h - oy
        # Horizontal: place to the right if it fits, otherwise to the left.
        if cx + ox + win_w <= mon["x"] + mon["w"] - 4:
            x = cx + ox
        else:
            x = cx - win_w - ox
        # Clamp inside the monitor.
        x = max(mon["x"] + 4, min(x, mon["x"] + mon["w"] - win_w - 4))
        y = max(mon["y"] + 4, min(y, mon["y"] + mon["h"] - win_h - 4))
        return x, y

    if position == "top-left":
        x, y = pad, pad
    elif position == "top-right":
        x, y = screen_w - win_w - pad, pad
    elif position == "bottom-left":
        x, y = pad, screen_h - win_h - pad
    elif position == "bottom-center":
        x, y = (screen_w - win_w) // 2, screen_h - win_h - pad
    elif position == "center":
        x, y = (screen_w - win_w) // 2, (screen_h - win_h) // 2
    else:  # default to bottom-right
        x, y = screen_w - win_w - pad, screen_h - win_h - pad

    x = max(0, min(x, screen_w - win_w))
    y = max(0, min(y, screen_h - win_h))
    return x, y


class Overlay:
    """Transparent toast notifier. `enqueue()` is thread-safe; `run()` blocks the main thread."""

    def __init__(self, caret_tracker=None) -> None:
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._stop = Event()
        self._root = None  # type: ignore[assignment]
        self._available = False  # set True once Tk is up
        self._caret = caret_tracker  # may be None
        # Currently-displayed Toplevels (Tk main thread only — see dismiss()
        # for cross-thread access).
        self._active_toasts: list = []
        # How many toasts have used caret pos vs mouse pos — for /api/status.
        self.toasts_via_caret = 0
        self.toasts_via_mouse = 0
        self.toasts_dismissed = 0

    # --- producer side (any thread) -------------------------------------

    def enqueue(self, word: str, suggestion: str) -> None:
        self._queue.put((word, suggestion))

    def dismiss(self) -> None:
        """Close any active toast and drop any pending one. Thread-safe.

        Used when the engine retracts a typo (user backspaced quickly) — the
        toast that just appeared is no longer relevant.
        """
        if self._root is None or not self._available:
            # Pending toasts are in the queue; clear them so they don't show.
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
            return
        try:
            self._root.after(0, self._dismiss_on_main)
        except Exception:
            pass

    def _dismiss_on_main(self) -> None:
        # Drain anything queued but not yet shown.
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        # Tear down anything currently on screen.
        for w in list(self._active_toasts):
            try:
                w.destroy()
            except Exception:
                pass
            self.toasts_dismissed += 1
        self._active_toasts.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._root is not None:
            try:
                self._root.after(0, self._root.quit)
            except Exception:
                pass

    # --- consumer side (main thread) ------------------------------------

    def run(self) -> None:
        """Blocking. Returns when stop() is called or Tk window is closed."""
        try:
            tk = _import_tk()
        except Exception as e:
            log.error("tkinter unavailable (%s) — falling back to notify-send", e)
            self._fallback_loop()
            return

        try:
            self._root = tk.Tk(className="keyfreq")
        except _TclError as e:
            log.error("Tk failed to start (%s) — falling back to notify-send", e)
            self._fallback_loop()
            return

        self._root.withdraw()  # hide the implicit root window
        self._available = True
        self._poll()
        try:
            self._root.mainloop()
        finally:
            try:
                self._root.destroy()
            except Exception:
                pass

    def _poll(self) -> None:
        if self._stop.is_set():
            try:
                self._root.quit()
            except Exception:
                pass
            return
        try:
            while True:
                word, suggestion = self._queue.get_nowait()
                self._show_toast(word, suggestion)
        except queue.Empty:
            pass
        # Re-arm.
        self._root.after(80, self._poll)

    def _show_toast(self, word: str, suggestion: str) -> None:
        tk = _tk
        try:
            w = tk.Toplevel(self._root)
            w.overrideredirect(True)            # no titlebar/border
            w.attributes("-topmost", True)
            w.attributes("-alpha", OVERLAY_ALPHA)
            # Some compositors honour the type hint and skip animations/decorations.
            try:
                w.attributes("-type", "notification")
            except _TclError:
                pass
            w.configure(bg="#11141a", highlightthickness=1, highlightbackground="#3a4358")

            # Two-line layout: the misspelled word, then the suggestion.
            container = tk.Frame(w, bg="#11141a")
            container.pack(padx=14, pady=10)

            tk.Label(
                container,
                text=word,
                bg="#11141a",
                fg="#ff7b7b",
                font=("TkDefaultFont", OVERLAY_FONT_SIZE, "bold"),
            ).pack(anchor="w")

            tk.Label(
                container,
                text=f"→  {suggestion}",
                bg="#11141a",
                fg="#9ee29a",
                font=("TkDefaultFont", OVERLAY_FONT_SIZE),
            ).pack(anchor="w")

            # Position now that we know the natural size.
            w.update_idletasks()
            sw = w.winfo_screenwidth()
            sh = w.winfo_screenheight()
            ww = w.winfo_reqwidth()
            wh = w.winfo_reqheight()
            caret_pos = self._caret.get_position() if self._caret is not None else None
            if caret_pos is not None:
                self.toasts_via_caret += 1
            else:
                self.toasts_via_mouse += 1
            x, y = _compute_origin(
                OVERLAY_POSITION, sw, sh, ww, wh,
                OVERLAY_OFFSET_X, OVERLAY_OFFSET_Y,
                caret_pos=caret_pos,
            )
            w.geometry(f"+{x}+{y}")
            w.deiconify()
            self._active_toasts.append(w)

            # Schedule the fade and destruction.
            self._schedule_fade(w)
        except Exception:
            log.exception("toast render failed for %r → %r", word, suggestion)

    def _forget_toast(self, w) -> None:
        try:
            self._active_toasts.remove(w)
        except ValueError:
            pass  # already dismissed

    def _schedule_fade(self, w) -> None:
        # After OVERLAY_DURATION_MS, fade alpha → 0 over OVERLAY_FADE_MS in ~25 steps.
        steps = max(5, OVERLAY_FADE_MS // 25)
        step_delay = max(15, OVERLAY_FADE_MS // steps)
        decrement = OVERLAY_ALPHA / steps

        def fade(remaining: int, alpha: float) -> None:
            if remaining <= 0:
                self._forget_toast(w)
                try:
                    w.destroy()
                except Exception:
                    pass
                return
            new_alpha = max(0.0, alpha - decrement)
            try:
                w.attributes("-alpha", new_alpha)
            except Exception:
                self._forget_toast(w)
                try:
                    w.destroy()
                except Exception:
                    pass
                return
            w.after(step_delay, lambda: fade(remaining - 1, new_alpha))

        w.after(OVERLAY_DURATION_MS, lambda: fade(steps, OVERLAY_ALPHA))

    # --- fallback if Tk is unavailable ----------------------------------

    def _fallback_loop(self) -> None:
        """Drain the queue using notify-send (or stderr) until stop() is called."""
        notify = shutil.which("notify-send")
        while not self._stop.is_set():
            try:
                word, suggestion = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            body = f"{word} → {suggestion}"
            if notify:
                subprocess.Popen(
                    [notify, "--app-name=keyfreq", "--expire-time=4000", "Typo?", body],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                log.warning("typo: %s", body)
