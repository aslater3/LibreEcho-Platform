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
        "int airplay_volume_attempted = 0",
        "int airplay_volume_applied = 0",
        "int startup_volume = saved_volume",
        "airplay_volume_attempted = 1",
        "if (airplay_volume_attempted || airplay_volume_applied)",
        "else if (saved_volume >= 0)",
        "set_pcm_volume(card, startup_volume)",
        "airplay_volume_applied = 1",
        "!airplay_volume_applied || requested != airplay_volume",
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
    startup = engine[engine.index("int airplay_media_playing ="):first_write]
    if "int startup_volume = saved_volume" not in startup:
        raise SystemExit("non-AirPlay playback must retain the shared device volume")
    if "if (airplay_volume >= 0 && airplay_media_playing)" not in startup:
        raise SystemExit("sender volume must be scoped to AirPlay media at startup")
    if "startup_volume = airplay_volume" not in startup:
        raise SystemExit("AirPlay media must still receive the sender volume")

    failure_start = engine.index("playback start failed")
    failure_end = engine.index("process_music_visualizer", failure_start)
    failure = engine[failure_start:failure_end]
    if not failure.index("disable_output_controls") < failure.index("pcm_close"):
        raise SystemExit("partial-start failure must mute before PCM close")
    if "if (airplay_volume_attempted || airplay_volume_applied)" not in failure:
        raise SystemExit("partial-start failure must restore after any sender-volume attempt")
    pcm_failure = engine[engine.index("PCM %u,%u unavailable"):failure_start]
    if "airplay_volume_applied ? saved_volume : -1" not in pcm_failure:
        raise SystemExit("PCM-open failure must not restore an unowned device volume")
    loop_start = engine.index("while (!stopping && sources_active(sources))")
    loop_end = engine.index("\n\t\tclear_source_activity(sources", loop_start)
    live = engine[loop_start:loop_end]
    if "int previous_airplay_volume =\n\t\t\t\t\t\tairplay_volume_applied ? airplay_volume : -1;" not in live:
        raise SystemExit(
            "live sender failure must gate rollback on applied AirPlay ownership"
        )
    if "int previous_airplay_volume = airplay_volume;" in live:
        raise SystemExit("observed sender volume must not count as owned")
    prior_restore = live.index("set_pcm_volume(card, previous_airplay_volume)")
    saved_restore = live.index("set_pcm_volume(card, saved_volume)")
    if prior_restore > saved_restore:
        raise SystemExit("owned AirPlay level must be restored before saved_volume fallback")
    if "previous_airplay_volume >= 0" not in live:
        raise SystemExit("live sender failure must restore the prior AirPlay level")
    normal_start = loop_end
    normal_end = engine.index("\n\t}\n\tresult", normal_start)
    normal = engine[normal_start:normal_end]
    normal_disable = normal.index("disable_output_controls")
    normal_close = normal.index("pcm_close")
    normal_restore = normal.index("set_pcm_volume(card, saved_volume)")
    if not normal_disable < normal_close < normal_restore:
        raise SystemExit("normal teardown must mute, close PCM, then restore volume")
    if "airplay_volume_applied && saved_volume >= 0" not in normal:
        raise SystemExit("normal teardown must restore only after AirPlay volume was applied")

    cleanup = engine[engine.index("out:"):]
    if "if (airplay_volume_applied && saved_volume >= 0)" not in cleanup:
        raise SystemExit("final cleanup must not restore an unowned device volume")

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
