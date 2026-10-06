#!/usr/bin/env python3
"""Radar bit-exactness: release/0.14.0 audio_engine.c versus this branch.

Both engine sources are compiled into the same harness (tinyalsa stubbed).
Identical deterministic programme is pushed through render_period() for a
volume sweep, period by period exactly as run_engine() drives it, and every
mixer write made by arm_output_controls() is logged.  The Radar result must be
byte-identical; the only allowed new mixer write is the Radar codec profile
select (absent on an old kernel, where it is skipped without failing).

Usage: test_radar_speaker_equivalence.py [BASELINE_REF]
The baseline is audio_engine.c at BASELINE_REF (default: the merge-base with
origin/release/0.14.0), extracted with git; the candidate is the work tree.
Skips (exit 0, with a message) outside a git checkout with that ref.
"""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

AIRPLAY = Path(__file__).resolve().parent

STUBS = r'''
#include <stdio.h>
#include <string.h>
#include <tinyalsa/mixer.h>
#include <tinyalsa/pcm.h>
static FILE *mixlog;
static int have_profile_ctl;
struct mixer { int x; };
struct mixer_ctl { char name[64]; };
static struct mixer the_mixer;
static struct mixer_ctl ctls[64];
static int nctl;
struct mixer *mixer_open(unsigned int card) { (void)card; return &the_mixer; }
void mixer_close(struct mixer *m) { (void)m; }
struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *m, const char *name) {
    (void)m;
    if (!have_profile_ctl && !strcmp(name, "Speaker Codec Profile")) {
        fprintf(mixlog, "lookup-missing %s\n", name);
        return NULL;
    }
    if (nctl >= 64) return NULL;
    snprintf(ctls[nctl].name, sizeof(ctls[nctl].name), "%s", name);
    return &ctls[nctl++];
}
int mixer_ctl_set_enum_by_string(struct mixer_ctl *c, const char *v) {
    fprintf(mixlog, "enum %s=%s\n", c->name, v); return 0; }
unsigned int mixer_ctl_get_num_values(struct mixer_ctl *c) { (void)c; return 2; }
int mixer_ctl_set_value(struct mixer_ctl *c, unsigned int i, int v) {
    fprintf(mixlog, "int %s[%u]=%d\n", c->name, i, v); return 0; }
int mixer_ctl_get_value(struct mixer_ctl *c, unsigned int i) { (void)c; (void)i; return 127; }
struct pcm { int x; };
struct pcm *pcm_open(unsigned int a, unsigned int b, unsigned int f, const struct pcm_config *c)
{ (void)a; (void)b; (void)f; (void)c; return NULL; }
int pcm_is_ready(struct pcm *p) { (void)p; return 0; }
const char *pcm_get_error(struct pcm *p) { (void)p; return ""; }
void pcm_close(struct pcm *p) { (void)p; }
int pcm_prepare(struct pcm *p) { (void)p; return 0; }
int pcm_writei(struct pcm *p, const void *d, unsigned int n) { (void)p; (void)d; return (int)n; }
'''

