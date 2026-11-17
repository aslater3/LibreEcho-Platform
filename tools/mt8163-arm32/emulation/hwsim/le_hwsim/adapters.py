"""Simulated adapters: the state and commands the real daemons expose on their sockets.

Field names match what LibreEcho-UI src/backend_linux.c reads from each daemon's
"status" reply, so the real libreecho-web --backend linux works against them.
Values are plausible defaults; scenarios change them through Simulator.set().
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from .protocol import AdapterError


def _int(args: dict, key: str, lo: int, hi: int) -> int:
    value = args.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise AdapterError(f"{key} must be an integer in {lo}..{hi}")
    return value


def _bool(args: dict, key: str) -> bool:
    value = args.get(key)
    if not isinstance(value, bool):
        raise AdapterError(f"{key} must be a boolean")
    return value


class Adapter:
    name = ""
    commands: dict[str, str] = {}

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = self.initial()

    def initial(self) -> dict[str, Any]:
        return {}

    def handle(self, cmd: str, args: dict) -> Any:
        method = self.commands.get(cmd)
        if not method:
            raise AdapterError(f"unknown command {cmd}")
        with self.lock:
            return getattr(self, method)(args)

    def status(self, args: dict) -> dict:
        return copy.deepcopy(self.state)

    def accept(self, args: dict) -> dict:
        """A command the simulator records but has no state for (speech, cues, scans...)."""
        return {}

    def set(self, values: dict) -> None:
        with self.lock:
            self.state.update(copy.deepcopy(values))


class Audio(Adapter):
    name = "audio"
    commands = {c: "accept" for c in ("speak", "cue", "stop_speech", "test_tone", "wake_chirp", "sample",
                                      "airplay_volume", "airplay_end")}
    commands.update(status="status", set_volume="set_volume", set_mute="set_mute", set_gain="set_gain",
                    noise_start="noise_start", noise_stop="noise_stop")

    def initial(self) -> dict:
        return {"volume": 40, "notification_volume": 70, "microphone_gain": 65, "muted": False,
                "microphone_muted": False, "startup_sound": True, "amplifier_on": True, "output_available": True,
                "noise_active": False, "noise_colour": "white", "noise_source": "white", "noise_bed": "none",
                "noise_level": 0, "noise_tempo": 60, "noise_fade_seconds": 0, "noise_remaining_seconds": -1}

    def set_volume(self, args: dict) -> dict:
        self.state["volume"] = _int(args, "volume", 0, 100)
        return {"volume": self.state["volume"]}

    def set_mute(self, args: dict) -> dict:
        self.state["muted"] = self.state["microphone_muted"] = _bool(args, "muted")
        return {"muted": self.state["muted"]}

    def set_gain(self, args: dict) -> dict:
        self.state["microphone_gain"] = _int(args, "gain", 0, 100)
        return {"microphone_gain": self.state["microphone_gain"]}

    def noise_start(self, args: dict) -> dict:
        self.state.update(noise_active=True, noise_level=args.get("level", 40) if isinstance(args.get("level"), int) else 40)
        return {"noise_active": True}

    def noise_stop(self, args: dict) -> dict:
        self.state["noise_active"] = False
        return {"noise_active": False}


class Led(Adapter):
    name = "led"
    commands = {c: "accept" for c in ("set_boot_profile", "set_profile", "pattern", "meter", "visualizer", "activity",
                                      "test", "animate", "sleep_light", "set_night")}
    commands.update(status="status", set_colour="set_colour", set_brightness="set_brightness",
                    set_visualizer_enabled="set_visualizer_enabled", set_idle_mode="set_idle_mode")

    def initial(self) -> dict:
        return {"current": {"r": 72, "g": 216, "b": 118, "brightness": 70},
                "boot_profile": {"r": 0, "g": 96, "b": 255, "brightness": 100},
                "profiles": {p: {"r": 72, "g": 185, "b": 255, "brightness": 80}
                             for p in ("listening", "thinking", "error", "dnd", "night")},
                "night": {"enabled": False, "active": False, "start_minute": 1320, "end_minute": 420},
                "visualizer_enabled": True, "visualizer_active": False, "visualizer_owner": "", "visualizer_mood": "idle",
                "animation_active": False, "animation_profile": "none", "pattern_active": False, "pattern": "none",
                "idle_mode": "indicator",
                "sleep_light": {"mode": "off", "active": False, "brightness": 8, "period_ms": 8000,
                                "timer_minutes": 0, "remaining_ms": 0, "restore_on_boot": False}}

    def set_colour(self, args: dict) -> dict:
        for key in ("r", "g", "b"):
            self.state["current"][key] = _int(args, key, 0, 255)
        return {}

    def set_brightness(self, args: dict) -> dict:
        self.state["current"]["brightness"] = _int(args, "brightness", 0, 100)
        return {}

    def set_visualizer_enabled(self, args: dict) -> dict:
        self.state["visualizer_enabled"] = _bool(args, "enabled")
        return {"visualizer_enabled": self.state["visualizer_enabled"]}

    def set_idle_mode(self, args: dict) -> dict:
        mode = args.get("mode")
        if mode not in ("off", "indicator", "always"):
            raise AdapterError("idle mode must be off, indicator or always")
        self.state["idle_mode"] = mode
        return {"idle_mode": True}


class Network(Adapter):
    name = "network"
    commands = {c: "accept" for c in ("scan", "connect", "disconnect", "recovery_start", "recovery_stop",
                                      "recovery_status", "recovery_configure", "recovery_prepare", "recovery_psk")}
    commands.update(status="status", set_hostname="set_hostname")

    def initial(self) -> dict:
        # Documentation addresses (RFC 5737): never a real network.
        return {"state": "connected", "connectivity": "healthy", "recovery_stage": "none", "mode": "client",
                "ssid": "SimNet", "signal": 72, "rssi_dbm": -54, "ip": "192.0.2.10", "gateway": "192.0.2.1",
                "dns": "192.0.2.1", "hostname": "libreecho", "gateway_reachable": True, "liveness_failures": 0,
                "recovery": {"mode": "off", "trigger": "none", "available": True, "ssid": "LibreEcho-Setup-SIM",
                             "address": "192.168.4.1", "reason": "", "error": "", "secret_available": False,
                             "led_owner": "", "enabled": False, "net_configured": True, "auto_enabled": False,
                             "auto_pending": False, "rate_count": 0, "children": 0}}

    def set_hostname(self, args: dict) -> dict:
        hostname = args.get("hostname")
        if not isinstance(hostname, str) or not 1 <= len(hostname) <= 63:
            raise AdapterError("invalid hostname")
        self.state["hostname"] = hostname
        return {}


class Bluetooth(Adapter):
    name = "bluetooth"
    commands = {c: "accept" for c in ("scan", "scan_stop", "pair", "pair_confirm", "unpair", "disconnect")}
    commands.update(status="status", set_enabled="set_enabled", set_discoverable="set_discoverable",
                    set_connectable="set_connectable", pairing_mode="pairing_mode")

    def initial(self) -> dict:
        return {"available": True, "enabled": True, "state": "powered", "hci": "hci0", "address": "00:00:00:00:00:00",
                "local_name": "LibreEcho", "transport": "uart", "discoverable": False, "connectable": True,
                "pairing": False, "pairing_mode": False, "scanning": False, "bondable": True, "classic": True,
                "le": False, "ssp": True, "secure_connection": True, "sdp": True, "rfcomm": False, "bnep": False,
                "hidp": False, "avrcp": True, "activation_attempted": True, "profile_state": "ready",
                "profile_error": "", "last_error": "", "last_connect_failed_status": "", "last_disconnect_reason": ""}

    def set_enabled(self, args: dict) -> dict:
        self.state["enabled"] = _bool(args, "enabled")
        return {}

    def set_discoverable(self, args: dict) -> dict:
        self.state["discoverable"] = _bool(args, "enabled")
        return {}

    def set_connectable(self, args: dict) -> dict:
        self.state["connectable"] = _bool(args, "enabled")
        return {}

    def pairing_mode(self, args: dict) -> dict:
        self.state["pairing_mode"] = _bool(args, "enabled")
        return {"pairing_mode": self.state["pairing_mode"]}


ADAPTERS = {cls.name: cls for cls in (Audio, Led, Network, Bluetooth)}
