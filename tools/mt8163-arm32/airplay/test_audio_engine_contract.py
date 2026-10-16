#!/usr/bin/env python3
"""Source contract checks for the production Radar-Puffin PCM boundary."""

import re
import subprocess
import tempfile
from pathlib import Path


SOURCE = Path(__file__).with_name("audio_engine.c")
DSP = Path(__file__).with_name("speaker_dsp.h")
BISCUIT_DSP = Path(__file__).with_name("biscuit_speaker_dsp.h")


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
        "int poll_timeout = 20;",
        "power_output_controls(card)",
        "unmute_output_controls(card)",
        '#include "speaker_dsp.h"',
        "speaker_dsp_init(speaker, speaker_volume_percent(sources, master_volume));",
        "mixed = speaker_dsp_process(speaker, mixed);",
        "speaker_dsp_set_volume(&speaker,",
        "speaker_volume_percent(sources, master)",
        "logical_master_gain(master)",
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

    # The stock order is EQ -> MBCL -> OutputTrim.  The original MBCL module
    # lives inside speaker_dsp_process, and the final PCM safety limiter must
    # remain after the common mixed-bus processing.
    tune_at = text.index("mixed = speaker_dsp_process(speaker, mixed);")
    master_at = text.index("mixed = (int32_t)(((int64_t)mixed * *current_master_q15) >> 15);")
    trim_at = text.index("rendered = puffin_render_mono(dynamics, mixed);")
    if not master_at < tune_at < trim_at:
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
                     "speaker_biquad_design", "SPEAKER_DSP_SECTIONS",
                     '#include "speaker_mbcl.h"', "speaker_mbcl_init(&dsp->mbcl)",
                     "speaker_mbcl_process(&dsp->mbcl, x)"):
        if fragment not in dsp:
            raise SystemExit(f"speaker tuning module is missing {fragment}")

    check_biscuit_selection(text)

    print("audio_engine_contract: mono programme duplicated into stereo PCM 23 PASS")
    print("audio_engine_contract: speaker tuning runs before the trim/limiter PASS")
    print("audio_engine_contract: no vendor coefficient table embedded PASS")
    print("audio_engine_contract: per-target audio profile drives chain/codec/gain, Radar unchanged PASS")


def extract(text: str, signature: str) -> str:
    start = text.index(signature)
    return text[start:text.index("\n}\n", start) + 3]


RADAR_PROFILE = ("schema=1\ntarget_id=radar_puffin\nspeaker_chain=radar_puffin\n"
                 "codec_profile=Radar\nhp_driver_gain=6\npre_gain_db=0.00\nbass_makeup_db=0.00\n")
DOT_PROFILE = ("schema=1\ntarget_id=biscuit\nspeaker_chain=biscuit\n"
               "codec_profile=Flat\nhp_driver_gain=6\npre_gain_db=6.00\nbass_makeup_db=2.50\n")
RADAR_DEFAULT = "chain=0 flat=0 gain=6 pre=1.000 bass=0.00"


