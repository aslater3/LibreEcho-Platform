#!/usr/bin/env python3
"""Source contract checks for the production Radar-Puffin PCM boundary."""

import re
from pathlib import Path


SOURCE = Path(__file__).with_name("audio_engine.c")
DSP = Path(__file__).with_name("speaker_dsp.h")


def main() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    required = (
        "#define INPUT_CHANNELS 2U",
        "#define OUTPUT_CHANNELS 2U",
        ".channels = OUTPUT_CHANNELS",
        "const size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);",
        "const size_t bytes = period_bytes * LE_AUDIO_PERIOD_BUFFER_PERIODS;",
        'set_enum_control(mixer, "Board Channel Config", "Stereo")',
        "int16_t rendered;",
        "rendered = puffin_render_mono(dynamics, mixed);",
        "output[frame * OUTPUT_CHANNELS] = rendered;",
        "output[frame * OUTPUT_CHANNELS + 1] = rendered;",
        "OUTPUT_CHANNELS, activity_mask",
        "output=S16_LE/48000/duplicated-stereo",
        "static int prepare_initial_period",
        "ready_activity_mask(sources)",
        "read_or_retain_sources",
        "int poll_timeout = period_ready(sources) ? 20 : -1;",
        "power_output_controls(card)",
        "unmute_output_controls(card)",
        '#include "speaker_dsp.h"',
        "speaker_dsp_init(speaker, speaker_volume_percent(root));",
        "mixed = speaker_dsp_process(speaker, mixed);",
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

    # The tuned speaker stage must run before the trim/limiter: the stock
    # pipeline equalises first and applies OutputTrim and its full-band limiter
    # afterwards, and this engine's limiter must stay the last thing the
    # programme bus passes through.
    tune_at = text.index("mixed = speaker_dsp_process(speaker, mixed);")
    trim_at = text.index("rendered = puffin_render_mono(dynamics, mixed);")
    if tune_at > trim_at:
        raise SystemExit("speaker tuning must run before the trim/limiter")

    # The tuned stage is only valid on the mono programme bus.
    if re.search(r"speaker_dsp_process\(speaker,\s*samples", text):
        raise SystemExit("speaker tuning must not run on the stereo input bus")

    dsp = DSP.read_text(encoding="utf-8")
    # Comments legitimately cite the stock file names when documenting
    # provenance, so the vendored-data checks run against code only.
    dsp_code = re.sub(r"/\*.*?\*/", "", dsp, flags=re.S)
    dsp_code = re.sub(r"//[^\n]*", "", dsp_code)

    # Provenance: the artifact ships parameters and our own design code, never a
    # vendor coefficient table.  A pasted stock curve would add thousands of
    # numeric literals; our own tables are a few dozen.
    literals = re.findall(r"[-+]?\d+\.\d*f|[-+]?\d+f", dsp_code)
    if len(literals) > 200:
        raise SystemExit(
            f"speaker_dsp.h carries {len(literals)} float literals; the tuning must "
            "be our own fitted parameters, not an embedded vendor coefficient table")
    for vendor in ("EQ_50", "EQ_60", "EQ_70", "EQ_80", "EQ_90", "EQ_100",
                   ".cfg", "BLOUD", "asp.cfg", "ParametricEQ"):
        if vendor in dsp_code:
            raise SystemExit(f"speaker_dsp.h must not reference vendor tuning: {vendor}")

    # The loudness ladder must stay anchored on the stock volume boundaries.
    for boundary in ("50.0f", "60.0f", "70.0f", "80.0f", "100.0f"):
        if boundary not in dsp:
            raise SystemExit(f"loudness ladder is missing volume boundary {boundary}")

    for fragment in ("speaker_dsp_process", "speaker_dsp_init",
                     "speaker_biquad_design", "SPEAKER_DSP_SECTIONS"):
        if fragment not in dsp:
            raise SystemExit(f"speaker tuning module is missing {fragment}")

    print("audio_engine_contract: mono programme duplicated into stereo PCM 23 PASS")
    print("audio_engine_contract: speaker tuning runs before the trim/limiter PASS")
    print("audio_engine_contract: no vendor coefficient table embedded PASS")


if __name__ == "__main__":
    main()
