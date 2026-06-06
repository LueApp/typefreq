"""Text-caret position tracker using AT-SPI (assistive technology bus).

For apps that participate in accessibility (GTK, Qt, Electron text fields,
Firefox, Chrome, GNOME apps, etc.), AT-SPI emits caret-moved events with the
on-screen position of the text cursor. We subscribe to those events in a
background thread; the overlay reads the most recent position when it needs
to anchor a toast.

Requirements to actually receive events:
  * `python3-gi` + `gir1.2-atspi-2.0` (system packages)
  * `gsettings set org.gnome.desktop.interface toolkit-accessibility true`
  * Apps must be restarted after the gsettings change so they re-init their
    a11y bridge.

If any of those is missing, this module imports cleanly but reports
`available = False`, and the overlay falls back to mouse-pointer anchoring.
"""
from __future__ import annotations

import logging
import time
from threading import Event, Lock, Thread

log = logging.getLogger("keyfreq.caret")

# Caret positions older than this are ignored (caret hasn't moved → user may
# not even be in a text field anymore).
MAX_AGE_S = 12.0


class CaretTracker:
    """Best-effort caret position tracker. Thread-safe."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._pos: tuple[int, int] | None = None
        self._pos_at = 0.0
        self._stop = Event()
        self._thread: Thread | None = None
        self._atspi = None
        self._available = False
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi  # noqa: F401
            self._atspi = Atspi
            self._available = True
        except (ImportError, ValueError) as e:
            log.info("AT-SPI unavailable (%s) — caret tracking disabled", e)

    @property
    def available(self) -> bool:
        return self._available

    def get_position(self) -> tuple[int, int] | None:
        """Return the most recent caret (x, y) on the screen, or None if stale/unknown."""
        with self._lock:
            if self._pos is None:
                return None
            if time.monotonic() - self._pos_at > MAX_AGE_S:
                return None
            return self._pos

    def start(self) -> None:
        if not self._available or self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="keyfreq-caret", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._atspi is not None:
            try:
                self._atspi.event_quit()
            except Exception:
                pass

    # --- internals ------------------------------------------------------

    def _run(self) -> None:
        Atspi = self._atspi
        try:
            listener = Atspi.EventListener.new(self._on_event)
            listener.register("object:text-caret-moved")
            # Also track focus changes so we can refresh on a new field.
            listener.register("object:state-changed:focused")
            log.info("AT-SPI caret listener started")
            Atspi.event_main()  # blocks until event_quit()
        except Exception:
            log.exception("AT-SPI event loop crashed; caret tracking disabled")
            self._available = False

    def _on_event(self, event) -> None:
        try:
            src = event.source
            if src is None:
                return
            # Only the text widget where the caret moved is interesting.
            # Apps emit focus events too; for those we only act if Text is queryable.
            try:
                text = src.queryText()
            except Exception:
                return
            if text is None:
                return
            offset = text.caretOffset
            ex = text.getCharacterExtents(offset, 0)  # 0 = SCREEN coords
            if ex.width == 0 and ex.height == 0:
                # Empty field — fall back to widget extents.
                try:
                    bbox = src.getExtents(0)
                except Exception:
                    return
                if bbox.width == 0 and bbox.height == 0:
                    return
                x = bbox.x + 6
                y = bbox.y + bbox.height
            else:
                x = ex.x
                y = ex.y + ex.height  # one line below the caret
            with self._lock:
                self._pos = (int(x), int(y))
                self._pos_at = time.monotonic()
        except Exception:
            # AT-SPI calls can transiently fail (app shutdown, racy widget tree).
            pass
