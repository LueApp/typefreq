"""Global keyboard event tracker using evdev.

Reads keyboard input directly from /dev/input/event*, which works on both X11
and Wayland (Wayland clients can't read other clients' input, but evdev reads
from the kernel below the display server). Requires the user to be in the
'input' group.

The tracker assembles characters into word candidates, runs them through the
privacy filters, and hands accepted words to callbacks. It also detects a
global pause hotkey (Ctrl+Alt+Shift+P).
"""
from __future__ import annotations

import logging
import selectors
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock

import evdev
from evdev import ecodes

from . import keymap
from .config import DEVICE_BLOCKLIST, IDLE_TIMEOUT_S
from .filters import normalize

log = logging.getLogger("typefreq.tracker")

# Pause hotkey: Ctrl + Alt + Shift + P. The extra Shift keeps ordinary
# application shortcuts from silently pausing tracking for hours.
HOTKEY_PAUSE_KEY = "KEY_P"
DEVICE_RESCAN_INTERVAL_S = 5.0


def find_keyboards() -> list[evdev.InputDevice]:
    """Return all input devices that look like keyboards."""
    out: list[evdev.InputDevice] = []
    for path in evdev.list_devices():
        if path in DEVICE_BLOCKLIST:
            continue
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError) as e:
            log.warning("cannot open %s: %s", path, e)
            continue
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        # A keyboard reports the letter and digit keys.
        if ecodes.KEY_A in keys and ecodes.KEY_SPACE in keys:
            out.append(dev)
        else:
            dev.close()
    return out


