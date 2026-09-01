#!/usr/bin/env python3
"""Regression test for accumulating short PCM FIFO reads into periods."""

import os
from pathlib import Path
import subprocess
import tempfile


SOURCE_DIR = Path(__file__).resolve().parent
ENGINE_SOURCE = SOURCE_DIR / "audio_engine.c"


BUFFER_PROGRAM = r'''
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "audio_period_buffer.h"

static int all_bytes(const unsigned char *buffer, size_t length,
                     unsigned char expected)
{
    size_t i;

    for (i = 0; i < length; ++i)
        if (buffer[i] != expected)
            return 0;
    return 1;
}

int main(void)
{
    unsigned char queue[32];
    unsigned char first_half[8];
    unsigned char second_half[8];
    unsigned char third_half[8];
    unsigned char fourth_half[8];
    size_t used = 0;
    const size_t period_bytes = 16;

    memset(first_half, 0x11, sizeof(first_half));
    memset(second_half, 0x22, sizeof(second_half));
    memset(third_half, 0x33, sizeof(third_half));
    memset(fourth_half, 0x44, sizeof(fourth_half));

    if (le_audio_period_buffer_append(queue, &used, sizeof(queue),
                                      first_half, sizeof(first_half)) !=
        sizeof(first_half) ||
        le_audio_period_buffer_ready(used, period_bytes))
        return 1;
    if (le_audio_period_buffer_append(queue, &used, sizeof(queue),
                                      second_half, sizeof(second_half)) !=
        sizeof(second_half) ||
        !le_audio_period_buffer_ready(used, period_bytes) ||
        !all_bytes(queue, 8, 0x11) || !all_bytes(queue + 8, 8, 0x22))
        return 2;

    if (le_audio_period_buffer_append(queue, &used, sizeof(queue),
                                      third_half, sizeof(third_half)) !=
        sizeof(third_half))
        return 3;
    le_audio_period_buffer_consume(queue, &used, period_bytes);
    if (used != 8 || !all_bytes(queue, used, 0x33))
        return 4;

    if (le_audio_period_buffer_append(queue, &used, sizeof(queue),
                                      fourth_half, sizeof(fourth_half)) !=
        sizeof(fourth_half) ||
        !le_audio_period_buffer_ready(used, period_bytes) ||
        !all_bytes(queue, 8, 0x33) || !all_bytes(queue + 8, 8, 0x44))
        return 5;
    le_audio_period_buffer_consume(queue, &used, period_bytes);
    return used == 0 ? 0 : 6;
}
'''


