#!/usr/bin/env python3
"""Owner equaliser and boost applied live through the real engine (audio_engine.c compiled in).

The driver publishes <root>/tone.conf and <root>/boost.conf the way audiod
does (temp file + rename, removal for flat) and drives the engine's own
apply_owner_settings(), poll_owner_settings() and render_period(), on the
Radar-Puffin and the Echo Dot (biscuit) chains:

- playback start reads both files once and applies them at once;
- while playing the files are only stat'ed every OWNER_SETTINGS_POLL_PERIODS
  periods (about 0.5 s), and an unchanged file is never re-read;
- a changed file is re-read on the next poll, alone, and crossfaded in; a
  removed file is flat / 0 dB; schema 1 still applies; a rejected file is flat;
- a change while rendering a tone does not put a step in the PCM output
  larger than the programme's own sample-to-sample change (within the
  protection's attack overshoot, as in the chain host tests), while the level
  does move by the new setting.
"""
import subprocess
import sys
import tempfile
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
static int fails;

#define CHECK(cond, ...) do { if (cond) { printf("  ok   "); } else { printf("  FAIL "); ++fails; } \
    printf(__VA_ARGS__); printf("\n"); } while (0)

/* Atomic publish, as audiod does: temp file in the same directory, rename. */
static void put(const char *name, const char *text)
{
    char path[512], tmp[520]; FILE *f;
    snprintf(path, sizeof(path), "%s/%s", root, name);
    if (!text) { remove(path); return; }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    f = fopen(tmp, "w"); fputs(text, f); fclose(f);
    rename(tmp, path);
}

static int db_is(const float *db, float a, float b, float c, float d)
{
    return db[0] == a && db[1] == b && db[2] == c && db[3] == d;
}

/* One playback period of audio through the chain(s), so fades progress
 * between polls as they do in the playback loop. */
static void play_period(struct speaker_dsp *dsp)
{
    size_t i;
    for (i = 0; i < PERIOD_SIZE; ++i) {
        float x = (float)(3000.0 * sin(2 * M_PI * 200.0 * (double)i / 48000.0));
        (void)speaker_dsp_process_f(dsp, x);
        if (biscuit_chain)
            (void)biscuit_dsp_process(biscuit_chain, x);
    }
}

static int polls_until_reread(struct speaker_dsp *dsp, int limit)
{
    int p;
    for (p = 1; p <= limit; ++p) {
        if (poll_owner_settings(root, dsp))
            return p; /* state just after the re-read, before its period */
        play_period(dsp);
    }
    return 0;
}

