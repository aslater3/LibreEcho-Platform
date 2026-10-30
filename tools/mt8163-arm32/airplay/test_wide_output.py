#!/usr/bin/env python3
"""24-bit speaker output: resolution, S16 fallback and PCM configuration.

The engine renders through the same chain (Radar and Dot) into an S32_LE
PCM when the kernel accepts it.  At a typical master the 16-bit path keeps
only ~8-10 significant bits; the wide path must keep the tone clean, match
the S16 render to within rounding, open 3 x 1024-frame S32_LE periods, and
fall back to the original S16 configuration when the kernel refuses S32.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

AIRPLAY = Path(__file__).resolve().parent
sys.path.insert(0, str(AIRPLAY))
sys.path.insert(0, str(AIRPLAY.parent))
from test_audio_period_buffer import PCM_HEADER  # noqa: E402
from test_radar_speaker_equivalence import MIXER_H, STUBS  # noqa: E402
import libreecho_platform_targets as targets  # noqa: E402

# Replace the PCM stubs: accept S32_LE only when the "kernel" supports it.
PCM_STUBS = r'''
static int kernel_wide;
static struct pcm_config opened;
static int open_calls;
static struct pcm the_pcm;
struct pcm *pcm_open(unsigned int a, unsigned int b, unsigned int f, const struct pcm_config *c)
{ (void)a; (void)b; (void)f; ++open_calls;
  if (c->format == PCM_FORMAT_S32_LE && !kernel_wide) return NULL;
  opened = *c; return &the_pcm; }
int pcm_is_ready(struct pcm *p) { return p != NULL; }
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

static double thdn(const double *x, size_t n, double hz)
{
    /* Two-term least squares (the window is not a whole number of cycles). */
    double ss = 0, cc = 0, sc = 0, sx = 0, cx = 0, e = 0, t = 0, det, a1, b1;
    size_t i;
    for (i = 0; i < n; ++i) {
        double a = 2 * M_PI * hz * (double)i / 48000.0, s = sin(a), c = cos(a);
        ss += s * s; cc += c * c; sc += s * c; sx += s * x[i]; cx += c * x[i];
    }
    det = ss * cc - sc * sc;
    a1 = (sx * cc - cx * sc) / det;
    b1 = (cx * ss - sx * sc) / det;
    for (i = 0; i < n; ++i) {
        double a = 2 * M_PI * hz * (double)i / 48000.0;
        double fit = a1 * sin(a) + b1 * cos(a);
        e += (x[i] - fit) * (x[i] - fit); t += fit * fit;
    }
    return 10.0 * log10(e / t);
}

/* Render 1 kHz @ -12 dBFS at master 50; returns THD+N of the chosen output. */
static double render_tone(int wide, double *max_s16_diff)
{
    static struct source_bus sources[SOURCE_COUNT];
    static int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    static double s16[PERIOD_SIZE * 8], w[PERIOD_SIZE * 8];
    struct puffin_dynamics dynamics;
    struct speaker_dsp speaker;
    size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);
    int32_t master_gain;
    unsigned int p, i, n = 0;

    wide_supported = wide;
    for (i = 0; i < SOURCE_COUNT; ++i) {
        free(sources[i].samples);
        memset(&sources[i], 0, sizeof(sources[i]));
        sources[i].fd = -1;
        sources[i].capacity = period_bytes * LE_AUDIO_PERIOD_BUFFER_PERIODS;
        sources[i].samples = calloc(1, sources[i].capacity);
        sources[i].gain_q15 = 32768;
    }
    puffin_dynamics_init(&dynamics);
    speaker_dsp_init(&speaker, speaker_volume_percent(sources, 50));
    if (biscuit_chain)
        biscuit_dsp_init(biscuit_chain, speaker_volume_percent(sources, 50));
    master_gain = logical_master_gain(50);
    *max_s16_diff = 0;
    for (p = 0; p < 12; ++p) {
        sources[SOURCE_MEDIA].received = period_bytes;
        for (i = 0; i < PERIOD_SIZE; ++i) {
            double t = (double)(p * PERIOD_SIZE + i) / 48000.0;
            int16_t v = (int16_t)lrint(32767.0 * 0.2512 * sin(2 * M_PI * 1000.0 * t));
            sources[SOURCE_MEDIA].samples[2 * i] = v;
            sources[SOURCE_MEDIA].samples[2 * i + 1] = v;
        }
        render_period(sources, NULL, 0, output, &dynamics, &speaker,
                      logical_master_gain(50), &master_gain);
        if (p < 4)
            continue; /* let the EQ and dynamics settle */
        for (i = 0; i < PERIOD_SIZE; ++i, ++n) {
            s16[n] = output[i * OUTPUT_CHANNELS];
            w[n] = wide_samples[i * OUTPUT_CHANNELS] / 65536.0;
            if (wide && fabs(w[n] - s16[n]) > *max_s16_diff)
                *max_s16_diff = fabs(w[n] - s16[n]);
            if (wide && wide_samples[i * OUTPUT_CHANNELS] !=
                        wide_samples[i * OUTPUT_CHANNELS + 1])
                *max_s16_diff = 1e9;
            if (wide && (wide_samples[i * OUTPUT_CHANNELS] & 0xff))
                *max_s16_diff = 1e9; /* MSB-aligned 24-bit: low byte zero */
        }
    }
    return thdn(wide ? w : s16, n, 1000.0);
}

int main(int argc, char **argv)
{
    double diff, narrow, wide;
    struct pcm_config s16 = { .channels = 2, .rate = 48000, .period_size = PERIOD_SIZE,
        .period_count = PERIOD_COUNT, .format = PCM_FORMAT_S16_LE };
    int fails = 0;

    mixlog = fopen("/dev/null", "w");
#ifdef MUSIC_AVL_UP_RATIO
    avl_enabled = 0; /* music levelling has its own tests */
#endif
    if (argc > 1 && argv[1][0]) {
        load_audio_profile(argv[1], &speaker_profile);
        if (speaker_profile.biscuit_chain) biscuit_chain = &biscuit_storage;
    }
    narrow = render_tone(0, &diff);
    wide = render_tone(1, &diff);
    printf("THD+N at master 50: S16 %.1f dB, 24-bit %.1f dB, wide vs S16 max %.2f LSB\n",
           narrow, wide, diff);
    if (!(wide < -75.0 && wide < narrow - 20.0)) { puts("FAIL: 24-bit path not cleaner"); ++fails; }
    if (!(diff <= 1.0)) { puts("FAIL: 24-bit render diverges from S16 or bad packing"); ++fails; }

    /* Kernel with 24-bit playback: S32_LE, 3 x 1024 frames, retained. */
    wide_supported = -1; kernel_wide = 1; open_calls = 0;
    if (!open_output_pcm(0, 23, &s16) || !wide_output || wide_supported != 1 ||
        opened.format != PCM_FORMAT_S32_LE || opened.period_size != 1024 ||
        opened.period_count != 3 || opened.stop_threshold != 3072)
        { puts("FAIL: wide PCM configuration"); ++fails; }
    /* Older kernel: one refused S32 open, then S16 exactly as before, and
     * later opens go straight to S16. */
    wide_supported = -1; kernel_wide = 0; open_calls = 0;
    if (!open_output_pcm(0, 23, &s16) || wide_output || wide_supported != 0 ||
        opened.format != PCM_FORMAT_S16_LE || opened.period_size != PERIOD_SIZE ||
        opened.period_count != PERIOD_COUNT || open_calls != 2)
        { puts("FAIL: S16 fallback"); ++fails; }
    open_calls = 0;
    if (!open_output_pcm(0, 23, &s16) || wide_output || open_calls != 1)
        { puts("FAIL: fallback is not remembered"); ++fails; }
    return fails != 0;
}
'''


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wide-output.") as tmp:
        work = Path(tmp)
        (work / "tinyalsa").mkdir()
        (work / "tinyalsa/mixer.h").write_text(MIXER_H)
        (work / "tinyalsa/pcm.h").write_text(PCM_HEADER)
        stubs = STUBS[:STUBS.index("struct pcm *pcm_open")] + PCM_STUBS
        (work / "STUBS.c").write_text(stubs)
        src = work / "wide.c"
        src.write_text(DRIVER.replace("ENGINE", str(AIRPLAY / "audio_engine.c"))
                       .replace("STUBS.c", str(work / "STUBS.c")))
        exe = work / "wide"
        subprocess.run(["cc", "-std=c99", "-O2", "-w", "-fmax-errors=5", "-ffunction-sections", "-fdata-sections",
                        "-Wl,--gc-sections", "-I", str(work), "-I", str(AIRPLAY), str(src),
                        "-lm", "-o", str(exe)], check=True, timeout=180)
        for target in ("radar_puffin", "biscuit"):
            profile = work / f"{target}.profile"
            profile.write_bytes(targets.audio_profile_bytes(target))
            print(f"{target}: ", end="", flush=True)
            subprocess.run([str(exe), str(profile)], check=True, timeout=120)
    print("wide_output: 24-bit render, S32_LE configuration and S16 fallback PASS")


if __name__ == "__main__":
    main()