ENGINE_PROGRAM = r'''
#define _GNU_SOURCE
#define main libreecho_audio_engine_main
#include "audio_engine.c"
#undef main

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static int fail(const char *message)
{
    fprintf(stderr, "%s\n", message);
    return 1;
}

int main(void)
{
    struct source_bus sources[SOURCE_COUNT];
    struct puffin_dynamics dynamics;
    int pipes[SOURCE_COUNT][2];
    int16_t half[PERIOD_SIZE * INPUT_CHANNELS / 2];
    int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    unsigned int activity_mask = 0;
    size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);
    size_t i;

    memset(sources, 0, sizeof(sources));
    for (i = 0; i < SOURCE_COUNT; ++i) {
        if (pipe(pipes[i]) < 0)
            return fail("pipe failed");
        if (fcntl(pipes[i][0], F_SETFL, O_NONBLOCK) < 0)
            return fail("fcntl failed");
        sources[i].fd = pipes[i][0];
        sources[i].capacity = period_bytes * LE_AUDIO_PERIOD_BUFFER_PERIODS;
        sources[i].samples = calloc(1, sources[i].capacity);
        sources[i].gain_q15 = 32768;
        if (!sources[i].samples)
            return fail("allocation failed");
    }
    for (i = 0; i < sizeof(half) / sizeof(half[0]); ++i)
        half[i] = 1000;

    if (write(pipes[0][1], half, sizeof(half)) != (ssize_t)sizeof(half))
        return fail("first short write failed");
    if (read_sources(sources, "/tmp") < 0 ||
        sources[0].received != sizeof(half))
        return fail("first short read was not retained");
    if (period_ready(sources))
        return fail("half period was reported ready");

    for (i = 0; i < sizeof(half) / sizeof(half[0]); ++i)
        half[i] = 2000;
    if (write(pipes[0][1], half, sizeof(half)) != (ssize_t)sizeof(half))
        return fail("second short write failed");
    for (i = 0; i < sizeof(half) / sizeof(half[0]); ++i)
        half[i] = 3000;
    if (write(pipes[1][1], half, sizeof(half)) != (ssize_t)sizeof(half))
        return fail("concurrent partial write failed");
    if (read_sources(sources, "/tmp") < 0 ||
        sources[0].received != period_bytes ||
        sources[1].received != sizeof(half) || !period_ready(sources))
        return fail("ready source was blocked by partial source");

    puffin_dynamics_init(&dynamics);
    render_period(sources, output, &dynamics);
    for (i = 0; i < PERIOD_SIZE; ++i) {
        if (output[i * OUTPUT_CHANNELS] == 0 ||
            output[i * OUTPUT_CHANNELS] != output[i * OUTPUT_CHANNELS + 1])
            return fail("ready source did not render while another was partial");
    }
    consume_period(sources);

    for (i = 0; i < SOURCE_COUNT; ++i) {
        sources[i].idle_periods = 0;
        sources[i].received = 0;
    }
    if (write(pipes[0][1], half, sizeof(half)) != (ssize_t)sizeof(half) ||
        write(pipes[0][1], half, sizeof(half)) != (ssize_t)sizeof(half))
        return fail("media prebuffer write failed");
    if (read_sources(sources, "/tmp") < 0 ||
        sources[0].received != period_bytes)
        return fail("media period was not prebuffered");
    if (write(pipes[3][1], half, sizeof(half)) != (ssize_t)sizeof(half) ||
        write(pipes[3][1], half, sizeof(half)) != (ssize_t)sizeof(half))
        return fail("priority arrival write failed");
    if (wait_for_period(sources, "/tmp") != 1 ||
        sources[3].received != period_bytes)
        return fail("newly arrived priority period was not drained");
    consume_period(sources);

    for (i = 0; i < SOURCE_COUNT; ++i) {
        sources[i].idle_periods = 0;
        sources[i].received = 0;
    }
    for (i = 0; i < LE_AUDIO_PERIOD_BUFFER_PERIODS * 2; ++i) {
        if (write(pipes[0][1], half, sizeof(half)) != (ssize_t)sizeof(half))
            return fail("full-buffer write failed");
    }
    if (read_sources(sources, "/tmp") < 0 ||
        sources[0].received != period_bytes * LE_AUDIO_PERIOD_BUFFER_PERIODS)
        return fail("full media buffer was not retained");
    if (read_or_retain_sources(sources, "/tmp") != 1 ||
        sources[0].received != period_bytes * LE_AUDIO_PERIOD_BUFFER_PERIODS)
        return fail("retained media was not actionable without a new read");
    sources[0].idle_periods = 0;
    consume_period(sources);
    if (!sources_active(sources) || !period_ready(sources))
        return fail("retained second period was not kept active");
    consume_period(sources);
    if (sources_active(sources) || period_ready(sources))
        return fail("retained periods did not drain cleanly");

    for (i = 0; i < SOURCE_COUNT; ++i) {
        sources[i].idle_periods = 0;
        sources[i].received = 0;
    }
    if (write(pipes[2][1], half, sizeof(half)) != (ssize_t)sizeof(half) ||
        write(pipes[2][1], half, sizeof(half)) != (ssize_t)sizeof(half))
        return fail("one-period startup write failed");
    if (read_sources(sources, "/tmp") < 0 ||
        prepare_initial_period(sources, "/tmp", output, &dynamics,
                               &activity_mask) != 1 ||
        activity_mask == 0)
        return fail("one complete period was not accepted at startup");
    for (i = 0; i < PERIOD_SIZE; ++i) {
        if (output[i * OUTPUT_CHANNELS] == 0 ||
            output[i * OUTPUT_CHANNELS] != output[i * OUTPUT_CHANNELS + 1])
            return fail("one-period startup render contains a gap");
    }

    consume_period(sources);

    /* A final short announcement is padded after its grace window.  It must
       still count as announcing until the padded period has been consumed. */
    for (i = 0; i < SOURCE_COUNT; ++i) {
        sources[i].idle_periods = 0;
        sources[i].received = 0;
    }
    if (write(pipes[SOURCE_ANNOUNCEMENT][1], half, sizeof(half)) !=
        (ssize_t)sizeof(half))
        return fail("short announcement write failed");
    if (read_sources(sources, "/tmp") < 0)
        return fail("short announcement read failed");
    for (i = 0; i < SOURCE_IDLE_PERIODS; ++i)
        if (read_sources(sources, "/tmp") < 0)
            return fail("short announcement inactivity read failed");
    if (!source_period_ready(&sources[SOURCE_ANNOUNCEMENT]) ||
        !(source_activity_mask(sources) & PLAYBACK_BUS_ANNOUNCEMENT))
        return fail("padded announcement tail did not remain active");
    consume_period(sources);
    if (source_activity_mask(sources) & PLAYBACK_BUS_ANNOUNCEMENT)
        return fail("consumed announcement tail remained active");

    for (i = 0; i < SOURCE_COUNT; ++i) {
        close(pipes[i][0]);
        close(pipes[i][1]);
        free(sources[i].samples);
    }
    puts("audio_engine FIFO integration: short reads form continuous stereo period PASS");
    return 0;
}
'''


