"""Line-JSON adapter protocol server (LibreEcho-UI src/adapter/adapter.h).

Request  {"v":1,"id":N,"cmd":"...","args":{...}}\\n
Response {"v":1,"id":N,"ok":true,"data":{...}}\\n  or  {"v":1,"id":N,"ok":false,"error":"..."}\\n
Messages are at most 4096 bytes, as in the daemons.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Callable

MSG_MAX = 4096
PROTO_VERSION = 1


class AdapterError(Exception):
    """A request the simulated adapter refuses, reported as ok:false."""


class EventLog:
    """Every request any adapter receives, as JSONL, for scenario assertions."""

    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        self.entries: list[dict[str, Any]] = []
        self.start = time.monotonic()

    def record(self, adapter: str, cmd: str, args: dict, ok: bool, error: str = "") -> None:
        entry = {"t": round(time.monotonic() - self.start, 3), "adapter": adapter, "cmd": cmd, "args": args, "ok": ok}
        if error:
            entry["error"] = error
        with self.lock:
            self.entries.append(entry)
            if self.path:
                with self.path.open("a") as f:
                    f.write(json.dumps(entry, sort_keys=True) + "\n")

    def find(self, adapter: str, cmd: str, args: dict | None = None) -> list[dict]:
        with self.lock:
            return [e for e in self.entries if e["adapter"] == adapter and e["cmd"] == cmd and
                    all(e["args"].get(k) == v for k, v in (args or {}).items())]


def respond(request_id: Any, ok: bool, payload: Any) -> bytes:
    body: dict[str, Any] = {"v": PROTO_VERSION, "id": request_id if isinstance(request_id, int) else 0, "ok": ok}
    if ok:
        body["data"] = payload if payload is not None else {}
    else:
        body["error"] = str(payload)
    line = (json.dumps(body, separators=(",", ":")) + "\n").encode()
    if len(line) > MSG_MAX:
        line = (json.dumps({"v": PROTO_VERSION, "id": body["id"], "ok": False, "error": "response too large"}) + "\n").encode()
    return line


def handle_line(raw: bytes, name: str, handler: Callable[[str, dict], Any], log: EventLog) -> bytes:
    if len(raw) > MSG_MAX:
        log.record(name, "<oversize>", {}, False, "message too large")
        return respond(0, False, "message too large")
    try:
        request = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        log.record(name, "<malformed>", {}, False, "malformed request")
        return respond(0, False, "malformed request")
    if not isinstance(request, dict) or not isinstance(request.get("cmd"), str):
        log.record(name, "<malformed>", {}, False, "malformed request")
        return respond(0, False, "malformed request")
    request_id = request.get("id", 0)
    args = request.get("args", {})
    if args is None:
        args = {}
    if request.get("v", PROTO_VERSION) != PROTO_VERSION or not isinstance(args, dict):
        log.record(name, request["cmd"], {}, False, "unsupported request")
        return respond(request_id, False, "unsupported request")
    try:
        data = handler(request["cmd"], args)
    except AdapterError as error:
        log.record(name, request["cmd"], args, False, str(error))
        return respond(request_id, False, error)
    log.record(name, request["cmd"], args, True)
    return respond(request_id, True, data)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(path: Path, name: str, handler: Callable[[str, dict], Any], log: EventLog) -> socketserver.BaseServer:
    """Bind path (replacing a stale socket) and serve in a background thread."""

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(30)
            while True:
                try:
                    raw = self.rfile.readline(MSG_MAX + 2)
                except (OSError, socket.timeout):
                    return
                if not raw:
                    return
                if not raw.endswith(b"\n") and len(raw) > MSG_MAX:
                    self.wfile.write(respond(0, False, "message too large"))
                    return
                self.wfile.write(handle_line(raw.rstrip(b"\n"), name, handler, log))

    if path.exists() or path.is_symlink():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    server = _Server(str(path), Handler)
    os.chmod(path, 0o666)
    threading.Thread(target=server.serve_forever, name=f"hwsim-{name}", daemon=True).start()
    return server
