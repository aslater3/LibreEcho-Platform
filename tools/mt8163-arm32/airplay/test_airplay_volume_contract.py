#!/usr/bin/env python3
"""Contract: sender callback sets audiod master once; PCM media stays unity."""
from pathlib import Path
import subprocess
import tempfile

AIRPLAY_AUDIO = Path(__file__).with_name("airplay_audio.c")
AUDIO_ENGINE = Path(__file__).with_name("audio_engine.c")


def main() -> None:
    producer = AIRPLAY_AUDIO.read_text(encoding="utf-8")
    engine = AUDIO_ENGINE.read_text(encoding="utf-8")
    bridge = producer.index("static int forward_stream")
    producer_main = producer.index("int main")
    if "clear_session_state()" in producer[bridge:producer_main]:
        raise SystemExit("bridge must not erase an in-session volume callback")
    bridge_cleanup = producer[producer.index("out:\n", bridge):producer_main]
    if not bridge_cleanup.index("close(output)") < bridge_cleanup.index("set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 0)"):
        raise SystemExit("bridge must close media writer before dropping marker")
    if producer.index("if (clear_session_state() != 0)", producer_main) > producer.index("while (!stopping)", producer_main):
        raise SystemExit("producer must clear stale volume before new session")
    if "unlink(DEFAULT_AIRPLAY_VOLUME_FILE)" not in producer:
        raise SystemExit("producer must remove stale callback")

    # Execute the actual bridge with isolated runtime paths, including a
    # leftover media.volume from the legacy dual-write callback behavior.
    with tempfile.TemporaryDirectory(prefix="radar-bridge-volume-") as directory:
        root = Path(directory)
        local_source = producer
        for suffix in ("media.volume", "airplay.volume", "airplay.master", "airplay.active",
                       "airplay.pcm", "media.pcm"):
            local_source = local_source.replace(
                f"/run/libreecho-audio/{suffix}" if suffix != "airplay.pcm"
                else "/run/libreecho/airplay.pcm", str(root / suffix))
        binary = root / "airplay-audio-test"
        source = root / "airplay_audio.c"
        source.write_text(local_source, encoding="utf-8")
        subprocess.run(["cc", "-std=c99", "-O2", "-Wall", "-Wextra",
                        "-Wpedantic", "-Werror", str(source), "-lm", "-o",
                        str(binary)], check=True, timeout=30)
        legacy = root / "media.volume"
        legacy.write_text("-144.000000\n", encoding="ascii")
        subprocess.run([str(binary), "--stop"], check=True, timeout=10)
        if legacy.exists():
            raise SystemExit("stale AirPlay mute persisted as generic media volume")
        subprocess.run([str(binary), "--set-volume", "-144"], check=True, timeout=10)
        if legacy.exists() or not (root / "airplay.volume").exists():
            raise SystemExit("AirPlay callback leaked into generic media gain")
        (root / "airplay.master").write_text("stale", encoding="ascii")
        subprocess.run([str(binary), "--stop"], check=True, timeout=10)
        if legacy.exists() or (root / "airplay.volume").exists() or (root / "airplay.master").exists():
            raise SystemExit("disconnect left an AirPlay override")
        subprocess.run([str(binary), "--set-volume", "-25"], check=True, timeout=10)
        (root / "airplay.master").write_text("stale", encoding="ascii")
        subprocess.run([str(binary), "--start"], check=True, timeout=10)
        if (root / "airplay.volume").exists() or (root / "airplay.master").exists():
            raise SystemExit("new playback inherited a late callback or acknowledgment")
        subprocess.run([str(binary), "--set-volume", "-12"], check=True, timeout=10)
        if (root / "airplay.volume").read_text(encoding="ascii") != "-12.000000\n":
            raise SystemExit("new playback did not accept its initial sender callback")
        subprocess.run([str(binary), "--stop"], check=True, timeout=10)
        for invalid in ("nan", "1", "-145", "-31"):
            if subprocess.run([str(binary), "--set-volume", invalid], timeout=10).returncode == 0:
                raise SystemExit("invalid callback accepted: " + invalid)

    # audiod reads this device-wide control as the button/API master.  A
    # platform-side snapshot/restore is unsafe when audiod changes it live.
    if "set_pcm_volume(" in engine or '"PCM Playback Volume", volume' in engine or "restore_volume" in engine:
        raise SystemExit("engine must never overwrite audiod's shared master")
    required = (
        "? airplay_media_gain(phone) : read_media_gain(root)",
        "return raw <= 0 ? 0 : 32768;",
        "airplay volume unavailable; deferring media",
        "priority audio continues while AirPlay",
        "speaker_volume_percent(sources, current_pcm_volume(card))",
        "arm_output_controls(card)",
        "disable_output_controls(card)",
    )
    missing = [fragment for fragment in required if fragment not in engine]
    if missing:
        raise SystemExit("missing volume ownership contract: " + ", ".join(missing))
    gate_start = engine.index("if (airplay_is_active(root) && airplay_volume_to_mixer(root) < 0)")
    gate_end = engine.index("if (arm_output_controls", gate_start)
    if "clear_source_activity(sources" in engine[gate_start:gate_end]:
        raise SystemExit("missing callback must not clear priority buses")
    start = engine.index("int playback_start_failed = 0", gate_end)
    first_write = engine.index("write_period(pcm, output", start)
    if not start < engine.index("power_output_controls(card)", start) < first_write:
        raise SystemExit("amplifier must settle before first PCM write")
    live = engine[engine.index("while (!stopping && sources_active(sources))"):]
    if not live.index("read_sources(sources, root)") < live.index("speaker_dsp_set_volume(&speaker") < live.index("render_period(sources, output"):
        raise SystemExit("phone/master EQ update must precede live rendering")
    if ("AIRPLAY_MASTER_FILE" not in engine or "callback.st_ino" not in engine or
            "return db <= -144.0 ? 0 : 127;" not in engine):
        raise SystemExit("engine must gate on matching ack and play nonmute media at unity")
    print("AirPlay callback acknowledgment / shared master ownership contract: PASS")


if __name__ == "__main__":
    main()
