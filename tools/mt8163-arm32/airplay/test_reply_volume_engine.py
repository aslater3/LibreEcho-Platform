#!/usr/bin/env python3
"""Separate reply volume through the real engine render path (LibreEcho-UI #409).

audio_engine.c is compiled in.  The driver writes <root>/master.volume and
<root>/reply.volume exactly as audiod publishes them and drives the engine's
own apply_reply_volume() and render_period(), on both the S16 and the wide
(float) output chains:

- without reply.volume the reply buses follow the master, bit for bit;
- with it, a reply on the announcement or system bus at master M sounds as
  loud as the same reply at master R (boost over quiet music and cut under
  loud music), while media and the alarm bus keep the master level;
- a master of zero still silences replies;
- moving the master from 100 to 1 while a reply plays never bursts;
- malformed or out-of-range reply.volume content means "follow the master".
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

static const char *root;
static int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];

static double rms_db(const int16_t *o) { double s = 0; size_t i;
    for (i = 0; i < PERIOD_SIZE; ++i) s += (double)o[i * 2] * o[i * 2];
    return 10 * log10(s / PERIOD_SIZE / (32768.0 * 32768.0) + 1e-30); }

static void put(const char *name, const char *text)
{
    char path[512]; FILE *f;
    snprintf(path, sizeof(path), "%s/%s", root, name);
    if (!text) { remove(path); return; }
    f = fopen(path, "w"); fputs(text, f); fclose(f);
}

static void put_level(const char *name, int value)
{
    char text[16];
    if (value < 0) { put(name, NULL); return; }
    snprintf(text, sizeof(text), "%d\n", value);
    put(name, text);
}

/* Render `periods` of a 1 kHz tone on `bus` at master `vol` with the reply
 * level `reply` (-1: no reply.volume).  If `vol_end` >= 0 the master moves
 * there after `periods / 2`.  Returns the last period's level; *peak gets the
 * loudest period after the first few (ramp-in) periods. */
static double run(unsigned int bus, int vol, int reply, int periods, int vol_end,
                  double *peak, int16_t *last_out)
{
    static struct source_bus sources[SOURCE_COUNT];
    struct puffin_dynamics dyn; struct speaker_dsp dsp;
    size_t pb = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t), i; int p; int32_t mg;
    double last = -300;
    int master = vol;
    avl_enabled = 0; music_avl_init(&music_avl_state); avl_gain = 1.0f;
    reply_current_q15 = -1;
    for (i = 0; i < SOURCE_COUNT; ++i) { free(sources[i].samples); memset(&sources[i], 0, sizeof(sources[i]));
        sources[i].fd = -1; sources[i].capacity = pb * 2; sources[i].samples = calloc(1, pb * 2); sources[i].gain_q15 = 32768; }
    put_level("master.volume", master); put_level("reply.volume", reply);
    apply_reply_volume(root);
    puffin_dynamics_init(&dyn);
    speaker_dsp_init(&dsp, speaker_volume_percent(sources, master));
    mg = logical_master_gain(master);
    if (peak) *peak = -300;
    for (p = 0; p < periods; ++p) {
        int16_t *dst = (int16_t *)sources[bus].samples;
        if (vol_end >= 0 && p == periods / 2) { master = vol_end; put_level("master.volume", master); }
        sources[bus].received = pb;
        for (i = 0; i < PERIOD_SIZE; ++i) { int16_t v = (int16_t)lrint(32768 * 0.2512 * sin(2 * M_PI * 1000.0 * (double)(p * PERIOD_SIZE + i) / 48000.0));
            dst[2 * i] = v; dst[2 * i + 1] = v; }
        apply_reply_volume(root);
        speaker_dsp_set_volume(&dsp, speaker_volume_percent(sources, master));
        render_period(sources, NULL, 0, output, &dyn, &dsp, logical_master_gain(master), &mg);
        last = rms_db(output);
        if (peak && p > 4 && last > *peak) *peak = last;
    }
    if (last_out) memcpy(last_out, output, sizeof(output));
    put("reply.volume", NULL);
    return last;
}

#define NEAR(a, b, tol) (fabs((a) - (b)) <= (tol))

static int chain(int wide)
{
    static int16_t a[PERIOD_SIZE * OUTPUT_CHANNELS], b[PERIOD_SIZE * OUTPUT_CHANNELS];
    double ref, got, med, med_ref, peak, steady;
    int fails = 0;
    const char *name = wide ? "wide" : "s16";

    wide_supported = wide ? 1 : 0;
    /* Follow: no file renders exactly as before. */
    reply_target_q15 = -1;
    run(SOURCE_ANNOUNCEMENT, 50, -1, 40, -1, NULL, a);
    put_level("master.volume", 50);
    run(SOURCE_ANNOUNCEMENT, 50, -1, 40, -1, NULL, b);
    if (memcmp(a, b, sizeof(a))) { printf("FAIL %s: follow mode not deterministic\n", name); ++fails; }

    /* Boost: reply 80 over master 30 matches master 80. */
    ref = run(SOURCE_ANNOUNCEMENT, 80, -1, 40, -1, NULL, NULL);
    got = run(SOURCE_ANNOUNCEMENT, 30, 80, 40, -1, NULL, NULL);
    printf("%s announcement: master 80 %.2f dB, master 30 + reply 80 %.2f dB\n", name, ref, got);
    if (!NEAR(got, ref, 0.3)) { printf("FAIL %s: reply boost not at the reply level\n", name); ++fails; }
    /* Cut: reply 20 under master 90 matches master 20, on the system bus. */
    ref = run(SOURCE_SYSTEM, 20, -1, 40, -1, NULL, NULL);
    got = run(SOURCE_SYSTEM, 90, 20, 40, -1, NULL, NULL);
    printf("%s system: master 20 %.2f dB, master 90 + reply 20 %.2f dB\n", name, ref, got);
    if (!NEAR(got, ref, 0.3)) { printf("FAIL %s: reply cut not at the reply level\n", name); ++fails; }
    if (!(run(SOURCE_SYSTEM, 90, -1, 40, -1, NULL, NULL) - got > 10.0)) {
        printf("FAIL %s: reply cut had no effect\n", name); ++fails; }

    /* Media and alarm keep the master level whatever the reply level. */
    med_ref = run(SOURCE_MEDIA, 40, -1, 40, -1, NULL, NULL);
    med = run(SOURCE_MEDIA, 40, 90, 40, -1, NULL, NULL);
    printf("%s media at master 40: %.2f dB, with reply 90 %.2f dB\n", name, med_ref, med);
    if (!NEAR(med, med_ref, 0.01)) { printf("FAIL %s: media followed the reply level\n", name); ++fails; }
    med_ref = run(SOURCE_ALARM, 40, -1, 40, -1, NULL, NULL);
    med = run(SOURCE_ALARM, 40, 90, 40, -1, NULL, NULL);
    if (!NEAR(med, med_ref, 0.01)) { printf("FAIL %s: alarm followed the reply level\n", name); ++fails; }

    /* Master zero still mutes replies. */
    got = run(SOURCE_ANNOUNCEMENT, 0, 80, 40, -1, NULL, NULL);
    if (!(got < -150.0)) { printf("FAIL %s: reply audible at master 0 (%.1f dB)\n", name, got); ++fails; }

    /* Master 100 -> 1 during a reply at 60: no burst above the steady reply. */
    steady = run(SOURCE_ANNOUNCEMENT, 60, -1, 40, -1, NULL, NULL);
    got = run(SOURCE_ANNOUNCEMENT, 100, 60, 60, 1, &peak, NULL);
    printf("%s master 100->1 under reply 60: steady %.2f dB, peak %.2f dB, end %.2f dB\n", name, steady, peak, got);
    if (!(peak < steady + 0.5)) { printf("FAIL %s: reply burst when the master moved\n", name); ++fails; }
    if (!NEAR(got, steady, 0.3)) { printf("FAIL %s: reply did not hold its level\n", name); ++fails; }

    /* Malformed content follows the master. */
    ref = run(SOURCE_ANNOUNCEMENT, 50, -1, 20, -1, NULL, NULL);
    {
        static const char *const bad[] = { "abc\n", "101\n", "-3\n", "50\n\n", " \n", "60x\n" };
        size_t k;
        for (k = 0; k < sizeof(bad) / sizeof(bad[0]); ++k) {
            put("reply.volume", bad[k]);
            if (read_reply_volume(root) != -1) { printf("FAIL: accepted reply.volume %s", bad[k]); ++fails; }
        }
        put("reply.volume", "75");
        if (read_reply_volume(root) != 75) { puts("FAIL: bare 75 rejected"); ++fails; }
        put("reply.volume", NULL);
    }
    (void)ref;
    return fails;
}

int main(int argc, char **argv)
{
    int fails = 0;
    mixlog = fopen("/dev/null", "w");
    (void)argc;
    root = argv[1];
    fails += chain(0);
    fails += chain(1);
    return fails != 0;
}
'''

def main() -> None:
    with tempfile.TemporaryDirectory(prefix="reply-volume-engine.") as tmp:
        work = Path(tmp)
        (work / "tinyalsa").mkdir()
        (work / "tinyalsa/mixer.h").write_text(MIXER_H)
        (work / "tinyalsa/pcm.h").write_text(PCM_HEADER)
        (work / "STUBS.c").write_text(STUBS)
        src = work / "reply.c"
        src.write_text(DRIVER.replace("ENGINE", str(AIRPLAY / "audio_engine.c")).replace("STUBS.c", str(work / "STUBS.c")))
        exe = work / "reply"
        subprocess.run(["cc", "-std=c99", "-O2", "-w", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                        "-I", str(work), "-I", str(AIRPLAY), str(src), "-lm", "-o", str(exe)], check=True, timeout=180)
        subprocess.run([str(exe), str(work)], check=True, timeout=300)
    print("reply_volume_engine: follow, boost, cut, media/alarm untouched, mute, no burst, malformed PASS")

if __name__ == "__main__":
    main()
