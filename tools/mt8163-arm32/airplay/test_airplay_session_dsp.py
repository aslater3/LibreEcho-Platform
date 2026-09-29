#!/usr/bin/env python3
"""Compiled host regressions for the AirPlay transport/session and logical gain."""
import os
from pathlib import Path
import subprocess
import tempfile

from test_audio_period_buffer import MIXER_HEADER, PCM_HEADER

HERE = Path(__file__).resolve().parent
ENGINE_TEST = r'''
#define main engine_main
#include "audio_engine.c"
#undef main
#include <assert.h>

/* Stub only codec controls; the compiled session/DSP path uses real functions. */
struct mixer { int unused; };
struct mixer_ctl { int unused; };
static struct mixer fixture_mixer;
static struct mixer_ctl fixture_control;
static int codec_index[2] = {-1, -1};
static int codec_writes;
static int codec_present = 1;
struct mixer *mixer_open(unsigned int card)
{
    assert(card == 0); return &fixture_mixer;
}
void mixer_close(struct mixer *mixer) { assert(mixer == &fixture_mixer); }
struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *mixer, const char *name)
{
    assert(mixer == &fixture_mixer);
    assert(strcmp(name, "PCM Playback Volume") == 0);
    return codec_present ? &fixture_control : NULL;
}
unsigned int mixer_ctl_get_num_values(struct mixer_ctl *ctl)
{
    assert(ctl == &fixture_control); return 2;
}
int mixer_ctl_set_value(struct mixer_ctl *ctl, unsigned int index, int value)
{
    assert(ctl == &fixture_control && index < 2);
    ++codec_writes;
    codec_index[index] = value;
    return 0;
}
int mixer_ctl_get_value(struct mixer_ctl *ctl, unsigned int index)
{
    assert(ctl == &fixture_control && index < 2);
    return codec_index[index];
}

static void put(const char *root, const char *name, const char *text)
{
    char path[256];
    FILE *f;
    assert(snprintf(path, sizeof(path), "%s/%s", root, name) < (int)sizeof(path));
    f = fopen(path, "w"); assert(f); assert(fputs(text, f) >= 0); assert(fclose(f) == 0);
}
static void remove_file(const char *root, const char *name)
{
    char path[256];
    assert(snprintf(path, sizeof(path), "%s/%s", root, name) < (int)sizeof(path));
    assert(unlink(path) == 0);
}
static void ack(const char *root)
{
    char path[256]; struct stat m, v; FILE *f;
    assert(snprintf(path, sizeof(path), "%s/airplay.active", root) < (int)sizeof(path));
    assert(stat(path, &m) == 0);
    assert(snprintf(path, sizeof(path), "%s/airplay.volume", root) < (int)sizeof(path));
    assert(stat(path, &v) == 0);
    assert(snprintf(path, sizeof(path), "%s/airplay.master", root) < (int)sizeof(path));
    f = fopen(path, "w"); assert(f);
    assert(fprintf(f, "%llu %llu %lld %ld %llu %llu %lld %ld\n",
        (unsigned long long)m.st_dev, (unsigned long long)m.st_ino,
        (long long)m.st_ctim.tv_sec, m.st_ctim.tv_nsec,
        (unsigned long long)v.st_dev, (unsigned long long)v.st_ino,
        (long long)v.st_ctim.tv_sec, v.st_ctim.tv_nsec) > 0);
    assert(fclose(f) == 0);
}
static void callback(const char *root, const char *text)
{
    remove_file(root, "airplay.volume"); put(root, "airplay.volume", text); ack(root);
}
int main(void)
{
    char root[256];
    struct source_bus sources[SOURCE_COUNT] = {0};
    int pipes[SOURCE_COUNT][2];
    int16_t samples[PERIOD_SIZE * INPUT_CHANNELS];
    int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    struct puffin_dynamics dyn; struct speaker_dsp dsp;
    size_t i; int reference, attenuated; int32_t smoothed = 32768;
    assert(getenv("TMPDIR"));
    assert(snprintf(root, sizeof(root), "%s/le-session-XXXXXX", getenv("TMPDIR")) < (int)sizeof(root));
    assert(mkdtemp(root));
    for (i = 0; i < SOURCE_COUNT; ++i) {
        assert(pipe(pipes[i]) == 0);
        assert(fcntl(pipes[i][0], F_SETFL, O_NONBLOCK) == 0);
        sources[i].fd = pipes[i][0];
        sources[i].capacity = sizeof(samples) * LE_AUDIO_PERIOD_BUFFER_PERIODS;
        sources[i].samples = calloc(1, sources[i].capacity); assert(sources[i].samples);
        sources[i].gain_q15 = 32768;
    }
    for (i = 0; i < sizeof(samples)/sizeof(samples[0]); ++i) samples[i] = 4000;
    /* AirPlay FIFO bytes must never appear as generic media without a marker. */
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    assert(!source_period_ready(&sources[SOURCE_AIRPLAY]));
    /* Start and first PCM arrive in one poll interval after a reset ack. */
    sources[SOURCE_AIRPLAY].airplay_reset_ready = 1;
    sources[SOURCE_AIRPLAY].airplay_reset_pending = 1;
    put(root, "airplay.active", "");
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    assert(source_period_ready(&sources[SOURCE_AIRPLAY]));
    assert(sources[SOURCE_AIRPLAY].airplay_volume_missing);
    /* A missing AirPlay callback cannot defer independent generic media. */
    assert(write(pipes[SOURCE_MEDIA][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    {
        unsigned int mask = 0; int32_t master = 32768;
        assert(prepare_initial_period(sources, root, output, &dyn, &dsp,
                                      &mask, 100, &master) == 1);
        assert(mask & PLAYBACK_BUS_MEDIA);
        consume_period(sources);
        assert(source_period_ready(&sources[SOURCE_AIRPLAY]));
    }
    /* Alarm remains live while first sender callback is unacknowledged. */
    assert(write(pipes[SOURCE_ALARM][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    {
        unsigned int mask = 0; int32_t master = 32768;
        assert(prepare_initial_period(sources, root, output, &dyn, &dsp,
                                      &mask, 100, &master) == 1);
        assert((mask & PLAYBACK_BUS_ALARM) && output[100 * OUTPUT_CHANNELS] != 0);
        consume_period(sources);
        assert(source_period_ready(&sources[SOURCE_AIRPLAY]));
    }
    put(root, "airplay.volume", "-20\n");
    assert(read_sources(sources, root) >= 0 && sources[SOURCE_AIRPLAY].airplay_volume_missing);
    put(root, "airplay.master", "invalid\n");
    assert(airplay_volume_to_mixer(root) < 0);
    remove_file(root, "airplay.master");
    {
        char path[256];
        assert(snprintf(path, sizeof(path), "%s/airplay.master", root) < (int)sizeof(path));
        assert(mkfifo(path, 0600) == 0 && airplay_volume_to_mixer(root) < 0);
        remove_file(root, "airplay.master");
        put(root, "airplay.master", "99999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999");
        assert(airplay_volume_to_mixer(root) < 0);
        remove_file(root, "airplay.master");
    }
    ack(root);
    assert(read_sources(sources, root) >= 0);
    assert(!sources[SOURCE_AIRPLAY].airplay_volume_missing);
    consume_period(sources);
    /* Callback replacement must not repeatedly defer already admitted PCM. */
    callback(root, "-12\n");
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    assert(!sources[SOURCE_AIRPLAY].airplay_volume_missing);
    consume_period(sources);
    assert(!source_period_ready(&sources[SOURCE_AIRPLAY]));
    remove_file(root, "airplay.master");
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    assert(!sources[SOURCE_AIRPLAY].airplay_volume_missing &&
           sources[SOURCE_AIRPLAY].gain_q15 == 32768);
    consume_period(sources);
    for (i = 0; i < 30; ++i)
        assert(read_sources(sources, root) >= 0);
    assert(!sources[SOURCE_AIRPLAY].airplay_volume_missing &&
           sources[SOURCE_AIRPLAY].gain_q15 == 0);
    callback(root, "-144\n");
    assert(read_sources(sources, root) >= 0);
    assert(sources[SOURCE_AIRPLAY].gain_q15 == 0);
    /* Marker loss discards both queued periods and live FIFO bytes. */
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    remove_file(root, "airplay.active");
    assert(read_sources(sources, root) >= 0);
    assert(!source_period_ready(&sources[SOURCE_AIRPLAY]));
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    assert(!source_period_ready(&sources[SOURCE_AIRPLAY]));
    /* Same surviving FIFO, second playback after another completed reset. */
    remove_file(root, "airplay.volume"); remove_file(root, "airplay.master");
    sources[SOURCE_AIRPLAY].airplay_reset_pending = 1;
    put(root, "airplay.active", "");
    assert(read_sources(sources, root) >= 0);
    assert(!source_period_ready(&sources[SOURCE_AIRPLAY]));
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    assert(sources[SOURCE_AIRPLAY].airplay_volume_missing);
    put(root, "airplay.volume", "-20\n"); ack(root);
    assert(read_sources(sources, root) >= 0);
    assert(!sources[SOURCE_AIRPLAY].airplay_volume_missing);
    /* A replacement marker without an observed idle interval is ambiguous:
     * old PCM already in the FIFO must not be admitted to the successor. */
    remove_file(root, "airplay.active");
    put(root, "airplay.active", "");
    assert(write(pipes[SOURCE_AIRPLAY][1], samples, sizeof(samples)) == sizeof(samples));
    assert(read_sources(sources, root) >= 0);
    assert(!source_period_ready(&sources[SOURCE_AIRPLAY]));
    consume_period(sources);
    /* Logical master, not fixed physical codec, drives pre-DSP amplitude. */
    assert(speaker_volume_percent_for_mix(100, 32768, 1, 0) == 100);
    assert(speaker_volume_percent_for_mix(0, 32768, 1, 0) == 0);
    assert(speaker_volume_percent_for_mix(100, 3277, 1, 0) < 100);
    assert(speaker_volume_percent_for_mix(60, 3277, 1, 1) ==
           speaker_volume_percent_for_mix(60, 32768, 1, 1));
    put(root, "master.volume", "42\n");
    assert(logical_master_volume(root) == 42 && logical_master_gain(42) > 0 &&
           logical_master_gain(42) < 32768);
    remove_file(root, "master.volume"); put(root, "master.volume", "101\n");
    assert(logical_master_volume(root) == 0);
    remove_file(root, "master.volume"); put(root, "master.volume", "invalid\n");
    assert(logical_master_volume(root) == 0);
    codec_present = 0;
    assert(verify_codec_reference(0) < 0 && codec_writes == 0);
    codec_present = 1;
    assert(verify_codec_reference(0) < 0 && codec_writes == 0);
    codec_index[0] = 127; codec_index[1] = 126;
    assert(verify_codec_reference(0) < 0 && codec_writes == 0);
    codec_index[1] = 127;
    assert(verify_codec_reference(0) == 0 && codec_writes == 0);
    /* Muted AirPlay must not override an independently audible media EQ. */
    sources[SOURCE_MEDIA].received = sizeof(samples);
    sources[SOURCE_MEDIA].gain_q15 = 32768;
    sources[SOURCE_AIRPLAY].received = sizeof(samples);
    sources[SOURCE_AIRPLAY].airplay_volume_missing = 0;
    sources[SOURCE_AIRPLAY].gain_q15 = 0;
    assert(speaker_volume_percent(sources, 51) ==
           speaker_volume_percent_for_mix(51, 32768, 1, 0));
    /* Taper: -60 * (1 - x)^1.3 dB.  Wider dB steps at the bottom of the
     * slider, narrower at the top, so the upper half no longer dominates the
     * perceived range.  Mute, -60 dB floor and 0 dB are unchanged. */
    assert(logical_master_raw(1) == 7);           /* -60 dB */
    assert(logical_master_raw(2) == 9);           /* -59 dB */
    assert(logical_master_raw(26) == 45);         /* -41 dB at a quarter */
    assert(logical_master_raw(51) == 79);         /* -24 dB at half */
    assert(logical_master_raw(76) == 108);        /* -9.5 dB at three quarters */
    assert(logical_master_raw(100) == 127);       /*   0 dB */
    for (i = 2; i <= 100; ++i)
        assert(logical_master_raw(i) >= logical_master_raw(i - 1));
    /* Lower half spans at least 1.4x the dB of the upper half, and the top
     * iPhone notch (logical 94 -> 100) still moves at least 1 dB. */
    assert(10 * (logical_master_raw(51) - logical_master_raw(1)) >=
           14 * (logical_master_raw(100) - logical_master_raw(51)));
    assert(logical_master_raw(100) - logical_master_raw(94) >= 2);
    /* Logical 51: raw 79 (-24 dB), effective percent 62, preset 70. */
    {
        float gains[SPEAKER_DSP_LOUDNESS_SECTIONS];
        assert(speaker_volume_percent_for_mix(51, 32768, 1, 0) == 62);
        speaker_dsp_loudness_gains(62.0f, gains);
        assert(gains[0] == speaker_loudness_gain_db[2][0]);
    }
    /* The per-source gain is multiplied once before the shared master. */
    sources[SOURCE_AIRPLAY].received = 0;
    sources[SOURCE_MEDIA].received = sizeof(samples);
    sources[SOURCE_MEDIA].gain_q15 = 16384;
    sources[SOURCE_MEDIA].samples[200] = 4000;
    sources[SOURCE_MEDIA].samples[201] = 4000;
    assert(mix_sources_frame(sources, 100) == 2000);
    sources[SOURCE_ALARM].received = sizeof(samples);
    sources[SOURCE_ALARM].samples[200] = 1000;
    sources[SOURCE_ALARM].samples[201] = 1000;
    assert(mix_sources_frame(sources, 100) == 1000);
    sources[SOURCE_ALARM].received = 0;
    sources[SOURCE_MEDIA].received = 0;
    for (i = 0; i < PERIOD_SIZE * INPUT_CHANNELS; ++i)
        sources[SOURCE_SYSTEM].samples[i] = 4000;
    sources[SOURCE_SYSTEM].received = sizeof(samples);
    sources[SOURCE_AIRPLAY].received = 0;
    puffin_dynamics_init(&dyn); speaker_dsp_init(&dsp, 100);
    render_period(sources, output, &dyn, &dsp, 32768, &smoothed);
    reference = output[100 * OUTPUT_CHANNELS];
    puffin_dynamics_init(&dyn); speaker_dsp_init(&dsp, 100);
    smoothed = 32768;
    render_period(sources, output, &dyn, &dsp, 0, &smoothed);
    attenuated = output[(PERIOD_SIZE - 1) * OUTPUT_CHANNELS];
    assert(reference != 0 && smoothed == 0 && abs(attenuated) < abs(reference));
    remove_file(root, "airplay.active"); remove_file(root, "airplay.volume");
    remove_file(root, "airplay.master"); remove_file(root, "master.volume");
    for (i = 0; i < SOURCE_COUNT; ++i) {
        close(pipes[i][0]); close(pipes[i][1]); free(sources[i].samples);
    }
    assert(rmdir(root) == 0);
    puts("AirPlay session FIFO and logical pre-DSP gain: PASS");
    return 0;
}
'''

