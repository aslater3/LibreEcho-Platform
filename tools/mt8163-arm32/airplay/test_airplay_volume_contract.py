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
        "arm_output_controls(card, -1, &saved_volume)",
        # The sender's level belongs to AirPlay media, not to every source.
        # Applying it unconditionally put a system tone, an announcement or a
        # spoken reply out at the sender's volume, which for a sender at 0 dB
        # is a raw 127 -- the codec's unity point -- so any local playback
        # pushed the device to full volume and left it there.
        "int airplay_media_playing =",
        "(second_activity & PLAYBACK_BUS_MEDIA) != 0",
        "int startup_volume = (airplay_volume >= 0 && airplay_media_playing)",
        "set_pcm_volume(card, startup_volume)",
    )

    gate_start = engine.index("if (airplay_session && airplay_volume < 0)")
    gate_end = engine.index("if (arm_output_controls", gate_start)
    gate = engine[gate_start:gate_end]
    if "clear_source_activity(sources" in gate:
        raise SystemExit("missing AirPlay volume must not clear priority buses")
    if "render_period(sources" in gate:
        raise SystemExit("priority-bus gate must preserve the already rendered periods")

    pcm_open = engine.index("pcm = pcm_open")
    startup_apply = engine.index("set_pcm_volume(card, startup_volume)")
    first_write = engine.index("write_period(pcm, output", pcm_open)
    if not pcm_open < startup_apply < first_write:
        raise SystemExit("startup volume must be applied after PCM open and before first write")

    failure_start = engine.index("playback start failed")
    failure_end = engine.index("process_music_visualizer", failure_start)
    failure = engine[failure_start:failure_end]
    if not failure.index("disable_output_controls") < failure.index("pcm_close"):
        raise SystemExit("partial-start failure must mute before PCM close")
    normal_start = engine.index(
        "\n\t\tclear_source_activity(sources",
        engine.index("while (!stopping && sources_active(sources))"),
    )
    normal_end = engine.index("\n\t}\n\tresult", normal_start)
    normal = engine[normal_start:normal_end]
    normal_disable = normal.index("disable_output_controls")
    normal_close = normal.index("pcm_close")
    normal_restore = normal.index("set_pcm_volume(card, saved_volume)")
    if not normal_disable < normal_close < normal_restore:
        raise SystemExit("normal teardown must mute, close PCM, then restore volume")
    # The engine must only undo a level it actually imposed, so the restore is
    # gated on the same condition as the apply.
    if "airplay_media_playing" not in normal:
        raise SystemExit(
            "teardown restore must be scoped to AirPlay media, like the apply")

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
