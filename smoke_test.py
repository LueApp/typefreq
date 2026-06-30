"""Smoke test for typefreq — exercises everything that doesn't require /dev/input."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import json
from pathlib import Path

# Force test DB into a tmpdir before importing the package.
tmpdir = Path(tempfile.mkdtemp(prefix="typefreq-test-"))
os.environ["TYPEFREQ_DATA"] = str(tmpdir)
os.environ["TYPEFREQ_DB"] = str(tmpdir / "test.db")

from typefreq import db, filters  # noqa: E402
from typefreq.caret import CaretTracker  # noqa: E402
from typefreq.ime import IMEMonitor  # noqa: E402
from typefreq.input_recorder import InputRecorder  # noqa: E402
from typefreq.spellcheck import SpellNotifier  # noqa: E402

errors: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        errors.append(label)


# 1. Filters: keep words, reject junk.
KEEPERS = ["hello", "Python", "well-known", "don't", "the"]
REJECTS = ["", "a", "passw0rd", "Xy7Qz1Aa", "...", "abc123", "_underscore_"]
for w in KEEPERS:
    norm = filters.normalize(w)
    check(f"filter keeps {w!r}", norm is not None and norm == w.lower().strip("-'"), repr(norm))
for w in REJECTS:
    check(f"filter rejects {w!r}", filters.normalize(w) is None)

# 2. DB schema + writes + reads.
db.init_db()
conn = db.connect()
for w in ["the", "the", "quick", "brown", "fox", "the"]:
    db.record_word(conn, w)
top = db.top_words(conn, limit=10)
check("top_words returns rows", len(top) == 4, f"{len(top)} rows")
check("top word is 'the' x3", top[0]["word"] == "the" and top[0]["count"] == 3, repr(top[0]))

t = db.totals(conn)
check("totals.words == 6", t["words"] == 6, repr(t))
check("totals.unique == 4", t["unique_words"] == 4, repr(t))

db.record_typo(conn, "teh", "the")
recent = db.recent_typos(conn, limit=10)
check("recent_typos returns the typo", len(recent) == 1 and recent[0]["word"] == "teh", repr(recent))

since = db.day_start_utc()
hourly = db.hourly_activity(conn, since)
check("hourly_activity returns >=1 bucket", len(hourly) >= 1, f"{len(hourly)} buckets")

# 2b. Per-day word counts power the weekly/monthly/yearly leaderboards.
top_today = db.top_words_in_period(conn, since=since, limit=10)
check("top_words_in_period(today) returns rows",
      len(top_today) == 4, f"{len(top_today)} rows")
check("top_words_in_period(today) top is 'the' x3",
      top_today[0]["word"] == "the" and top_today[0]["count"] == 3,
      repr(top_today[0]))
# Period helpers should chain: year_start <= month_start <= week_start <= today.
y, m, wk, td = (
    db.year_start_utc(), db.month_start_utc(),
    db.week_start_utc(), db.day_start_utc(),
)
check("year_start_utc <= month_start_utc <= week_start_utc <= day_start_utc",
      y <= m <= wk <= td, f"y={y} m={m} w={wk} d={td}")
# A future-period filter should return nothing.
future = db.top_words_in_period(conn, since=td + 86400, limit=10)
check("top_words_in_period(tomorrow) is empty", future == [], repr(future))

conn.close()

# 3. Spell checker: detects a common typo (>=5 chars), doesn't flag a correct word.
spell = SpellNotifier()
mis, sug = spell.check("becuase")
check("spell.check flags 'becuase'", mis is True and sug == "because", f"mis={mis} sug={sug}")
mis2, _ = spell.check("because")
check("spell.check passes 'because'", mis2 is False)
mis3, _ = spell.check("teh")  # below TYPO_MIN_LEN
check("spell.check ignores short word (teh, 3 chars)", mis3 is False)
for variant in ["colour", "organise", "organisation", "defence", "behaviour", "neighbour", "programme"]:
    mis_variant, sug_variant = spell.check(variant)
    check(f"spell.check accepts spelling variant {variant!r}",
          mis_variant is False and sug_variant is None,
          f"mis={mis_variant} sug={sug_variant}")
mis4, sug4 = spell.check("recieve")
check("spell.check still flags real typo 'recieve'",
      mis4 is True and sug4 == "receive", f"mis={mis4} sug={sug4}")

# 4. Rate limiter: first should_notify allowed, second within cooldown blocked.
ok1 = spell.should_notify("becuase")
ok2 = spell.should_notify("becuase")
check("first should_notify -> True", ok1 is True)
check("second should_notify (cooldown) -> False", ok2 is False)

# 4b. Filter never accepts CJK / non-ASCII (sanity for the user's request).
for cjk in ["你好", "再見", "héllo", "naïve"]:
    check(f"filter rejects non-ASCII {cjk!r}", filters.normalize(cjk) is None)

# 4c. InputRecorder: disabled by default, bounded, clearable, JSON-safe.
rec = InputRecorder(limit=2)
rec.record("keyboard", "key_down", {"key": "KEY_A"})
check("input recorder disabled by default",
      rec.snapshot()["entries"] == [] and rec.snapshot()["count"] == 0,
      repr(rec.snapshot()))
rec.set_enabled(True)
rec.record("keyboard", "key_down", {"key": "KEY_A", "nested": {"raw": object()}})
rec.record("mouse", "mouse_click", {"button": "BTN_LEFT", "pressed": True, "pos": (10, 20)})
rec.record("tracker", "word_emitted", {"raw": "helo", "normalized": "helo"})
snap = rec.snapshot()
check("input recorder keeps bounded recent entries",
      snap["enabled"] is True and snap["count"] == 2
      and [e["action"] for e in snap["entries"]] == ["mouse_click", "word_emitted"],
      repr(snap))
payload = rec.export_payload()
try:
    json.dumps(payload)
    payload_json_safe = True
except TypeError:
    payload_json_safe = False
check("input recorder export payload is JSON-safe",
      payload_json_safe and payload["count"] == 2 and payload["limit"] == 2,
      repr(payload))
rec.clear()
check("input recorder clear empties history",
      rec.snapshot()["entries"] == [] and rec.snapshot()["count"] == 0,
      repr(rec.snapshot()))

# 4d. MouseMonitor event conversion: fake evdev mouse events record clicks.
try:
    from evdev import ecodes as _mouse_ecodes  # noqa: E402
    from typefreq.mouse import MouseMonitor  # noqa: E402

    class _FakeMouseEvent:
        def __init__(self, event_type, code, value):
            self.type = event_type
            self.code = code
            self.value = value

    rec_mouse = InputRecorder(limit=10)
    rec_mouse.set_enabled(True)
    mouse = MouseMonitor(rec_mouse)
    mouse._handle_event(_FakeMouseEvent(_mouse_ecodes.EV_REL, _mouse_ecodes.REL_X, 7))
    mouse._handle_event(_FakeMouseEvent(_mouse_ecodes.EV_REL, _mouse_ecodes.REL_Y, 5))
    mouse._handle_event(_FakeMouseEvent(_mouse_ecodes.EV_KEY, _mouse_ecodes.BTN_LEFT, 1))
    mouse._handle_event(_FakeMouseEvent(_mouse_ecodes.EV_KEY, _mouse_ecodes.BTN_LEFT, 0))
    mouse_entries = rec_mouse.snapshot()["entries"]
    check("mouse monitor records button press and release",
          [e["data"].get("pressed") for e in mouse_entries] == [True, False]
          and all(e["source"] == "mouse" and e["action"] == "mouse_click" for e in mouse_entries)
          and all(e["data"].get("button") == "BTN_LEFT" for e in mouse_entries)
          and all(e["data"].get("x") == 7 and e["data"].get("y") == 5 for e in mouse_entries),
          repr(mouse_entries))
except ModuleNotFoundError:
    raise

# 4e. Overlay: enqueue is thread-safe and stop() is idempotent. Don't start Tk here.
from typefreq.overlay import Overlay, _compute_origin, _monitor_containing  # noqa: E402

ov = Overlay()
ov.enqueue("teh", "the")
ov.enqueue("becuase", "because")
check("overlay queue accepted 2 items", ov._queue.qsize() == 2)
ov.stop(); ov.stop()
check("overlay stop is idempotent", True)

# 4f. Monitor-aware positioning: simulate user's 3-monitor layout
# (eDP-1 at +2304+2304 2560x1600, DP-5 at +2304+0 4096x2304, DP-8 at +0+0 2304x4096).
mons = [
    {"x": 2304, "y": 2304, "w": 2560, "h": 1600},  # eDP-1 (laptop)
    {"x": 2304, "y": 0,    "w": 4096, "h": 2304},  # DP-5 (top)
    {"x": 0,    "y": 0,    "w": 2304, "h": 4096},  # DP-8 (left, rotated)
]
mid_laptop = _monitor_containing(3500, 3000, mons)
check("cursor at (3500,3000) is on laptop monitor",
      mid_laptop is not None and mid_laptop["x"] == 2304 and mid_laptop["y"] == 2304,
      repr(mid_laptop))
top_monitor = _monitor_containing(4000, 1000, mons)
check("cursor at (4000,1000) is on top monitor",
      top_monitor is not None and top_monitor["x"] == 2304 and top_monitor["y"] == 0,
      repr(top_monitor))

# Quick check that the toast is clamped to the cursor's monitor by patching
# _get_monitors / _mouse_position. (Not depending on a real X server.)
import typefreq.overlay as _ov_mod  # noqa: E402
_ov_mod._monitor_cache = mons
_ov_mod._monitor_cache_at = time.monotonic() + 9999  # never refresh during test
_ov_mod._mouse_position = lambda default: (4500, 3500)  # near bottom-right of laptop
x, y = _compute_origin("cursor", 6400, 4096, 250, 80, 16, 20)
check("toast on laptop stays in laptop bounds",
      2304 <= x and x + 250 <= 2304 + 2560 and 2304 <= y and y + 80 <= 2304 + 1600,
      f"x={x} y={y}")
# Cursor near bottom of monitor -> toast should flip ABOVE the cursor
_ov_mod._mouse_position = lambda default: (3500, 3850)
x, y = _compute_origin("cursor", 6400, 4096, 250, 80, 16, 20)
check("toast flips above when cursor is near bottom",
      y < 3850,
      f"x={x} y={y} (cursor y=3850)")

# 4g. _compute_origin with caret_pos given takes precedence over mouse pointer.
_ov_mod._mouse_position = lambda default: (0, 0)  # would put toast far away
x, y = _compute_origin("cursor", 6400, 4096, 250, 80, 16, 20, caret_pos=(3500, 3000))
mon = _monitor_containing(3500, 3000, mons)
check("caret_pos overrides mouse pointer",
      mon is not None and mon["x"] <= x < mon["x"] + mon["w"],
      f"x={x} y={y}")

# 4h. IMEMonitor: returns a bool whether or not fcitx5 is running; cache is stable.
ime = IMEMonitor()
v1 = ime.is_composing()
v2 = ime.is_composing()
check("IMEMonitor returns a stable bool", isinstance(v1, bool) and v1 == v2, f"v1={v1} v2={v2}")

# 4i. CaretTracker initialises whether or not AT-SPI is available.
caret = CaretTracker()
check("CaretTracker constructs without raising", True, f"available={caret.available}")
pos = caret.get_position()
check("CaretTracker returns None before any caret event", pos is None, repr(pos))
caret.stop()  # idempotent

# 4j. Tracker idle-reset: buffer must be cleared when too much time passes
# between keystrokes. Regression test for the "vercommit" bug — stale buffer
# fragments from before a mouse click / window switch leaking into a new word.
try:
    import typefreq.tracker as _tracker_mod  # noqa: E402
    from typefreq.tracker import Tracker  # noqa: E402
    from typefreq.config import IDLE_TIMEOUT_S as _ITS  # noqa: E402

    t = Tracker(on_word=lambda _w: None)
    # Case 1: same instant -> no reset (gap is zero, well below timeout).
    t._buf[:] = list("ver"); t._last_activity_at = 100.0
    reset = t._apply_idle_reset(100.0)
    check("idle_reset: same instant doesn't clear buffer",
          reset is False and t._buf == ["v", "e", "r"], f"buf={t._buf}")
    # Case 2: within timeout -> no reset.
    t._buf[:] = list("ver"); t._last_activity_at = 100.0; t.idle_resets = 0
    reset = t._apply_idle_reset(100.0 + _ITS - 0.1)
    check("idle_reset: within timeout doesn't clear",
          reset is False and t._buf == ["v", "e", "r"] and t.idle_resets == 0,
          f"buf={t._buf} resets={t.idle_resets}")
    # Case 3: past timeout -> reset.
    t._buf[:] = list("ver"); t._last_activity_at = 100.0; t.idle_resets = 0
    reset = t._apply_idle_reset(100.0 + _ITS + 0.5)
    check("idle_reset: past timeout clears buffer",
          reset is True and t._buf == [] and t.idle_resets == 1,
          f"buf={t._buf} resets={t.idle_resets}")
    # Case 4: empty buffer + idle again -> never counts as a reset.
    t._buf[:] = []; t._last_activity_at = 100.0; t.idle_resets = 0
    reset = t._apply_idle_reset(100.0 + _ITS + 5.0)
    check("idle_reset: empty buffer never counts as a reset",
          reset is False and t.idle_resets == 0, f"resets={t.idle_resets}")
    # Case 5: simulate the actual user bug — old "ver" + gap + "Commit".
    t._buf[:] = list("ver"); t._last_activity_at = 100.0; t.idle_resets = 0
    # Long pause; user clicks/switches windows.
    t._apply_idle_reset(105.0)  # gap=5s, well past 1.5s
    # Now they type Commit one char at a time, each within timeout.
    for i, ch in enumerate("Commit"):
        t._apply_idle_reset(105.0 + (i + 1) * 0.1)
        t._buf.append(ch)
    check("idle_reset: stale 'ver' is discarded before 'Commit'",
          "".join(t._buf) == "Commit" and t.idle_resets == 1,
          f"buf={''.join(t._buf)!r} resets={t.idle_resets}")

    # Shared fake key event for tracker _handle_key tests.
    class _FakeKE:
        def __init__(self, keycode, keystate):
            self.keycode = keycode
            self.keystate = keystate

    # Recorder integration: raw key actions and tracker decisions are
    # available when the user enables the debug recorder.
    rec_keys = InputRecorder(limit=20)
    rec_keys.set_enabled(True)
    emitted_rec: list[str] = []
    t_rec = Tracker(on_word=emitted_rec.append, input_recorder=rec_keys)
    for ch in "helo":
        t_rec._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t_rec._handle_key(_FakeKE("KEY_SPACE", 1))
    rec_entries = rec_keys.snapshot()["entries"]
    rec_actions = [e["action"] for e in rec_entries]
    check("input recorder captures keyboard key_down events",
          "key_down" in rec_actions and any(
              e["action"] == "key_down" and e["data"].get("key") == "KEY_H"
              for e in rec_entries
          ),
          repr(rec_entries))
    check("input recorder captures emitted words",
          emitted_rec == ["helo"] and any(
              e["action"] == "word_emitted"
              and e["data"].get("raw") == "helo"
              and e["data"].get("normalized") == "helo"
              for e in rec_entries
          ),
          f"emitted={emitted_rec} entries={rec_entries}")

    class _AlwaysActiveGuard:
        def is_active(self): return True

    rec_secure = InputRecorder(limit=10)
    rec_secure.set_enabled(True)
    t_secure = Tracker(
        on_word=lambda _w: None,
        locker_monitor=_AlwaysActiveGuard(),
        input_recorder=rec_secure,
    )
    t_secure._handle_key(_FakeKE("KEY_S", 1))
    secure_entries = rec_secure.snapshot()["entries"]
    check("input recorder redacts secure-context dropped keys",
          len(secure_entries) == 1
          and secure_entries[0]["action"] == "secure_context_drop"
          and "KEY_S" not in repr(secure_entries[0]["data"]),
          repr(secure_entries))

    # Case 6: pausing mid-word should not let the resumed suffix become a
    # standalone word/typo. The next word after a boundary still records.
    emitted_idle: list[str] = []
    t_idle = Tracker(on_word=emitted_idle.append)
    fake_now = {"t": 1000.0}
    original_monotonic = _tracker_mod.time.monotonic
    try:
        _tracker_mod.time.monotonic = lambda: fake_now["t"]
        for ch in "recog":
            fake_now["t"] += 0.1
            t_idle._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
        fake_now["t"] += _ITS + 0.5
        for ch in "nize":
            fake_now["t"] += 0.1
            t_idle._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
        fake_now["t"] += 0.1
        t_idle._handle_key(_FakeKE("KEY_SPACE", 1))
        check("idle_reset: resumed suffix is suppressed",
              emitted_idle == [] and t_idle.idle_resets == 1,
              f"emitted={emitted_idle} resets={t_idle.idle_resets}")

        for ch in "hello":
            fake_now["t"] += 0.1
            t_idle._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
        fake_now["t"] += 0.1
        t_idle._handle_key(_FakeKE("KEY_SPACE", 1))
        check("idle_reset: word after suppressed suffix records normally",
              emitted_idle == ["hello"], f"emitted={emitted_idle}")
    finally:
        _tracker_mod.time.monotonic = original_monotonic

    # 4i. Ctrl+Backspace: discard in-progress word, do NOT flush as typo.
    # Also verify on_backspace fires (so the engine's retract path runs).
    emitted: list[str] = []
    bs_calls: list[int] = []
    t2 = Tracker(on_word=lambda w: emitted.append(w), on_backspace=lambda: bs_calls.append(1))
    # Pre-state: user has typed "hellp" and is now holding Ctrl.
    t2._buf[:] = list("hellp")
    t2._mods.add("KEY_LEFTCTRL")
    t2._last_activity_at = time.monotonic()
    t2._handle_key(_FakeKE("KEY_BACKSPACE", 1))
    check("Ctrl+Backspace: buffer cleared", t2._buf == [], f"buf={t2._buf}")
    check("Ctrl+Backspace: no word was emitted (not recorded as typo)",
          emitted == [], f"emitted={emitted}")
    check("Ctrl+Backspace: on_backspace was fired",
          len(bs_calls) == 1, f"calls={len(bs_calls)}")

    # Regression: a non-backspace Ctrl chord still flushes the buffer as a word.
    emitted.clear(); bs_calls.clear()
    t2._buf[:] = list("hello")
    t2._last_activity_at = time.monotonic()
    t2._handle_key(_FakeKE("KEY_S", 1))  # Ctrl+S — should still flush "hello"
    check("Ctrl+S still flushes the buffer (regression check)",
          emitted == ["hello"] and bs_calls == [], f"emitted={emitted}")

    # Ctrl+C cancels the current input, so discard the in-progress word.
    emitted.clear(); bs_calls.clear()
    t2._buf[:] = list("wrong")
    t2._mods.add("KEY_LEFTCTRL")
    t2._last_activity_at = time.monotonic()
    t2._handle_key(_FakeKE("KEY_C", 1))
    check("Ctrl+C discards the in-progress word",
          t2._buf == [] and emitted == [] and bs_calls == [],
          f"buf={t2._buf} emitted={emitted} bs_calls={bs_calls}")

    # 4j. Ctrl+Alt+P used to be the global pause hotkey, but it is too easy
    # to collide with application shortcuts. It should only pause with Shift.
    t_hotkey = Tracker(on_word=lambda _w: None)
    t_hotkey._mods.update({"KEY_LEFTCTRL", "KEY_LEFTALT"})
    t_hotkey._handle_key(_FakeKE("KEY_P", 1))
    check("Ctrl+Alt+P does not pause tracking",
          t_hotkey.paused is False, f"paused={t_hotkey.paused}")
    t_hotkey._mods.add("KEY_LEFTSHIFT")
    t_hotkey._handle_key(_FakeKE("KEY_P", 1))
    check("Ctrl+Alt+Shift+P pauses tracking",
          t_hotkey.paused is True, f"paused={t_hotkey.paused}")

    # 4k. Tab is terminal shell completion, not a committed word boundary.
    emitted.clear(); bs_calls.clear()
    t2._mods.clear()
    t2._buf[:] = list("pyth")
    t2._last_activity_at = time.monotonic()
    t2._handle_key(_FakeKE("KEY_TAB", 1))
    check("Tab completion clears buffer without emitting a word",
          t2._buf == [] and emitted == [],
          f"buf={t2._buf} emitted={emitted}")

    # 4l. Ctrl+arrow navigation: arms "skip next word" — the next word
    # typed after navigation is treated as a mid-word insertion and dropped.
    emitted.clear(); bs_calls.clear()
    t3 = Tracker(on_word=lambda w: emitted.append(w))
    t3._mods.add("KEY_LEFTCTRL")
    t3._last_activity_at = time.monotonic()
    t3._handle_key(_FakeKE("KEY_LEFT", 1))
    check("Ctrl+Left sets _skip_next_word", t3._skip_next_word is True)

    # Simulate the user then typing "big" + space.
    t3._mods.discard("KEY_LEFTCTRL")
    t3._buf[:] = list("big")
    t3._flush()
    check("first word after Ctrl+Left is dropped, not emitted",
          emitted == [] and t3.skipped_after_nav == 1,
          f"emitted={emitted} skipped={t3.skipped_after_nav}")
    check("skip flag is consumed after one flush", t3._skip_next_word is False)

    # The SECOND word after navigation is recorded normally.
    t3._buf[:] = list("hello")
    t3._flush()
    check("subsequent words after Ctrl+Left are emitted normally",
          emitted == ["hello"], f"emitted={emitted}")

    # Idle reset also clears the skip flag (user moved on).
    t3._mods.add("KEY_LEFTCTRL")
    t3._handle_key(_FakeKE("KEY_RIGHT", 1))
    check("Ctrl+Right also arms the flag", t3._skip_next_word is True)
    t3._mods.discard("KEY_LEFTCTRL")
    # Force an idle gap.
    t3._last_activity_at = time.monotonic() - (_ITS + 1.0)
    t3._apply_idle_reset(time.monotonic())
    check("idle reset clears the skip flag", t3._skip_next_word is False)

    # 4m. Tracker should rescan for keyboards after a device disappears.
    # This guards the "service stays up but capture never resumes" failure
    # seen after suspend / reconnect / input-device churn.
    class _FakeDev:
        def __init__(self, path: str):
            self.path = path
            self.closed = False
        def close(self):
            self.closed = True

    class _FakeSel:
        def __init__(self):
            self._calls = 0
        def register(self, dev, _mask):
            pass
        def select(self, timeout=0.5):
            self._calls += 1
            if self._calls == 1:
                tracker_ref["t"]._stop.set()
            return []
        def unregister(self, _dev):
            pass
        def close(self):
            pass

    original_find_keyboards = _tracker_mod.find_keyboards
    original_default_selector = _tracker_mod.selectors.DefaultSelector
    original_rescan_interval = _tracker_mod.DEVICE_RESCAN_INTERVAL_S
    try:
        first = [_FakeDev("/dev/input/event1")]
        second = [_FakeDev("/dev/input/event2")]
        calls = {"n": 0}
        tracker_ref: dict[str, object] = {}

        def fake_find_keyboards():
            calls["n"] += 1
            return first if calls["n"] == 1 else second

        _tracker_mod.find_keyboards = fake_find_keyboards
        _tracker_mod.selectors.DefaultSelector = _FakeSel
        _tracker_mod.DEVICE_RESCAN_INTERVAL_S = 0.0

        recvd: list[str] = []
        t4 = _tracker_mod.Tracker(on_word=recvd.append)
        tracker_ref["t"] = t4
        t4.run()
        check("tracker rescans keyboards after a device drop",
              calls["n"] >= 2 and first[0].closed is True and second[0].closed is True,
              f"calls={calls['n']} first_closed={first[0].closed} second_closed={second[0].closed}")
    finally:
        _tracker_mod.find_keyboards = original_find_keyboards
        _tracker_mod.selectors.DefaultSelector = original_default_selector
        _tracker_mod.DEVICE_RESCAN_INTERVAL_S = original_rescan_interval

    # --- Screen locker: keystrokes must be dropped while a locker is up. ---
    from typefreq.locker import LockerMonitor

    class FakeLocker:
        def __init__(self): self.locked = False
        def is_locked(self): return self.locked
        # Tracker uses the generic guard interface now (is_active()), so the
        # fake must expose it too.
        def is_active(self): return self.locked

    fake = FakeLocker()
    emitted: list[str] = []
    t4 = Tracker(on_word=emitted.append, locker_monitor=fake)
    # Baseline: no locker -> word records normally.
    for ch in "hello":
        t4._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t4._handle_key(_FakeKE("KEY_SPACE", 1))
    check("locker off: normal words still flow", emitted == ["hello"], repr(emitted))
    # Lock the screen mid-stream — buffer must be wiped and event ignored.
    for ch in "ab":
        t4._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    fake.locked = True
    skipped_before = t4.locker_skipped
    for ch in "secret":
        t4._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t4._handle_key(_FakeKE("KEY_SPACE", 1))
    check("locker on: keystrokes counted as locker_skipped",
          t4.locker_skipped == skipped_before + len("secret") + 1,
          f"locker_skipped delta={t4.locker_skipped - skipped_before}")
    check("locker on: in-flight buffer was wiped on lock",
          t4._buf == [], f"buf={t4._buf}")
    check("locker on: no word emitted from the locked period",
          emitted == ["hello"], f"emitted={emitted}")
    # Unlock and verify recording resumes.
    fake.locked = False
    for ch in "world":
        t4._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t4._handle_key(_FakeKE("KEY_SPACE", 1))
    check("locker off again: subsequent words flow normally",
          emitted == ["hello", "world"], repr(emitted))

    # LockerMonitor._scan() on a normal test runner -> no locker is running,
    # so it should return False (true positives would mean we'd disable
    # capture for everyone running the test suite).
    lm = LockerMonitor()
    check("LockerMonitor._scan() with no locker active -> False",
          lm._scan() is False)
    check("LockerMonitor.is_locked() defaults to False before start()",
          lm.is_locked() is False)
    check("LockerMonitor.is_active() aliases is_locked()",
          lm.is_active() is False and hasattr(lm, "is_active"))

    # --- Polkit / sudo / askpass guard: same drop-on-active semantics. ---
    from typefreq.polkit import PolkitMonitor

    class FakePolkit:
        def __init__(self): self.active = False
        def is_active(self): return self.active

    fake_pk = FakePolkit()
    emitted5: list[str] = []
    t5 = Tracker(on_word=emitted5.append, polkit_monitor=fake_pk)
    for ch in "hello":
        t5._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t5._handle_key(_FakeKE("KEY_SPACE", 1))
    check("polkit off: words flow normally", emitted5 == ["hello"], repr(emitted5))
    fake_pk.active = True
    skipped_before = t5.polkit_skipped
    for ch in "secret":
        t5._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t5._handle_key(_FakeKE("KEY_SPACE", 1))
    check("polkit on: keystrokes counted as polkit_skipped",
          t5.polkit_skipped == skipped_before + len("secret") + 1,
          f"polkit_skipped delta={t5.polkit_skipped - skipped_before}")
    check("polkit on: no word emitted from the locked period",
          emitted5 == ["hello"], f"emitted={emitted5}")

    # Regression: if a stale/false-positive polkit guard drops the start of
    # "However" and expires before "owever,", the suffix must not become a
    # standalone typo candidate.
    emitted5.clear()
    fake_pk.active = True
    t5._handle_key(_FakeKE("KEY_H", 1))
    fake_pk.active = False
    for ch in "owever":
        t5._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t5._handle_key(_FakeKE("KEY_COMMA", 1))
    check("polkit off after drop: resumed suffix is suppressed",
          emitted5 == [] and t5.polkit_skipped >= skipped_before + len("secret") + 2,
          f"emitted={emitted5} polkit_skipped={t5.polkit_skipped}")

    for ch in "next":
        t5._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t5._handle_key(_FakeKE("KEY_SPACE", 1))
    check("polkit off after suppressed suffix: next word records normally",
          emitted5 == ["next"], f"emitted={emitted5}")

    # Either guard active should drop — verify the OR logic.
    fake_locker6 = FakeLocker()
    fake_polkit6 = FakePolkit()
    emitted6: list[str] = []
    t6 = Tracker(
        on_word=emitted6.append,
        locker_monitor=fake_locker6, polkit_monitor=fake_polkit6,
    )
    fake_polkit6.active = True  # locker off, polkit on
    for ch in "ab":
        t6._handle_key(_FakeKE(f"KEY_{ch.upper()}", 1))
    t6._handle_key(_FakeKE("KEY_SPACE", 1))
    check("either guard active drops keystrokes",
          emitted6 == [] and t6.polkit_skipped >= 2 and t6.locker_skipped == 0,
          f"emitted={emitted6} polkit_skipped={t6.polkit_skipped} locker_skipped={t6.locker_skipped}")

    # PolkitMonitor._scan_processes() should be False on a normal runner —
    # no sudo / pkexec / askpass should be live during the test.
    pkm = PolkitMonitor()
    check("PolkitMonitor._scan_processes() with no auth helper -> False",
          pkm._scan_processes() is False)
    check("PolkitMonitor.is_active() defaults to False before start()",
          pkm.is_active() is False)
except ModuleNotFoundError:
    print("[skip] Tracker idle-reset (evdev not installed in this venv)")

# 5. App + Flask routes (only if evdev is importable).
try:
    import evdev  # noqa: F401
    from typefreq.app import Engine, make_app

    engine = Engine()
    app = make_app(engine)
    client = app.test_client()

    public_origin = "https://typefreq.lue-app.com"
    r = client.get("/api/health", headers={"Origin": public_origin})
    check("GET /api/health -> 200", r.status_code == 200, f"status={r.status_code}")
    health = r.get_json()
    check("health identifies typefreq", health.get("service") == "typefreq", repr(health))
    check("allowed public origin gets CORS header",
          r.headers.get("Access-Control-Allow-Origin") == public_origin,
          repr(dict(r.headers)))

    r = client.options(
        "/api/status",
        headers={
            "Origin": public_origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    check("API preflight -> 204", r.status_code == 204, f"status={r.status_code}")
    check("preflight allows private network access",
          r.headers.get("Access-Control-Allow-Private-Network") == "true",
          repr(dict(r.headers)))

    r = client.get("/api/status", headers={"Origin": "https://example.invalid"})
    check("unknown origin does not get CORS access",
          "Access-Control-Allow-Origin" not in r.headers,
          repr(dict(r.headers)))

    r = client.get("/api/status")
    check("GET /api/status -> 200", r.status_code == 200, f"status={r.status_code}")
    data = r.get_json()
    check("status JSON has db_path", "db_path" in data, repr(list(data.keys()))[:80])
    check("status JSON exposes locker_active + locker_skipped",
          "locker_active" in data and "locker_skipped" in data,
          f"locker_active={data.get('locker_active')!r} locker_skipped={data.get('locker_skipped')!r}")
    check("status JSON exposes polkit_active + polkit_skipped",
          "polkit_active" in data and "polkit_skipped" in data,
          f"polkit_active={data.get('polkit_active')!r} polkit_skipped={data.get('polkit_skipped')!r}")
    check("status JSON exposes active keyboard diagnostics",
          "active_keyboard_count" in data and "active_keyboard_paths" in data
          and "device_read_errors" in data and "keyboard_rescans" in data,
          repr({k: data.get(k) for k in (
              "active_keyboard_count", "active_keyboard_paths",
              "device_read_errors", "keyboard_rescans",
          )}))
    check("status JSON exposes input recorder diagnostics",
          "input_recording_enabled" in data and "input_recording_count" in data
          and data.get("input_recording_enabled") is False
          and data.get("input_recording_count") == 0,
          repr({k: data.get(k) for k in (
              "input_recording_enabled", "input_recording_count",
          )}))

    r = client.get("/api/debug/input-recorder")
    recorder_state = r.get_json() if r.status_code == 200 else {}
    check("GET /api/debug/input-recorder -> 200",
          r.status_code == 200 and recorder_state.get("enabled") is False
          and recorder_state.get("count") == 0,
          f"status={r.status_code} body={recorder_state!r}")

    r = client.post("/api/debug/input-recorder", json={"enabled": True})
    recorder_state = r.get_json() if r.status_code == 200 else {}
    check("POST /api/debug/input-recorder enables recording",
          r.status_code == 200 and recorder_state.get("enabled") is True
          and engine.input_recorder.enabled is True
          and engine.read(db.get_meta, "input_recorder_enabled") == "1",
          f"status={r.status_code} body={recorder_state!r}")

    engine.input_recorder.record("system", "smoke_marker", {"ok": True})
    r = client.get("/api/debug/input-recorder/export")
    export_payload = r.get_json() if r.status_code == 200 else {}
    check("GET /api/debug/input-recorder/export returns JSON attachment",
          r.status_code == 200
          and "attachment" in r.headers.get("Content-Disposition", "")
          and any(e["action"] == "smoke_marker" for e in export_payload.get("entries", [])),
          f"status={r.status_code} headers={dict(r.headers)!r} body={export_payload!r}")

    r = client.post("/api/debug/input-recorder/clear")
    clear_state = r.get_json() if r.status_code == 200 else {}
    check("POST /api/debug/input-recorder/clear empties recorder",
          r.status_code == 200 and clear_state.get("count") == 0
          and engine.input_recorder.snapshot()["entries"] == [],
          f"status={r.status_code} body={clear_state!r}")

    class FakeServiceController:
        def __init__(self):
            self.calls = 0
        def restart(self):
            self.calls += 1
            return {"scheduled": True, "command": ["fake-systemctl", "restart"]}

    fake_service = FakeServiceController()
    engine.service_controller = fake_service
    r = client.post("/api/service/restart")
    service_restart = r.get_json() if r.status_code == 202 else {}
    check("POST /api/service/restart schedules restart",
          r.status_code == 202 and service_restart.get("ok") is True
          and service_restart.get("scheduled") is True
          and fake_service.calls == 1,
          f"status={r.status_code} body={service_restart!r} calls={fake_service.calls}")

    r = client.get("/api/stats/today")
    check("GET /api/stats/today -> 200", r.status_code == 200)
    data = r.get_json()
    check("today JSON has top_words", "top_words" in data, str(len(data.get("top_words", []))) + " words")

    r = client.get("/api/stats/leaderboards")
    check("GET /api/stats/leaderboards -> 200", r.status_code == 200)
    lb = r.get_json()
    check("leaderboards JSON has today/week/month/year/alltime",
          all(k in lb for k in ("today", "week", "month", "year", "alltime")),
          repr(list(lb.keys())))
    check("leaderboards.today.top_words is a list",
          isinstance(lb.get("today", {}).get("top_words"), list),
          repr(type(lb.get("today", {}).get("top_words")).__name__))
    check("leaderboards.alltime.top_words capped at 25",
          len(lb["alltime"]["top_words"]) <= 25,
          f"{len(lb['alltime']['top_words'])} rows")

    r = client.get("/")
    check("GET / -> 200 (dashboard renders)", r.status_code == 200 and b"Typefreq" in r.data)
    local_restart_visible = (
        b'id="service-restart-btn"' in r.data and b"Restart service" in r.data
    )
    check("local dashboard exposes restart service control",
          local_restart_visible,
          "" if local_restart_visible else r.data[:200].decode("utf-8", "replace"))

    site_source = (Path(__file__).resolve().parent / "src/pages/index.astro").read_text()
    check("public dashboard exposes service action controls",
          'id="service-restart-btn"' in site_source
          and 'id="service-reinstall-btn"' in site_source)

    # 5b. Cross-thread DB access (Flask handlers run in worker threads with
    # threaded=True). Regression test for a SQLite check_same_thread crash we
    # hit during live testing.
    import threading
    err: list[Exception] = []

    def worker():
        try:
            engine.read(__import__("typefreq").db.totals)
        except Exception as e:
            err.append(e)

    t = threading.Thread(target=worker)
    t.start(); t.join(timeout=2.0)
    check("engine.read() works from a different thread", not err, repr(err))

    # 5c. When a typo is detected, word_counts records the SUGGESTION, not the
    # literal typo. The typo is still preserved in the typos table.
    engine.input_recorder.set_enabled(True)
    engine.input_recorder.clear()
    before = {r["word"]: r["count"] for r in engine.read(db.top_words, limit=50)}
    typos_before = len(engine.read(db.recent_typos, limit=100))
    engine._on_word("becausee")
    after = {r["word"]: r["count"] for r in engine.read(db.top_words, limit=50)}
    typos_after = engine.read(db.recent_typos, limit=100)
    check("typo -> word_counts records suggestion 'because'",
          after.get("because", 0) == before.get("because", 0) + 1,
          f"before={before.get('because', 0)} after={after.get('because', 0)}")
    check("typo -> word_counts does NOT record 'becausee'",
          after.get("becausee", 0) == before.get("becausee", 0),
          f"before={before.get('becausee', 0)} after={after.get('becausee', 0)}")
    check("typo -> typos table preserves the original typo",
          len(typos_after) == typos_before + 1 and any(
              t["word"] == "becausee" and t["suggestion"] == "because" for t in typos_after
          ),
          f"typos rows: {len(typos_after)} (was {typos_before})")
    check("input recorder captures engine typo_recorded",
          any(
              e["source"] == "engine" and e["action"] == "typo_recorded"
              and e["data"].get("word") == "becausee"
              and e["data"].get("suggestion") == "because"
              for e in engine.input_recorder.snapshot()["entries"]
          ),
          repr(engine.input_recorder.snapshot()))

    # 5d. Custom words: whitelist suppresses the typo and counts the original.
    # Spell-check sanity: plain check still flags "becausee".
    mis, sug = engine.spell.check("becausee")
    check("baseline: spell flags 'becausee' before whitelisting",
          mis is True and sug == "because", f"mis={mis} sug={sug}")

    # API: add a custom word that's an actual misspelling and verify
    # subsequent checks treat it as correct.
    r = client.post("/api/custom-words", json={"word": "Becausee"})
    check("POST /api/custom-words 'Becausee' -> 200", r.status_code == 200)
    data = r.get_json()
    check("POST normalizes to lowercase", data["word"] == "becausee", repr(data))
    check("POST removed past typo rows for the word",
          data["typos_removed"] >= 1, f"typos_removed={data.get('typos_removed')}")
    check("POST returns the updated list", any(w["word"] == "becausee" for w in data["words"]))

    # Spell-checker now treats it as correct.
    mis2, _ = engine.spell.check("becausee")
    check("after whitelist: spell.check('becausee') -> not misspelled", mis2 is False)

    # _on_word now records 'becausee' itself (no typo, no suggestion).
    before2 = {r["word"]: r["count"] for r in engine.read(db.top_words, limit=50)}
    typos_before2 = len(engine.read(db.recent_typos, limit=100))
    engine._on_word("becausee")
    after2 = {r["word"]: r["count"] for r in engine.read(db.top_words, limit=50)}
    check("whitelisted word increments its own count",
          after2.get("becausee", 0) == before2.get("becausee", 0) + 1,
          f"before={before2.get('becausee', 0)} after={after2.get('becausee', 0)}")
    check("whitelisted word does NOT add a new typo row",
          len(engine.read(db.recent_typos, limit=100)) == typos_before2,
          "typo rows changed unexpectedly")

    # GET endpoint returns the same word.
    r = client.get("/api/custom-words")
    check("GET /api/custom-words -> 200", r.status_code == 200)
    check("GET returns the whitelisted word",
          any(w["word"] == "becausee" for w in r.get_json()["words"]))

    # Idempotent POST: adding again returns added=False.
    r = client.post("/api/custom-words", json={"word": "becausee"})
    check("re-POST same word -> added=False", r.get_json()["added"] is False)

    # Validation: rejects words that don't normalize.
    r = client.post("/api/custom-words", json={"word": "p4ssw0rd"})
    check("POST rejects digit-containing word with 400", r.status_code == 400, f"got {r.status_code}")
    r = client.post("/api/custom-words", json={"word": ""})
    check("POST rejects empty word with 400", r.status_code == 400)

    # DELETE: removes from set + DB. Spell-check flags it again.
    r = client.delete("/api/custom-words/becausee")
    check("DELETE /api/custom-words/becausee -> 200", r.status_code == 200)
    check("DELETE response says removed=True", r.get_json()["removed"] is True)
    mis3, sug3 = engine.spell.check("becausee")
    check("after DELETE: spell.check flags 'becausee' again",
          mis3 is True and sug3 == "because", f"mis={mis3} sug={sug3}")

    # Re-DELETE: idempotent removed=False.
    r = client.delete("/api/custom-words/becausee")
    check("re-DELETE same word -> removed=False", r.get_json()["removed"] is False)

    # 5e. Retract-on-correction: typo recorded immediately, undone on backspace.
    # We test via direct engine method calls — the tracker thread isn't running
    # in this smoke test, so we exercise _on_word + _on_backspace directly.
    import time as _time
    before_words = {r["word"]: r["count"] for r in engine.read(db.top_words, limit=50)}
    typos_before = len(engine.read(db.recent_typos, limit=100))
    retracted_before = engine.typos_retracted

    engine._on_word("recieve")  # canonical typo
    mid_words = {r["word"]: r["count"] for r in engine.read(db.top_words, limit=50)}
    typos_mid = len(engine.read(db.recent_typos, limit=100))
    check("typo recorded immediately (no delay)",
          mid_words.get("receive", 0) == before_words.get("receive", 0) + 1
          and typos_mid == typos_before + 1,
          f"receive: {before_words.get('receive', 0)} -> {mid_words.get('receive', 0)}, typos: {typos_before} -> {typos_mid}")
    check("typo queued for possible retraction",
          len(engine._recent_typos) >= 1 and engine._recent_typos[-1][2] == "recieve")

    # Backspace within window: undoes everything.
    engine._on_backspace()
    after_words = {r["word"]: r["count"] for r in engine.read(db.top_words, limit=50)}
    typos_after = len(engine.read(db.recent_typos, limit=100))
    check("retract: word_counts undone",
          after_words.get("receive", 0) == before_words.get("receive", 0),
          f"before={before_words.get('receive', 0)} after={after_words.get('receive', 0)}")
    check("retract: typo row removed",
          typos_after == typos_before, f"before={typos_before} after={typos_after}")
    check("retract: typos_retracted incremented",
          engine.typos_retracted == retracted_before + 1,
          f"before={retracted_before} after={engine.typos_retracted}")
    check("input recorder captures engine typo_retracted",
          any(
              e["source"] == "engine" and e["action"] == "typo_retracted"
              and e["data"].get("word") == "recieve"
              and e["data"].get("suggestion") == "receive"
              for e in engine.input_recorder.snapshot()["entries"]
          ),
          repr(engine.input_recorder.snapshot()))

    # Backspace with no recent typo is a no-op.
    retracted_before2 = engine.typos_retracted
    engine._on_backspace()
    check("backspace with no pending typo is a no-op",
          engine.typos_retracted == retracted_before2)

    # Aged-out typos can't be retracted. Force the deque entry to look old.
    from typefreq.config import TYPO_RETRACT_WINDOW_S
    engine._on_word("recieve")
    # Mutate the just-added entry to look like it was recorded long ago.
    old_mono = _time.monotonic() - TYPO_RETRACT_WINDOW_S - 5.0
    last = engine._recent_typos[-1]
    engine._recent_typos[-1] = (old_mono, last[1], last[2], last[3])
    typos_pre = len(engine.read(db.recent_typos, limit=100))
    retracted_pre = engine.typos_retracted
    engine._on_backspace()
    typos_post = len(engine.read(db.recent_typos, limit=100))
    check("aged-out typo is NOT retracted on backspace",
          typos_post == typos_pre and engine.typos_retracted == retracted_pre,
          f"typos {typos_pre}->{typos_post}, retracted {retracted_pre}->{engine.typos_retracted}")

    engine.shutdown()
except ModuleNotFoundError:
    print("[skip] Flask routes (evdev not installed in this venv — install python3-dev and re-run install.sh)")

print()
if errors:
    print(f"{len(errors)} failure(s):", errors)
    sys.exit(1)
print("All smoke checks passed.")
