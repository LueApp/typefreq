"""Combined daemon: tracker + Flask dashboard + transparent overlay notifier.

Threading layout (tkinter dictates the structure — Tk.mainloop must own the
main thread):

  main thread     : Overlay.run()  — Tk mainloop; renders typo toasts
  worker thread A : Tracker.run()  — evdev keyboard listener
  worker thread B : werkzeug serve_forever — Flask dashboard

Run with:  python -m keyfreq.app
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from threading import Lock

from flask import Flask, jsonify, make_response, render_template, request
from werkzeug.serving import make_server

import time
from collections import deque

from . import __version__, db
from .caret import CaretTracker
from .config import (
    DB_PATH,
    HTTP_ALLOWED_ORIGINS,
    HTTP_HOST,
    HTTP_PORT,
    PUBLIC_SITE_URL,
    TYPO_RETRACT_WINDOW_S,
)
from .filters import normalize
from .ime import IMEMonitor
from .locker import LockerMonitor
from .overlay import Overlay
from .polkit import PolkitMonitor
from .spellcheck import SpellNotifier
from .tracker import Tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger("keyfreq")


class Engine:
    """Owns the tracker, spell checker, overlay, and a write-side DB connection."""

    def __init__(self) -> None:
        db.init_db()
        self.ime = IMEMonitor()
        self.locker = LockerMonitor()
        self.polkit = PolkitMonitor()
        self.caret = CaretTracker()
        self._db_lock = Lock()
        self._conn = db.connect()
        # Load custom-word whitelist into an in-memory set; SpellNotifier
        # holds a reference, so additions/removals via the API are picked
        # up automatically.
        self.custom_words: set[str] = {
            r["word"] for r in db.list_custom_words(self._conn)
        }
        self.spell = SpellNotifier(custom_words=self.custom_words)
        self.overlay = Overlay(caret_tracker=self.caret)
        self.tracker = Tracker(
            on_word=self._on_word,
            on_backspace=self._on_backspace,
            ime_monitor=self.ime,
            locker_monitor=self.locker,
            polkit_monitor=self.polkit,
        )
        # Recently-recorded typos that could still be retracted. Each entry
        # is (monotonic_at_record, db_ts, word, suggestion). Ordered by time
        # (left = oldest). Only the most recent within the window is eligible
        # for retraction on the next backspace.
        self._recent_typos: deque[tuple[float, int, str, str]] = deque()
        self._retract_lock = Lock()
        # Stat: how many typos were retracted by a quick backspace.
        self.typos_retracted = 0
        log.info("loaded %d custom word(s) from DB", len(self.custom_words))

    # --- custom-words API surface (called from Flask handlers) ----------

    def add_custom_word(self, word: str) -> tuple[bool, str | None, int]:
        """Add a word to the whitelist. Returns (added, normalized, typos_removed).

        `added` is False if the word was already present.
        `normalized` is None if the input failed validation.
        `typos_removed` is how many historical `typos` rows were deleted.
        """
        norm = normalize(word)
        if norm is None:
            return False, None, 0
        with self._db_lock:
            added = db.add_custom_word(self._conn, norm)
            removed = db.delete_typos_for_word(self._conn, norm)
        if added:
            self.custom_words.add(norm)
        return added, norm, removed

    def remove_custom_word(self, word: str) -> tuple[bool, str | None]:
        """Remove a word from the whitelist. Returns (removed, normalized)."""
        norm = normalize(word)
        if norm is None:
            return False, None
        with self._db_lock:
            removed = db.remove_custom_word(self._conn, norm)
        if removed:
            self.custom_words.discard(norm)
        return removed, norm

    def _on_word(self, word: str) -> None:
        misspelled, suggestion = self.spell.check(word)
        # Record immediately — toast feedback is fast, stats stay current.
        # If the user backspaces within TYPO_RETRACT_WINDOW_S, _on_backspace
        # undoes everything (DB rows + active toast).
        canonical = suggestion if (misspelled and suggestion) else word
        ts = int(time.time())
        with self._db_lock:
            db.record_word(self._conn, canonical, ts=ts)
            if misspelled and suggestion:
                db.record_typo(self._conn, word, suggestion, ts=ts)
        if misspelled and suggestion:
            with self._retract_lock:
                self._recent_typos.append((time.monotonic(), ts, word, suggestion))
                self._prune_recent_typos_locked()
            if self.spell.should_notify(word):
                self.overlay.enqueue(word, suggestion)
                log.info("typo: %s -> %s", word, suggestion)

    def _prune_recent_typos_locked(self) -> None:
        """Drop entries older than the retract window. Caller holds the lock."""
        cutoff = time.monotonic() - TYPO_RETRACT_WINDOW_S
        while self._recent_typos and self._recent_typos[0][0] < cutoff:
            self._recent_typos.popleft()

    def _on_backspace(self) -> None:
        """Tracker calls this on every backspace. If the user typed a typo
        within the retract window, undo the whole record and dismiss the
        toast — they've already noticed.

        We only retract the MOST RECENT in-window typo: that's almost
        certainly the one being fixed. Earlier (still-in-window) typos are
        left alone because the user is much more likely to fix what they
        just typed than something a few words back.
        """
        if TYPO_RETRACT_WINDOW_S <= 0:
            return
        with self._retract_lock:
            self._prune_recent_typos_locked()
            if not self._recent_typos:
                return
            _mono, ts, word, suggestion = self._recent_typos.pop()
        retracted = False
        try:
            with self._db_lock:
                retracted = db.retract_typo(self._conn, ts, word, suggestion)
        except Exception:
            log.exception("retract_typo failed for word=%r", word)
        if retracted:
            self.typos_retracted += 1
            self.overlay.dismiss()
            log.info("retracted typo: %s -> %s", word, suggestion)

    def shutdown(self) -> None:
        self.tracker.stop()
        self.overlay.stop()
        self.caret.stop()
        self.ime.shutdown()
        self.locker.stop()
        self.polkit.stop()
        with self._db_lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def read(self, fn, *args, **kwargs):
        with self._db_lock:
            return fn(self._conn, *args, **kwargs)


def make_app(engine: Engine) -> Flask:
    app = Flask(__name__)

    @app.before_request
    def api_preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            return make_response(("", 204))
        return None

    @app.after_request
    def add_api_cors(response):
        if not request.path.startswith("/api/"):
            return response
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin not in HTTP_ALLOWED_ORIGINS:
            return response
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
        if request.headers.get("Access-Control-Request-Private-Network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/health")
    def api_health():
        return jsonify(
            ok=True,
            service="keyfreq",
            version=__version__,
            public_site=PUBLIC_SITE_URL,
        )

    @app.get("/api/status")
    def api_status():
        ime = engine.ime.debug_state()
        lock = engine.locker.debug_state()
        pk = engine.polkit.debug_state()
        return jsonify(
            paused=engine.tracker.paused,
            events_seen=engine.tracker.events_seen,
            words_emitted=engine.tracker.words_emitted,
            active_keyboard_count=len(engine.tracker.active_keyboard_paths),
            active_keyboard_paths=engine.tracker.active_keyboard_paths,
            device_read_errors=engine.tracker.device_read_errors,
            devices_added=engine.tracker.devices_added,
            keyboard_rescans=engine.tracker.keyboard_rescans,
            ime_skipped=engine.tracker.ime_skipped,
            locker_skipped=engine.tracker.locker_skipped,
            polkit_skipped=engine.tracker.polkit_skipped,
            idle_resets=engine.tracker.idle_resets,
            skipped_after_nav=engine.tracker.skipped_after_nav,
            typos_retracted=engine.typos_retracted,
            toasts_dismissed=engine.overlay.toasts_dismissed,
            ime_active=ime["available"],
            ime_current=ime["current_im"],
            ime_composing=ime["composing"],
            ime_poll_count=ime["poll_count"],
            ime_error_count=ime["error_count"],
            locker_active=lock["locked"],
            locker_poll_count=lock["poll_count"],
            locker_error_count=lock["error_count"],
            polkit_active=pk["active"],
            polkit_active_for_s=round(pk["active_for_s"], 2),
            polkit_proc_match_count=pk["proc_match_count"],
            polkit_dbus_match_count=pk["dbus_match_count"],
            polkit_poll_count=pk["poll_count"],
            polkit_error_count=pk["error_count"],
            spell_checked=engine.spell.checked,
            spell_notified=engine.spell.notified,
            caret_available=engine.caret.available,
            toasts_via_caret=engine.overlay.toasts_via_caret,
            toasts_via_mouse=engine.overlay.toasts_via_mouse,
            db_path=str(DB_PATH),
        )

    @app.post("/api/pause")
    def api_pause():
        body = request.get_json(silent=True) or {}
        if "paused" in body:
            engine.tracker.set_paused(bool(body["paused"]))
        else:
            engine.tracker.toggle_paused()
        return jsonify(paused=engine.tracker.paused)

    @app.get("/api/stats/today")
    def api_today():
        since = db.day_start_utc()
        return jsonify(
            since=since,
            totals=engine.read(db.totals, since=since),
            top_words=engine.read(db.top_words_in_period, since=since, limit=25),
            hourly=engine.read(db.hourly_activity, since=since),
            recent_typos=engine.read(db.recent_typos, limit=20),
        )

    @app.get("/api/stats/alltime")
    def api_alltime():
        return jsonify(
            totals=engine.read(db.totals),
            top_words=engine.read(db.top_words, limit=25),
        )

    @app.get("/api/stats/leaderboards")
    def api_leaderboards():
        """Top 25 words for each period.

        today/week/month/year use the per-day table (accurate counts within
        the period). alltime falls back to the cumulative word_counts so
        historical typing predating daily_word_counts is preserved.
        """
        today = db.day_start_utc()
        week = db.week_start_utc()
        month = db.month_start_utc()
        year = db.year_start_utc()
        return jsonify(
            today={
                "since": today,
                "top_words": engine.read(db.top_words_in_period, since=today, limit=25),
            },
            week={
                "since": week,
                "top_words": engine.read(db.top_words_in_period, since=week, limit=25),
            },
            month={
                "since": month,
                "top_words": engine.read(db.top_words_in_period, since=month, limit=25),
            },
            year={
                "since": year,
                "top_words": engine.read(db.top_words_in_period, since=year, limit=25),
            },
            alltime={
                "top_words": engine.read(db.top_words, limit=25),
            },
        )

    @app.get("/api/debug/ime")
    def api_debug_ime():
        return jsonify(engine.ime.debug_state())

    @app.get("/api/custom-words")
    def api_list_custom():
        return jsonify(words=engine.read(db.list_custom_words))

    @app.post("/api/custom-words")
    def api_add_custom():
        body = request.get_json(silent=True) or {}
        word = (body.get("word") or "").strip()
        if not word:
            return jsonify(error="missing 'word'"), 400
        added, norm, removed = engine.add_custom_word(word)
        if norm is None:
            return jsonify(
                error=(
                    "word failed normalization (must be 2-30 letters, "
                    "no digits, optional internal hyphen/apostrophe)"
                ),
                input=word,
            ), 400
        return jsonify(
            added=added, word=norm, typos_removed=removed,
            words=engine.read(db.list_custom_words),
        )

    @app.delete("/api/custom-words/<word>")
    def api_remove_custom(word: str):
        removed, norm = engine.remove_custom_word(word)
        if norm is None:
            return jsonify(error="invalid word"), 400
        return jsonify(
            removed=removed, word=norm,
            words=engine.read(db.list_custom_words),
        )

    return app


def main() -> int:
    engine = Engine()

    # --- start tracker thread ---
    try:
        # Validate that we have at least one keyboard before kicking off the loop.
        engine.tracker  # noqa: B018 — touching the attribute confirms construction
    except Exception as e:
        log.error("tracker init failed: %s", e)
        return 2

    tracker_thread = threading.Thread(
        target=_run_tracker, args=(engine,), name="keyfreq-tracker", daemon=True,
    )
    tracker_thread.start()

    # --- start AT-SPI caret tracker (no-op if a11y isn't available) ---
    engine.caret.start()

    # --- start secure-context guards BEFORE the tracker starts emitting key
    # callbacks, so we never have a window where keys are processed without
    # the guards' first readings in place.
    engine.locker.start()
    engine.polkit.start()

    # --- start Flask via werkzeug make_server (so we can shut it down cleanly) ---
    server = make_server(HTTP_HOST, HTTP_PORT, make_app(engine), threaded=True)
    flask_thread = threading.Thread(
        target=server.serve_forever, name="keyfreq-flask", daemon=True,
    )
    flask_thread.start()
    log.info("dashboard at http://%s:%d", HTTP_HOST, HTTP_PORT)

    # --- signal handlers stop everything cleanly ---
    def _shutdown(_sig=None, _frm=None):
        log.info("shutdown signal received")
        try:
            server.shutdown()
        except Exception:
            pass
        engine.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # --- run Tk on the main thread (blocks) ---
    try:
        engine.overlay.run()
    finally:
        _shutdown()
    return 0


def _run_tracker(engine: Engine) -> None:
    try:
        engine.tracker.run()
    except RuntimeError as e:
        log.error("tracker stopped: %s", e)
    except Exception:
        log.exception("tracker crashed")


if __name__ == "__main__":
    sys.exit(main())
