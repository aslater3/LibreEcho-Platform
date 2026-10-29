"""Run the simulated adapters on a socket directory and drive JSON scenarios."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS, Adapter
from .protocol import EventLog, serve

SOCKETS = {"audio": "audio.sock", "led": "led.sock", "network": "network.sock", "bluetooth": "bluetooth.sock"}


class Simulator:
    def __init__(self, run_dir: Path, log_path: Path | None = None, adapters: list[str] | None = None):
        self.run_dir = Path(run_dir)
        self.log = EventLog(log_path)
        self.adapters: dict[str, Adapter] = {}
        self.servers = []
        for name in adapters or list(SOCKETS):
            adapter = ADAPTERS[name]()
            self.adapters[name] = adapter
            self.servers.append(serve(self.run_dir / SOCKETS[name], name, adapter.handle, self.log))

    def set(self, adapter: str, values: dict[str, Any]) -> None:
        self.adapters[adapter].set(values)

    def close(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for name in self.adapters:
            (self.run_dir / SOCKETS[name]).unlink(missing_ok=True)


def run_scenario(sim: Simulator, scenario: dict, act=None, timeout: float = 120.0) -> list[str]:
    """Play a scenario timeline and check its expectations; returns failure messages.

    steps:  [{"at": seconds, "set": {"adapter": {...state...}}} | {"at": s, "act": name}]
    expect: [{"adapter": "...", "cmd": "...", "args": {...}, "within_s": s}]
    "act" steps call act(name), so a test can drive the system under test (for
    example a web API request) at a point in the timeline.
    """
    start = time.monotonic()
    for step in sorted(scenario.get("steps", []), key=lambda s: s.get("at", 0)):
        delay = start + float(step.get("at", 0)) - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, timeout))
        for adapter, values in step.get("set", {}).items():
            sim.set(adapter, values)
        if "act" in step and act:
            act(step["act"])
    failures = []
    for expect in scenario.get("expect", []):
        deadline = start + min(float(expect.get("within_s", 10)), timeout)
        while not sim.log.find(expect["adapter"], expect["cmd"], expect.get("args")):
            if time.monotonic() >= deadline:
                failures.append(f"expected {json.dumps(expect, sort_keys=True)}")
                break
            time.sleep(0.05)
    return failures
