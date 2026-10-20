#!/usr/bin/env python3
"""Music AVL through the real engine render path (audio_engine.c compiled in).

A quiet media tone is lifted toward the stock curve (+12 dB static at -34
dBFS; ~+11 dB after 25 s of the 10 s release), a loud tone and the system bus
are untouched, the boost ramps within each period (no steps), and the runtime
kill switch (<root>/avl.disabled) turns it off at the next period.
"""
import subprocess, sys, tempfile
from pathlib import Path
AIRPLAY = Path(__file__).resolve().parent
sys.path.insert(0, str(AIRPLAY))
from test_audio_period_buffer import PCM_HEADER  # noqa: E402
from test_radar_speaker_equivalence import MIXER_H, STUBS  # noqa: E402

DRIVER = r'''
#define _GNU_SOURCE
#define main libreecho_audio_engine_main
#include "STUBS.c"
#include "ENGINE"
#undef main
#include <stdlib.h>
#include <stdio.h>

static double rms_db(const int16_t *o) { double s = 0; size_t i;
    for (i = 0; i < PERIOD_SIZE; ++i) s += (double)o[i * 2] * o[i * 2];
    return 10 * log10(s / PERIOD_SIZE / (32768.0 * 32768.0) + 1e-30); }

/* Render `periods` of a 1 kHz tone at `amp` on `bus`; returns last-period dB. */
static double run(unsigned int bus, double amp, int periods, int avl, double *max_step)
{
    static struct source_bus sources[SOURCE_COUNT];
    static int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    struct puffin_dynamics dyn; struct speaker_dsp dsp;
    size_t pb = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t), i; int p; int32_t mg;
    double last = 0, prev = 0; int16_t prev_last = 0;
    avl_enabled = avl; music_avl_init(&music_avl_state); avl_gain = 1.0f; avl_master_percent = 50;
    for (i = 0; i < SOURCE_COUNT; ++i) { free(sources[i].samples); memset(&sources[i], 0, sizeof(sources[i]));
        sources[i].fd = -1; sources[i].capacity = pb * 2; sources[i].samples = calloc(1, pb * 2); sources[i].gain_q15 = 32768; }
    puffin_dynamics_init(&dyn); speaker_dsp_init(&dsp, 50); mg = logical_master_gain(50);
    *max_step = 0;
    for (p = 0; p < periods; ++p) {
        sources[bus].received = pb;
        for (i = 0; i < PERIOD_SIZE; ++i) { int16_t v = (int16_t)lrint(amp * sin(2 * M_PI * 1000.0 * (double)(p * PERIOD_SIZE + i) / 48000.0));
            sources[bus].samples[2 * i] = v; sources[bus].samples[2 * i + 1] = v; }
        render_period(sources, output, &dyn, &dsp, logical_master_gain(50), &mg);
        last = rms_db(output);
        if (p > 4 && fabs(last - prev) > *max_step) *max_step = fabs(last - prev);
        prev = last; (void)prev_last;
    }
    return last;
}

int main(int argc, char **argv)
{
    double step, off, on, loud_off, loud_on, sys_off, sys_on, killed;
    int fails = 0; FILE *f;
    mixlog = fopen("/dev/null", "w");
    wide_supported = 0;
    (void)argc;
    off = run(SOURCE_MEDIA, 32768 * 0.02818, 590, 0, &step);   /* -34 dBFS RMS mono */
    on  = run(SOURCE_MEDIA, 32768 * 0.02818, 590, 1, &step);   /* ~25 s */
    printf("quiet media tone: AVL off %.1f dB, on %.1f dB -> boost %+.1f dB; max period-to-period change %.2f dB\n", off, on, on - off, step);
    if (!(on - off > 9.5 && on - off < 12.5)) { puts("FAIL: quiet media not lifted toward +12 dB"); ++fails; }
    if (!(step < 0.2)) { puts("FAIL: AVL boost changes in steps"); ++fails; }
    loud_off = run(SOURCE_MEDIA, 32768 * 0.7079, 120, 0, &step); /* -6 dBFS RMS */
    loud_on  = run(SOURCE_MEDIA, 32768 * 0.7079, 120, 1, &step);
    printf("loud media tone: boost %+.2f dB\n", loud_on - loud_off);
    if (!(fabs(loud_on - loud_off) < 0.05)) { puts("FAIL: loud media was levelled"); ++fails; }
    sys_off = run(SOURCE_SYSTEM, 32768 * 0.02818, 300, 0, &step);
    sys_on  = run(SOURCE_SYSTEM, 32768 * 0.02818, 300, 1, &step);
    printf("quiet system tone: boost %+.2f dB\n", sys_on - sys_off);
    if (!(fabs(sys_on - sys_off) < 0.05)) { puts("FAIL: system bus was levelled"); ++fails; }
    /* Kill switch: the file turns AVL off at the next period. */
    snprintf(avl_disable_path, sizeof(avl_disable_path), "%s/avl.disabled", argv[1]);
    f = fopen(avl_disable_path, "w"); fclose(f);
    killed = run(SOURCE_MEDIA, 32768 * 0.02818, 590, 1, &step);
    printf("kill switch present: boost %+.2f dB\n", killed - off);
    if (!(fabs(killed - off) < 0.05)) { puts("FAIL: avl.disabled did not turn AVL off"); ++fails; }
    remove(avl_disable_path);
    return fails != 0;
}
'''

def main() -> None:
    with tempfile.TemporaryDirectory(prefix="music-avl-engine.") as tmp:
        work = Path(tmp)
        (work / "tinyalsa").mkdir()
        (work / "tinyalsa/mixer.h").write_text(MIXER_H)
        (work / "tinyalsa/pcm.h").write_text(PCM_HEADER)
        (work / "STUBS.c").write_text(STUBS)
        src = work / "avl.c"
        src.write_text(DRIVER.replace("ENGINE", str(AIRPLAY / "audio_engine.c")).replace("STUBS.c", str(work / "STUBS.c")))
        exe = work / "avl"
        subprocess.run(["cc", "-std=c99", "-O2", "-w", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                        "-I", str(work), "-I", str(AIRPLAY), str(src), "-lm", "-o", str(exe)], check=True, timeout=180)
        subprocess.run([str(exe), str(work)], check=True, timeout=300)
    print("music_avl_engine: lift, loud/system untouched, smooth, kill switch PASS")

if __name__ == "__main__":
    main()