def main():
    # Source-order gate complements the compiled read-only control fixture:
    # audiod could update the codec after the initial prepare check.
    engine = (HERE / "audio_engine.c").read_text()
    assert "if (!playback_start_failed && power_output_controls(card) < 0)" in engine
    assert "write_period(pcm, output, &reference, first_activity) < 0 ||\n\t\t\t\t    verify_codec_reference(card) < 0 ||" in engine
    with tempfile.TemporaryDirectory(prefix="le-airplay-session-") as tmp:
        root = Path(tmp)
        (root / "tinyalsa").mkdir()
        (root / "tinyalsa/mixer.h").write_text(MIXER_HEADER)
        (root / "tinyalsa/pcm.h").write_text(PCM_HEADER)
        src = root / "test.c"
        src.write_text(ENGINE_TEST)
        binary = root / "test"
        fixture_env = {**os.environ, "TMPDIR": str(root)}
        subprocess.run([os.getenv("CC", "cc"), "-std=c99", "-Wall", "-Wextra", "-Werror", "-ffunction-sections", "-fdata-sections", "-I", str(root), "-I", str(HERE), str(src), "-Wl,--gc-sections", "-lm", "-o", str(binary)], check=True, timeout=60, env=fixture_env)
        subprocess.run([str(binary)], check=True, timeout=30, env=fixture_env)

        # Hook/transport subprocess coverage lives in test_airplay_generation_fence.py.

if __name__ == "__main__":
    main()
