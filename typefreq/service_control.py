"""Local service management helpers."""
from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable, Sequence

log = logging.getLogger("typefreq.service")


class ServiceController:
    """Schedules service commands outside the request thread."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        delay_s: float = 0.25,
    ) -> None:
        self._runner = runner
        self._delay_s = delay_s

    def restart(self) -> dict:
        command = ["systemctl", "--user", "restart", "typefreq.service"]
        timer = threading.Timer(self._delay_s, self._run, args=(command,))
        timer.daemon = True
        timer.start()
        return {"scheduled": True, "command": command}

    def _run(self, command: Sequence[str]) -> None:
        try:
            self._runner(list(command), check=False, timeout=30)
        except Exception:
            log.exception("service command failed: %s", " ".join(command))
