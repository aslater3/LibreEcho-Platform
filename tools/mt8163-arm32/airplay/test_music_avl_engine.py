#!/usr/bin/env python3
"""Music AVL through the real engine render path (audio_engine.c compiled in).

Levels are measured after the master volume (stock "smart volume").  At
volume 50 (-32 dB) a typical -12 dBFS programme is lifted toward +15 dB
(~+13.6 dB after 25 s of the 10 s release); a -34 dBFS tone sits partly under
the -55 dBFS gate and gets ~+5 dB; volume 95 (level 10) gets no boost; the
system bus and the Sendspin stream are untouched (per-device levelling would
skew the loudness of grouped rooms the server keeps in step); the boost ramps
within each period; and the runtime
kill switch (<root>/avl.disabled) turns it off.
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

/* Render `periods` of a 1 kHz tone at `amp` on `bus` (SOURCE_COUNT selects the
 * Sendspin stream input); returns last-period dB. */
static double run(unsigned int bus, double amp, int periods, int avl, int vol, double *max_step)
{
    static struct source_bus sources[SOURCE_COUNT];
    static int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    static int16_t sendspin[PERIOD_SIZE * OUTPUT_CHANNELS];
    int ss = bus == SOURCE_COUNT;
    struct puffin_dynamics dyn; struct speaker_dsp dsp;
    size_t pb = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t), i; int p; int32_t mg;
    double last = 0, prev = 0; int16_t prev_last = 0;
    avl_enabled = avl; music_avl_init(&music_avl_state); avl_gain = 1.0f; avl_master_percent = vol;
    for (i = 0; i < SOURCE_COUNT; ++i) { free(sources[i].samples); memset(&sources[i], 0, sizeof(sources[i]));
        sources[i].fd = -1; sources[i].capacity = pb * 2; sources[i].samples = calloc(1, pb * 2); sources[i].gain_q15 = 32768; }
    puffin_dynamics_init(&dyn); speaker_dsp_init(&dsp, vol); mg = logical_master_gain(vol);
    *max_step = 0;
    for (p = 0; p < periods; ++p) {
        int16_t *dst = ss ? sendspin : (int16_t *)sources[bus].samples;
        if (!ss) sources[bus].received = pb;
        for (i = 0; i < PERIOD_SIZE; ++i) { int16_t v = (int16_t)lrint(amp * sin(2 * M_PI * 1000.0 * (double)(p * PERIOD_SIZE + i) / 48000.0));
            dst[2 * i] = v; dst[2 * i + 1] = v; }
        render_period(sources, ss ? sendspin : NULL, ss ? PERIOD_SIZE : 0, output,
                      &dyn, &dsp, logical_master_gain(vol), &mg);
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
    off = run(SOURCE_MEDIA, 32768 * 0.2512, 590, 0, 50, &step);   /* -12 dBFS RMS, ~25 s */
    on  = run(SOURCE_MEDIA, 32768 * 0.2512, 590, 1, 50, &step);
    printf("typical programme (-12 dBFS) at volume 50: boost %+.1f dB; max period-to-period change %.2f dB\n", on - off, step);
    if (!(on - off > 12.5 && on - off < 14.8)) { puts("FAIL: programme not lifted toward +15 dB at volume 50"); ++fails; }
    if (!(step < 0.2)) { puts("FAIL: AVL boost changes in steps"); ++fails; }
    loud_off = run(SOURCE_MEDIA, 32768 * 0.02818, 590, 0, 50, &step); /* -34 dBFS, partly gated */
    loud_on  = run(SOURCE_MEDIA, 32768 * 0.02818, 590, 1, 50, &step);
    printf("quiet tone (-34 dBFS) at volume 50: boost %+.1f dB (gate)\n", loud_on - loud_off);
    if (!(loud_on - loud_off > 4.0 && loud_on - loud_off < 6.5)) { puts("FAIL: gate fade not applied"); ++fails; }
    off = run(SOURCE_MEDIA, 32768 * 0.2512, 300, 0, 95, &step);
    on  = run(SOURCE_MEDIA, 32768 * 0.2512, 300, 1, 95, &step);
    printf("volume 95 (level 10): boost %+.2f dB\n", on - off);
    if (!(fabs(on - off) < 0.05)) { puts("FAIL: boost at the top volume levels"); ++fails; }
    sys_off = run(SOURCE_SYSTEM, 32768 * 0.2512, 300, 0, 50, &step);
    sys_on  = run(SOURCE_SYSTEM, 32768 * 0.2512, 300, 1, 50, &step);
    printf("system bus: boost %+.2f dB\n", sys_on - sys_off);
    if (!(fabs(sys_on - sys_off) < 0.05)) { puts("FAIL: system bus was levelled"); ++fails; }
    sys_off = run(SOURCE_COUNT, 32768 * 0.2512, 590, 0, 50, &step);
    sys_on  = run(SOURCE_COUNT, 32768 * 0.2512, 590, 1, 50, &step);
    printf("sendspin stream: boost %+.2f dB (level %.1f dBFS)\n", sys_on - sys_off, sys_off);
    if (!(fabs(sys_on - sys_off) < 0.05)) { puts("FAIL: Sendspin stream was AVL levelled"); ++fails; }
    if (!(sys_off > -60.0)) { puts("FAIL: Sendspin stream did not reach the render output"); ++fails; }
    off = run(SOURCE_MEDIA, 32768 * 0.2512, 590, 0, 50, &step);
    /* Kill switch: the file turns AVL off at the next period. */
    snprintf(avl_disable_path, sizeof(avl_disable_path), "%s/avl.disabled", argv[1]);
    f = fopen(avl_disable_path, "w"); fclose(f);
    killed = run(SOURCE_MEDIA, 32768 * 0.2512, 590, 1, 50, &step);
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
    print("music_avl_engine: post-volume lift, gate, top-level, system bus, sendspin, smooth, kill switch PASS")

if __name__ == "__main__":
    main()