class Tracker:
    """Background keyboard listener. Run via `run()` (blocking) or in a thread."""

    def __init__(
        self,
        on_word: Callable[[str], None],
        on_raw_word: Callable[[str], None] | None = None,
        on_backspace: Callable[[], None] | None = None,
        ime_monitor=None,
        locker_monitor=None,
        polkit_monitor=None,
        input_recorder=None,
    ) -> None:
        self.on_word = on_word
        self.on_raw_word = on_raw_word or (lambda _w: None)
        # Signals that the user is actively editing — used to cancel pending
        # typo notifications that haven't fired yet.
        self.on_backspace = on_backspace or (lambda: None)
        self._ime = ime_monitor  # may be None
        # Secure-context guards: when ANY of these reports is_active(), we
        # drop the keystroke so the user's password never reaches our DB or
        # callbacks. Currently: screen lockers + polkit/sudo/askpass.
        self._locker = locker_monitor  # may be None
        self._polkit = polkit_monitor  # may be None
        self._input_recorder = input_recorder  # may be None
        self._secure_guards = [
            g for g in (locker_monitor, polkit_monitor) if g is not None
        ]
        self._stop = Event()
        self._paused = False
        self._lock = Lock()
        # Modifier state (set of evdev key names currently held).
        self._mods: set[str] = set()
        self._caps_lock = False
        # Current word being assembled.
        self._buf: list[str] = []
        # If an idle reset discards the beginning of a word, the characters
        # typed after the pause are ambiguous: they may be the rest of that
        # same word, not a standalone word. Suppress that resumed fragment
        # until the user hits a boundary.
        self._suppress_current_token = False
        # If True, the next non-empty flush is silently dropped. Set after a
        # word-level navigation chord (Ctrl+Left/Right/Up/Down) because the
        # user has moved the caret into the middle of existing text — the
        # next "word" we form is likely a partial insertion, not a real
        # word. Cleared on the next flush OR on idle reset.
        self._skip_next_word = False
        # When did we last see a real keystroke? Used to detect context
        # switches (mouse clicks, window focus, idle thinking) we can't
        # observe directly — see `_apply_idle_reset`.
        self._last_activity_at = 0.0
        # Stats visible to other threads.
        self.events_seen = 0
        self.words_emitted = 0
        self.ime_skipped = 0
        self.locker_skipped = 0
        self.polkit_skipped = 0
        self.idle_resets = 0
        self.skipped_after_nav = 0
        self.active_keyboard_paths: list[str] = []
        self.device_read_errors = 0
        self.devices_added = 0
        self.keyboard_rescans = 0

    # --- public control --------------------------------------------------

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_paused(self, value: bool) -> None:
        with self._lock:
            self._paused = bool(value)
            if self._paused:
                self._buf.clear()
                self._suppress_current_token = False
        log.info("paused=%s", value)

    def toggle_paused(self) -> bool:
        with self._lock:
            self._paused = not self._paused
            if self._paused:
                self._buf.clear()
                self._suppress_current_token = False
            state = self._paused
        log.info("paused=%s", state)
        return state

    def stop(self) -> None:
        self._stop.set()

    # --- core loop -------------------------------------------------------

    def run(self) -> None:
        devices = find_keyboards()
        if not devices:
            raise RuntimeError(
                "No readable keyboards found. Are you in the 'input' group? "
                "Try: sudo usermod -aG input $USER  (then log out and back in)."
            )
        log.info("listening on %d device(s): %s", len(devices), [d.path for d in devices])

        sel = selectors.DefaultSelector()
        active_devices: dict[str, evdev.InputDevice] = {}
        for dev in devices:
            sel.register(dev, selectors.EVENT_READ)
            active_devices[dev.path] = dev
        self._set_active_keyboard_paths(active_devices)
        next_rescan_at = time.monotonic() + DEVICE_RESCAN_INTERVAL_S

        try:
            while not self._stop.is_set():
                for key, _mask in sel.select(timeout=0.5):
                    dev: evdev.InputDevice = key.fileobj  # type: ignore[assignment]
                    try:
                        for event in dev.read():
                            if event.type == ecodes.EV_KEY:
                                self._handle_key(evdev.KeyEvent(event))
                    except OSError as e:
                        log.warning("device %s read error: %s — dropping", dev.path, e)
                        sel.unregister(dev)
                        dev.close()
                        active_devices.pop(dev.path, None)
                        self.device_read_errors += 1
                        self._set_active_keyboard_paths(active_devices)

                # If a keyboard disappeared and later comes back (common after
                # suspend/resume or USB reconnect), discover it again instead of
                # waiting forever on the dead device set from startup.
                now = time.monotonic()
                if active_devices and now < next_rescan_at:
                    continue
                next_rescan_at = now + DEVICE_RESCAN_INTERVAL_S
                self.keyboard_rescans += 1
                for dev in find_keyboards():
                    if dev.path in active_devices:
                        dev.close()
                        continue
                    try:
                        sel.register(dev, selectors.EVENT_READ)
                    except Exception:
                        dev.close()
                        continue
                    log.info("added keyboard device: %s", dev.path)
                    active_devices[dev.path] = dev
                    self.devices_added += 1
                    self._set_active_keyboard_paths(active_devices)
        finally:
            for dev in active_devices.values():
                try:
                    dev.close()
                except Exception:
                    pass
            self._set_active_keyboard_paths({})
            sel.close()

    def _set_active_keyboard_paths(self, devices: dict[str, evdev.InputDevice]) -> None:
        self.active_keyboard_paths = sorted(devices)

    def _record_input(self, source: str, action: str, data: dict | None = None) -> None:
        if self._input_recorder is None:
            return
        try:
            self._input_recorder.record(source, action, data or {})
        except Exception:
            log.exception("input recorder failed")

    # --- event handling --------------------------------------------------

    def _handle_key(self, ev: evdev.KeyEvent) -> None:
        self.events_seen += 1

        # SECURITY: if any guard reports a secure context (screen locker up,
        # polkit dialog open, sudo/pkexec/askpass running), the user might be
        # typing a password. Drop the event entirely — no modifier state, no
        # buffer accumulation, no callbacks. Clear any in-flight buffer in
        # case the guard activated mid-word. Counters split per source so the
        # dashboard can show which protection is firing.
        locker_active = self._locker is not None and self._locker.is_active()
        polkit_active = self._polkit is not None and self._polkit.is_active()
        if locker_active or polkit_active:
            self._record_input(
                "tracker",
                "secure_context_drop",
                {
                    "locker_active": locker_active,
                    "polkit_active": polkit_active,
                    "buffer_len": len(self._buf),
                    "mod_count": len(self._mods),
                },
            )
            if self._buf:
                self._buf.clear()
            self._suppress_current_token = False
            # Drop modifier state too — leaving it set would let the next
            # post-unlock keystroke think a modifier is still held.
            if self._mods:
                self._mods.clear()
            if locker_active:
                self.locker_skipped += 1
            if polkit_active:
                self.polkit_skipped += 1
            return

        keyname = ev.keycode if isinstance(ev.keycode, str) else (
            ev.keycode[0] if ev.keycode else ""
        )
        if not keyname:
            return

        state = ev.keystate  # 0=up, 1=down, 2=hold
        key_action = {0: "key_up", 1: "key_down", 2: "key_hold"}.get(state, "key")
        self._record_input(
            "keyboard",
            key_action,
            {
                "key": keyname,
                "state": state,
                "mods": sorted(self._mods),
                "caps_lock": self._caps_lock,
                "paused": self.paused,
                "buffer": "".join(self._buf),
                "buffer_len": len(self._buf),
            },
        )

        # Track modifiers on key-down and key-up regardless of paused state.
        if keyname in keymap.SHIFT_KEYS or keyname in keymap.CTRL_KEYS \
                or keyname in keymap.ALT_KEYS or keyname in keymap.META_KEYS:
            if state == 1:
                self._mods.add(keyname)
            elif state == 0:
                self._mods.discard(keyname)
            return

        if keyname in keymap.CAPSLOCK_KEYS:
            if state == 1:
                self._caps_lock = not self._caps_lock
            return

        # We only act on key-down events for character processing.
        if state != 1:
            return

        # Pause hotkey: Ctrl+Alt+Shift+P.
        if (
            keyname == HOTKEY_PAUSE_KEY
            and self._has_ctrl()
            and self._has_alt()
            and self._has_shift()
        ):
            self.toggle_paused()
            self._buf.clear()
            self._suppress_current_token = False
            self._record_input("tracker", "pause_hotkey", {"paused": self.paused})
            return

        if self.paused:
            self._record_input("tracker", "paused_drop", {"key": keyname})
            return

        # If an IME is currently composing (e.g. fcitx5 in pinyin mode),
        # skip the keystroke entirely — the user is typing Hanzi via the IME,
        # not English words, so we mustn't record the pinyin syllables.
        if self._ime is not None and self._ime.is_composing():
            if self._buf:
                self._buf.clear()
            self._suppress_current_token = False
            self.ime_skipped += 1
            self._record_input("tracker", "ime_drop", {"key": keyname})
            return

        # Discard stale buffer if too much time has passed since the last
        # keystroke. This catches context switches we can't see directly
        # (mouse clicks, window focus changes, thinking pauses) which would
        # otherwise leave fragments like "ver" in the buffer and corrupt
        # the next word the user types.
        idle_reset = self._apply_idle_reset(time.monotonic())
        if idle_reset:
            self._record_input("tracker", "idle_reset", {"key": keyname})

        # Ignore chords with Ctrl/Alt/Meta — those are shortcuts, not typed words.
        if self._has_ctrl() or self._has_alt() or self._has_meta():
            # Ctrl+C commonly cancels the current terminal/input field. Treat
            # the partial token as abandoned, not committed.
            if self._has_ctrl() and keyname in keymap.CANCEL_KEYS:
                self._buf.clear()
                self._suppress_current_token = False
                self._record_input("tracker", "cancel_shortcut", {"key": keyname})
                return
            # Ctrl+Backspace (and Alt+Backspace in some editors) means
            # "delete word backward". The user is REMOVING the in-progress
            # word from their document, so we must NOT flush it as if they
            # had committed it. Just throw it away. We still call
            # on_backspace so the engine can retract a recently-recorded
            # typo if the user is going back to fix one.
            if keyname in keymap.BACKSPACE_KEYS:
                self._buf.clear()
                self._suppress_current_token = False
                self._record_input("tracker", "shortcut_backspace", {"key": keyname})
                try:
                    self.on_backspace()
                except Exception:
                    log.exception("on_backspace handler raised")
                return
            # Ctrl+arrow = word-level caret navigation. The next thing the
            # user types is likely INSIDE an existing word (insertion or
            # correction), not a fresh word — drop the next flush.
            if keyname in keymap.WORD_NAV_KEYS:
                self._flush()
                self._suppress_current_token = False
                self._skip_next_word = True
                self._record_input("tracker", "word_nav", {"key": keyname})
                return
            self._flush()
            self._suppress_current_token = False
            self._record_input("tracker", "shortcut_flush", {"key": keyname})
            return

        if keyname in keymap.BACKSPACE_KEYS:
            if self._buf:
                self._buf.pop()
            self._record_input(
                "tracker",
                "backspace",
                {"buffer": "".join(self._buf), "buffer_len": len(self._buf)},
            )
            # Any backspace signals "I'm fixing something" — let the engine
            # cancel any pending typo notifications.
            try:
                self.on_backspace()
            except Exception:
                log.exception("on_backspace handler raised")
            return

        if keyname in keymap.COMPLETION_KEYS:
            self._buf.clear()
            self._suppress_current_token = False
            self._record_input("tracker", "completion_clear", {"key": keyname})
            return

        if keyname in keymap.BOUNDARY_KEYS:
            self._flush()
            self._suppress_current_token = False
            self._record_input("tracker", "boundary", {"key": keyname})
            return

        ch = keymap.char_for(keyname, shift=self._has_shift(), caps=self._caps_lock)
        if ch is None:
            # Unknown/non-character key — close the current word.
            self._flush()
            self._suppress_current_token = False
            self._record_input("tracker", "unknown_key_flush", {"key": keyname})
            return

        if idle_reset:
            self._suppress_current_token = True
        self._buf.append(ch)
        self._record_input(
            "tracker",
            "char_buffered",
            {"key": keyname, "char": ch, "buffer": "".join(self._buf)},
        )

    def _flush(self) -> None:
        if not self._buf:
            return
        raw = "".join(self._buf)
        self._buf.clear()
        if self._suppress_current_token:
            self._suppress_current_token = False
            self._record_input("tracker", "word_suppressed", {"raw": raw})
            return
        # Skip flag is consumed by any non-empty flush, whether or not the
        # word would have passed the normalize filter — the point is to
        # drop the first finishable thing after a navigation chord.
        if self._skip_next_word:
            self._skip_next_word = False
            self.skipped_after_nav += 1
            self._record_input("tracker", "word_skipped_after_nav", {"raw": raw})
            return
        self.on_raw_word(raw)
        norm = normalize(raw)
        if norm is not None:
            self.words_emitted += 1
            self._record_input(
                "tracker",
                "word_emitted",
                {"raw": raw, "normalized": norm},
            )
            try:
                self.on_word(norm)
            except Exception:
                log.exception("on_word handler raised")
        else:
            self._record_input("tracker", "word_rejected", {"raw": raw})

    def _apply_idle_reset(self, now: float) -> bool:
        """If too much time has passed since the last keystroke, discard the
        partial buffer. Always updates `_last_activity_at` to `now`.

        Returns True iff the buffer was discarded.
        """
        reset = False
        if self._buf and (now - self._last_activity_at) > IDLE_TIMEOUT_S:
            self._buf.clear()
            self.idle_resets += 1
            reset = True
        # A long pause also invalidates the "skip next word" signal — the
        # user has clearly moved on from whatever navigation set it.
        if reset or (now - self._last_activity_at) > IDLE_TIMEOUT_S:
            self._skip_next_word = False
        self._last_activity_at = now
        return reset

    # --- modifier helpers ------------------------------------------------

    def _has_shift(self) -> bool:
        return bool(self._mods & keymap.SHIFT_KEYS)

    def _has_ctrl(self) -> bool:
        return bool(self._mods & keymap.CTRL_KEYS)

    def _has_alt(self) -> bool:
        return bool(self._mods & keymap.ALT_KEYS)

    def _has_meta(self) -> bool:
        return bool(self._mods & keymap.META_KEYS)
