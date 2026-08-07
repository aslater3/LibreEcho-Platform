#!/usr/bin/env python3
"""Source contract checks for the production Radar-Puffin PCM boundary."""

from pathlib import Path


SOURCE = Path(__file__).with_name("audio_engine.c")


def main() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    required = (
        "#define INPUT_CHANNELS 2U",
        "#define OUTPUT_CHANNELS 2U",
        ".channels = OUTPUT_CHANNELS",
        "const size_t bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);",
        'set_enum_control(mixer, "Board Channel Config", "Stereo")',
        "int16_t rendered = puffin_render_mono(dynamics, mixed);",
        "output[frame * OUTPUT_CHANNELS] = rendered;",
        "output[frame * OUTPUT_CHANNELS + 1] = rendered;",
        "OUTPUT_CHANNELS, activity_mask",
        "output=S16_LE/48000/duplicated-stereo",
    )
    missing = [fragment for fragment in required if fragment not in text]
    if missing:
        raise SystemExit("missing production audio contract: " + ", ".join(missing))
    if "#define DEFAULT_CHANNELS" in text:
        raise SystemExit("stereo DEFAULT_CHANNELS contract must not remain")
    if "output[frame] = puffin_render_mono" in text:
        raise SystemExit("one-channel MonoRight PCM regresses the left DAC to noise")
    if "output=S16_LE/48000/mono MonoRight" in text:
        raise SystemExit("MonoRight output banner must not remain")
    print("audio_engine_contract: mono programme duplicated into stereo PCM 23 PASS")


if __name__ == "__main__":
    main()
