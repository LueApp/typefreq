"""Mouse event monitor for input debug recording."""
from __future__ import annotations

import logging
import selectors
import threading
from threading import Event

import evdev
from evdev import ecodes

from .config import DEVICE_BLOCKLIST

log = logging.getLogger("typefreq.mouse")

MOUSE_RESCAN_INTERVAL_S = 5.0
BUTTON_CODES = {
    ecodes.BTN_LEFT,
    ecodes.BTN_RIGHT,
    ecodes.BTN_MIDDLE,
    ecodes.BTN_SIDE,
    ecodes.BTN_EXTRA,
    ecodes.BTN_FORWARD,
    ecodes.BTN_BACK,
}


def find_mice() -> list[evdev.InputDevice]:
    """Return readable devices that look like mice or touchpads."""
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
        keys = set(caps.get(ecodes.EV_KEY, []))
        rels = set(caps.get(ecodes.EV_REL, []))
        abss = set(caps.get(ecodes.EV_ABS, []))
        has_button = bool(keys & BUTTON_CODES)
        has_motion = bool(
            {ecodes.REL_X, ecodes.REL_Y} & rels
            or {ecodes.ABS_X, ecodes.ABS_Y} & abss
        )
        if has_button and has_motion:
            out.append(dev)
        else:
            dev.close()
    return out


class MouseMonitor:
    """Background mouse listener used only by the debug recorder."""

    def __init__(self, input_recorder, device_finder=find_mice) -> None:
        self._input_recorder = input_recorder
        self._device_finder = device_finder
        self._stop = Event()
        self._thread: threading.Thread | None = None
        self._x = 0
        self._y = 0
        self.active_mouse_paths: list[str] = []
        self.device_read_errors = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, name="typefreq-mouse", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        devices = self._device_finder()
        if not devices:
            log.info("no readable mouse devices found")
            return
        log.info("listening on %d mouse device(s): %s", len(devices), [d.path for d in devices])

        sel = selectors.DefaultSelector()
        active_devices: dict[str, evdev.InputDevice] = {}
        for dev in devices:
            try:
                sel.register(dev, selectors.EVENT_READ)
                active_devices[dev.path] = dev
            except Exception:
                dev.close()
        self._set_active_mouse_paths(active_devices)

        try:
            while not self._stop.is_set():
                for key, _mask in sel.select(timeout=0.5):
                    dev: evdev.InputDevice = key.fileobj  # type: ignore[assignment]
                    try:
                        for event in dev.read():
                            self._handle_event(event)
                    except OSError as e:
                        log.warning("mouse device %s read error: %s — dropping", dev.path, e)
                        try:
                            sel.unregister(dev)
                        except Exception:
                            pass
                        dev.close()
                        active_devices.pop(dev.path, None)
                        self.device_read_errors += 1
                        self._set_active_mouse_paths(active_devices)
        finally:
            for dev in active_devices.values():
                try:
                    dev.close()
                except Exception:
                    pass
            self._set_active_mouse_paths({})
            sel.close()

    def _set_active_mouse_paths(self, devices: dict[str, evdev.InputDevice]) -> None:
        self.active_mouse_paths = sorted(devices)

    def _handle_event(self, event) -> None:
        if event.type == ecodes.EV_REL:
            if event.code == ecodes.REL_X:
                self._x += int(event.value)
            elif event.code == ecodes.REL_Y:
                self._y += int(event.value)
            elif event.code in (ecodes.REL_WHEEL, ecodes.REL_HWHEEL):
                axis = "vertical" if event.code == ecodes.REL_WHEEL else "horizontal"
                self._record("mouse_scroll", {"axis": axis, "delta": int(event.value)})
            return

        if event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_X:
                self._x = int(event.value)
            elif event.code == ecodes.ABS_Y:
                self._y = int(event.value)
            return

        if event.type == ecodes.EV_KEY and event.code in BUTTON_CODES and event.value in (0, 1):
            self._record(
                "mouse_click",
                {
                    "button": _event_code_name(ecodes.EV_KEY, event.code),
                    "pressed": bool(event.value),
                },
            )

    def _record(self, action: str, data: dict) -> None:
        data = {**data, "x": self._x, "y": self._y}
        try:
            self._input_recorder.record("mouse", action, data)
        except Exception:
            log.exception("input recorder failed")


def _event_code_name(event_type: int, code: int) -> str:
    names = ecodes.bytype.get(event_type, {}).get(code)
    if isinstance(names, (list, tuple)):
        return str(names[0])
    if names is None:
        return str(code)
    return str(names)
