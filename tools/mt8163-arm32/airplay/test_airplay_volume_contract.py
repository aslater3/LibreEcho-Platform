#!/usr/bin/env python3
"""Regression contracts for AirPlay session-volume ownership."""

from pathlib import Path


AIRPLAY_AUDIO = Path(__file__).with_name("airplay_audio.c")
AUDIO_ENGINE = Path(__file__).with_name("audio_engine.c")


def main() -> None:
    producer = AIRPLAY_AUDIO.read_text(encoding="utf-8")
    engine = AUDIO_ENGINE.read_text(encoding="utf-8")
    bridge = producer.index("static int forward_stream")
    main = producer.index("int main")
    if "clear_session_state()" in producer[bridge:main]:
        raise SystemExit("bridge must not clear a callback that arrived after --start")
    main_state_clear = producer.index("if (clear_session_state() != 0)", main)
    main_loop = producer.index("while (!stopping)", main)
    if main_state_clear > main_loop:
        raise SystemExit("daemon must clear stale session state before its stream loop")
    producer_required = (
        "unlink(DEFAULT_AIRPLAY_VOLUME_FILE)",
        "clear_session_state",
    )
    engine_required = (
        "airplay volume unavailable; deferring media",
        "airplay_volume < 0",
        "higher_priority_active(sources)",
        "? (airplay_volume_to_mixer(root) >= 0 ? 32768 : 0)",
        "priority audio continues while AirPlay",
    )
    gate = engine[engine.index("if (airplay_session && airplay_volume < 0)"):engine.index(
        "if (arm_output_controls", engine.index("if (airplay_session && airplay_volume < 0)"))]
    if "clear_source_activity(sources" in gate:
        raise SystemExit("missing AirPlay volume must not clear priority buses")
    if "render_period(sources" in gate:
        raise SystemExit("priority-bus gate must preserve the already rendered periods")
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
