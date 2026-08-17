#!/usr/bin/env python3
"""Regression contracts for AirPlay session-volume ownership."""

from pathlib import Path


AIRPLAY_AUDIO = Path(__file__).with_name("airplay_audio.c")
AUDIO_ENGINE = Path(__file__).with_name("audio_engine.c")


def main() -> None:
    producer = AIRPLAY_AUDIO.read_text(encoding="utf-8")
    engine = AUDIO_ENGINE.read_text(encoding="utf-8")
    bridge = producer.index("/* A producer process can outlive")
    bridge_start = producer.index("clear_session_state()", bridge)
    active_publish = producer.index(
        "set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 1)", bridge
    )
    if bridge_start > active_publish:
        raise SystemExit("new bridge must clear stale session state before active")
    producer_required = (
        "unlink(DEFAULT_AIRPLAY_VOLUME_FILE)",
        "clear_session_state",
    )
    engine_required = (
        "wait_for_airplay_volume",
        "airplay volume unavailable; deferring playback",
        "airplay_volume < 0",
    )
    missing = [
        f"airplay_audio.c: {fragment}"
        for fragment in producer_required
        if fragment not in producer
    ]
    missing.extend(
        f"audio_engine.c: {fragment}"
        for fragment in engine_required
        if fragment not in engine
    )
    if missing:
        raise SystemExit("missing AirPlay volume contract: " + ", ".join(missing))
    print("AirPlay session-volume precedence contract: PASS")


if __name__ == "__main__":
    main()
