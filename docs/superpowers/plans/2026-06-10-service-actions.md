# Service Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add connected-dashboard controls to restart the local Typefreq service and reinstall it through the existing web2local/installer flow.

**Architecture:** Add a small backend service controller that schedules `systemctl --user restart typefreq.service` after the HTTP response is returned. Add public-dashboard service controls: restart uses the local API; reinstall checks web2local and reruns the existing installer deployment, falling back to the configured installer download.

**Tech Stack:** Python/Flask, systemd user service, Astro public dashboard, vanilla JavaScript.

---

### Task 1: Restart API

**Files:**
- Create: `typefreq/service_control.py`
- Modify: `typefreq/app.py`
- Modify: `smoke_test.py`

- [ ] **Step 1: Write failing test**

Add a smoke-test fake service controller and assert `POST /api/service/restart` returns HTTP 202 and calls the controller once.

- [ ] **Step 2: Run failing test**

Run: `rtk venv/bin/python smoke_test.py`
Expected: FAIL because `/api/service/restart` returns 404.

- [ ] **Step 3: Implement minimal API**

Create `ServiceController.restart()` to schedule `systemctl --user restart typefreq.service` on a short timer, wire `Engine.service_controller`, and add the Flask endpoint.

- [ ] **Step 4: Run passing test**

Run: `rtk venv/bin/python smoke_test.py`
Expected: all smoke checks pass.

### Task 2: Public Dashboard Buttons

**Files:**
- Modify: `src/pages/index.astro`
- Modify: `src/styles/site.css`

- [ ] **Step 1: Add UI and handlers**

Add a connected-dashboard service panel with Restart service and Reinstall service buttons. Restart calls `/api/service/restart`; reinstall uses web2local when available and otherwise calls the existing configured installer download.

- [ ] **Step 2: Build frontend**

Run: `rtk npm run build`
Expected: Astro build succeeds.