static void poll_mechanics(int dot)
{
    static struct speaker_dsp dsp;
    unsigned long reads;
    int p, n;
    const char *name = dot ? "dot" : "radar";

    printf("%s: poll cadence and re-read only on change\n", name);
    biscuit_chain = dot ? &biscuit_storage : NULL;
    put("tone.conf", "schema=2\nbass_db=6\nlow_mid_db=0\nhigh_mid_db=0\ntreble_db=0\n");
    put("boost.conf", "schema=1\nboost_db=3\n");
    speaker_dsp_init(&dsp, 70);
    if (dot) { biscuit_dsp_init(biscuit_chain, 70); biscuit_dsp_configure(biscuit_chain, 0.0f); }
    owner_settings.reads = 0;
    apply_owner_settings(root, &dsp);
    CHECK(owner_settings.reads == 2, "playback start reads each file once (reads=%lu)", owner_settings.reads);
    CHECK(dsp.owner.on == 1u && dsp.owner.fade == 0 && dsp.boost_gain == speaker_boost_gain(3.0f),
          "playback start applies the settings at once, no fade");
    if (dot)
        CHECK(biscuit_chain->owner.on == 1u && biscuit_chain->owner.fade == 0 &&
              biscuit_chain->boost == pow(10.0, 3.0 / 20.0), "dot chain gets the same settings");

    reads = owner_settings.reads;
    for (p = 0; p < 50 * (int)OWNER_SETTINGS_POLL_PERIODS; ++p)
        if (poll_owner_settings(root, &dsp))
            break;
    CHECK(owner_settings.reads == reads && p == 50 * (int)OWNER_SETTINGS_POLL_PERIODS,
          "unchanged files are not re-read over %u periods (reads %lu -> %lu)",
          50 * OWNER_SETTINGS_POLL_PERIODS, reads, owner_settings.reads);

    /* A change is picked up on the next poll tick, never sooner. */
    owner_settings.periods = 0;
    put("tone.conf", "schema=2\nbass_db=0\nlow_mid_db=3\nhigh_mid_db=-2\ntreble_db=1.5\n");
    reads = owner_settings.reads;
    n = polls_until_reread(&dsp, 100);
    CHECK(n == (int)OWNER_SETTINGS_POLL_PERIODS, "a change is seen on poll period %d (want %u, ~0.5 s)",
          n, OWNER_SETTINGS_POLL_PERIODS);
    CHECK(owner_settings.reads == reads + 1, "only the changed file is re-read (reads +%lu)",
          owner_settings.reads - reads);
    CHECK(db_is(dsp.owner.db, 0.0f, 3.0f, -2.0f, 1.5f) && dsp.owner.fade == SPEAKER_DSP_OWNER_FADE_FRAMES,
          "the new four-band setting crossfades in over one period");
    CHECK(dsp.boost_target == speaker_boost_gain(3.0f) && dsp.boost_ramp == 0, "the boost is untouched");
    if (dot)
        CHECK(db_is(biscuit_chain->owner.db, 0.0f, 3.0f, -2.0f, 1.5f) &&
              biscuit_chain->owner.fade == BISCUIT_OWNER_FADE_FRAMES, "dot chain crossfades the same setting");

    put("boost.conf", NULL);
    reads = owner_settings.reads;
    n = polls_until_reread(&dsp, 100);
    CHECK(n == (int)OWNER_SETTINGS_POLL_PERIODS && owner_settings.reads == reads + 1 &&
          dsp.boost_target == 1.0f && dsp.boost_ramp == SPEAKER_DSP_OWNER_FADE_FRAMES,
          "removed boost.conf ramps to 0 dB");
    if (dot)
        CHECK(biscuit_chain->boost_target == 1.0 && biscuit_chain->boost_ramp == BISCUIT_OWNER_FADE_FRAMES,
              "dot boost ramps to 0 dB");

    put("tone.conf", "schema=1\nbass_db=2\ntreble_db=-1\n");
    (void)polls_until_reread(&dsp, 100);
    CHECK(db_is(dsp.owner.db, 2.0f, 0.0f, 0.0f, -1.0f), "a schema 1 file still applies (mid bands flat)");

    put("tone.conf", "schema=2\nbass_db=2\nlow_mid_db=1\ntreble_db=-1\n");
    (void)polls_until_reread(&dsp, 100);
    CHECK(db_is(dsp.owner.db, 0.0f, 0.0f, 0.0f, 0.0f), "a rejected file (missing key) is flat");

    put("tone.conf", "schema=2\nbass_db=4\nlow_mid_db=0\nhigh_mid_db=0\ntreble_db=0\n");
    (void)polls_until_reread(&dsp, 100);
    put("tone.conf", NULL);
    (void)polls_until_reread(&dsp, 100);
    CHECK(db_is(dsp.owner.db, 0.0f, 0.0f, 0.0f, 0.0f), "removed tone.conf is flat");
    if (dot)
        CHECK(db_is(biscuit_chain->owner.db, 0.0f, 0.0f, 0.0f, 0.0f), "dot chain is flat too");

    /* No files at all: nothing is re-read while playing. */
    reads = owner_settings.reads;
    for (p = 0; p < 50 * (int)OWNER_SETTINGS_POLL_PERIODS; ++p)
        (void)poll_owner_settings(root, &dsp);
    CHECK(owner_settings.reads == reads, "absent files are not re-read while playing");
    biscuit_chain = NULL;
}

/* Render a 90 Hz + 400 Hz media programme period by period exactly as the
 * playback loop does (poll, then render) and change the settings at
 * period `change`.  Returns the largest sample step around the change over
 * the larger natural step of the steady stretches before and after it. */
#define PERIODS 60
static int16_t pcm[PERIODS * PERIOD_SIZE];

