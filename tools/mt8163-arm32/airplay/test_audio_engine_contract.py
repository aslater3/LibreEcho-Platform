#!/usr/bin/env python3
"""Source contract checks for the production Radar-Puffin PCM boundary."""

from pathlib import Path


SOURCE = Path(__file__).with_name("audio_engine.c")


def main() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    required = (
        "#define INPUT_CHANNELS 2U",
        "#define OUTPUT_CHANNELS 1U",
        ".channels = OUTPUT_CHANNELS",
        "const size_t bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);",
        "output[frame] = puffin_render_mono(dynamics, mixed);",
        "OUTPUT_CHANNELS, activity_mask",
        "output=S16_LE/48000/mono MonoRight",
    )
    missing = [fragment for fragment in required if fragment not in text]
    if missing:
        raise SystemExit("missing production audio contract: " + ", ".join(missing))
    if "#define DEFAULT_CHANNELS" in text:
        raise SystemExit("stereo DEFAULT_CHANNELS contract must not remain")
    if "output[frame * 2]" in text or "output[frame * 2 + 1]" in text:
        raise SystemExit("production PCM output must be one-channel mono")
    print("audio_engine_contract: mono PCM 23 output with stereo producer input PASS")


if __name__ == "__main__":
    main()