DRIVER = r'''
#define _GNU_SOURCE
#define main libreecho_audio_engine_main
#include "STUBS.c"
#include "ENGINE"
#undef main
#include <stdlib.h>

static uint32_t rng = 0x12345678u;
static int16_t noise(void) { rng = rng * 1664525u + 1013904223u; return (int16_t)(rng >> 16); }

int main(int argc, char **argv)
{
    static struct source_bus sources[SOURCE_COUNT];
    static struct puffin_dynamics dynamics;
    static struct speaker_dsp speaker;
    static int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    static const int masters[] = { 1, 10, 25, 40, 49, 50, 60, 70, 80, 90, 95, 100 };
    size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);
    FILE *pcm_out = fopen(argv[1], "wb");
    unsigned int m, p, i;
    double phase = 0.0;
    int32_t master_gain;
    (void)argc;

    mixlog = fopen(argv[2], "w");
    have_profile_ctl = atoi(argv[3]);
#ifdef NEW_ENGINE
    if (argc > 4 && argv[4][0]) {
        int rc = load_audio_profile(argv[4], &speaker_profile);
        fprintf(mixlog, "profile rc=%d chain=%d\n", rc, speaker_profile.biscuit_chain);
        if (speaker_profile.biscuit_chain) biscuit_chain = &biscuit_storage;
    }
#endif
    arm_output_controls(0);
    for (i = 0; i < SOURCE_COUNT; ++i) {
        sources[i].fd = -1;
        sources[i].capacity = period_bytes * LE_AUDIO_PERIOD_BUFFER_PERIODS;
        sources[i].samples = calloc(1, sources[i].capacity);
        sources[i].gain_q15 = 32768;
    }
    sources[SOURCE_AIRPLAY].gain_q15 = 23170; /* -3 dB sender volume */
    for (m = 0; m < sizeof(masters) / sizeof(masters[0]); ++m) {
        int master = masters[m];
        puffin_dynamics_init(&dynamics);
        speaker_dsp_init(&speaker, speaker_volume_percent(sources, master));
        master_gain = logical_master_gain(master);
        for (p = 0; p < 48; ++p) {
            /* bass-heavy programme: 55/110/220 Hz + 2.5 kHz + noise, with
             * level swells so the compressors attack and release. */
            double level = (p % 16 < 8) ? 0.9 : 0.15;
            unsigned int src = (p / 12) % 2 ? SOURCE_AIRPLAY : SOURCE_MEDIA;
            for (i = 0; i < SOURCE_COUNT; ++i) sources[i].received = 0;
            sources[src].received = period_bytes;
            if (p % 24 == 20) /* a system chime ducks media */
                sources[SOURCE_SYSTEM].received = period_bytes;
            for (i = 0; i < PERIOD_SIZE; ++i) {
                double t = phase + (double)i / 48000.0;
                double x = level * (0.45 * sin(2 * M_PI * 55 * t) + 0.3 * sin(2 * M_PI * 110 * t)
                         + 0.2 * sin(2 * M_PI * 220 * t) + 0.08 * sin(2 * M_PI * 2500 * t));
                int16_t l = (int16_t)lrint(x * 26000.0) + noise() / 64;
                sources[src].samples[2 * i] = l;
                sources[src].samples[2 * i + 1] = (int16_t)(l / 2);
                sources[SOURCE_SYSTEM].samples[2 * i] = (int16_t)(8000 * sin(2 * M_PI * 880 * t));
                sources[SOURCE_SYSTEM].samples[2 * i + 1] = sources[SOURCE_SYSTEM].samples[2 * i];
            }
            phase += (double)PERIOD_SIZE / 48000.0;
            speaker_dsp_set_volume(&speaker, speaker_volume_percent(sources, master));
#ifdef NEW_ENGINE
            if (biscuit_chain)
                biscuit_dsp_set_volume(biscuit_chain, speaker_volume_percent(sources, master));
#endif
            render_period(sources, output, &dynamics, &speaker,
                          logical_master_gain(master), &master_gain);
            fwrite(output, sizeof(output), 1, pcm_out);
        }
    }
    fclose(pcm_out);
    fclose(mixlog);
    return 0;
}
'''

MIXER_H = '''#ifndef TINYALSA_MIXER_H
#define TINYALSA_MIXER_H
struct mixer; struct mixer_ctl;
struct mixer *mixer_open(unsigned int card);
void mixer_close(struct mixer *mixer);
struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *mixer, const char *name);
int mixer_ctl_set_enum_by_string(struct mixer_ctl *ctl, const char *value);
unsigned int mixer_ctl_get_num_values(struct mixer_ctl *ctl);
int mixer_ctl_set_value(struct mixer_ctl *ctl, unsigned int index, int value);
int mixer_ctl_get_value(struct mixer_ctl *ctl, unsigned int index);
#endif
'''


