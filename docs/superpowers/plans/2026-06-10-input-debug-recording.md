# Input Debug Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a website-controlled input debug recorder with JSON export for diagnosing incorrect typo detection.

**Architecture:** Add an `InputRecorder` class for bounded event history, inject it into `Tracker` and `Engine`, expose Flask endpoints, and add dashboard controls to the public Astro UI plus the local Flask template. Mouse capture runs in a small optional thread and records only when enabled.

**Tech Stack:** Python 3, evdev, Flask, SQLite `meta`, Astro single-page dashboard, vanilla JavaScript, CSS.

---

### Task 1: Recorder Core

**Files:**
- Create: `typefreq/input_recorder.py`
- Modify: `smoke_test.py`

- [ ] **Step 1: Write failing tests**

Add smoke checks that instantiate `InputRecorder`, verify disabled recording stores nothing, enabled recording stores bounded JSON-safe events, `clear()` empties entries, and `export_payload()` returns state plus entries.

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python smoke_test.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'typefreq.input_recorder'`.

- [ ] **Step 3: Implement recorder**

Create `InputRecorder` with `set_enabled()`, `enabled`, `record()`, `snapshot()`, `clear()`, and `export_payload()`. Use a `deque(maxlen=limit)` and a `Lock`; keep entries JSON-safe by recursively converting unknown values to strings.

- [ ] **Step 4: Run test to verify it passes**

Run: `rtk python smoke_test.py`
Expected: recorder checks pass.

### Task 2: Tracker And Engine Events

**Files:**
- Modify: `typefreq/tracker.py`
- Modify: `typefreq/app.py`
- Modify: `smoke_test.py`

- [ ] **Step 1: Write failing tests**

Extend fake tracker key tests to pass a recorder, enable it, type a short word, and assert key/word events appear. Add a secure-context test asserting a drop event does not include the raw key name.

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python smoke_test.py`
Expected: FAIL because `Tracker` does not accept or call a recorder.

- [ ] **Step 3: Implement keyboard and engine recording**

Inject `input_recorder` into `Tracker`. Record key events after key name extraction, record decision events for paused, IME skip, idle reset, shortcuts, flush/word emission, and backspace. In `Engine._on_word()` and `_on_backspace()`, record typo detected/retracted events.

- [ ] **Step 4: Run test to verify it passes**

Run: `rtk python smoke_test.py`
Expected: tracker and engine recorder checks pass.

### Task 3: Mouse Event Capture

**Files:**
- Create: `typefreq/mouse.py`
- Modify: `typefreq/app.py`
- Modify: `smoke_test.py`

- [ ] **Step 1: Write failing tests**

Add a test around a helper that converts evdev mouse button events into recorder entries without requiring real devices.

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python smoke_test.py`
Expected: FAIL because `typefreq.mouse` does not exist.

- [ ] **Step 3: Implement mouse monitor**

Create `MouseMonitor` with `start()`, `stop()`, and injectable device discovery. Use evdev to read pointer devices, maintain last X/Y from relative and absolute events, and record button press/release events only when the recorder is enabled.

- [ ] **Step 4: Run test to verify it passes**

Run: `rtk python smoke_test.py`
Expected: mouse helper checks pass.

### Task 4: Local API

**Files:**
- Modify: `typefreq/app.py`
- Modify: `smoke_test.py`

- [ ] **Step 1: Write failing API tests**

Add Flask client checks for `GET /api/status` recorder fields, `GET/POST /api/debug/input-recorder`, `POST /api/debug/input-recorder/clear`, and `GET /api/debug/input-recorder/export`.

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python smoke_test.py`
Expected: FAIL with 404 or missing JSON fields.

- [ ] **Step 3: Implement API**

Persist enabled state in `meta` as `input_recorder_enabled`. Add state, toggle, clear, and export routes with JSON responses/attachment. Start/stop the mouse monitor with engine lifecycle.

- [ ] **Step 4: Run test to verify it passes**

Run: `rtk python smoke_test.py`
Expected: API checks pass.

### Task 5: Dashboard Controls

**Files:**
- Modify: `src/pages/index.astro`
- Modify: `src/styles/site.css`
- Modify: `typefreq/templates/index.html`

- [ ] **Step 1: Add UI controls**

Add an `Input debug log` panel with a toggle, event count, Download JSON, and Clear actions. Wire public-site controls to the new local API and refresh them from `/api/status`.

- [ ] **Step 2: Build frontend**

Run: `rtk npm run build`
Expected: Astro build succeeds.

- [ ] **Step 3: Run full smoke test**

Run: `rtk python smoke_test.py`
Expected: all smoke checks pass.