MIXER_HEADER = """#ifndef TINYALSA_MIXER_H
#define TINYALSA_MIXER_H
struct mixer;
struct mixer_ctl;
struct mixer *mixer_open(unsigned int card);
void mixer_close(struct mixer *mixer);
struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *mixer, const char *name);
int mixer_ctl_set_enum_by_string(struct mixer_ctl *ctl, const char *value);
unsigned int mixer_ctl_get_num_values(struct mixer_ctl *ctl);
int mixer_ctl_set_value(struct mixer_ctl *ctl, unsigned int index, int value);
int mixer_ctl_get_value(struct mixer_ctl *ctl, unsigned int index);
#endif
"""

PCM_HEADER = """#ifndef TINYALSA_PCM_H
#define TINYALSA_PCM_H
#define PCM_OUT 0x00000000U
enum pcm_format { PCM_FORMAT_S16_LE = 0 };
struct pcm_config {
    unsigned int channels;
    unsigned int rate;
    unsigned int period_size;
    unsigned int period_count;
    enum pcm_format format;
    unsigned int start_threshold;
    unsigned int stop_threshold;
    unsigned int silence_threshold;
    unsigned int silence_size;
    unsigned int avail_min;
};
struct pcm;
struct pcm *pcm_open(unsigned int card, unsigned int device, unsigned int flags,
                     const struct pcm_config *config);
int pcm_is_ready(struct pcm *pcm);
const char *pcm_get_error(struct pcm *pcm);
void pcm_close(struct pcm *pcm);
int pcm_prepare(struct pcm *pcm);
int pcm_writei(struct pcm *pcm, const void *data, unsigned int frame_count);
#endif
"""


def compile_and_run(compiler: str, source: Path, binary: Path, *includes: Path) -> None:
    command = [
        compiler,
        "-std=c99",
        "-Wall",
        "-Wextra",
        "-Wpedantic",
        "-Werror",
    ]
    for include in includes:
        command.extend(("-I", str(include)))
    command.extend((str(source), "-o", str(binary)))
    subprocess.run(command, check=True, timeout=60)
    subprocess.run([str(binary)], check=True, timeout=60)


def main() -> None:
    compiler = os.environ.get("CC", "cc")
    with tempfile.TemporaryDirectory(prefix="libreecho-period-test-") as directory:
        directory_path = Path(directory)
        buffer_source = directory_path / "test_period_buffer.c"
        buffer_binary = directory_path / "test_period_buffer"
        buffer_source.write_text(BUFFER_PROGRAM, encoding="ascii")
        compile_and_run(compiler, buffer_source, buffer_binary, SOURCE_DIR)

        stub_root = directory_path / "tinyalsa"
        stub_root.mkdir()
        (stub_root / "mixer.h").write_text(MIXER_HEADER, encoding="ascii")
        (stub_root / "pcm.h").write_text(PCM_HEADER, encoding="ascii")
        engine_source = directory_path / "test_audio_engine_periods.c"
        engine_binary = directory_path / "test_audio_engine_periods"
        engine_source.write_text(ENGINE_PROGRAM, encoding="ascii")
        command = [
            compiler,
            "-std=c99",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            "-ffunction-sections",
            "-fdata-sections",
            "-I",
            str(directory_path),
            "-I",
            str(SOURCE_DIR),
            str(engine_source),
            "-Wl,--gc-sections",
            "-lm",
            "-o",
            str(engine_binary),
        ]
        subprocess.run(command, check=True, timeout=60)
        subprocess.run([str(engine_binary)], check=True, timeout=60)

    engine = ENGINE_SOURCE.read_text(encoding="utf-8")
    required = (
        '#include "audio_period_buffer.h"',
        "le_audio_period_buffer_append",
        "le_audio_period_buffer_consume",
        "wait_for_period",
        "read_or_retain_sources",
    )
    missing = [fragment for fragment in required if fragment not in engine]
    if missing:
        raise SystemExit(
            "audio engine does not use the period buffer: " + ", ".join(missing)
        )
    read_sources_start = engine.index("static int read_sources")
    read_sources_end = engine.index("static int sources_active", read_sources_start)
    read_sources = engine[read_sources_start:read_sources_end]
    if "sources[i].received = 0;\n\t\twhile" in read_sources:
        raise SystemExit("audio engine must retain queued source bytes across reads")
    if "memset(cursor, 0, bytes);" in read_sources:
        raise SystemExit("audio engine must not zero-fill a short source period")
    run_start = engine.index("static int run_engine")
    run_engine = engine[run_start:]
    if "int poll_timeout = period_ready(sources) ? 20 : -1;" not in run_engine:
        raise SystemExit("retained periods need a timed state recheck")
    if "if (read_or_retain_sources(sources, root) <= 0)" not in run_engine:
        raise SystemExit("retained periods must advance without a new FIFO read")
    print("audio_period_buffer: short-read accumulation and engine continuity PASS")


if __name__ == "__main__":
    main()