def check_biscuit_selection(text: str) -> None:
    """Per-target speaker policy comes from /etc/libreecho/audio-profile.

    Radar (and anything missing, rejected or unknown) keeps the Radar chain,
    the Radar codec crossover and +6 dB analogue gain; only a valid profile
    may select another chain or codec profile.
    """
    for fragment in (
        '#include "biscuit_speaker_dsp.h"',
        '#define AUDIO_PROFILE_FILE "/etc/libreecho/audio-profile"',
        '#define AUDIO_PROFILE_FILE_HOST "/proc/1/root/etc/libreecho/audio-profile"',
        "rendered = biscuit_render_s16(biscuit_chain, mixed);",
        "biscuit_dsp_init(biscuit_chain,",
        "biscuit_dsp_configure(biscuit_chain, speaker_profile.bass_makeup_db);",
        "biscuit_dsp_set_volume(biscuit_chain,",
        "select_speaker_profile();",
        'set_stereo_control(mixer, "HP Driver Gain Volume",\n                           speaker_profile.hp_driver_gain)',
        'set_enum_control(mixer, "Speaker Codec Profile",',
        "biscuit_dsp_process(dsp, (float)mixed * speaker_profile.pre_gain)",
    ):
        if fragment not in text:
            raise SystemExit(f"missing speaker-profile contract: {fragment}")
    if re.search(r'"HP Driver Gain Volume",\s*\d', text):
        raise SystemExit("HP driver gain must come from the target profile, not a literal")

    body = text[text.index("static void render_period("):]
    body = body[:body.index("\n}\n")]
    branch = re.search(
        r"if \(biscuit_chain\) \{\s*rendered = biscuit_render_s16\(biscuit_chain, mixed\);"
        r"\s*\} else \{\s*mixed = speaker_dsp_process\(speaker, mixed\);"
        r"\s*rendered = puffin_render_mono\(dynamics, mixed\);\s*\}", body)
    if not branch:
        raise SystemExit("render_period must run either the Dot chain or the Radar chain, never both")

    dsp = re.sub(r"/\*.*?\*/", "", BISCUIT_DSP.read_text(encoding="utf-8"), flags=re.S)
    if len(re.findall(r"[-+]?\d+\.\d*f?|[-+]?\d+f", dsp)) > 250:
        raise SystemExit("biscuit_speaker_dsp.h must not embed a vendor coefficient table")
    for vendor in ("EQ_50", "EQ_100", ".cfg", "MBCL", "ParametricEQ"):
        if vendor in dsp:
            raise SystemExit(f"biscuit_speaker_dsp.h must not reference vendor tuning: {vendor}")

    # Behaviour: compile the real profile code out of audio_engine.c.
    struct = text[text.index("struct speaker_profile {"):]
    struct = struct[:struct.index("};\n", struct.index("static struct speaker_profile")) + 3]
    harness = (
        "#define _GNU_SOURCE\n#include <errno.h>\n#include <fcntl.h>\n#include <math.h>\n"
        "#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n#include <unistd.h>\n"
        "struct biscuit_dsp { int unused; };\n"
        + struct
        + "#undef AUDIO_PROFILE_FILE\n#undef AUDIO_PROFILE_FILE_HOST\n"
          "#undef IMAGE_TARGET_FILE\n#undef IMAGE_TARGET_FILE_HOST\n"
          "static const char *AUDIO_PROFILE_FILE, *AUDIO_PROFILE_FILE_HOST,"
          " *IMAGE_TARGET_FILE, *IMAGE_TARGET_FILE_HOST;\n"
          "static struct biscuit_dsp biscuit_storage;\nstatic struct biscuit_dsp *biscuit_chain;\n"
        + extract(text, "static int read_small_file(")
        + extract(text, "static int image_target_is_biscuit(")
        + extract(text, "static int parse_db(")
        + extract(text, "static int load_audio_profile(")
        + extract(text, "static void select_speaker_profile(")
        + "int main(int argc, char **argv) { (void)argc;\n"
          " AUDIO_PROFILE_FILE = argv[1]; AUDIO_PROFILE_FILE_HOST = argv[2];\n"
          " IMAGE_TARGET_FILE = argv[3]; IMAGE_TARGET_FILE_HOST = argv[4];\n"
          " select_speaker_profile();\n"
          " if ((biscuit_chain != NULL) != speaker_profile.biscuit_chain) return 2;\n"
          " return printf(\"chain=%d flat=%d gain=%d pre=%.3f bass=%.2f\","
          " speaker_profile.biscuit_chain, speaker_profile.codec_flat,"
          " speaker_profile.hp_driver_gain, (double)speaker_profile.pre_gain,"
          " (double)speaker_profile.bass_makeup_db) < 0; }\n"
    )
    dot = "chain=1 flat=1 gain=6 pre=1.995 bass=2.50"
    legacy_dot = "chain=1 flat=1 gain=6 pre=1.000 bass=0.00"
    biscuit_id = "target_id=biscuit\nrelease_slug=biscuit\nhw_profile=biscuit@0\n"
    # (profile, host profile, target, host target) -> expected
    cases = {
        "radar": ((RADAR_PROFILE, None, None, None), RADAR_DEFAULT),
        "dot": ((DOT_PROFILE, None, None, None), dot),
        "dot_host_only": ((None, DOT_PROFILE, None, None), dot),
        "nothing": ((None, None, None, None), RADAR_DEFAULT),
        # Images built before the profile existed: identity-only fallback.
        "legacy_dot_identity": ((None, None, biscuit_id, None), legacy_dot),
        "legacy_dot_host_identity": ((None, None, None, biscuit_id), legacy_dot),
        "legacy_radar_identity": ((None, None, "target_id=radar_puffin\n", None), RADAR_DEFAULT),
        # A present profile is final: rejected never falls back to identity.
        "rejected_beats_identity": ((DOT_PROFILE + "extra=1\n", None, biscuit_id, None), RADAR_DEFAULT),
        "profile_beats_identity": ((RADAR_PROFILE, None, biscuit_id, None), RADAR_DEFAULT),
        "unknown_key": ((DOT_PROFILE + "x=1\n", None, None, None), RADAR_DEFAULT),
        "duplicate_key": ((DOT_PROFILE + "codec_profile=Flat\n", None, None, None), RADAR_DEFAULT),
        "missing_key": ((DOT_PROFILE.replace("hp_driver_gain=6\n", ""), None, None, None), RADAR_DEFAULT),
        "schema2": ((DOT_PROFILE.replace("schema=1", "schema=2"), None, None, None), RADAR_DEFAULT),
        "bad_chain": ((DOT_PROFILE.replace("chain=biscuit", "chain=woofer"), None, None, None), RADAR_DEFAULT),
        "bad_codec": ((DOT_PROFILE.replace("Flat", "flat"), None, None, None), RADAR_DEFAULT),
        "gain_range": ((DOT_PROFILE.replace("gain=6", "gain=36"), None, None, None), RADAR_DEFAULT),
        "gain_float": ((DOT_PROFILE.replace("gain=6", "gain=6.0"), None, None, None), RADAR_DEFAULT),
        "pre_range": ((DOT_PROFILE.replace("pre_gain_db=6.00", "pre_gain_db=12.5"), None, None, None), RADAR_DEFAULT),
        "pre_nan": ((DOT_PROFILE.replace("pre_gain_db=6.00", "pre_gain_db=nan"), None, None, None), RADAR_DEFAULT),
        "bass_negative": ((DOT_PROFILE.replace("bass_makeup_db=2.50", "bass_makeup_db=-1"), None, None, None), RADAR_DEFAULT),
        "radar_with_gain": ((RADAR_PROFILE.replace("pre_gain_db=0.00", "pre_gain_db=3"), None, None, None), RADAR_DEFAULT),
        "no_equals": ((DOT_PROFILE + "garbage\n", None, None, None), RADAR_DEFAULT),
        "empty": (("", None, None, None), RADAR_DEFAULT),
        "oversize": ((DOT_PROFILE + "#" * 2000, None, None, None), RADAR_DEFAULT),
        "new_model_flat_radar_chain": ((RADAR_PROFILE.replace("target_id=radar_puffin", "target_id=future")
                                        .replace("codec_profile=Radar", "codec_profile=Flat")
                                        .replace("gain=6", "gain=0"), None, None, None),
                                       "chain=0 flat=1 gain=0 pre=1.000 bass=0.00"),
    }
    with tempfile.TemporaryDirectory(prefix="libreecho-audio-profile.") as work:
        work_path = Path(work)
        src = work_path / "select.c"
        exe = work_path / "select"
        src.write_text(harness, encoding="utf-8")
        subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror", str(src), "-lm", "-o", str(exe)],
                       check=True, timeout=60)

        def run(paths):
            return subprocess.run([str(exe), *map(str, paths)], capture_output=True,
                                  text=True, check=True, timeout=10).stdout

        for name, (files, want) in cases.items():
            case = work_path / name
            case.mkdir()
            paths = []
            for index, content in enumerate(files):
                path = case / f"f{index}"
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                paths.append(path)
            got = run(paths)
            if got != want:
                raise SystemExit(f"speaker profile case {name}: got {got}, want {want}")
        link_case = work_path / "symlink"
        link_case.mkdir()
        (link_case / "real").write_text(DOT_PROFILE, encoding="utf-8")
        (link_case / "link").symlink_to(link_case / "real")
        if run([link_case / "link", link_case / "none", link_case / "none", link_case / "none"]) != RADAR_DEFAULT:
            raise SystemExit("speaker profile must not follow a symlinked profile")
        # A present-but-unreadable profile is final, never a fallthrough.
        (link_case / "id").write_text("target_id=biscuit\n", encoding="utf-8")
        if run([work_path, link_case / "none", link_case / "id", link_case / "none"]) != RADAR_DEFAULT:
            raise SystemExit("an unreadable profile path must not fall through to identity")


if __name__ == "__main__":
    main()