def build(work: Path, engine: Path, name: str, new: bool) -> Path:
    src = work / f"{name}.c"
    src.write_text(DRIVER.replace("ENGINE", str(engine)).replace("STUBS.c", str(work / "STUBS.c")))
    exe = work / name
    cmd = ["cc", "-std=c99", "-O2", "-w", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections", "-I", str(work), "-I", str(engine.parent), "-I", str(AIRPLAY)]
    if new:
        cmd.append("-DNEW_ENGINE")
    subprocess.run(cmd + [str(src), "-lm", "-o", str(exe)], check=True, timeout=180)
    return exe


def run(exe: Path, work: Path, tag: str, have_ctl: int, profile: str = "") -> tuple[str, str]:
    pcm, log = work / f"{tag}.pcm", work / f"{tag}.log"
    subprocess.run([str(exe), str(pcm), str(log), str(have_ctl), profile], check=True, timeout=600)
    return hashlib.sha256(pcm.read_bytes()).hexdigest(), log.read_text()


def baseline_source(work: Path) -> Path | None:
    ref = sys.argv[1] if len(sys.argv) > 1 else None
    git = ["git", "-C", str(AIRPLAY)]
    if ref is None:
        base = subprocess.run(git + ["merge-base", "HEAD", "origin/release/0.14.0"],
                              capture_output=True, text=True)
        if base.returncode:
            return None
        ref = base.stdout.strip()
    tree = work / "baseline"
    tree.mkdir()
    archive = subprocess.run(git + ["archive", ref, "--", "."], capture_output=True)
    if archive.returncode:
        return None
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
    return tree / "audio_engine.c"


def main() -> None:
    new_src = AIRPLAY / "audio_engine.c"
    sys.path.insert(0, str(AIRPLAY.parent))
    import libreecho_platform_targets as targets
    with tempfile.TemporaryDirectory(prefix="radar-equiv.") as tmp:
        work = Path(tmp)
        (work / "tinyalsa").mkdir()
        (work / "tinyalsa/mixer.h").write_text(MIXER_H)
        sys.path.insert(0, str(AIRPLAY))
        from test_audio_period_buffer import PCM_HEADER
        (work / "tinyalsa/pcm.h").write_text(PCM_HEADER)
        (work / "STUBS.c").write_text(STUBS)
        radar_profile = work / "radar.profile"
        radar_profile.write_bytes(targets.audio_profile_bytes("radar_puffin"))
        old_src = baseline_source(work)
        if old_src is None:
            print("radar_equivalence: SKIP (no git baseline available)")
            return
        old = build(work, old_src, "old", False)
        new = build(work, new_src, "new", True)
        base_hash, base_log = run(old, work, "old", 0)
        print(f"old engine radar PCM sha256 {base_hash}")
        print(f"pcm bytes {(work / 'old.pcm').stat().st_size}")
        failures = 0
        for label, have_ctl, profile in (
            ("new, Radar profile, new kernel", 1, str(radar_profile)),
            ("new, Radar profile, old kernel", 0, str(radar_profile)),
            ("new, no profile (legacy image)", 0, ""),
        ):
            h, log = run(new, work, label.replace(" ", "_").replace(",", ""), have_ctl, profile)
            writes = [l for l in log.splitlines() if not l.startswith(("profile rc", "lookup-missing"))]
            base_writes = base_log.splitlines()
            extra = [w for w in writes if w not in base_writes]
            missing = [w for w in base_writes if w not in writes]
            pcm_ok = h == base_hash
            mixer_ok = not missing and extra in ([], ["enum Speaker Codec Profile=Radar"])
            print(f"{label}: pcm {'IDENTICAL' if pcm_ok else 'DIFFERS ' + h}; "
                  f"mixer {'same' if mixer_ok else 'DIFFERS'} extra={extra} missing={missing}")
            failures += not (pcm_ok and mixer_ok)
        print("old mixer writes:\n  " + "\n  ".join(base_log.splitlines()))
        if failures:
            raise SystemExit(f"radar equivalence FAILED ({failures})")
        print("radar_equivalence: Radar output and mixer writes unchanged PASS")


if __name__ == "__main__":
    main()
