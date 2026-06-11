# Input Debug Recording Design

## Goal

Add a website-controlled debug recorder so a user can temporarily capture recent keyboard and mouse actions, then export the log when a typo is detected incorrectly or disappears too quickly to diagnose.

## Scope

The recorder is disabled by default. The dashboard can enable or disable it through the local service, view the current recorder state, and download the current log as JSON. The log is bounded in memory and persisted only as a setting flag in SQLite `meta`; captured event history resets when the service restarts.

## Architecture

Create a small `InputRecorder` component owned by `Engine`. `Tracker` calls it on keyboard events and word-processing decisions. A lightweight mouse listener thread feeds mouse events when recording is enabled. The Flask API exposes state, toggle, clear, and export endpoints. The public Astro dashboard and Flask fallback dashboard show a debug panel with a toggle and download/clear actions.

## Data Model

Each log entry is JSON-safe:

- `seq`: monotonically increasing integer
- `ts`: Unix timestamp with milliseconds
- `source`: `keyboard`, `mouse`, `tracker`, `engine`, or `system`
- `action`: short event name such as `key_down`, `mouse_click`, `word_emitted`, `typo_recorded`, `typo_retracted`
- `data`: structured context for debugging

The recorder stores the last 2,000 events by default. Keyboard entries include evdev key names, key state, modifier names, buffer length/content, paused state, IME/secure-context decisions, and emitted raw/normalized words. Mouse entries include button, pressed/released state, and coordinates. Typo events include the word and suggestion.

## Privacy And Security

The recorder is off by default and must be enabled from the dashboard. Secure-context guards keep priority: when locker or polkit is active, the tracker records at most a redacted `secure_context_drop` diagnostic and never records the dropped key name or buffer content. The API remains bound to configured local origins using the existing CORS policy. Export is explicit and user-triggered.

## API

- `GET /api/debug/input-recorder`: returns `enabled`, `count`, `limit`, and `max_seq`.
- `POST /api/debug/input-recorder`: accepts `{ "enabled": true|false }`, persists the setting in `meta`, and records a system event.
- `POST /api/debug/input-recorder/clear`: clears in-memory history.
- `GET /api/debug/input-recorder/export`: returns a JSON attachment containing recorder state and entries.

`GET /api/status` also includes `input_recording_enabled` and `input_recording_count` so the existing dashboard polling can refresh the panel.

## UI

Both dashboards add a compact `Input debug log` panel near the custom words section. It contains a checkbox-style toggle, event count, Download JSON button, Clear button, and a short privacy note. The public site includes English and Chinese strings.

## Testing

Extend `smoke_test.py` first:

- Unit-level recorder tests: disabled ignores events, enabled stores bounded entries, clear empties history, export payload is JSON-safe.
- Tracker tests: fake key events produce recorder entries and secure-context drops do not include raw key names.
- App/API tests: status exposes recorder fields, toggle persists through `meta`, clear/export endpoints work.

Run `python smoke_test.py` for Python behavior and `npm run build` for the public site.