static double render_change(int dot, int wide, int master, double *lift_db)
{
    static struct source_bus sources[SOURCE_COUNT];
    struct puffin_dynamics dyn; static struct speaker_dsp dsp;
    static int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    size_t pb = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t), i;
    int p, change = 24;
    int32_t mg;
    double before = 0, after = 0, window = 0, rb = 0, ra = 0;
    long n;

    wide_supported = wide;
    avl_enabled = 0; music_avl_init(&music_avl_state); avl_gain = 1.0f;
    reply_target_q15 = -1; reply_current_q15 = -1;
    biscuit_chain = dot ? &biscuit_storage : NULL;
    for (i = 0; i < SOURCE_COUNT; ++i) { free(sources[i].samples); memset(&sources[i], 0, sizeof(sources[i]));
        sources[i].fd = -1; sources[i].capacity = pb * 2; sources[i].samples = calloc(1, pb * 2);
        sources[i].gain_q15 = 32768; }
    put("tone.conf", NULL);
    put("boost.conf", NULL);
    puffin_dynamics_init(&dyn);
    speaker_dsp_init(&dsp, speaker_volume_percent(sources, master));
    if (dot) { biscuit_dsp_init(biscuit_chain, speaker_volume_percent(sources, master));
               biscuit_dsp_configure(biscuit_chain, 0.0f); }
    apply_owner_settings(root, &dsp);
    mg = logical_master_gain(master);
    for (p = 0; p < PERIODS; ++p) {
        int16_t *dst = (int16_t *)sources[SOURCE_MEDIA].samples;
        if (p == change - (int)OWNER_SETTINGS_POLL_PERIODS + 1) {
            put("tone.conf", "schema=2\nbass_db=6\nlow_mid_db=6\nhigh_mid_db=-6\ntreble_db=6\n");
            put("boost.conf", "schema=1\nboost_db=6\n");
        }
        sources[SOURCE_MEDIA].received = pb;
        for (i = 0; i < PERIOD_SIZE; ++i) {
            double t = (double)(p * PERIOD_SIZE + i) / 48000.0;
            int16_t v = (int16_t)lrint(32768 * 0.5 * (0.7 * sin(2 * M_PI * 90.0 * t) + 0.3 * sin(2 * M_PI * 400.0 * t)));
            dst[2 * i] = v; dst[2 * i + 1] = v; }
        speaker_dsp_set_volume(&dsp, speaker_volume_percent(sources, master));
        if (biscuit_chain)
            biscuit_dsp_set_volume(biscuit_chain, speaker_volume_percent(sources, master));
        (void)poll_owner_settings(root, &dsp);
        render_period(sources, NULL, 0, output, &dyn, &dsp, logical_master_gain(master), &mg);
        for (i = 0; i < PERIOD_SIZE; ++i)
            pcm[p * PERIOD_SIZE + i] = output[i * OUTPUT_CHANNELS];
    }
    for (n = 1; n < PERIODS * PERIOD_SIZE; ++n) {
        double step = fabs((double)pcm[n] - (double)pcm[n - 1]);
        long period = n / PERIOD_SIZE;
        if (period >= 12 && period < change && step > before) before = step;
        if (period >= PERIODS - 12 && step > after) after = step;
        if (period >= change && period < change + 3 && step > window) window = step;
        if (period >= 12 && period < change) rb += (double)pcm[n] * pcm[n];
        if (period >= PERIODS - 12) ra += (double)pcm[n] * pcm[n];
    }
    *lift_db = 10 * log10(ra / rb);
    put("tone.conf", NULL);
    put("boost.conf", NULL);
    biscuit_chain = NULL;
    return window / (before > after ? before : after);
}

int main(int argc, char **argv)
{
    int dot, wide;
    mixlog = fopen("/dev/null", "w");
    (void)argc;
    root = argv[1];
    poll_mechanics(0);
    poll_mechanics(1);
    for (dot = 0; dot <= 1; ++dot)
        for (wide = 0; wide <= 1; ++wide) {
            double lift, ratio = render_change(dot, wide, 30, &lift);
            printf("%s %s: live change while rendering, step ratio %.3f, level %+.1f dB\n",
                   dot ? "dot" : "radar", wide ? "wide" : "s16", ratio, lift);
            /* Bound as in the chain tests: the bus does not step; at the
             * output the Dot's compressors/limiters (and Radar MBCL) may
             * lag a +12 dB rise inside one period by a few tens of percent. */
            CHECK(ratio <= 1.25, "no click: change-window step within the protection's attack overshoot");
            CHECK(lift > 3.0, "the new setting is audible in the output (level moved %+.1f dB)", lift);
        }
    return fails != 0;
}
'''


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="owner-settings-engine.") as tmp:
        work = Path(tmp)
        (work / "tinyalsa").mkdir()
        (work / "tinyalsa/mixer.h").write_text(MIXER_H)
        (work / "tinyalsa/pcm.h").write_text(PCM_HEADER)
        (work / "STUBS.c").write_text(STUBS)
        root = work / "root"
        root.mkdir()
        src = work / "owner.c"
        src.write_text(DRIVER.replace("ENGINE", str(AIRPLAY / "audio_engine.c"))
                       .replace("STUBS.c", str(work / "STUBS.c")))
        exe = work / "owner"
        subprocess.run(["cc", "-std=c99", "-O2", "-w", "-ffunction-sections", "-fdata-sections",
                        "-Wl,--gc-sections", "-I", str(work), "-I", str(AIRPLAY), str(src), "-lm",
                        "-o", str(exe)], check=True, timeout=180)
        result = subprocess.run([str(exe), str(root)], timeout=300)
        if result.returncode:
            raise SystemExit("owner_settings_engine: FAILED")
    print("owner_settings_engine: start apply, 0.5 s stat poll, no re-read when unchanged, "
          "live crossfade on both chains PASS")


if __name__ == "__main__":
    main()
