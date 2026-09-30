#!/usr/bin/env python3
"""Validate the portable version-2 visualizer feature-packet trace.

Builds the real producer path (audio_visualizer.c + test_music_feature_trace.c),
runs it twice, requires byte-identical output, and checks every emitted packet
against the frozen contract: envelope, owner, feature_version, session, the
monotonic sequence and millisecond timestamp, the twelve-hex levels field, the
bounded 0..255 fields, the phase/bpm/event bounds and the packet size budget.

This is the host-side half of the cross-repository roundtrip: the same
trace.jsonl is what a LibreEcho-UI consumer replays.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BANDS = 12
FRAME_BOUND = 768
U8_FIELDS = (
    "energy", "warmth", "brightness_axis", "density", "transientness",
    "groove", "build", "spaciousness", "loudness_fast", "loudness_slow",
    "onset_low", "onset_mid", "onset_high", "beat_strength",
    "beat_confidence", "novelty", "event_strength",
)
EVENT_BITS = {
    "kick": 0x0001, "snare": 0x0002, "high": 0x0004, "fill": 0x0008,
    "build": 0x0010, "reentry": 0x0020, "breakdown": 0x0040,
    "section": 0x0080, "drop": 0x0100,
}


def build_and_run(tmp: Path) -> bytes:
    here = Path(__file__).resolve().parent
    binary = tmp / "test-music-feature-trace"
    cc = shutil.which("cc") or "cc"
    subprocess.run(
        [
            cc, "-O2", "-std=c99", "-Wall", "-Wextra", "-Wpedantic",
            "-Werror", str(here / "audio_visualizer.c"),
            str(here / "test_music_feature_trace.c"),
            "-lm", "-o", str(binary),
        ],
        check=True,
    )
    first = tmp / "trace-a.jsonl"
    second = tmp / "trace-b.jsonl"
    subprocess.run([str(binary), str(first)], check=True)
    subprocess.run([str(binary), str(second)], check=True)
    a = first.read_bytes()
    b = second.read_bytes()
    if a != b:
        raise SystemExit("feature trace is not deterministic across runs")
    return a


def check_packet(line: str, index: int, last_seq: int, last_ts: int,
                 session: int) -> tuple:
    raw = len(line)
    if raw >= FRAME_BOUND:
        raise SystemExit(f"packet {index} is {raw} bytes, over bound")
    if raw >= 4096:
        raise SystemExit(f"packet {index} exceeds a plausible socket buffer")

    packet = json.loads(line)
    if packet.get("v") != 1 or packet.get("id") != 2:
        raise SystemExit(f"packet {index} envelope changed: {packet}")
    if packet.get("cmd") != "visualizer":
        raise SystemExit(f"packet {index} command changed")
    args = packet["args"]
    if args.get("action") != "frame" or args.get("owner") != "music":
        raise SystemExit(f"packet {index} action/owner changed")
    if args.get("feature_version") != 2:
        raise SystemExit(f"packet {index} is not feature_version 2")
    if args.get("brightness") != 70:
        raise SystemExit(f"packet {index} lost the LED master brightness")

    levels = args.get("levels")
    if not isinstance(levels, str) or len(levels) != BANDS * 2:
        raise SystemExit(f"packet {index} levels field is not 24 hex digits")
    int(levels, 16)

    got_session = args.get("session")
    if got_session != session or got_session == 0:
        raise SystemExit(f"packet {index} session unstable or zero")
    seq = args["seq"]
    timestamp = args["timestamp_ms"]
    if seq != last_seq + 1:
        raise SystemExit(f"packet {index} seq not monotonic: {seq}")
    if timestamp < last_ts:
        raise SystemExit(f"packet {index} timestamp moved backwards")

    for field in U8_FIELDS:
        value = args[field]
        if not isinstance(value, int) or not 0 <= value <= 255:
            raise SystemExit(f"packet {index} {field}={value} out of 0..255")
    if not 0 <= args["beat_phase"] <= 65535:
        raise SystemExit(f"packet {index} beat_phase out of range")
    if not 0 <= args["bpm_x100"] <= 30000:
        raise SystemExit(f"packet {index} bpm_x100 out of range")
    if not 0 <= args["events"] <= 0x1FF:
        raise SystemExit(f"packet {index} events bitmask out of range")
    return seq, timestamp, raw, args["events"]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="libreecho-music-packet-") as tmp:
        data = build_and_run(Path(tmp))
    lines = [line for line in data.decode("utf-8").splitlines() if line]
    if len(lines) < 100:
        raise SystemExit(f"trace too short: {len(lines)} frames")

    session = json.loads(lines[0])["args"]["session"]
    seen = {name: 0 for name in EVENT_BITS}
    last_seq = -1
    last_ts = -1
    max_raw = 0
    for index, line in enumerate(lines):
        last_seq, last_ts, raw, events = check_packet(
            line, index, last_seq, last_ts, session)
        max_raw = max(max_raw, raw)
        for name, bit in EVENT_BITS.items():
            if events & bit:
                seen[name] += 1

    # The trace must actually exercise the structural gates, otherwise the
    # roundtrip corpus would not prove the consumer anything.
    for required in ("build", "drop", "reentry", "breakdown", "kick"):
        if seen[required] == 0:
            raise SystemExit(f"trace never exercised the {required} event")

    print(
        f"music feature packet: {len(lines)} v2 frames, "
        f"session=0x{session:08x}, max_packet={max_raw}B, "
        f"events={seen} PASS"
    )


if __name__ == "__main__":
    sys.exit(main())
