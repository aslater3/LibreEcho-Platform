#!/usr/bin/env python3
"""Task 4b/4b-fence: real-shared-engine Sendspin integration behaviour, fake PCM.

This test compiles the **production** ``audio_engine.c`` (via the same
``#define main`` include trick the sibling fixtures use) together with the real
``audio_sink.c``, ``audio_timing.c`` and ``aec_reference.c``, and drives the
engine's own Sendspin integration helpers (``sendspin_service``,
``sendspin_sync_identity``, ``sendspin_live``, ``sendspin_stage``,
``sendspin_write_and_account``, ``sendspin_publish_progress``,
``sendspin_close_timing``) plus ``mix_sources_frame`` against a *fake* TinyALSA
PCM.  Nothing here is a replacement engine or a source-string assertion: the
objects under test are the real compiled functions, and the only fake is the
kernel TinyALSA boundary the host does not have.

A private scratch root holds the real SOCK_SEQPACKET sink; a client connects to
it in-process and its OPEN/DATA are serviced by the engine's real
``sendspin_service``.  No host hardware, ``/run`` path or device node is ever
touched.

Every gap named in the Task 4b acceptance list has an executable scenario:

  priming   - a bound sink with a live generation paces periods with no FIFO
  split     - 100 source frames + engine padding are recorded source/generated
  short     - a scripted positive-short write replays no frame, drops none
  xrun      - EPIPE/ESTRPIPE with PCM_NORESTART fence + reconcile, never credit
  finish    - a FINISHed tail completes on the physical cursor, then goes quiet
  fence     - a superseded generation's late feedback is rejected untouched
  invalid   - an untrustworthy/uncalibrated sample publishes INVALID, never a
              fabricated finish (a calibrated estimate is injected explicitly)
  priority  - a higher-priority bus ducks/mutes Sendspin but still consumes it
  cleanup   - timing/sink teardown is idempotent and order-safe

Task 4b-fence closes three loop-level gaps with real RED/GREEN regressions:

  1. generation fencing on write error - an error after a partial acceptance
     must CANCEL the affected sink generation (so the same still-active
     generation is never re-adopted) and must never leave a live, unaccounted
     accepted write behind.  Covers EPIPE, ESTRPIPE, bounded zero-write, fatal
     EIO, an over-accepting backend and a postwrite ledger-accounting failure.
  2. partial-prefix accounting - the exact source/padding split of the accepted
     hardware prefix is recorded as the writes succeed, the accepted source
     prefix is consumed from the queue (no retransmit-from-cursor) and a later
     error fences/drops the session without pretending the accepted samples
     never occurred.
  3. stale timing publication - a failed/stale progress query clears a previous
     VALID horizon, and BOTH epoch and generation must match before a source
     cursor or a source finish is published, so a predecessor tail draining
     after a successor OPEN can never be reported as the successor's valid
     horizon.  The exact source tail survives the completed-event drain.
"""

import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

SOURCE_DIR = Path(__file__).resolve().parent
FIXTURE_ROOT = Path(
    os.environ.get(
        "LE_AUDIO_ENGINE_SENDPIN_FIXTURE_ROOT",
        str(Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/libreecho-tests"))) / "sendspin-engine-behavior"),
    )
)


PCM_HEADER = """#ifndef TINYALSA_PCM_H
#define TINYALSA_PCM_H
#include <time.h>
#define PCM_OUT 0x00000000U
#define PCM_MONOTONIC 0x00000008U
#define PCM_NORESTART 0x00000004U
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
unsigned int pcm_get_buffer_size(const struct pcm *pcm);
int pcm_get_htimestamp(struct pcm *pcm, unsigned int *avail,
                       struct timespec *timestamp);
#endif
"""

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


ENGINE_PROGRAM = r'''#define _GNU_SOURCE
#define main libreecho_audio_engine_main
#include "audio_engine.c"
#undef main

#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Fake TinyALSA PCM: the only boundary the host does not provide.      */
/* ------------------------------------------------------------------ */

struct pcm {
    unsigned int buffer_frames;
};

static unsigned int fake_avail;
static int fake_ts_valid = 1;
/*
 * Write scripts:
 *   0 full accept, 1 positive-short chain (partial, 0, rest),
 *   2 EPIPE, 3 EIO, 4 forever-zero, 5 prefix then configured fail/zero,
 *   6 backend claims to accept more frames than requested.
 * 7 ESTRPIPE.
 */
static int fake_write_mode = 0;
static int fake_write_errno;         /* mode 5: 0 -> stall forever */
static int fake_write_calls;
static unsigned int fake_partial;
static int16_t fake_written[1 << 16];
static size_t fake_written_frames;

struct pcm *pcm_open(unsigned int card, unsigned int device, unsigned int flags,
                     const struct pcm_config *config)
{
    struct pcm *pcm;

    (void)card;
    (void)device;
    (void)flags;
    pcm = (struct pcm *)calloc(1, sizeof(*pcm));
    if (!pcm)
        return NULL;
    pcm->buffer_frames = config->period_size * config->period_count;
    return pcm;
}

int pcm_is_ready(struct pcm *pcm)
{
    return pcm != NULL;
}

const char *pcm_get_error(struct pcm *pcm)
{
    (void)pcm;
    return "fake-pcm";
}

void pcm_close(struct pcm *pcm)
{
    free(pcm);
}

int pcm_prepare(struct pcm *pcm)
{
    (void)pcm;
    return 0;
}

unsigned int pcm_get_buffer_size(const struct pcm *pcm)
{
    return pcm ? pcm->buffer_frames : 0u;
}

int pcm_get_htimestamp(struct pcm *pcm, unsigned int *avail,
                       struct timespec *timestamp)
{
    (void)pcm;
    if (!avail || !timestamp)
        return -1;
    *avail = fake_avail;
    if (fake_ts_valid) {
        if (clock_gettime(CLOCK_MONOTONIC, timestamp) != 0)
            return -1;
    } else {
        timestamp->tv_sec = 0;
        timestamp->tv_nsec = 0;
    }
    return 0;
}

int pcm_writei(struct pcm *pcm, const void *data, unsigned int frame_count)
{
    const int16_t *samples = (const int16_t *)data;
    unsigned int accepted;

    (void)pcm;
    fake_write_calls += 1;
    switch (fake_write_mode) {
    case 2:
        errno = EPIPE;
        return -1;
    case 3:
        errno = EIO;
        return -1;
    case 7:
        errno = ESTRPIPE;
        return -1;
    case 4:
        return 0;                 /* never advances */
    case 6:
        return (int)frame_count + 5;   /* claims more than requested */
    case 5:
        if (fake_write_calls == 1) {
            accepted = fake_partial < frame_count ? fake_partial : frame_count;
            break;
        }
        if (fake_write_errno != 0) {
            errno = fake_write_errno;
            return -1;
        }
        return 0;                 /* stall forever */
    case 1:
        if (fake_write_calls == 1) {
            accepted = fake_partial < frame_count ? fake_partial : frame_count;
            break;
        }
        if (fake_write_calls == 2)
            return 0;             /* one zero-progress retry */
        accepted = frame_count;
        break;
    default:
        accepted = frame_count;
        break;
    }
    memcpy(fake_written + fake_written_frames * OUTPUT_CHANNELS, samples,
           (size_t)accepted * OUTPUT_CHANNELS * sizeof(int16_t));
    fake_written_frames += accepted;
    return (int)accepted;
}

/* ------------------------------------------------------------------ */
/* Assertions                                                          */
/* ------------------------------------------------------------------ */

static int failures;

static void check(int condition, const char *label)
{
    if (!condition) {
        fprintf(stderr, "engine-sendspin FAIL: %s\n", label);
        failures += 1;
    }
}

static void reset_fake_pcm(void)
{
    fake_avail = 0u;
    fake_ts_valid = 1;
    fake_write_mode = 0;
    fake_write_errno = 0;
    fake_write_calls = 0;
    fake_partial = 0u;
    fake_written_frames = 0u;
}

/* The engine services the socket once per poll cycle: one call admits a new
 * peer, the next reads its datagrams.  Drive a few bounded cycles so a single
 * client OPEN is fully processed without busy-waiting. */
static void service_cycles(struct sendspin_state *sp)
{
    int i;

    for (i = 0; i < 4; ++i)
        sendspin_service(sp);
}

/* Minimal real-socket client for the engine's own SOCK_SEQPACKET listener. */
static void put_header(uint8_t *out, uint8_t type, uint32_t length)
{
    le_audio_sink_encode_header(out, type, length);
}

/* 20-byte OPEN payload. */
static void open_payload(uint8_t *out, uint32_t generation)
{
    le_audio_sink_put_u32(out + 0, LE_AUDIO_SINK_SOURCE_SENDPIN);
    le_audio_sink_put_u32(out + 4, generation);
    le_audio_sink_put_u32(out + 8, LE_AUDIO_SINK_OUTPUT_RATE);
    le_audio_sink_put_u16(out + 12, LE_AUDIO_SINK_OUTPUT_CHANNELS);
    le_audio_sink_put_u16(out + 14, LE_AUDIO_SINK_FORMAT_S16_LE);
    le_audio_sink_put_u32(out + 16, 0u);
}

/* Open a fresh, strictly higher generation over the real socket and adopt it. */
static void open_generation(struct sendspin_state *sp, const char *path,
                            uint32_t generation)
{
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    struct sockaddr_un address;
    uint8_t datagram[LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_OPEN_PAYLOAD_BYTES];
    ssize_t sent;

    check(fd >= 0, "open_generation socket failed");
    check(strlen(path) < sizeof(address.sun_path),
          "open_generation socket path exceeds sun_path");
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strncpy(address.sun_path, path, sizeof(address.sun_path) - 1u);
    check(connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0,
          "open_generation connect failed");
    put_header(datagram, LE_AUDIO_SINK_TYPE_OPEN,
               LE_AUDIO_SINK_OPEN_PAYLOAD_BYTES);
    open_payload(datagram + LE_AUDIO_SINK_HEADER_BYTES, generation);
    sent = send(fd, datagram, sizeof(datagram), 0);
    check(sent == (ssize_t)sizeof(datagram), "open_generation send failed");
    service_cycles(sp);
    sendspin_sync_identity(sp);
    close(fd);
}

/* Queue `frames` constant source frames on the live generation. */
static int queue_source(struct le_audio_sink *sink, uint32_t epoch,
                        uint32_t generation, uint64_t first_frame,
                        uint32_t sequence, size_t frames, int16_t value)
{
    static int16_t buffer[PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    size_t i;

    if (frames > PERIOD_SIZE)
        frames = PERIOD_SIZE;
    for (i = 0; i < frames; ++i) {
        buffer[i * LE_AUDIO_SINK_OUTPUT_CHANNELS] = value;
        buffer[i * LE_AUDIO_SINK_OUTPUT_CHANNELS + 1] = value;
    }
    return le_audio_sink_submit(sink, (const uint8_t *)buffer,
                                (uint32_t)frames, first_frame, epoch,
                                generation, sequence);
}

static struct le_audio_timing *make_calibrated_timing(struct pcm *device)
{
    struct le_audio_timing_config config;

    memset(&config, 0, sizeof(config));
    config.pcm = device;
    config.rate = DEFAULT_RATE;
    config.buffer_frames = 0u;
    config.clock = LE_AUDIO_TIMING_CLOCK_MONOTONIC;
    config.output_latency_us = 2000u;
    config.latency_calibrated = 1;
    return le_audio_timing_create(&config);
}

static int submit_timing(struct le_audio_timing *timing, uint32_t epoch,
                         uint32_t generation, uint64_t first_frame,
                         uint32_t frames, int generated)
{
    struct le_audio_timing_submission submission;

    memset(&submission, 0, sizeof(submission));
    submission.epoch = epoch;
    submission.generation = generation;
    submission.source_first_frame = first_frame;
    submission.frames = frames;
    submission.generated = generated;
    return le_audio_timing_submit(timing, &submission);
}

int main(int argc, char **argv)
{
    struct le_audio_sink_config sink_config;
    struct le_audio_sink *sink;
    struct le_audio_sink_progress sp_progress;
    struct le_audio_timing_config timing_config;
    struct le_audio_timing *timing;
    struct pcm *pcm;
    struct le_aec_reference_sender reference;
    struct source_bus sources[SOURCE_COUNT];
    struct le_audio_sink_progress progress;
    const size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);
    int16_t payload[PERIOD_SIZE * INPUT_CHANNELS];
    int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
    uint8_t datagram[LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_OPEN_PAYLOAD_BYTES];
    uint32_t epoch;
    size_t i;

    if (argc < 2)
        return 2;

    memset(&reference, 0, sizeof(reference));
    reference.fd = -1;
    memset(sources, 0, sizeof(sources));
    for (i = 0; i < SOURCE_COUNT; ++i)
        sources[i].fd = -1;
    memset(payload, 0, sizeof(payload));

    reset_fake_pcm();

    /* ---- real listener + real client on a private scratch path ---- */
    memset(&sink_config, 0, sizeof(sink_config));
    sink_config.socket_path = argv[1];
    sink_config.capacity_frames = 0;
    sink_config.allowed_uid = (uid_t)-1;
    sink_config.allow_root = 0;
    sink = le_audio_sink_create(&sink_config);
    check(sink != NULL, "real sink create failed");
    if (!sink)
        return 1;
    epoch = le_audio_sink_epoch(sink);
    check(epoch != 0u, "sink epoch is zero");

    /* ---- production timing object: MONOTONIC, uncalibrated ---- */
    {
        struct pcm_config fake_config;

        memset(&fake_config, 0, sizeof(fake_config));
        fake_config.channels = OUTPUT_CHANNELS;
        fake_config.rate = DEFAULT_RATE;
        fake_config.period_size = PERIOD_SIZE;
        fake_config.period_count = PERIOD_COUNT;
        fake_config.format = PCM_FORMAT_S16_LE;
        fake_config.start_threshold = 1u;
        fake_config.stop_threshold = PERIOD_SIZE * PERIOD_COUNT;
        pcm = pcm_open(0u, 23u, PCM_OUT | PCM_MONOTONIC | PCM_NORESTART,
                       &fake_config);
    }
    check(pcm != NULL, "fake pcm open failed");
    memset(&timing_config, 0, sizeof(timing_config));
    timing_config.pcm = pcm;
    timing_config.rate = DEFAULT_RATE;
    timing_config.buffer_frames = 0;
    timing_config.clock = LE_AUDIO_TIMING_CLOCK_MONOTONIC;
    timing_config.output_latency_us = 0;
    timing_config.latency_calibrated = 0;
    timing = le_audio_timing_create(&timing_config);
    check(timing != NULL, "timing create failed");

    memset(&g_sendspin, 0, sizeof(g_sendspin));
    g_sendspin.sink = sink;
    g_sendspin.timing = timing;

    /* ---- priming: idle sink is not a live generation ---- */
    check(!sendspin_live(&g_sendspin), "idle sink reported live");
    check(!period_ready(sources), "idle engine reported period_ready");
    check(!sources_active(sources), "idle engine reported sources_active");

    /* The client OPENs over the real socket; the engine's own service path
     * must admit it and sync_identity must adopt that generation while the
     * PCM is still idle (no FIFO period has ever been produced). */
    {
        int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
        struct sockaddr_un address;
        ssize_t sent;

        check(fd >= 0, "client socket failed");
        memset(&address, 0, sizeof(address));
        address.sun_family = AF_UNIX;
        strncpy(address.sun_path, argv[1], sizeof(address.sun_path) - 1u);
        check(connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0,
              "client connect failed");
        put_header(datagram, LE_AUDIO_SINK_TYPE_OPEN,
                   LE_AUDIO_SINK_OPEN_PAYLOAD_BYTES);
        open_payload(datagram + LE_AUDIO_SINK_HEADER_BYTES, 1u);
        sent = send(fd, datagram, sizeof(datagram), 0);
        check(sent == (ssize_t)sizeof(datagram), "client OPEN send failed");
        service_cycles(&g_sendspin);
        sendspin_sync_identity(&g_sendspin);
        check(g_sendspin.have_generation == 1,
              "socket OPEN did not produce a live generation");
        check(g_sendspin.generation == 1u && g_sendspin.epoch == epoch,
              "synced identity does not match the sink epoch/generation");
        close(fd);
    }

    /* ---- priming: a live generation paces periods with NO FIFO ---- */
    check(sendspin_live(&g_sendspin), "live generation not live");
    check(period_ready(sources),
          "live generation with no FIFO did not pace a period");
    check(sources_active(sources),
          "live generation with no FIFO did not report active");

    /* ---- bounded priming: an unbounded empty generation stops ---- */
    {
        unsigned int saved = g_sendspin.starve_periods;
        g_sendspin.starve_periods = SENDPIN_PRIME_LIMIT;
        check(!sendspin_live(&g_sendspin),
              "starved generation ignored the priming bound");
        g_sendspin.starve_periods = saved;
    }

    /* ---- split: 100 source frames + engine padding under the prefix ---- */
    /* Queue 100 known source frames directly on the live generation. */
    {
        int16_t src[100 * LE_AUDIO_SINK_OUTPUT_CHANNELS];

        for (i = 0; i < 100u; ++i) {
            src[i * 2] = (int16_t)(1000 + (int)i);
            src[i * 2 + 1] = (int16_t)(1000 + (int)i);
        }
        check(le_audio_sink_submit(sink, (const uint8_t *)src, 100u, 0u, epoch,
                                   1u, 0u) == LE_AUDIO_SINK_OK,
              "source submit rejected");
        check(sendspin_stage(&g_sendspin) == 100u,
              "stage did not peek the queued source frames");
        check(g_sendspin.first_frame == 0u, "first frame cursor wrong");
        check(g_sendspin.stage[0] == 1000 && g_sendspin.stage[1] == 1000,
              "staged samples do not match the submitted source");
    }

    reset_fake_pcm();
    {
        struct puffin_dynamics dynamics;
        struct speaker_dsp speaker;
        int32_t master_gain = 32768;

        puffin_dynamics_init(&dynamics);
        speaker_dsp_init(&speaker, 60);
        (void)sendspin_stage(&g_sendspin);
        render_period(sources, g_sendspin.stage, g_sendspin.stage_frames,
                      output, &dynamics, &speaker, master_gain, &master_gain);
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         g_sendspin.stage_frames,
                                         g_sendspin.first_frame, &reference,
                                         0u) == 0,
              "full accepted write failed");
    }
    /* The hardware range is the whole period, but only the 100 real source
     * frames are credited: submitted advances by the full period, the sink
     * credits exactly 100, and nothing is left queued. */
    check(le_audio_timing_submitted(timing) == PERIOD_SIZE,
          "hardware submission did not account the full period");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK,
          "sink progress failed");
    check(progress.submitted_frames == 100u,
          "sink did not commit exactly the source prefix");
    check(progress.queued_frames == 0u,
          "source frames remained queued after commit");
    check(progress.accepted_frames == 100u, "accepted count wrong");

    /* Drive the fake ring so the physical playhead sits inside the engine
     * padding at frame 100.  The source high-water must be exactly 100 and the
     * playhead must be reported as generated silence: if padding had been
     * credited as source this value would be the full period. */
    {
        struct le_audio_timing_progress tp;

        fake_avail = pcm_get_buffer_size(pcm) - (PERIOD_SIZE - 100u);
        check(le_audio_timing_progress(timing, &tp) == LE_AUDIO_TIMING_OK,
              "timing progress failed");
        check(tp.valid && tp.source_valid, "timing sample not valid");
        check(tp.source_frame == 100u,
              "engine padding was credited to the source cursor");
        check(tp.generated == 1, "playhead in padding was not flagged generated");
        /* Republish from the engine so the sink picks up source progress. */
        sendspin_publish_progress(&g_sendspin);
    }

    /* ---- short writes: no replay, no drop, bounded zero progress ---- */
    {
        struct le_audio_sink_progress p2;
        int16_t full[PERIOD_SIZE * OUTPUT_CHANNELS];
        size_t k;

        for (k = 0; k < PERIOD_SIZE; ++k) {
            full[k * 2] = (int16_t)((k * 7u + 1u) & 0x7fff);
            full[k * 2 + 1] = (int16_t)((k * 7u + 1u) & 0x7fff);
        }
        /* Source the period from the real sink queue so the commit is real. */
        check(le_audio_sink_submit(sink, (const uint8_t *)full, PERIOD_SIZE,
                                   100u, epoch, 1u, 1u) == LE_AUDIO_SINK_OK,
              "short-write source submit rejected");
        check(sendspin_stage(&g_sendspin) == PERIOD_SIZE,
              "short-write stage wrong");
        reset_fake_pcm();
        fake_write_mode = 1;
        fake_partial = 1000u;   /* call 1 -> 1000, call 2 -> 0, call 3 -> rest */
        check(sendspin_write_and_account(&g_sendspin, pcm, full, PERIOD_SIZE,
                                         PERIOD_SIZE, 100u, &reference,
                                         0u) == 0,
              "scripted short write was not completed");
        check(fake_written_frames == PERIOD_SIZE,
              "short writes dropped or duplicated rendered frames");
        check(memcmp(fake_written, full, sizeof(full)) == 0,
              "reconstructed output does not match the rendered buffer once");
        check(le_audio_sink_get_progress(sink, &p2) == LE_AUDIO_SINK_OK &&
              p2.submitted_frames == 100u + PERIOD_SIZE,
              "short-write commit credited the wrong frame count");
    }

    /* ================================================================== */
    /* Failure fencing: every write error must CANCEL the sink generation  */
    /* (never re-adopt it) and account the accepted hardware prefix.       */
    /* ================================================================== */

    /* ---- bounded zero progress: fence + cancel, no re-adoption ---- */
    {
        struct le_audio_sink_progress before, after;
        uint32_t gen = g_sendspin.generation;
        uint32_t failed_epoch = g_sendspin.epoch;

        reset_fake_pcm();
        fake_write_mode = 4;      /* every write returns 0 */
        check(le_audio_sink_get_progress(sink, &before) == LE_AUDIO_SINK_OK,
              "progress before zero-progress failed");
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         0u, 0u, &reference, 0u) == -1,
              "unbounded zero progress was not fenced");
        check(g_sendspin.have_generation == 0,
              "zero-progress failure left a live generation");
        check(before.submitted_frames == 100u + PERIOD_SIZE,
              "zero-progress changed prior accounting");

        /* DEFECT1: the fenced generation is CANCELLED, so the next poll cannot
         * re-adopt the same still-active sink generation.  No socket service
         * runs in between, so a re-adoption would be the loop-level defect. */
        sendspin_sync_identity(&g_sendspin);
        check(g_sendspin.have_generation == 0,
              "fenced generation was re-adopted at the next poll");
        check(le_audio_sink_get_progress(sink, &after) == LE_AUDIO_SINK_OK &&
              !after.active && after.queued_frames == 0u,
              "fenced generation stayed active or kept its unread queue");
        check((after.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u &&
              after.finish_us == 0u,
              "fenced generation kept a valid timing horizon");
        /* Old DATA and FINISH for the fenced generation are rejected.  The
         * (epoch, generation) of the failed write was captured before the
         * fence, so a successor is never a cancellation target. */
        check(le_audio_sink_submit(sink, (const uint8_t *)payload, 64u, 0u,
                                   failed_epoch, gen, 0u) ==
              LE_AUDIO_SINK_ERR_STALE_GENERATION,
              "DATA for the fenced generation was accepted");
        check(le_audio_sink_finish(sink, failed_epoch, gen, 0u) ==
              LE_AUDIO_SINK_ERR_STALE_GENERATION,
              "FINISH for the fenced generation was accepted");
        /* A fresh, strictly higher OPEN is permitted with fresh timing. */
        open_generation(&g_sendspin, argv[1], gen + 1u);
        check(g_sendspin.have_generation == 1 &&
              g_sendspin.generation == gen + 1u,
              "fresh higher generation was not adopted after the fence");
        check(le_audio_sink_get_progress(sink, &after) == LE_AUDIO_SINK_OK &&
              after.active &&
              (after.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u,
              "fresh generation inherited a valid timing horizon");
    }

    /* ---- EPIPE after a positive prefix: account it, then fence ---- */
    {
        struct le_audio_sink_progress before, after;
        struct le_audio_timing_progress tp;
        uint32_t gen = g_sendspin.generation;
        uint64_t submitted_before;

        check(queue_source(sink, epoch, gen, 0u, 0u, PERIOD_SIZE, 700) ==
              LE_AUDIO_SINK_OK, "EPIPE prefix source submit rejected");
        check(sendspin_stage(&g_sendspin) == PERIOD_SIZE,
              "EPIPE prefix stage wrong");
        reset_fake_pcm();
        fake_write_mode = 5;
        fake_partial = 1000u;             /* call 1 accepts 1000 frames */
        fake_write_errno = EPIPE;         /* call 2 underruns */
        submitted_before = le_audio_timing_submitted(timing);
        check(le_audio_sink_get_progress(sink, &before) == LE_AUDIO_SINK_OK,
              "EPIPE pre-progress failed");
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         g_sendspin.stage_frames,
                                         g_sendspin.first_frame, &reference,
                                         0u) == -1,
              "EPIPE did not fail the write");
        /* The accepted hardware prefix is recorded, never rewound. */
        check(le_audio_timing_submitted(timing) == submitted_before + 1000u,
              "EPIPE did not account the accepted hardware prefix");
        check(le_audio_timing_dropped(timing) > 0u,
              "EPIPE did not reconcile the dropped backlog");
        check(le_audio_timing_unplayed(timing) == 0u,
              "EPIPE reconcile left unplayed frames outstanding");
        /* The accepted source prefix is consumed from the queue (no replay)
         * and the generation is cancelled. */
        check(le_audio_sink_get_progress(sink, &after) == LE_AUDIO_SINK_OK &&
              after.submitted_frames == before.submitted_frames + 1000u &&
              after.queued_frames == 0u && !after.active,
              "EPIPE did not consume exactly the accepted source prefix");
        check(g_sendspin.have_generation == 0,
              "EPIPE left the affected generation live");
        check((after.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u &&
              after.finish_us == 0u,
              "EPIPE kept a valid timing horizon");
        sendspin_sync_identity(&g_sendspin);
        check(g_sendspin.have_generation == 0,
              "EPIPE generation was re-adopted at the next poll");
        check(le_audio_timing_progress(timing, &tp) == LE_AUDIO_TIMING_OK &&
              tp.source_frame <= 1000u,
              "EPIPE invented source progress beyond the accepted prefix");
        open_generation(&g_sendspin, argv[1], gen + 1u);
    }

    /* ---- ESTRPIPE with no prefix: fence + cancel, no credit ---- */
    {
        struct le_audio_sink_progress after;
        uint32_t gen = g_sendspin.generation;
        uint64_t submitted_before;

        check(queue_source(sink, epoch, gen, 0u, 0u, PERIOD_SIZE, 300) ==
              LE_AUDIO_SINK_OK, "ESTRPIPE source submit rejected");
        (void)sendspin_stage(&g_sendspin);
        reset_fake_pcm();
        fake_write_mode = 7;              /* immediate ESTRPIPE */
        submitted_before = le_audio_timing_submitted(timing);
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         g_sendspin.stage_frames,
                                         g_sendspin.first_frame, &reference,
                                         0u) == -1,
              "ESTRPIPE did not fail the write");
        check(le_audio_timing_submitted(timing) == submitted_before,
              "ESTRPIPE with no prefix changed the accepted cursor");
        check(g_sendspin.have_generation == 0,
              "ESTRPIPE left the generation live");
        sendspin_sync_identity(&g_sendspin);
        check(g_sendspin.have_generation == 0,
              "ESTRPIPE generation was re-adopted at the next poll");
        check(le_audio_sink_get_progress(sink, &after) == LE_AUDIO_SINK_OK &&
              !after.active && after.queued_frames == 0u,
              "ESTRPIPE generation stayed active");
        open_generation(&g_sendspin, argv[1], gen + 1u);
    }

    /* ---- fatal EIO after a prefix that straddles the source boundary ---- */
    {
        struct le_audio_sink_progress before, after;
        uint32_t gen = g_sendspin.generation;
        uint64_t submitted_before;

        /* Only 500 real source frames: a 700-frame accepted prefix straddles
         * the source/padding boundary (500 source + 200 engine padding). */
        check(queue_source(sink, epoch, gen, 0u, 0u, 500u, 120) ==
              LE_AUDIO_SINK_OK, "EIO source submit rejected");
        check(sendspin_stage(&g_sendspin) == 500u, "EIO stage wrong");
        reset_fake_pcm();
        fake_write_mode = 5;
        fake_partial = 700u;              /* call 1 accepts 700 frames */
        fake_write_errno = EIO;           /* call 2 fatal */
        submitted_before = le_audio_timing_submitted(timing);
        check(le_audio_sink_get_progress(sink, &before) == LE_AUDIO_SINK_OK,
              "EIO pre-progress failed");
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         g_sendspin.stage_frames,
                                         g_sendspin.first_frame, &reference,
                                         0u) == -1,
              "EIO did not fail the write");
        /* The whole accepted prefix is recorded (500 source + 200 generated),
         * never silently discarded. */
        check(le_audio_timing_submitted(timing) == submitted_before + 700u,
              "EIO did not account the accepted source/padding prefix split");
        /* Only the real source prefix is consumed from the sink queue. */
        check(le_audio_sink_get_progress(sink, &after) == LE_AUDIO_SINK_OK &&
              after.submitted_frames == before.submitted_frames + 500u &&
              after.queued_frames == 0u && !after.active,
              "EIO did not consume exactly the accepted source prefix");
        check(g_sendspin.have_generation == 0,
              "EIO left the generation live");
        sendspin_sync_identity(&g_sendspin);
        check(g_sendspin.have_generation == 0,
              "EIO generation was re-adopted at the next poll");
        open_generation(&g_sendspin, argv[1], gen + 1u);
    }

    /* ---- over-accepting backend: bounded defensively, never credited ---- */
    {
        uint32_t gen = g_sendspin.generation;
        uint64_t submitted_before;

        check(queue_source(sink, epoch, gen, 0u, 0u, PERIOD_SIZE, 900) ==
              LE_AUDIO_SINK_OK, "over-accept source submit rejected");
        (void)sendspin_stage(&g_sendspin);
        reset_fake_pcm();
        fake_write_mode = 6;              /* returns frame_count + 5 */
        submitted_before = le_audio_timing_submitted(timing);
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         g_sendspin.stage_frames,
                                         g_sendspin.first_frame, &reference,
                                         0u) == -1,
              "an over-accepting backend was not contained");
        check(le_audio_timing_submitted(timing) == submitted_before,
              "over-accept invented accepted frames");
        check(g_sendspin.have_generation == 0,
              "over-accept left the generation live");
        open_generation(&g_sendspin, argv[1], gen + 1u);
    }

    /* ---- postwrite accounting failure: never leave an accepted write live -- */
    {
        struct le_audio_sink_progress after;
        struct le_audio_timing *saved = g_sendspin.timing;
        struct le_audio_timing *full;
        uint32_t gen = g_sendspin.generation;
        unsigned int k;

        check(queue_source(sink, epoch, gen, 0u, 0u, PERIOD_SIZE, 400) ==
              LE_AUDIO_SINK_OK, "accounting-failure source submit rejected");
        (void)sendspin_stage(&g_sendspin);
        /* Fill the bounded ledger so the engine's own submission must fail. */
        full = make_calibrated_timing(pcm);
        check(full != NULL, "accounting-failure timing create failed");
        for (k = 0; k < LE_AUDIO_LEDGER_CAPACITY; ++k)
            check(submit_timing(full, epoch, gen, 900000u + k * 16u, 16u, 0) ==
                  LE_AUDIO_TIMING_OK, "ledger fill rejected");
        g_sendspin.timing = full;
        reset_fake_pcm();
        fake_write_mode = 0;              /* hardware accepts the whole period */
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         g_sendspin.stage_frames,
                                         g_sendspin.first_frame, &reference,
                                         0u) == -1,
              "an unaccountable accepted write was not contained");
        check(g_sendspin.have_generation == 0,
              "accounting failure left the accepted write live");
        check(le_audio_sink_get_progress(sink, &after) == LE_AUDIO_SINK_OK &&
              !after.active && after.queued_frames == 0u &&
              (after.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u,
              "accounting failure did not cancel the generation");
        sendspin_sync_identity(&g_sendspin);
        check(g_sendspin.have_generation == 0,
              "accounting-failure generation was re-adopted");
        g_sendspin.timing = saved;
        le_audio_timing_destroy(full);
        open_generation(&g_sendspin, argv[1], gen + 1u);
    }

    /* ---- finite FINISH drain: no endless silence once finished ---- */
    {
        struct le_audio_sink_progress p4;
        uint64_t total;
        uint32_t gen;

        open_generation(&g_sendspin, argv[1], g_sendspin.generation + 1u);
        gen = g_sendspin.generation;
        check(le_audio_sink_get_progress(sink, &p4) == LE_AUDIO_SINK_OK &&
              p4.active && !p4.finished,
              "successor session not active before FINISH");

        total = 512u;
        check(queue_source(sink, epoch, gen, 0u, 0u, (size_t)total, 55) ==
              LE_AUDIO_SINK_OK, "finish source submit rejected");
        check(le_audio_sink_finish(sink, epoch, gen, total) == LE_AUDIO_SINK_OK,
              "FINISH rejected");
        /* Still paced: the tail has not reached the physical cursor. */
        check(sendspin_live(&g_sendspin),
              "FINISHed generation stopped pacing before completion");
        check(le_audio_sink_commit(sink, epoch, gen, (size_t)total) ==
              LE_AUDIO_SINK_OK, "finish commit rejected");
        check(le_audio_sink_note_playhead(sink, epoch, gen, total) ==
              LE_AUDIO_SINK_OK, "finish playhead rejected");
        check(le_audio_sink_get_progress(sink, &p4) == LE_AUDIO_SINK_OK &&
              p4.completed == 1, "exact FINISH tail did not complete");
        /* Completed -> no pacing, no endless silence. */
        check(!sendspin_live(&g_sendspin),
              "completed generation kept pacing silence forever");
        check(!period_ready(sources),
              "completed generation still reported a ready period");
        check(p4.played_frames == total,
              "FINISH tail was not delivered as the exact source end");
    }

    /* ---- superseded generation: late feedback rejected untouched ---- */
    {
        struct le_audio_sink_progress p5;

        /* Generation 1 (fenced long ago) must not move the live generation. */
        check(le_audio_sink_note_playhead(sink, epoch, 1u, 999999u) ==
              LE_AUDIO_SINK_ERR_STALE_GENERATION,
              "stale generation feedback was accepted");
        check(le_audio_sink_get_progress(sink, &p5) == LE_AUDIO_SINK_OK &&
              p5.played_frames == 512u,
              "stale generation feedback moved the successor");
        /* A generation that does not exist yet is likewise rejected. */
        check(le_audio_sink_note_playhead(sink, epoch + 1u, p5.generation, 0u) ==
              LE_AUDIO_SINK_ERR_STALE_EPOCH,
              "stale-epoch feedback was accepted");
    }

    /* ================================================================== */
    /* DEFECT3: stale timing must not survive, and identity needs BOTH     */
    /* epoch and generation for the source cursor AND the source finish.   */
    /* ================================================================== */
    {
        struct le_audio_sink_progress pv, pa;
        struct le_audio_timing *saved = g_sendspin.timing;

        /* (A) A predecessor generation still draining after a successor OPEN
         * must never be published as the successor's valid horizon. */
        {
            struct le_audio_timing *cal;
            uint32_t gen;

            open_generation(&g_sendspin, argv[1], g_sendspin.generation + 1u);
            gen = g_sendspin.generation;
            cal = make_calibrated_timing(pcm);
            check(cal != NULL, "d3a calibrated timing create failed");
            g_sendspin.timing = cal;

            check(queue_source(sink, epoch, gen, 0u, 0u, 2048u, 111) ==
                  LE_AUDIO_SINK_OK, "d3a sink submit failed");
            check(le_audio_sink_commit(sink, epoch, gen, 2048u) ==
                  LE_AUDIO_SINK_OK, "d3a sink commit failed");
            check(submit_timing(cal, epoch, gen, 0u, 4096u, 0) ==
                  LE_AUDIO_TIMING_OK, "d3a timing submit failed");

            /* Baseline: the live generation publishes a genuine VALID horizon. */
            reset_fake_pcm();
            fake_avail = 1000u;          /* playhead 1000, inside [0,4096) */
            sendspin_publish_progress(&g_sendspin);
            check(le_audio_sink_get_progress(sink, &pv) == LE_AUDIO_SINK_OK &&
                  pv.active &&
                  (pv.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) != 0u &&
                  pv.finish_us != 0u && pv.played_frames == 1000u,
                  "d3a baseline valid horizon was not published");

            /* The successor OPENs while the predecessor range still drains. */
            open_generation(&g_sendspin, argv[1], gen + 1u);
            check(g_sendspin.generation == gen + 1u,
                  "d3a successor not adopted");

            reset_fake_pcm();
            fake_avail = 1000u;          /* still inside the predecessor range */
            sendspin_publish_progress(&g_sendspin);
            sendspin_publish_progress(&g_sendspin);
            check(le_audio_sink_get_progress(sink, &pa) == LE_AUDIO_SINK_OK &&
                  (pa.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u &&
                  pa.finish_us == 0u,
                  "predecessor tail published as a successor-valid horizon");
            /* The successor OPEN reset its own cursor; predecessor progress
             * must leave it exactly there. */
            check(pa.played_frames == 0u,
                  "predecessor cursor moved the successor");

            g_sendspin.timing = saved;
            le_audio_timing_destroy(cal);
        }

        /* (B) A foreign-epoch range with a matching generation must move
         * neither the source cursor nor the finish: identity needs BOTH. */
        {
            struct le_audio_timing *cal;
            uint32_t gen;

            open_generation(&g_sendspin, argv[1], g_sendspin.generation + 1u);
            gen = g_sendspin.generation;
            cal = make_calibrated_timing(pcm);
            check(cal != NULL, "d3b calibrated timing create failed");
            g_sendspin.timing = cal;

            check(queue_source(sink, epoch, gen, 0u, 0u, 2048u, 222) ==
                  LE_AUDIO_SINK_OK, "d3b sink submit 1 failed");
            check(le_audio_sink_commit(sink, epoch, gen, 2048u) ==
                  LE_AUDIO_SINK_OK, "d3b sink commit 1 failed");
            check(queue_source(sink, epoch, gen, 2048u, 1u, 2048u, 222) ==
                  LE_AUDIO_SINK_OK, "d3b sink submit 2 failed");
            check(le_audio_sink_commit(sink, epoch, gen, 2048u) ==
                  LE_AUDIO_SINK_OK, "d3b sink commit 2 failed");

            /* The live generation's own range, then a foreign-epoch range. */
            check(submit_timing(cal, epoch, gen, 0u, 2048u, 0) ==
                  LE_AUDIO_TIMING_OK, "d3b live timing submit failed");
            check(submit_timing(cal, epoch + 1u, gen, 0u, 2048u, 0) ==
                  LE_AUDIO_TIMING_OK, "d3b foreign timing submit failed");

            reset_fake_pcm();
            fake_avail = 3000u;          /* playhead 3000, inside the foreign range */
            sendspin_publish_progress(&g_sendspin);
            check(le_audio_sink_get_progress(sink, &pa) == LE_AUDIO_SINK_OK &&
                  (pa.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u &&
                  pa.finish_us == 0u,
                  "foreign-epoch range published a valid horizon");
            check(pa.played_frames == 0u,
                  "foreign-epoch range moved the live source cursor");

            g_sendspin.timing = saved;
            le_audio_timing_destroy(cal);
        }

        /* (C) The exact source tail survives reaping and the completed-event
         * drain: it is still delivered to the sink. */
        {
            struct le_audio_timing *cal;
            uint32_t gen;

            open_generation(&g_sendspin, argv[1], g_sendspin.generation + 1u);
            gen = g_sendspin.generation;
            cal = make_calibrated_timing(pcm);
            check(cal != NULL, "d3c calibrated timing create failed");
            g_sendspin.timing = cal;

            check(queue_source(sink, epoch, gen, 0u, 0u, 2048u, 333) ==
                  LE_AUDIO_SINK_OK, "d3c sink submit failed");
            check(le_audio_sink_commit(sink, epoch, gen, 2048u) ==
                  LE_AUDIO_SINK_OK, "d3c sink commit failed");
            check(submit_timing(cal, epoch, gen, 0u, 2048u, 0) ==
                  LE_AUDIO_TIMING_OK, "d3c timing submit failed");

            reset_fake_pcm();
            fake_avail = pcm_get_buffer_size(pcm);  /* playhead at the exact tail */
            sendspin_publish_progress(&g_sendspin);
            check(le_audio_sink_get_progress(sink, &pa) == LE_AUDIO_SINK_OK &&
                  (pa.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) != 0u &&
                  pa.finish_us != 0u,
                  "d3c exact tail horizon was not delivered");
            check(pa.played_frames == 2048u,
                  "d3c exact source tail was lost by the completed drain");

            g_sendspin.timing = saved;
            le_audio_timing_destroy(cal);
        }
    }

    /* ---- invalid vs calibrated timing publication ---- */
    {
        struct le_audio_sink_progress p6;

        /* Untrustworthy sample (zero timestamp) with the production object
         * (uncalibrated): the engine must publish TIMING_INVALID, never a
         * fabricated finish. */
        reset_fake_pcm();
        fake_ts_valid = 0;
        sendspin_publish_progress(&g_sendspin);
        check(le_audio_sink_get_progress(sink, &p6) == LE_AUDIO_SINK_OK &&
              (p6.flags & LE_AUDIO_SINK_PROGRESS_TIMING_INVALID) != 0u &&
              (p6.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u &&
              p6.finish_us == 0u,
              "invalid sample published a valid timing horizon");

        /* Production stays invalid even with a real fresh sample, because the
         * latency constant is uncalibrated. */
        reset_fake_pcm();
        fake_avail = pcm_get_buffer_size(pcm);
        sendspin_publish_progress(&g_sendspin);
        check(le_audio_sink_get_progress(sink, &p6) == LE_AUDIO_SINK_OK &&
              (p6.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u &&
              p6.finish_us == 0u,
              "uncalibrated production fabricated a calibrated finish");

        /* A stale query after a valid calibrated snapshot must clear it. */
        {
            struct le_audio_timing *cal;
            struct le_audio_timing *saved = g_sendspin.timing;

            cal = make_calibrated_timing(pcm);
            check(cal != NULL, "stale-query calibrated timing create failed");
            check(submit_timing(cal, g_sendspin.epoch, g_sendspin.generation,
                                0u, 2048u, 0) == LE_AUDIO_TIMING_OK,
                  "stale-query calibrated submit failed");
            g_sendspin.timing = cal;
            reset_fake_pcm();
            fake_avail = pcm_get_buffer_size(pcm) - 256u;
            sendspin_publish_progress(&g_sendspin);
            check(le_audio_sink_get_progress(sink, &p6) == LE_AUDIO_SINK_OK &&
                  (p6.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) != 0u &&
                  p6.finish_us != 0u,
                  "calibrated fresh sample did not publish valid timing");

            /* Now the query itself fails (zero timestamp: not a DAC sample).
             * The earlier VALID horizon must not survive it. */
            reset_fake_pcm();
            fake_ts_valid = 0;
            sendspin_publish_progress(&g_sendspin);
            check(le_audio_sink_get_progress(sink, &p6) == LE_AUDIO_SINK_OK &&
                  (p6.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0u &&
                  p6.finish_us == 0u,
                  "a failed query left a stale valid timing horizon");

            g_sendspin.timing = saved;
            le_audio_timing_destroy(cal);
        }
    }

    /* ---- priority duck/mute still consumes the source ---- */
    {
        int32_t mixed;
        uint32_t gen;

        sources[SOURCE_SYSTEM].samples =
            (int16_t *)calloc(1, period_bytes);
        sources[SOURCE_SYSTEM].capacity = period_bytes;
        sources[SOURCE_SYSTEM].received = period_bytes;
        check(sources[SOURCE_SYSTEM].samples != NULL, "system buffer alloc");
        output[0] = 1000;
        output[1] = 1000;
        mixed = mix_sources_frame(sources, output, 1u, 0u);
        check(mixed == (int32_t)((1000 * MEDIA_DUCK_Q15) >> 15),
              "Sendspin was not ducked under a higher-priority bus");
        free(sources[SOURCE_SYSTEM].samples);
        sources[SOURCE_SYSTEM].samples = NULL;
        sources[SOURCE_SYSTEM].received = 0u;

        sources[SOURCE_ALARM].samples = (int16_t *)calloc(1, period_bytes);
        sources[SOURCE_ALARM].capacity = period_bytes;
        sources[SOURCE_ALARM].received = period_bytes;
        check(sources[SOURCE_ALARM].samples != NULL, "alarm buffer alloc");
        mixed = mix_sources_frame(sources, output, 1u, 0u);
        check(mixed == 0, "Sendspin was not muted by the alarm bus");
        free(sources[SOURCE_ALARM].samples);
        sources[SOURCE_ALARM].samples = NULL;
        sources[SOURCE_ALARM].received = 0u;

        /* Even when muted, the period still commits the source time: mute is
         * an output-gain decision, not a reason to stall the source cursor.
         * Open a fresh generation so a FINISHed predecessor cannot interfere. */
        open_generation(&g_sendspin, argv[1], g_sendspin.generation + 1u);
        gen = g_sendspin.generation;
        check(queue_source(sink, epoch, gen, 0u, 0u, 256u, 444) ==
              LE_AUDIO_SINK_OK, "muted source submit rejected");
        (void)sendspin_stage(&g_sendspin);
        reset_fake_pcm();
        check(sendspin_write_and_account(&g_sendspin, pcm, output, PERIOD_SIZE,
                                         g_sendspin.stage_frames,
                                         g_sendspin.first_frame, &reference,
                                         0u) == 0,
              "muted-ducked period did not complete");
        check(le_audio_sink_get_progress(sink, &sp_progress) == LE_AUDIO_SINK_OK &&
              sp_progress.submitted_frames == 256u,
              "muted-ducked period did not advance the source cursor");
    }

    /* ---- cleanup: teardown is idempotent and order-safe ---- */
    sendspin_close_timing(&g_sendspin);
    check(g_sendspin.timing == NULL, "close_timing did not clear the timing");
    sendspin_close_timing(&g_sendspin);   /* second call must be a no-op */
    check(g_sendspin.timing == NULL, "second close_timing changed state");
    if (g_sendspin.sink) {
        le_audio_sink_destroy(g_sendspin.sink);
        g_sendspin.sink = NULL;
    }
    pcm_close(pcm);
    check(failures == 0, "one or more engine-Sendspin scenarios failed");
    if (failures)
        return 1;
    puts("engine-sendspin: real engine + fake PCM behaviour PASS");
    return 0;
}
'''



LOOP_PROGRAM = r'''#define _GNU_SOURCE
#define main libreecho_audio_engine_main
#include "audio_engine.c"
#undef main

#include <stdarg.h>
#include <sys/wait.h>

/*
 * Task 4b-loop: outer-loop harness for run_engine().
 *
 * The REAL production run_engine()/main loop is executed in a forked child
 * against a fake TinyALSA PCM *and* mixer, private root FIFOs and a real
 * SOCK_SEQPACKET client driven from the parent.  No host /sys /dev /run /data
 * path is ever reached: open/open64/openat/connect are link-wrapped and any
 * denylisted path is refused and recorded.
 */

/* ------------------------------------------------------------------ */
/* Event log                                                          */
/* ------------------------------------------------------------------ */

#define EV_MAX 16384
struct ev { uint32_t tag, a, b, c; int32_t d; };
#define T_PCM_OPEN 1u
#define T_PCM_PREPARE 2u
#define T_PCM_WRITE 3u
#define T_PCM_CLOSE 4u
#define T_MIX_OPEN 5u
#define T_MIX_CLOSE 6u
#define T_MIX_ENUM 7u
#define T_MIX_SET 8u
#define T_AUDIT_DENY 9u
#define T_PCM_OPEN_FAIL 10u
#define T_PCM_WRITE_NEG 11u

struct engine_log {
    uint32_t magic;
    uint32_t count;
    uint32_t audit_denied;
    struct ev ev[EV_MAX];
};

#define LOG_MAGIC 0x4C4F4F50u

static struct ev g_ev[EV_MAX];
static int g_ev_n;
static int g_audit_denied;
static char g_root[700];
static int g_ev_fd = -1;
static int g_ctl_fd = -1;

static void logev(uint32_t tag, uint32_t a, uint32_t b, uint32_t c, int32_t d)
{
    if (g_ev_n < EV_MAX) {
        g_ev[g_ev_n].tag = tag;
        g_ev[g_ev_n].a = a;
        g_ev[g_ev_n].b = b;
        g_ev[g_ev_n].c = c;
        g_ev[g_ev_n].d = d;
        g_ev_n += 1;
    }
}

static void notify(char c)
{
    if (g_ev_fd >= 0) {
        ssize_t ignored = write(g_ev_fd, &c, 1);
        (void)ignored;
    }
}

/* ------------------------------------------------------------------ */
/* Path audit: deny any host hardware path except our private root    */
/* ------------------------------------------------------------------ */

static const char *const g_deny_prefix[] = {
    "/sys/", "/dev/", "/run/", "/data/", "/proc/", "/etc/"
};

static int path_denied(const char *path)
{
    unsigned int i;

    if (!path || path[0] != '/')
        return 0;
    if (g_root[0] != '\0' && strncmp(path, g_root, strlen(g_root)) == 0)
        return 0;
    for (i = 0; i < sizeof(g_deny_prefix) / sizeof(g_deny_prefix[0]); ++i)
        if (strncmp(path, g_deny_prefix[i], strlen(g_deny_prefix[i])) == 0)
            return 1;
    return 0;
}

static void record_denied(uint32_t kind, const char *path)
{
    uint32_t b = 0u, c = 0u;
    size_t n = path ? strlen(path) : 0u;

    if (n > 0u) memcpy(&b, path, n > 4u ? 4u : n);
    if (n > 4u) memcpy(&c, path + 4u, n > 8u ? 4u : n - 4u);
    g_audit_denied += 1;
    logev(T_AUDIT_DENY, kind, b, c, (int32_t)n);
}

extern int __real_open(const char *path, int flags, ...);
extern int __real_open64(const char *path, int flags, ...);
extern int __real_openat(int dirfd, const char *path, int flags, ...);
extern int __real_connect(int fd, const struct sockaddr *addr, socklen_t len);

int __wrap_open(const char *path, int flags, ...)
{
    if (path_denied(path)) {
        record_denied(1u, path);
        errno = EACCES;
        return -1;
    }
    if (flags & O_CREAT) {
        va_list ap;
        int mode;
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
        return __real_open(path, flags, mode);
    }
    return __real_open(path, flags);
}

int __wrap_open64(const char *path, int flags, ...)
{
    if (path_denied(path)) {
        record_denied(1u, path);
        errno = EACCES;
        return -1;
    }
    if (flags & O_CREAT) {
        va_list ap;
        int mode;
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
        return __real_open64(path, flags, mode);
    }
    return __real_open64(path, flags);
}

int __wrap_openat(int dirfd, const char *path, int flags, ...)
{
    if (path_denied(path)) {
        record_denied(1u, path);
        errno = EACCES;
        return -1;
    }
    if (flags & O_CREAT) {
        va_list ap;
        int mode;
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
        return __real_openat(dirfd, path, flags, mode);
    }
    return __real_openat(dirfd, path, flags);
}

int __wrap_connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    if (addr && addr->sa_family == AF_UNIX) {
        const struct sockaddr_un *un = (const struct sockaddr_un *)addr;
        if (path_denied(un->sun_path)) {
            record_denied(2u, un->sun_path);
            errno = EACCES;
            return -1;
        }
    }
    return __real_connect(fd, addr, len);
}

/* ------------------------------------------------------------------ */
/* Fake TinyALSA PCM                                                  */
/* ------------------------------------------------------------------ */

struct pcm {
    unsigned int buffer_frames;
    int ready;
};

/* Scenario fault scripts (set in the child before run_engine). */
#define SCEN_PRIME 1
#define SCEN_FINISH 2
#define SCEN_LEGACY 3
#define SCEN_EPIPE 4
#define SCEN_RESTART 5
#define SCEN_MIXFAIL 6
#define SCEN_PREPFAIL 7
#define SCEN_STOP 8
#define SCEN_MEDIA 9
#define SCEN_PCMFAIL 10
/* Timing model unavailable after the PCM starts: the opened PCM reports a
 * zero-frame buffer, which is the realistic path by which
 * le_audio_timing_create() fails closed. */
#define SCEN_NOTIMING 11
#define SCEN_NOTIMING_LEGACY 12
/* Timing model present but the DAC timestamp query fails persistently: the
 * bounded ledger must fail the generation closed (cancel, not complete). */
#define SCEN_QUERYFAIL 13

static int g_scen;
static int g_fail_open;
static int g_fail_prepare;
static int g_no_timing_buffer;   /* opened PCM reports a 0-frame buffer */
static int g_hts_fail;           /* pcm_get_htimestamp() fails persistently */

/* Fault injection for one period: armed by the parent through the ctl pipe. */
static int g_arm_fail;
static int g_fail_state;         /* 0 idle, 1 partial done, 2 failed */
static unsigned int g_partial = 500u;
static int g_fail_errno = EPIPE;
static unsigned int g_write_calls;
static int16_t g_written[1 << 20];
static size_t g_written_frames;

static void poll_ctl(void)
{
    char cmd;
    ssize_t n;

    if (g_ctl_fd < 0 || g_arm_fail)
        return;
    n = read(g_ctl_fd, &cmd, 1);
    if (n == 1) {
        if (cmd == 'F') { g_arm_fail = 1; g_fail_state = 0; g_fail_errno = EPIPE; }
        else if (cmd == 'S') { g_arm_fail = 1; g_fail_state = 0; g_fail_errno = ESTRPIPE; }
    }
}

struct pcm *pcm_open(unsigned int card, unsigned int device, unsigned int flags,
                     const struct pcm_config *config)
{
    struct pcm *pcm;

    (void)card;
    (void)device;
    if (g_fail_open) {
        /* A failed open is still recorded so the harness can prove the loop
         * really attempted the PCM (and did not spin retrying it). */
        logev(T_PCM_OPEN_FAIL, flags, config->channels, config->rate, 0);
        notify('X');
        return NULL;
    }
    pcm = (struct pcm *)calloc(1, sizeof(*pcm));
    if (!pcm)
        return NULL;
    /* A zero-frame buffer is the realistic fault by which
     * le_audio_timing_create() returns NULL (no observable DAC timeline). */
    pcm->buffer_frames = g_no_timing_buffer
                             ? 0u
                             : config->period_size * config->period_count;
    pcm->ready = 1;
    logev(T_PCM_OPEN, flags, config->channels, config->rate,
          (int32_t)(config->period_size | (config->period_count << 16)));
    notify('O');
    return pcm;
}

int pcm_is_ready(struct pcm *pcm)
{
    return pcm != NULL && pcm->ready;
}

const char *pcm_get_error(struct pcm *pcm)
{
    (void)pcm;
    return "fake-pcm";
}

void pcm_close(struct pcm *pcm)
{
    logev(T_PCM_CLOSE, 0u, 0u, 0u, 0);
    notify('C');
    free(pcm);
}

int pcm_prepare(struct pcm *pcm)
{
    (void)pcm;
    logev(T_PCM_PREPARE, 0u, 0u, 0u, 0);
    return g_fail_prepare ? -1 : 0;
}

unsigned int pcm_get_buffer_size(const struct pcm *pcm)
{
    return pcm ? pcm->buffer_frames : 0u;
}

int pcm_get_htimestamp(struct pcm *pcm, unsigned int *avail,
                       struct timespec *timestamp)
{
    (void)pcm;
    if (!avail || !timestamp)
        return -1;
    if (g_hts_fail) {
        errno = EIO;             /* persistent query failure: no timestamp */
        return -1;
    }
    /* Deterministic fully-drained ring: nothing left in flight, so the
     * physical playhead equals the accepted-frame counter. */
    *avail = pcm ? pcm->buffer_frames : 0u;
    if (clock_gettime(CLOCK_MONOTONIC, timestamp) != 0)
        return -1;
    return 0;
}

int pcm_writei(struct pcm *pcm, const void *data, unsigned int frame_count)
{
    const int16_t *samples = (const int16_t *)data;
    unsigned int accepted;
    unsigned int i;
    unsigned int half = frame_count / 2u;
    unsigned int nz_first = 0u, nz_second = 0u;
    unsigned int neg = 0u;
    int16_t first;

    (void)pcm;
    poll_ctl();
    g_write_calls += 1;

    if (g_arm_fail && g_fail_state == 0) {
        accepted = g_partial < frame_count ? g_partial : frame_count;
        g_fail_state = 1;
    } else if (g_arm_fail && g_fail_state == 1) {
        g_fail_state = 2;
        errno = g_fail_errno;
        return -1;
    } else {
        accepted = frame_count;
    }

    if (!samples)
        return -1;
    first = accepted > 0u ? samples[0] : 0;
    for (i = 0; i < accepted; ++i) {
        int16_t v = samples[i * OUTPUT_CHANNELS];
        if (v != 0) {
            if (i < half) nz_first += 1; else nz_second += 1;
        }
        if (v < 0) neg += 1;
    }
    /* Bounded capture buffer: an intentionally unbounded write sequence (the
     * regression this fixture must catch) must not overflow the harness's own
     * log buffer -- the write-count assertions are what bound the loop. */
    if (g_written_frames + accepted <=
        (sizeof(g_written) / sizeof(g_written[0])) / OUTPUT_CHANNELS)
        memcpy(g_written + g_written_frames * OUTPUT_CHANNELS, samples,
               (size_t)accepted * OUTPUT_CHANNELS * sizeof(int16_t));
    g_written_frames += accepted;
    logev(T_PCM_WRITE, frame_count, (uint32_t)(uint16_t)first,
          nz_first | (nz_second << 16), (int32_t)accepted);
    if (neg > 0u)
        logev(T_PCM_WRITE_NEG, neg, frame_count, 0u, 0);
    notify('W');
    return (int)accepted;
}

/* ------------------------------------------------------------------ */
/* Fake TinyALSA mixer                                                */
/* ------------------------------------------------------------------ */

enum {
    C_MUTE = 0, C_AMP, C_DACMUX, C_CHCFG, C_RIGHT, C_HPDAC, C_HPL, C_HPR,
    C_HPRIN1, C_HPGAIN, C_PCMVOL, C_COUNT
};

static const char *const ctl_names[C_COUNT] = {
    "MFP Gpio Mute", "Ext_Speaker_Amp_Switch", "Audio_DacMux_Setting",
    "Board Channel Config", "Right Channel Only", "HP DAC Playback Switch",
    "HPL Output Mixer L_DAC Switch", "HPR Output Mixer R_DAC Switch",
    "HPR Output Mixer IN1_R Switch", "HP Driver Gain Volume",
    "PCM Playback Volume"
};

static int g_mixer_open_fail;
static int g_enum_off_fail;      /* make "MFP Gpio Mute"="Off" fail */

static int ctl_kind(int idx)
{
    if (idx == C_HPDAC || idx == C_HPGAIN || idx == C_PCMVOL)
        return 2;
    return 1;
}

struct mixer { int unused; };
struct mixer_ctl { int unused; };

struct mixer *mixer_open(unsigned int card)
{
    (void)card;
    if (g_mixer_open_fail)
        return NULL;
    logev(T_MIX_OPEN, 0u, 0u, 0u, 0);
    return (struct mixer *)malloc(sizeof(struct mixer));
}

void mixer_close(struct mixer *mixer)
{
    logev(T_MIX_CLOSE, 0u, 0u, 0u, 0);
    free(mixer);
}

struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *mixer, const char *name)
{
    int i;

    (void)mixer;
    for (i = 0; i < C_COUNT; ++i)
        if (strcmp(name, ctl_names[i]) == 0)
            return (struct mixer_ctl *)(intptr_t)(i + 1);
    return NULL;
}

unsigned int mixer_ctl_get_num_values(struct mixer_ctl *ctl)
{
    int idx = (int)(intptr_t)ctl - 1;
    return (unsigned int)ctl_kind(idx);
}

int mixer_ctl_get_value(struct mixer_ctl *ctl, unsigned int index)
{
    int idx = (int)(intptr_t)ctl - 1;
    (void)index;
    if (idx == C_PCMVOL)
        return 127;
    return 0;
}

int mixer_ctl_set_value(struct mixer_ctl *ctl, unsigned int index, int value)
{
    int idx = (int)(intptr_t)ctl - 1;
    logev(T_MIX_SET, (uint32_t)idx, index, (uint32_t)value, 0);
    return 0;
}

int mixer_ctl_set_enum_by_string(struct mixer_ctl *ctl, const char *value)
{
    int idx = (int)(intptr_t)ctl - 1;
    uint32_t code = 9u;

    if (strcmp(value, "On") == 0) code = 1u;
    else if (strcmp(value, "Off") == 0) code = 0u;
    else if (strcmp(value, "Stereo") == 0) code = 2u;
    logev(T_MIX_ENUM, (uint32_t)idx, code, 0u, 0);
    if (g_enum_off_fail && idx == C_MUTE && code == 0u)
        return -1;
    return 0;
}

/* ------------------------------------------------------------------ */
/* Child                                                              */
/* ------------------------------------------------------------------ */

static void dump_log(const char *root, int rc)
{
    char path[600];
    struct engine_log *out = (struct engine_log *)calloc(1, sizeof(*out));
    int fd;

    if (!out)
        return;
    out->magic = LOG_MAGIC;
    out->count = (uint32_t)g_ev_n;
    out->audit_denied = (uint32_t)g_audit_denied;
    memcpy(out->ev, g_ev, sizeof(struct ev) * (size_t)g_ev_n);
    (void)snprintf(path, sizeof(path), "%s/engine.log", root);
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0640);
    if (fd >= 0) {
        ssize_t ignored = write(fd, out, sizeof(*out));
        (void)ignored;
        close(fd);
    }
    free(out);
    (void)rc;
}

static void child_main(const char *root, int scen, int ev_w, int ctl_r)
{
    struct sigaction action;

    g_scen = scen;
    g_ev_n = 0;
    g_audit_denied = 0;
    g_ev_fd = ev_w;
    g_ctl_fd = ctl_r;
    (void)snprintf(g_root, sizeof(g_root), "%s", root);
    if (ctl_r >= 0) {
        int fl = fcntl(ctl_r, F_GETFL, 0);
        if (fl >= 0)
            (void)fcntl(ctl_r, F_SETFL, fl | O_NONBLOCK);
    }
    signal(SIGPIPE, SIG_IGN);
    memset(&action, 0, sizeof(action));
    action.sa_handler = on_signal;
    sigemptyset(&action.sa_mask);
    (void)sigaction(SIGTERM, &action, NULL);
    (void)sigaction(SIGINT, &action, NULL);

    g_fail_open = 0;
    g_fail_prepare = 0;
    g_mixer_open_fail = 0;
    g_enum_off_fail = 0;
    g_arm_fail = 0;
    g_fail_state = 0;
    g_write_calls = 0;
    g_written_frames = 0u;
    g_no_timing_buffer = 0;
    g_hts_fail = 0;
    if (scen == SCEN_MIXFAIL)
        g_mixer_open_fail = 1;
    if (scen == SCEN_PREPFAIL)
        g_fail_prepare = 1;
    if (scen == SCEN_PCMFAIL)
        g_fail_open = 1;
    if (scen == SCEN_NOTIMING || scen == SCEN_NOTIMING_LEGACY)
        g_no_timing_buffer = 1;
    if (scen == SCEN_QUERYFAIL)
        g_hts_fail = 1;

    (void)run_engine(root, 0u, 23u);
    dump_log(root, 0);
    _exit(0);
}

/* ------------------------------------------------------------------ */
/* Parent helpers                                                     */
/* ------------------------------------------------------------------ */

static int g_fail;

static void check(int cond, const char *label)
{
    if (!cond) {
        fprintf(stderr, "engine-loop FAIL: %s\n", label);
        g_fail += 1;
    }
}

static int spawn(const char *root, int scen, pid_t *out_pid, int *out_ev,
                 int *out_ctl)
{
    int ev[2], ctl[2];
    pid_t pid;

    if (pipe(ev) != 0 || pipe(ctl) != 0)
        return -1;
    pid = fork();
    if (pid < 0)
        return -1;
    if (pid == 0) {
        close(ev[0]);
        close(ctl[1]);
        child_main(root, scen, ev[1], ctl[0]);
        _exit(0);
    }
    close(ev[1]);
    close(ctl[0]);
    *out_pid = pid;
    *out_ev = ev[0];
    *out_ctl = ctl[1];
    return 0;
}

static int finish_child(pid_t pid, int ev_r, int ctl_w)
{
    int status = 0, i;

    close(ev_r);
    if (ctl_w >= 0)
        close(ctl_w);
    kill(pid, SIGTERM);
    for (i = 0; i < 300; ++i) {
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid)
            return status;
        {
            struct timespec ts = {0, 10000000L};
            nanosleep(&ts, NULL);
        }
    }
    kill(pid, SIGKILL);
    (void)waitpid(pid, &status, 0);
    return status;
}

/* Poll the child event pipe for byte `want`.  Returns 0 on seen, -1 timeout
 * or EOF.  The budget is a wall-clock deadline, not only accumulated poll
 * timeouts: a child that keeps emitting events (for example the unbounded
 * pacing this harness must be able to catch) must still time out. */
static int wait_event(int fd, char want, int timeout_ms)
{
    struct timespec start;

    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0)
        return -1;
    for (;;) {
        struct pollfd p;
        char buf[256];
        ssize_t n;
        ssize_t i;
        struct timespec now;
        long elapsed;
        int r;

        p.fd = fd;
        p.events = POLLIN;
        p.revents = 0;
        r = poll(&p, 1, 100);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (r > 0) {
            n = read(fd, buf, sizeof(buf));
            if (n <= 0)
                return -1;
            for (i = 0; i < n; ++i)
                if (buf[i] == want)
                    return 0;
        }
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
            return -1;
        elapsed = (now.tv_sec - start.tv_sec) * 1000L +
                  (now.tv_nsec - start.tv_nsec) / 1000000L;
        if (elapsed >= (long)timeout_ms)
            return -1;
    }
}

static int connect_client(const char *path, int timeout_ms)
{
    int waited = 0;

    /* Never truncate silently: an AF_UNIX sun_path holds at most
     * sizeof(sun_path) - 1 bytes plus NUL, and a path that does not fit would
     * connect nowhere and report an opaque "connect failed" instead of the
     * real over-length cause. */
    if (strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
        check(0, "loop: socket path exceeds sun_path; shorten the scratch base");
        return -1;
    }
    while (waited < timeout_ms) {
        int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
        struct sockaddr_un addr;
        struct timeval tv = {5, 0};

        if (fd < 0)
            return -1;
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        (void)snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
        if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            return fd;
        }
        close(fd);
        {
            struct timespec ts = {0, 1000000L};
            nanosleep(&ts, NULL);
        }
        waited += 1;
    }
    return -1;
}

static int send_datagram(int fd, uint8_t type, const uint8_t *payload,
                         uint32_t plen)
{
    uint8_t buf[LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_MAX_PAYLOAD_BYTES];
    size_t total = LE_AUDIO_SINK_HEADER_BYTES + (size_t)plen;

    if (plen > LE_AUDIO_SINK_MAX_PAYLOAD_BYTES)
        return -1;
    le_audio_sink_encode_header(buf, type, plen);
    if (plen)
        memcpy(buf + LE_AUDIO_SINK_HEADER_BYTES, payload, plen);
    return send(fd, buf, total, 0) == (ssize_t)total ? 0 : -1;
}

static int recv_datagram(int fd, uint8_t *type, uint8_t *payload,
                         uint32_t *plen)
{
    uint8_t buf[LE_AUDIO_SINK_MAX_DATAGRAM_BYTES];
    struct le_audio_sink_header h;
    ssize_t n = recv(fd, buf, sizeof(buf), 0);

    if (n < (ssize_t)LE_AUDIO_SINK_HEADER_BYTES)
        return -1;
    if (le_audio_sink_decode_header(buf, (size_t)n, &h) != LE_AUDIO_SINK_OK)
        return -1;
    *type = h.type;
    if (plen)
        *plen = h.length;
    if (payload && h.length)
        memcpy(payload, buf + LE_AUDIO_SINK_HEADER_BYTES, h.length);
    return 0;
}

static uint32_t g_last_error;

static int expect_type(int fd, uint8_t want, uint8_t *payload, uint32_t *plen)
{
    int i;

    for (i = 0; i < 128; ++i) {
        uint8_t t;
        uint8_t tmp[64];
        uint32_t tl = 0;

        if (recv_datagram(fd, &t, tmp, &tl) != 0)
            return -1;
        if (t == LE_AUDIO_SINK_TYPE_ERROR) {
            if (tl >= 4u)
                g_last_error = le_audio_sink_get_u32(tmp + 0);
            continue;
        }
        if (t == want) {
            if (payload && tl)
                memcpy(payload, tmp, tl);
            if (plen)
                *plen = tl;
            return 0;
        }
    }
    return -1;
}

/* Wait for an ERROR reply; returns the status or -1 on timeout. */
static int expect_error(int fd)
{
    int i;

    g_last_error = 0xffffffffu;
    for (i = 0; i < 128; ++i) {
        uint8_t t;
        uint8_t tmp[64];
        uint32_t tl = 0;

        if (recv_datagram(fd, &t, tmp, &tl) != 0)
            return -1;
        if (t == LE_AUDIO_SINK_TYPE_ERROR) {
            if (tl >= 4u)
                g_last_error = le_audio_sink_get_u32(tmp + 0);
            return (int)g_last_error;
        }
    }
    return -1;
}

static int cl_open(int fd, uint32_t gen, uint32_t *epoch, uint32_t *status)
{
    uint8_t p[LE_AUDIO_SINK_OPEN_PAYLOAD_BYTES];
    uint8_t r[64];
    uint32_t rl = 0;
    int rc;

    memset(p, 0, sizeof(p));
    le_audio_sink_put_u32(p + 0, LE_AUDIO_SINK_SOURCE_SENDPIN);
    le_audio_sink_put_u32(p + 4, gen);
    le_audio_sink_put_u32(p + 8, LE_AUDIO_SINK_OUTPUT_RATE);
    le_audio_sink_put_u16(p + 12, LE_AUDIO_SINK_OUTPUT_CHANNELS);
    le_audio_sink_put_u16(p + 14, LE_AUDIO_SINK_FORMAT_S16_LE);
    if (send_datagram(fd, LE_AUDIO_SINK_TYPE_OPEN, p, sizeof(p)) != 0)
        return -1;
    rc = expect_type(fd, LE_AUDIO_SINK_TYPE_OPEN_ACK, r, &rl);
    if (rc != 0)
        return rc;
    *status = le_audio_sink_get_u32(r + 0);
    *epoch = le_audio_sink_get_u32(r + 4);
    return 0;
}

static int cl_data(int fd, uint32_t epoch, uint32_t gen, uint32_t seq,
                   uint64_t first, uint32_t frames, int16_t value, int wait_credit)
{
    uint8_t p[LE_AUDIO_SINK_DATA_PREFIX_BYTES + LE_AUDIO_SINK_MAX_DATA_FRAMES * 4u];
    uint8_t r[64];
    uint32_t rl = 0;
    uint32_t i;

    memset(p, 0, LE_AUDIO_SINK_DATA_PREFIX_BYTES);
    le_audio_sink_put_u32(p + 0, epoch);
    le_audio_sink_put_u32(p + 4, gen);
    le_audio_sink_put_u32(p + 8, seq);
    le_audio_sink_put_u64(p + 16, first);
    le_audio_sink_put_u32(p + 24, frames);
    for (i = 0; i < frames; ++i) {
        le_audio_sink_put_u16(p + LE_AUDIO_SINK_DATA_PREFIX_BYTES + i * 4u,
                              (uint16_t)value);
        le_audio_sink_put_u16(p + LE_AUDIO_SINK_DATA_PREFIX_BYTES + i * 4u + 2u,
                              (uint16_t)value);
    }
    if (send_datagram(fd, LE_AUDIO_SINK_TYPE_DATA, p,
                      LE_AUDIO_SINK_DATA_PREFIX_BYTES + frames * 4u) != 0)
        return -1;
    if (!wait_credit)
        return 0;
    return expect_type(fd, LE_AUDIO_SINK_TYPE_CREDIT, r, &rl);
}

static int cl_finish(int fd, uint32_t epoch, uint32_t gen, uint64_t total,
                     uint32_t seq)
{
    uint8_t p[LE_AUDIO_SINK_FINISH_PAYLOAD_BYTES];

    memset(p, 0, sizeof(p));
    le_audio_sink_put_u32(p + 0, epoch);
    le_audio_sink_put_u32(p + 4, gen);
    le_audio_sink_put_u64(p + 8, total);
    le_audio_sink_put_u32(p + 16, seq);
    return send_datagram(fd, LE_AUDIO_SINK_TYPE_FINISH, p, sizeof(p));
}

static int cl_progress(int fd, uint32_t epoch, uint32_t gen, uint8_t *out,
                       uint32_t *outlen)
{
    uint8_t p[LE_AUDIO_SINK_PROGRESS_REQ_PAYLOAD_BYTES];

    memset(p, 0, sizeof(p));
    le_audio_sink_put_u32(p + 0, epoch);
    le_audio_sink_put_u32(p + 4, gen);
    if (send_datagram(fd, LE_AUDIO_SINK_TYPE_PROGRESS_REQ, p, sizeof(p)) != 0)
        return -1;
    return expect_type(fd, LE_AUDIO_SINK_TYPE_PROGRESS, out, outlen);
}

/* Write a full hardware period to a legacy FIFO (non-blocking). */
static int aec_receiver_open(const char *root)
{
    char path[700];
    struct sockaddr_un addr;
    int fd;

    (void)snprintf(path, sizeof(path), "%s/aec-reference.sock", root);
    if (strlen(path) >= sizeof(addr.sun_path))
        return -1;
    fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    memcpy(addr.sun_path, path, strlen(path) + 1u);
    (void)unlink(path);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

/* Look for one published AEC reference packet carrying at least `bits`
 * (0 = any).  Proves the real le_aec_reference_publish path ran in the loop. */
static int aec_receiver_check(int fd, unsigned int bits)
{
    int i;

    for (i = 0; i < 16; ++i) {
        struct le_aec_reference_packet packet;
        struct pollfd p;
        ssize_t n;

        p.fd = fd;
        p.events = POLLIN;
        p.revents = 0;
        if (poll(&p, 1, 1000) <= 0)
            return -1;
        n = recv(fd, &packet, sizeof(packet), 0);
        if (n < (ssize_t)sizeof(packet.header))
            return -1;
        if (packet.header.magic != LE_AEC_REFERENCE_MAGIC ||
            packet.header.frames != PERIOD_SIZE ||
            packet.header.channels != 1u)
            return -1;
        if ((packet.header.activity_mask & bits) == bits)
            return 0;
    }
    return -1;
}

static int write_fifo_period(const char *root, const char *name,
                             const int16_t *pattern, size_t frames)
{
    char path[600];
    int fd;
    int i;
    size_t total = frames * LE_AUDIO_SINK_OUTPUT_CHANNELS;
    uint8_t buf[2048u * 4u];

    (void)snprintf(path, sizeof(path), "%s/%s", root, name);
    for (i = 0; i < 2000; ++i) {
        fd = open(path, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
        if (fd >= 0)
            break;
        {
            struct timespec ts = {0, 1000000L};
            nanosleep(&ts, NULL);
        }
    }
    if (fd < 0)
        return -1;
    for (i = 0; i < (int)total; ++i)
        ((int16_t *)buf)[i] = pattern[i];
    if ((size_t)frames * 4u > sizeof(buf))
        return -1;
    if (write(fd, buf, total * sizeof(int16_t)) < 0) {
        close(fd);
        return -1;
    }
    close(fd);
    return 0;
}

/* ------------------------------------------------------------------ */
/* Log loading + assertions                                           */
/* ------------------------------------------------------------------ */

static struct engine_log g_log;

static int load_log(const char *root)
{
    char path[600];
    int fd;
    ssize_t n;

    memset(&g_log, 0, sizeof(g_log));
    (void)snprintf(path, sizeof(path), "%s/engine.log", root);
    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    n = read(fd, &g_log, sizeof(g_log));
    close(fd);
    if (n < (ssize_t)offsetof(struct engine_log, ev) ||
        g_log.magic != LOG_MAGIC)
        return -1;
    if (g_log.count > EV_MAX)
        g_log.count = EV_MAX;
    return 0;
}

static int count_tag(uint32_t tag)
{
    uint32_t i;
    int c = 0;

    for (i = 0; i < g_log.count; ++i)
        if (g_log.ev[i].tag == tag)
            c += 1;
    return c;
}

static int index_tag(uint32_t tag, int occurrence)
{
    uint32_t i;
    int c = 0;

    for (i = 0; i < g_log.count; ++i) {
        if (g_log.ev[i].tag == tag) {
            if (c == occurrence)
                return (int)i;
            c += 1;
        }
    }
    return -1;
}

static int count_mix(uint32_t name, uint32_t code)
{
    uint32_t i;
    int c = 0;

    for (i = 0; i < g_log.count; ++i)
        if (g_log.ev[i].tag == T_MIX_ENUM && g_log.ev[i].a == name &&
            g_log.ev[i].b == code)
            c += 1;
    return c;
}

static int last_mix_index(void)
{
	int i;
	for (i = (int)g_log.count - 1; i >= 0; --i) {
		if (g_log.ev[i].tag == T_MIX_ENUM || g_log.ev[i].tag == T_MIX_SET)
			return i;
	}
	return -1;
}

/* Count intercepted path denials by kind (1 = open, 2 = connect). */
static int count_audit_kind(uint32_t kind)
{
	uint32_t i;
	int c = 0;

	for (i = 0; i < g_log.count; ++i)
		if (g_log.ev[i].tag == T_AUDIT_DENY && g_log.ev[i].a == kind)
			c += 1;
	return c;
}

/* Total negative samples across every period written to the fake PCM. */
static unsigned int count_negative_samples(void)
{
	uint32_t i;
	unsigned int total = 0u;

	for (i = 0; i < g_log.count; ++i)
		if (g_log.ev[i].tag == T_PCM_WRITE_NEG)
			total += g_log.ev[i].a;
	return total;
}

/* check() with a "<scenario>: <message>" label built at run time. */
static void checkf(const char *tag, const char *message, int cond)
{
	char label[192];

	if (cond)
		return;
	(void)snprintf(label, sizeof(label), "%s: %s", tag, message);
	check(0, label);
}

/* ------------------------------------------------------------------ */
/* Scenarios                                                          */
/* ------------------------------------------------------------------ */

static const char *const g_scen_names[] = {
    "", "prime", "finish", "legacy", "epipe", "restart", "mixfail",
    "prepfail", "stop", "media", "pcmfail", "notiming", "notiminglegacy",
    "queryfail"
};

static void make_root(const char *base, const char *name, char *out,
                      size_t outlen)
{
    char vol[700];
    int fd;

    (void)snprintf(out, outlen, "%s/%s", base, name);
    (void)mkdir(out, 0770);
    /* A concrete logical master keeps the renderer/DSP path audible so the
     * loop's content checks are meaningful. */
    (void)snprintf(vol, sizeof(vol), "%s/master.volume", out);
    fd = open(vol, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0640);
    if (fd >= 0) {
        ssize_t ignored = write(fd, "80\n", 3);
        (void)ignored;
        close(fd);
    }
}

/* Scenario 1: socket OPEN with every legacy FIFO idle -> priming reaches the
 * PCM and produces bounded paced silence with no FIFO period ever ready. */
static void scen_prime(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, i;
    uint32_t epoch = 0, status = 0;
    uint8_t prog[64];
    uint32_t proglen = 0;
    int writes;

    make_root(base, g_scen_names[SCEN_PRIME], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_PRIME, &pid, &ev, &ctl) != 0) {
        check(0, "prime: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "prime: real SOCK_SEQPACKET connect failed");
    if (fd >= 0) {
        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u &&
              epoch != 0u,
              "prime: OPEN was not served while every FIFO was idle");
        check(wait_event(ev, 'O', 5000) == 0,
              "prime: idle-socket generation never reached the PCM");
        check(wait_event(ev, 'C', 8000) == 0,
              "prime: bounded priming never closed the PCM");
        check(cl_progress(fd, epoch, 1u, prog, &proglen) == 0,
              "prime: PROGRESS was not served");
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "prime: engine log missing");
    check(g_log.audit_denied == 0u,
          "prime: the loop reached an external /sys /dev /run /data path");
    writes = count_tag(T_PCM_WRITE);
    check(count_tag(T_PCM_OPEN) == 1, "prime: PCM was not opened exactly once");
    check(count_tag(T_PCM_CLOSE) == 1, "prime: PCM was not closed");
    check(count_tag(T_PCM_PREPARE) == 1, "prime: PCM was not prepared once");
    check(writes >= 2 && writes <= (int)SENDPIN_PRIME_LIMIT + 4,
          "prime: priming silence was not bounded");
    for (i = 0; i < (int)g_log.count; ++i) {
        if (g_log.ev[i].tag == T_PCM_WRITE)
            check((g_log.ev[i].c & 0xffffu) == 0u &&
                  (g_log.ev[i].c >> 16) == 0u,
                  "prime: priming write was not silence");
    }
    /* geometry + flags actually used by the loop */
    {
        int o = index_tag(T_PCM_OPEN, 0);
        check(o >= 0 &&
              g_log.ev[o].a == (PCM_OUT | PCM_MONOTONIC | PCM_NORESTART),
              "prime: PCM opened without PCM_OUT|PCM_MONOTONIC|PCM_NORESTART");
        check(o >= 0 && g_log.ev[o].b == OUTPUT_CHANNELS && g_log.ev[o].c == DEFAULT_RATE,
              "prime: PCM geometry channels/rate wrong");
        check(o >= 0 && (g_log.ev[o].d & 0xffff) == (int)PERIOD_SIZE &&
              ((g_log.ev[o].d >> 16) & 0xffff) == (int)PERIOD_COUNT,
              "prime: PCM period_size/period_count wrong");
    }
    /* armed muted, amp on while muted, then unmuted, then disabled on teardown */
    check(count_mix(C_MUTE, 1u) >= 1, "prime: mute was never asserted before amp");
    check(count_mix(C_AMP, 1u) >= 1, "prime: amp was never enabled");
    check(count_mix(C_MUTE, 0u) >= 1, "prime: output was never unmuted");
    {
        int l = last_mix_index();
        check(l >= 0 && g_log.ev[l].tag == T_MIX_ENUM &&
              g_log.ev[l].a == C_AMP && g_log.ev[l].b == 0u,
              "prime: teardown did not end with amp off");
        if (l >= 1)
            check(g_log.ev[l - 1].tag == T_MIX_ENUM &&
                  g_log.ev[l - 1].a == C_MUTE && g_log.ev[l - 1].b == 1u,
                  "prime: teardown did not mute before removing amp power");
    }
}

/* Scenario 2: DATA short exact tail + FINISH -> physical completion, PCM
 * closes, no further DATA. */
static void scen_finish(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd;
    uint32_t epoch = 0, status = 0;
    uint8_t prog[64];
    uint32_t proglen = 0;

    make_root(base, g_scen_names[SCEN_FINISH], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_FINISH, &pid, &ev, &ctl) != 0) {
        check(0, "finish: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "finish: connect failed");
    if (fd >= 0) {
        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "finish: OPEN failed");
        check(cl_data(fd, epoch, 1u, 0u, 0u, 512u, 2000, 1) == 0,
              "finish: short DATA period was not credited");
        check(cl_finish(fd, epoch, 1u, 512u, 0u) == 0,
              "finish: FINISH send failed");
        check(wait_event(ev, 'C', 8000) == 0,
              "finish: completed generation never closed the PCM");
        check(cl_progress(fd, epoch, 1u, prog, &proglen) == 0,
              "finish: PROGRESS not served after completion");
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "finish: engine log missing");
    check(g_log.audit_denied == 0u,
          "finish: the loop reached an external /sys /dev /run /data path");
    check(count_tag(T_PCM_OPEN) == 1 && count_tag(T_PCM_CLOSE) == 1,
          "finish: PCM did not open/close once");
    check(count_tag(T_PCM_WRITE) >= 1 && count_tag(T_PCM_WRITE) <= 2,
          "finish: unexpected number of periods after FINISH");
    if (proglen == LE_AUDIO_SINK_PROGRESS_PAYLOAD_BYTES) {
        uint64_t submitted = le_audio_sink_get_u64(prog + 16);
        uint64_t played = le_audio_sink_get_u64(prog + 32);
        check(submitted == 512u,
              "finish: physical completion did not stop at the exact source tail");
        check(played == 512u,
              "finish: playhead did not reach the exact FINISH tail");
    }
}

/* Scenario 3: legacy-only FIFO accumulation (two half-period writes per
 * period) preserves both halves with no zero padding; AEC reference and LED
 * (announcement) paths stay on the real loop. */
static void scen_legacy(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, aec;
    int16_t pat_a[PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    int16_t pat_b[PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    int16_t pat_c[PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    int16_t pat_d[PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    unsigned int i;
    int n_first, n_second;

    make_root(base, g_scen_names[SCEN_LEGACY], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    aec = aec_receiver_open(root);
    for (i = 0; i < PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS; ++i) {
        pat_a[i] = (int16_t)3000;
        pat_b[i] = (int16_t)-3000;
        pat_c[i] = (int16_t)1500;
        pat_d[i] = (int16_t)-1500;
    }
    if (spawn(root, SCEN_LEGACY, &pid, &ev, &ctl) != 0) {
        check(0, "legacy: spawn failed");
        return;
    }
    /* Announcement periods are queued first so the higher-priority bus stays
     * live for every rendered period; the real LED transport call is then
     * made and denied, and the visualizer path is not fed raw media. */
    check(write_fifo_period(root, "announcement.pcm", pat_a, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "announcement.pcm", pat_a, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "announcement.pcm", pat_a, PERIOD_SIZE) == 0,
          "legacy: announcement period writes failed");
    /* Two full media periods delivered as four half-period writes. */
    check(write_fifo_period(root, "media.pcm", pat_a, PERIOD_SIZE / 2u) == 0 &&
          write_fifo_period(root, "media.pcm", pat_b, PERIOD_SIZE / 2u) == 0 &&
          write_fifo_period(root, "media.pcm", pat_c, PERIOD_SIZE / 2u) == 0 &&
          write_fifo_period(root, "media.pcm", pat_d, PERIOD_SIZE / 2u) == 0,
          "legacy: half-period FIFO writes failed");
    check(wait_event(ev, 'W', 8000) == 0, "legacy: no legacy period reached the PCM");
    check(aec >= 0 && aec_receiver_check(aec, PLAYBACK_BUS_ANNOUNCEMENT) == 0,
          "legacy: the AEC reference / priority activity mask path was not exercised");
    if (aec >= 0)
        close(aec);
    check(wait_event(ev, 'C', 8000) == 0, "legacy: PCM was not closed");
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "legacy: engine log missing");
    check(count_tag(T_PCM_WRITE) >= 1, "legacy: no PCM period written");
    {
        int w = index_tag(T_PCM_WRITE, 0);
        n_first = w >= 0 ? (int)(g_log.ev[w].c & 0xffffu) : -1;
        n_second = w >= 0 ? (int)(g_log.ev[w].c >> 16) : -1;
        check(n_first > 0 && n_second > 0,
              "legacy: a half-period was zero-padded into the rendered period");
    }
    check(g_log.audit_denied >= 1u,
          "legacy: the engine LED /run path was not intercepted and denied");
    check(count_mix(C_CHCFG, 2u) >= 1,
          "legacy: stereo board channel config was not selected");
}

/* Scenario 4/4b: an injected XRUN mid-period tears the loop down, keeps the old
 * generation inactive across real polling, and a higher OPEN recovers with no
 * replay of the predecessor's frames.  The fault is always delivered as
 * pcm_writei()==-1 with errno set (never a -errno return); the ESTRPIPE errno
 * is exercised in addition to EPIPE. */
static void scen_xrun(const char *base, char ctl_cmd, const char *tag)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, i;
    uint32_t epoch1 = 0, status = 0, epoch2 = 0;
    int write2 = -1;

    make_root(base, tag, root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_EPIPE, &pid, &ev, &ctl) != 0) {
        checkf(tag, "spawn failed", 0);
        return;
    }
    fd = connect_client(sock, 3000);
    checkf(tag, "connect failed", fd >= 0);
    if (fd >= 0) {
        checkf(tag, "OPEN gen1 failed",
               cl_open(fd, 1u, &epoch1, &status) == 0 && status == 0u);
        /* Arm the injected failure before any write, then queue real source. */
        checkf(tag, "fail arm failed", write(ctl, &ctl_cmd, 1) == 1);
        checkf(tag, "gen1 DATA send failed",
               cl_data(fd, epoch1, 1u, 0u, 0u, PERIOD_SIZE, 1000, 0) == 0);
        checkf(tag, "injected failure did not tear down the PCM",
               wait_event(ev, 'C', 6000) == 0);
        /* Old generation must stay inactive across the loop's own polling. */
        checkf(tag, "the fenced generation was re-adopted at the next poll",
               cl_data(fd, epoch1, 1u, 1u, PERIOD_SIZE, 64u, 1000, 0) == 0 &&
               expect_error(fd) == LE_AUDIO_SINK_ERR_STALE_GENERATION);
        /* A fresh strictly higher OPEN recovers. */
        checkf(tag, "higher OPEN did not recover",
               cl_open(fd, 2u, &epoch2, &status) == 0 && status == 0u);
        checkf(tag, "epoch changed within the same engine", epoch2 == epoch1);
        checkf(tag, "recovery DATA was not credited",
               cl_data(fd, epoch2, 2u, 0u, 0u, PERIOD_SIZE, 20000, 1) == 0);
        checkf(tag, "recovery generation never reached the PCM",
               wait_event(ev, 'W', 6000) == 0);
        {
            uint8_t p2[64];
            uint32_t p2len = 0;
            if (cl_progress(fd, epoch2, 2u, p2, &p2len) == 0 &&
                p2len == LE_AUDIO_SINK_PROGRESS_PAYLOAD_BYTES)
                checkf(tag, "recovery replayed predecessor frames into the new generation",
                       le_audio_sink_get_u64(p2 + 16) == PERIOD_SIZE);
        }
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    checkf(tag, "engine log missing", load_log(root) == 0);
    checkf(tag, "the PCM was not reopened for the recovery generation",
           count_tag(T_PCM_OPEN) >= 2);
    for (i = 0; i < (int)g_log.count; ++i)
        if (g_log.ev[i].tag == T_PCM_WRITE &&
            (g_log.ev[i].c & 0xffffu) != 0u && write2 < 0)
            write2 = i;
    checkf(tag, "the recovery generation never carried its own payload",
           write2 >= 0);
}

static void scen_epipe(const char *base)
{
    scen_xrun(base, 'F', "epipe");
}

static void scen_estripe(const char *base)
{
    scen_xrun(base, 'S', "estripe");
}

/* Scenario 5: engine restart -> a new epoch; the old epoch is rejected and a
 * fresh generation is admitted. */
static void scen_restart(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd;
    uint32_t epoch1 = 0, epoch2 = 0, status = 0;

    make_root(base, g_scen_names[SCEN_RESTART], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_RESTART, &pid, &ev, &ctl) != 0) {
        check(0, "restart: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "restart: first connect failed");
    if (fd >= 0) {
        check(cl_open(fd, 5u, &epoch1, &status) == 0 && status == 0u &&
              epoch1 != 0u,
              "restart: first engine OPEN failed");
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);

    /* Second engine instance on the same private root. */
    if (spawn(root, SCEN_RESTART, &pid, &ev, &ctl) != 0) {
        check(0, "restart: second spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "restart: second connect failed");
    if (fd >= 0) {
        check(cl_data(fd, epoch1, 5u, 0u, 0u, 64u, 500, 0) == 0 &&
              expect_error(fd) == LE_AUDIO_SINK_ERR_STALE_EPOCH,
              "restart: a stale-epoch DATA reached the new engine");
        check(cl_open(fd, 1u, &epoch2, &status) == 0 && status == 0u &&
              epoch2 != 0u,
              "restart: fresh generation rejected after restart");
        check(epoch2 != epoch1,
              "restart: restarted engine reused the previous epoch");
        check(cl_data(fd, epoch2, 1u, 0u, 0u, 64u, 700, 1) == 0,
              "restart: fresh DATA was not credited");
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "restart: engine log missing");
}

/* Scenario 6: mixer-open failure -> no PCM is ever opened and the loop keeps
 * running. */
static void scen_mixfail(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, i;
    uint32_t epoch = 0, status = 0;

    make_root(base, g_scen_names[SCEN_MIXFAIL], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_MIXFAIL, &pid, &ev, &ctl) != 0) {
        check(0, "mixfail: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "mixfail: connect failed");
    if (fd >= 0) {
        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "mixfail: OPEN failed");
        /* Give the loop several serviced polls, then stop. */
        for (i = 0; i < 3; ++i) {
            uint8_t p[64];
            uint32_t pl = 0;
            (void)cl_progress(fd, epoch, 1u, p, &pl);
        }
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "mixfail: engine log missing");
    check(count_tag(T_PCM_OPEN) == 0,
          "mixfail: PCM was opened even though the mixer was unavailable");
}

/* Scenario 7: PCM-prepare failure after a successful open -> teardown mutes
 * then removes amplifier power. */
static void scen_prepfail(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, i;
    uint32_t epoch = 0, status = 0;
    int saw_open = 0;
    int lastl = -1;

    make_root(base, g_scen_names[SCEN_PREPFAIL], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_PREPFAIL, &pid, &ev, &ctl) != 0) {
        check(0, "prepfail: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "prepfail: connect failed");
    if (fd >= 0) {
        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "prepfail: OPEN failed");
        check(wait_event(ev, 'O', 5000) == 0, "prepfail: PCM not opened");
        for (i = 0; i < 2; ++i) {
            uint8_t p[64];
            uint32_t pl = 0;
            (void)cl_progress(fd, epoch, 1u, p, &pl);
        }
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "prepfail: engine log missing");
    check(count_tag(T_PCM_PREPARE) >= 1 && count_tag(T_PCM_CLOSE) >= 1,
          "prepfail: a failed prepare did not close the opened PCM");
    for (i = 0; i < (int)g_log.count; ++i)
        if (g_log.ev[i].tag == T_PCM_OPEN)
            saw_open = 1;
    check(saw_open, "prepfail: PCM was not opened");
    lastl = last_mix_index();
    check(lastl >= 1 && g_log.ev[lastl].tag == T_MIX_ENUM &&
          g_log.ev[lastl].a == C_AMP && g_log.ev[lastl].b == 0u &&
          g_log.ev[lastl - 1].a == C_MUTE && g_log.ev[lastl - 1].b == 1u,
          "prepfail: teardown did not mute before removing amplifier power");
}

/* Scenario 7: SIGTERM during an active stream -> the stop path mutes before
 * dropping amplifier power and closes the PCM. */
static void scen_stop(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd;
    uint32_t epoch = 0, status = 0, proglen = 0;
    uint8_t prog[64];
    uint64_t submitted = 0;
    int lastl;

    make_root(base, g_scen_names[SCEN_STOP], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_STOP, &pid, &ev, &ctl) != 0) {
        check(0, "stop: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "stop: connect failed");
    if (fd >= 0) {
        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "stop: OPEN failed");
        check(cl_data(fd, epoch, 1u, 0u, 0u, PERIOD_SIZE, 4000, 1) == 0,
              "stop: DATA was not credited");
        check(wait_event(ev, 'W', 6000) == 0,
              "stop: the active stream never wrote a period");
        check(cl_progress(fd, epoch, 1u, prog, &proglen) == 0,
              "stop: PROGRESS not served during the active stream");
        if (proglen == LE_AUDIO_SINK_PROGRESS_PAYLOAD_BYTES)
            submitted = le_audio_sink_get_u64(prog + 16);
        close(fd);
    }
    check(submitted > 0u, "stop: active stream did not advance the source cursor");
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "stop: engine log missing");
    check(count_tag(T_PCM_CLOSE) >= 1, "stop: PCM was not closed on stop");
    lastl = last_mix_index();
    check(lastl >= 1 && g_log.ev[lastl].tag == T_MIX_ENUM &&
          g_log.ev[lastl].a == C_AMP && g_log.ev[lastl].b == 0u &&
          g_log.ev[lastl - 1].a == C_MUTE && g_log.ev[lastl - 1].b == 1u,
          "stop: stop path did not mute before removing amplifier power");
}

/* Scenario 8: media-only playback with no priority bus.  With the announcement
 * bus silent the loop really enters the music visualizer on the rendered
 * (negative) programme, so under UBSan the analyzer's old signed shift would
 * abort right here.  The LED transport call the visualizer makes is the only
 * external path touched, which proves the visualizer ran. */
static void scen_media(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, i;
    int16_t pattern[PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    int writes;

    make_root(base, g_scen_names[SCEN_MEDIA], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    /* A 500 Hz bipolar tone: the rendered period carries real negative
     * samples into the visualizer (a constant offset could be high-passed
     * away by the speaker DSP, dodging the negative shift). */
    for (i = 0; i < (int)(PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS); ++i) {
        unsigned int frame =
            (unsigned int)i / LE_AUDIO_SINK_OUTPUT_CHANNELS;
        double phase = 2.0 * 3.14159265358979323846 * 500.0 *
            (double)frame / 48000.0;

        pattern[i] = (int16_t)lround(-12000.0 * sin(phase));
    }
    if (spawn(root, SCEN_MEDIA, &pid, &ev, &ctl) != 0) {
        check(0, "media: spawn failed");
        return;
    }
    check(write_fifo_period(root, "media.pcm", pattern, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pattern, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pattern, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pattern, PERIOD_SIZE) == 0,
          "media: media.pcm period writes failed");
    check(wait_event(ev, 'W', 8000) == 0,
          "media: no media period reached the PCM");
    check(wait_event(ev, 'C', 8000) == 0,
          "media: PCM was not closed after the bounded media stream");
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "media: engine log missing");
    /* The visualizer is the only component that talks to the LED socket on
     * this media-only scenario, and the LED path is the only external path
     * reached (as a connect denial). */
    check(count_audit_kind(2u) >= 1,
          "media: the music visualizer LED frame path was never exercised");
    check(count_audit_kind(1u) == 0,
          "media: an unexpected external open path was reached");
    check(count_negative_samples() > 0u,
          "media: no negative PCM reached the rendered/visualizer buffer");
    writes = count_tag(T_PCM_WRITE);
    check(writes >= 1, "media: no PCM period written");
    check(count_tag(T_PCM_OPEN) == 1 && count_tag(T_PCM_CLOSE) == 1,
          "media: PCM did not open/close once");
    check(count_mix(C_CHCFG, 2u) >= 1,
          "media: stereo board channel config was not selected");
}

/* Scenario 9: the PCM cannot be opened at all.  The loop must attempt the
 * open, never credit it as a usable stream, tear the amplifier down in the
 * mute-before-amp-off order, and not spin retrying the failed open. */
static void scen_pcmfail(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, i, lastl;
    uint32_t epoch = 0, status = 0;
    int attempts;

    make_root(base, g_scen_names[SCEN_PCMFAIL], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_PCMFAIL, &pid, &ev, &ctl) != 0) {
        check(0, "pcmfail: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "pcmfail: connect failed");
    if (fd >= 0) {
        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "pcmfail: OPEN failed");
        check(wait_event(ev, 'X', 6000) == 0,
              "pcmfail: the loop never attempted the PCM open");
        /* Several serviced polls, then stop. */
        for (i = 0; i < 3; ++i) {
            uint8_t p[64];
            uint32_t pl = 0;
            (void)cl_progress(fd, epoch, 1u, p, &pl);
        }
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "pcmfail: engine log missing");
    check(count_tag(T_PCM_OPEN) == 0,
          "pcmfail: a failed pcm_open was credited as a usable stream");
    attempts = count_tag(T_PCM_OPEN_FAIL);
    check(attempts >= 1, "pcmfail: the PCM open was never attempted");
    check(attempts <= 8,
          "pcmfail: the engine spun retrying a failed PCM open");
    check(count_tag(T_PCM_WRITE) == 0,
          "pcmfail: a period was written through an unopened PCM");
    /* Arm set mute + amp off; the failure teardown must leave the same safe
     * order (mute before amplifier power is removed). */
    lastl = last_mix_index();
    check(lastl >= 1 && g_log.ev[lastl].tag == T_MIX_ENUM &&
          g_log.ev[lastl].a == C_AMP && g_log.ev[lastl].b == 0u &&
          g_log.ev[lastl - 1].a == C_MUTE && g_log.ev[lastl - 1].b == 1u,
          "pcmfail: teardown did not mute before removing amplifier power");
    check(count_mix(C_MUTE, 1u) >= 2 && count_mix(C_AMP, 0u) >= 2,
          "pcmfail: the output was not armed and re-disabled on the failure path");
}

/* Scenario 10: the timing model is unavailable after the PCM starts.  The
 * opened PCM reports a zero-frame buffer, so le_audio_timing_create() returns
 * NULL and the engine keeps running without a DAC timeline.  A FINISHed
 * generation can then never be observed as physically complete, so it must be
 * failed closed: the exact generation is cancelled with an explicit timing
 * error and the PCM is retired to the legacy path instead of pacing generated
 * silence forever.  No completion/playhead may be fabricated, the cancelled
 * generation must stay rejected (a successor, not a replay, is required), and
 * a fresh generation must still be admitted for recovery. */
static void scen_notiming(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, l;
    uint32_t epoch = 0, status = 0, epoch2 = 0;
    int writes;

    make_root(base, g_scen_names[SCEN_NOTIMING], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_NOTIMING, &pid, &ev, &ctl) != 0) {
        check(0, "notiming: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "notiming: connect failed");
    if (fd >= 0) {
        uint8_t pr[LE_AUDIO_SINK_PROGRESS_REQ_PAYLOAD_BYTES];

        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "notiming: OPEN was not served");
        check(cl_data(fd, epoch, 1u, 0u, 0u, 512u, 1000, 1) == 0,
              "notiming: source DATA was not credited");
        /* A live (unfinished) generation must still drive PCM periods with no
         * timing model: priming must not be rejected outright. */
        check(wait_event(ev, 'W', 8000) == 0,
              "notiming: a live generation without a timing model was never paced");
        check(cl_finish(fd, epoch, 1u, 512u, 0u) == 0,
              "notiming: FINISH send failed");
        /* Without timing the tail cannot be observed: the loop must fail the
         * generation closed and retire the PCM within a bounded window. */
        check(wait_event(ev, 'C', 8000) == 0,
              "notiming: a FINISHed generation without a timing model paced the PCM forever");
        /* A cancelled generation must not report live progress: no fabricated
         * completion or playhead. */
        memset(pr, 0, sizeof(pr));
        le_audio_sink_put_u32(pr + 0, epoch);
        le_audio_sink_put_u32(pr + 4, 1u);
        check(send_datagram(fd, LE_AUDIO_SINK_TYPE_PROGRESS_REQ, pr,
                            sizeof(pr)) == 0 &&
              expect_error(fd) == LE_AUDIO_SINK_ERR_STALE_GENERATION,
              "notiming: the failed generation still reported live progress");
        /* The cancelled generation must stay rejected; recovery needs a
         * fresh, strictly higher generation. */
        check(cl_data(fd, epoch, 1u, 1u, 512u, 64u, 1000, 0) == 0 &&
              expect_error(fd) == LE_AUDIO_SINK_ERR_STALE_GENERATION,
              "notiming: a cancelled generation was revived by late DATA");
        check(cl_open(fd, 2u, &epoch2, &status) == 0 && status == 0u &&
              epoch2 == epoch,
              "notiming: a fresh generation was not admitted for recovery");
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "notiming: engine log missing");
    check(g_log.audit_denied == 0u,
          "notiming: the loop reached an external /sys /dev /run /data path");
    writes = count_tag(T_PCM_WRITE);
    check(writes >= 1,
          "notiming: no PCM period was written before the fail-closed retire");
    check(writes <= 2 * (int)SENDPIN_PRIME_LIMIT + 16,
          "notiming: a timing-less FINISH was paced without limit");
    check(count_tag(T_PCM_OPEN) >= 1 && count_tag(T_PCM_OPEN) <= 2,
          "notiming: the engine reopened the PCM in a spin after the fail-closed retire");
    check(count_tag(T_PCM_CLOSE) >= 1, "notiming: PCM was not retired");
    l = last_mix_index();
    check(l >= 1 && g_log.ev[l].tag == T_MIX_ENUM &&
          g_log.ev[l].a == C_AMP && g_log.ev[l].b == 0u &&
          g_log.ev[l - 1].a == C_MUTE && g_log.ev[l - 1].b == 1u,
          "notiming: teardown did not mute before removing amplifier power");
}

/* Scenario 11: a legacy FIFO session is already streaming when a Sendspin OPEN
 * arrives and FINISHes, and the running PCM has no timing model.  The
 * generation must still be failed closed, but legacy playback must continue:
 * the loop keeps mixing/ writing the legacy bus instead of starving or
 * spinning on the unobservable Sendspin tail. */
static void scen_notiming_legacy(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, i, l;
    uint32_t epoch = 0, status = 0;
    int16_t pat[PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    int nz_writes = 0;
    uint32_t idx;

    make_root(base, g_scen_names[SCEN_NOTIMING_LEGACY], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    /* A 500 Hz bipolar tone keeps real (nonzero) samples in the rendered
     * period, so the legacy path is distinguishable from generated silence. */
    for (i = 0; i < (int)(PERIOD_SIZE * LE_AUDIO_SINK_OUTPUT_CHANNELS); ++i) {
        unsigned int frame = (unsigned int)i / LE_AUDIO_SINK_OUTPUT_CHANNELS;
        double phase = 2.0 * 3.14159265358979323846 * 500.0 *
            (double)frame / 48000.0;

        pat[i] = (int16_t)lround(-12000.0 * sin(phase));
    }
    if (spawn(root, SCEN_NOTIMING_LEGACY, &pid, &ev, &ctl) != 0) {
        check(0, "notiminglegacy: spawn failed");
        return;
    }
    check(write_fifo_period(root, "media.pcm", pat, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pat, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pat, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pat, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pat, PERIOD_SIZE) == 0 &&
          write_fifo_period(root, "media.pcm", pat, PERIOD_SIZE) == 0,
          "notiminglegacy: legacy FIFO writes failed");
    check(wait_event(ev, 'W', 8000) == 0,
          "notiminglegacy: the legacy stream never reached the PCM");
    /* OPEN while the legacy PCM (no timing model) is already streaming. */
    fd = connect_client(sock, 3000);
    check(fd >= 0, "notiminglegacy: connect failed");
    if (fd >= 0) {
        uint8_t pr[LE_AUDIO_SINK_PROGRESS_REQ_PAYLOAD_BYTES];

        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "notiminglegacy: OPEN while legacy playing failed");
        check(cl_data(fd, epoch, 1u, 0u, 0u, 512u, 1000, 1) == 0,
              "notiminglegacy: source DATA was not credited");
        check(cl_finish(fd, epoch, 1u, 512u, 0u) == 0,
              "notiminglegacy: FINISH send failed");
        check(wait_event(ev, 'C', 10000) == 0,
              "notiminglegacy: the timing-less Sendspin tail kept the PCM paced forever");
        memset(pr, 0, sizeof(pr));
        le_audio_sink_put_u32(pr + 0, epoch);
        le_audio_sink_put_u32(pr + 4, 1u);
        check(send_datagram(fd, LE_AUDIO_SINK_TYPE_PROGRESS_REQ, pr,
                            sizeof(pr)) == 0 &&
              expect_error(fd) == LE_AUDIO_SINK_ERR_STALE_GENERATION,
              "notiminglegacy: the failed Sendspin generation still reported live progress");
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "notiminglegacy: engine log missing");
    /* The media visualizer's LED `connect` is an intended denial on this
     * path; no external open() may be reached. */
    check(count_audit_kind(1u) == 0,
          "notiminglegacy: an unexpected external open path was reached");
    for (idx = 0u; idx < g_log.count; ++idx)
        if (g_log.ev[idx].tag == T_PCM_WRITE &&
            ((g_log.ev[idx].c & 0xffffu) != 0u ||
             (g_log.ev[idx].c >> 16) != 0u))
            nz_writes += 1;
    check(nz_writes >= 1,
          "notiminglegacy: legacy audio stopped reaching the PCM at the Sendspin OPEN");
    check(count_tag(T_PCM_WRITE) <= 2 * (int)SENDPIN_PRIME_LIMIT + 32,
          "notiminglegacy: the loop paced an unbounded number of periods");
    check(count_tag(T_PCM_CLOSE) >= 1, "notiminglegacy: PCM was not retired");
    l = last_mix_index();
    check(l >= 1 && g_log.ev[l].tag == T_MIX_ENUM &&
          g_log.ev[l].a == C_AMP && g_log.ev[l].b == 0u &&
          g_log.ev[l - 1].a == C_MUTE && g_log.ev[l - 1].b == 1u,
          "notiminglegacy: teardown did not mute before removing amplifier power");
}

/* Scenario 12 (control, reviewer F2): the timing model IS present but
 * pcm_get_htimestamp() fails persistently.  This path is already bounded by
 * the fixed timing ledger: the generated padding fills LE_AUDIO_LEDGER_CAPACITY,
 * the accounting refuses, and the generation is CANCELLED -- never a
 * fabricated completion and never a wall-clock fallback.  The exact source
 * tail is not replayed.  This guards the timing-present path while the
 * no-timing path is fixed. */
static void scen_queryfail(const char *base)
{
    char root[600];
    char sock[700];
    pid_t pid;
    int ev, ctl, fd, l;
    uint32_t epoch = 0, status = 0;
    int writes;

    make_root(base, g_scen_names[SCEN_QUERYFAIL], root, sizeof(root));
    (void)snprintf(sock, sizeof(sock), "%s/sendspin.sock", root);
    if (spawn(root, SCEN_QUERYFAIL, &pid, &ev, &ctl) != 0) {
        check(0, "queryfail: spawn failed");
        return;
    }
    fd = connect_client(sock, 3000);
    check(fd >= 0, "queryfail: connect failed");
    if (fd >= 0) {
        uint8_t pr[LE_AUDIO_SINK_PROGRESS_REQ_PAYLOAD_BYTES];

        check(cl_open(fd, 1u, &epoch, &status) == 0 && status == 0u,
              "queryfail: OPEN failed");
        check(cl_data(fd, epoch, 1u, 0u, 0u, 512u, 1000, 1) == 0,
              "queryfail: source DATA was not credited");
        check(cl_finish(fd, epoch, 1u, 512u, 0u) == 0,
              "queryfail: FINISH send failed");
        check(wait_event(ev, 'C', 8000) == 0,
              "queryfail: a persistent timestamp-query failure paced the PCM without bound");
        memset(pr, 0, sizeof(pr));
        le_audio_sink_put_u32(pr + 0, epoch);
        le_audio_sink_put_u32(pr + 4, 1u);
        check(send_datagram(fd, LE_AUDIO_SINK_TYPE_PROGRESS_REQ, pr,
                            sizeof(pr)) == 0 &&
              expect_error(fd) == LE_AUDIO_SINK_ERR_STALE_GENERATION,
              "queryfail: a query-failed generation was reported completed/valid");
        close(fd);
    }
    (void)finish_child(pid, ev, ctl);
    check(load_log(root) == 0, "queryfail: engine log missing");
    check(g_log.audit_denied == 0u,
          "queryfail: the loop reached an external /sys /dev /run /data path");
    writes = count_tag(T_PCM_WRITE);
    check(writes >= 1, "queryfail: no PCM period was written");
    check(writes <= (int)LE_AUDIO_LEDGER_CAPACITY + 8,
          "queryfail: ledger-full containment did not bound the loop");
    check(count_tag(T_PCM_CLOSE) >= 1, "queryfail: PCM was not retired");
    l = last_mix_index();
    check(l >= 1 && g_log.ev[l].tag == T_MIX_ENUM &&
          g_log.ev[l].a == C_AMP && g_log.ev[l].b == 0u &&
          g_log.ev[l - 1].a == C_MUTE && g_log.ev[l - 1].b == 1u,
          "queryfail: teardown did not mute before removing amplifier power");
}

/* Interceptor self-test: the deny policy must reject a host hardware path. */
static void audit_selftest(const char *base)
{
    char root[600];
    int fd;

    make_root(base, "auditselftest", root, sizeof(root));
    (void)snprintf(g_root, sizeof(g_root), "%s", root);
    g_audit_denied = 0;
    fd = open("/dev/null", O_RDONLY);
    check(fd < 0, "audit: /dev/null was not denied");
    if (fd >= 0)
        close(fd);
    check(g_audit_denied == 1, "audit: deny interception was not exercised");
    g_root[0] = '\0';
}

struct loop_scenario {
    const char *name;
    void (*run)(const char *);
};

/* Every scenario is first-class and always runs unless one is named on the
 * command line; there is no silent skip. */
static const struct loop_scenario g_loop_scenarios[] = {
    { "prime", scen_prime },
    { "finish", scen_finish },
    { "legacy", scen_legacy },
    { "epipe", scen_epipe },
    { "estripe", scen_estripe },
    { "restart", scen_restart },
    { "mixfail", scen_mixfail },
    { "prepfail", scen_prepfail },
    { "stop", scen_stop },
    { "media", scen_media },
    { "pcmfail", scen_pcmfail },
    { "notiming", scen_notiming },
    { "notiminglegacy", scen_notiming_legacy },
    { "queryfail", scen_queryfail },
};

int main(int argc, char **argv)
{
    const unsigned int count =
        (unsigned int)(sizeof(g_loop_scenarios) / sizeof(g_loop_scenarios[0]));
    int selected = -1;
    unsigned int i;

    if (argc < 2) {
        fprintf(stderr, "usage: %s <scratch-base> [scenario]\n", argv[0]);
        return 2;
    }
    if (argc >= 3) {
        for (i = 0; i < count; ++i)
            if (strcmp(argv[2], g_loop_scenarios[i].name) == 0)
                selected = (int)i;
        if (selected < 0) {
            fprintf(stderr, "engine-loop: unknown scenario '%s'\n", argv[2]);
            return 2;
        }
    }
    signal(SIGPIPE, SIG_IGN);
    audit_selftest(argv[1]);
    for (i = 0; i < count; ++i) {
        if (selected >= 0 && (int)i != selected)
            continue;
        g_loop_scenarios[i].run(argv[1]);
    }
    if (g_fail) {
        fprintf(stderr, "engine-loop: %d outer-loop checks failed\n", g_fail);
        return 1;
    }
    if (selected >= 0)
        printf("engine-loop: scenario %s PASS\n", argv[2]);
    else
        puts("engine-loop: real run_engine + fake PCM/mixer + real socket PASS");
    return 0;
}
'''


def compile_loop_fixture(cc: str, scratch: Path, source_dir: Path,
                         sanitize: bool):
    """Build the outer-loop harness: the REAL run_engine() loop driving a fake
    TinyALSA PCM *and* mixer, private root FIFOs and a real SOCK_SEQPACKET
    client, with open/connect link-wrapped so no host hardware path is used."""
    include = scratch / "include"
    tinyalsa = include / "tinyalsa"
    tinyalsa.mkdir(parents=True, exist_ok=True)
    (tinyalsa / "pcm.h").write_text(PCM_HEADER, encoding="ascii")
    (tinyalsa / "mixer.h").write_text(MIXER_HEADER, encoding="ascii")

    source = scratch / "test_audio_engine_loop.c"
    source.write_text(LOOP_PROGRAM, encoding="ascii")
    binary = scratch / "test_audio_engine_loop"

    command = [
        cc, "-std=c99", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
        "-ffunction-sections", "-fdata-sections",
    ]
    if sanitize:
        command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-g"]
    command += [
        "-I", str(include), "-I", str(source_dir),
        str(source),
        str(source_dir / "audio_sink.c"),
        str(source_dir / "audio_timing.c"),
        str(source_dir / "aec_reference.c"),
        str(source_dir / "audio_visualizer.c"),
        str(source_dir / "playback_status.c"),
        "-Wl,--gc-sections",
        "-Wl,--wrap=open", "-Wl,--wrap=open64", "-Wl,--wrap=openat",
        "-Wl,--wrap=connect",
        "-lm",
        "-o", str(binary),
    ]
    subprocess.run(command, check=True, timeout=180)
    return binary


def compile_fixture(cc: str, scratch: Path, source_dir: Path, sanitize: bool):
    include = scratch / "include"
    tinyalsa = include / "tinyalsa"
    tinyalsa.mkdir(parents=True, exist_ok=True)
    (tinyalsa / "pcm.h").write_text(PCM_HEADER, encoding="ascii")
    (tinyalsa / "mixer.h").write_text(MIXER_HEADER, encoding="ascii")

    source = scratch / "test_audio_engine_sendspin.c"
    source.write_text(ENGINE_PROGRAM, encoding="ascii")
    binary = scratch / "test_audio_engine_sendspin"

    command = [
        cc, "-std=c99", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
        "-ffunction-sections", "-fdata-sections",
    ]
    if sanitize:
        command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-g"]
    command += [
        "-I", str(include), "-I", str(source_dir),
        str(source),
        str(source_dir / "audio_sink.c"),
        str(source_dir / "audio_timing.c"),
        str(source_dir / "aec_reference.c"),
        "-Wl,--gc-sections", "-lm",
        "-o", str(binary),
    ]
    subprocess.run(command, check=True, timeout=180)
    return binary


# AF_UNIX sun_path is 108 bytes including the terminating NUL, so a bound
# path may be at most 107 bytes.  The outer-loop engine binds its framed sink
# at <run>/loop/<scenario>/sendspin.sock (and the AEC tap at the sibling
# aec-reference.sock); a derived path longer than that limit makes bind()
# fail ENAMETOOLONG -- the engine logs "framed sink unavailable: File name
# too long", the listener never exists, and every client connect() then fails
# with the opaque "real SOCK_SEQPACKET connect failed".
#
# Every candidate scratch base must live *under the designated scratch root*:
# the system temp dir (/tmp), TMPDIR, and any path that resolves outside the
# scratch tree (e.g. through a symlink) are never used, so a long operator
# root can never be papered over by writing to /tmp or onto a production
# socket path.  FIXTURE_ROOT is honoured only while it stays inside the
# scratch root and its *real* derived paths fit sun_path; otherwise the run
# directory is re-anchored to a short private temp path directly under the
# scratch root, and if even that cannot fit the fixture fails closed with a
# named RuntimeError instead of binding a truncated path.
_SUN_PATH_MAX = 107
_RUN_PREFIX = "run-"
_LOOP_ROOT_NAME = "loop"
_SOCKET_LEAVES = ("sendspin.sock", "aec-reference.sock")

# The only tree this fixture may create run directories in.  Defaults to this
# workspace's scratch root; overridable for CI, but nothing outside it is ever
# written and the system temp dir is never consulted.
_SCRATCH_ROOT = Path(
    os.environ.get(
        "LE_AUDIO_ENGINE_SENDPIN_SCRATCH_ROOT",
        os.environ.get("HERMES_SCRATCH_DIR", os.environ.get("TMPDIR", str(Path.home() / ".cache/libreecho-tests"))),
    )
)


def _loop_scenario_names() -> list:
    """Per-scenario subdirectory names, read from the outer-loop harness.

    Derived from the shipped harness text so the path budget cannot silently
    drift away from the scenario table it must cover."""
    names = re.findall(r'\{\s*"([A-Za-z0-9_-]+)",\s*scen_', LOOP_PROGRAM)
    return names or ["x" * 32]


def _worst_socket_path_len(run_root: Path) -> int:
    """Longest AF_UNIX path the fixtures derive from `run_root`."""
    base = len(str(run_root)) + 1 + len(_LOOP_ROOT_NAME)
    worst = 0
    for name in _loop_scenario_names():
        for leaf in _SOCKET_LEAVES:
            worst = max(worst, base + 1 + len(name) + 1 + len(leaf))
    return worst


def _realpath(path) -> Path:
    """Fully resolved path, resolving any symlink components."""
    return Path(os.path.realpath(str(path)))


def _within_scratch(path) -> bool:
    """True when `path` resolves to the designated scratch root or below.

    realpath is used so a symlinked candidate cannot escape the scratch tree
    (or steer the fixture onto /tmp or a production socket path)."""
    try:
        _realpath(path).relative_to(_realpath(_SCRATCH_ROOT))
    except (ValueError, OSError):
        return False
    return True


def _candidate_scratch_parents() -> list:
    """Scratch parents to try, most preferred first.

    Both candidates are asserted (and re-resolved) to live under the
    designated scratch root: the operator's FIXTURE_ROOT when it is inside the
    scratch tree, then the scratch root itself, which yields a short private
    temp path.  The system temp dir and /tmp are deliberately absent --
    temporary files must stay under the designated scratch root."""
    unique = []
    for parent in (FIXTURE_ROOT, _SCRATCH_ROOT):
        if not _within_scratch(parent):
            continue
        resolved = _realpath(parent)
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _make_run_dir() -> tempfile.TemporaryDirectory:
    """Create the run directory under a scratch parent whose real derived
    AF_UNIX socket paths fit in sun_path.

    The configured scratch root can be too long for AF_UNIX; the run directory
    is then re-anchored to a short private temp path directly under the
    designated scratch root instead of binding a truncated path.  Raises
    RuntimeError, naming every rejected candidate and its reason (outside the
    scratch root, or over-length), when none fit -- the limit is never
    silently exceeded, the system temp dir is never used, and a break is never
    an opaque 'connect failed'."""
    attempts = []
    candidates = []
    for parent in (FIXTURE_ROOT, _SCRATCH_ROOT):
        if not _within_scratch(parent):
            attempts.append(
                f"{parent}: resolves outside the scratch root "
                f"{_realpath(_SCRATCH_ROOT)} (refused; /tmp and symlink "
                "escapes are not used)"
            )
            continue
        resolved = _realpath(parent)
        if resolved not in candidates:
            candidates.append(resolved)
    for parent in candidates:
        try:
            parent.mkdir(parents=True, exist_ok=True)
            run = tempfile.TemporaryDirectory(prefix=_RUN_PREFIX,
                                              dir=str(parent))
        except OSError as exc:
            attempts.append(f"{parent}: {exc}")
            continue
        # bind()/connect() copy the literal address (<= 107 bytes) and the
        # kernel then resolves it, so the binding length is bounded by the
        # longer of the literal and realpath-derived paths.
        length = max(
            _worst_socket_path_len(Path(run.name)),
            _worst_socket_path_len(_realpath(run.name)),
        )
        if length <= _SUN_PATH_MAX:
            return run
        attempts.append(
            f"{parent}: worst derived AF_UNIX path {length} > {_SUN_PATH_MAX}"
        )
        run.cleanup()
    raise RuntimeError(
        "no scratch directory under "
        f"{_realpath(_SCRATCH_ROOT)} can hold the AF_UNIX framed sink; set "
        "LE_AUDIO_ENGINE_SENDPIN_FIXTURE_ROOT to a shorter directory inside "
        "the scratch root: " + "; ".join(attempts)
    )


def probe_fixture_root() -> int:
    """Print the selected run root and its worst real derived AF_UNIX path.

    Used by the fixture-root regression to observe the real selection result
    out-of-process, so env-configured long roots are exercised faithfully."""
    with _make_run_dir() as tmp:
        literal = Path(tmp)
        real = _realpath(tmp)
        worst = max(
            _worst_socket_path_len(literal),
            _worst_socket_path_len(real),
        )
        # Bind the *real* longest derived path the harness derives, so this is
        # kernel evidence (a bind that succeeds) and not only an arithmetic
        # model of the limit.
        longest = max(_loop_scenario_names(), key=len)
        derived = real / _LOOP_ROOT_NAME / longest / max(_SOCKET_LEAVES, key=len)
        derived.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(derived))
            bind_ok = 1
        finally:
            sock.close()
        print(f"RUN_ROOT={literal}")
        print(f"RUN_ROOT_REAL={real}")
        print(f"SCRATCH_ROOT_REAL={_realpath(_SCRATCH_ROOT)}")
        print(f"WORST_DERIVED_SOCKET={derived}")
        print(f"WORST_SOCKET_PATH_LEN={worst}")
        print(f"SUN_PATH_MAX={_SUN_PATH_MAX}")
        print(f"DERIVED_BIND_OK={bind_ok}")
    return 0


def _probe_environment(overrides: dict) -> subprocess.CompletedProcess:
    """Run this file's root probe in a child with a deterministic fixture env."""
    env = dict(os.environ)
    env.pop("LE_AUDIO_ENGINE_SENDPIN_FIXTURE_ROOT", None)
    env.pop("LE_AUDIO_ENGINE_SENDPIN_SCRATCH_ROOT", None)
    env.update(overrides)
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--fixture-root-probe"],
        capture_output=True, text=True, timeout=120, env=env,
    )


def _parse_probe(stdout: str) -> dict:
    fields = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields


def _under(path, root) -> bool:
    try:
        _realpath(path).relative_to(_realpath(root))
    except (ValueError, OSError):
        return False
    return True


def _selftest_root_selection() -> None:
    """Deterministic regression for the scratch-root selection contract.

    Runs this module out-of-process (so env-configured roots are exercised
    faithfully) and proves: a short FIXTURE_ROOT inside the scratch root is
    honoured; a long FIXTURE_ROOT *and* a long TMPDIR still yield a run root
    directly under the designated scratch root whose real derived AF_UNIX
    paths fit sun_path; /tmp is never chosen or created; and an unusable
    scratch root fails closed with the named error instead of falling back to
    /tmp.  Also checks the length budget is really derived from the shipped
    scenario table."""
    scratch_real = _realpath(_SCRATCH_ROOT)
    # The module's own configured scratch root must already select a usable
    # run dir, and fail closed by name (never via /tmp) when it cannot.
    with _make_run_dir():
        pass
    names = _loop_scenario_names()
    if len(names) < 2:
        raise RuntimeError(
            "root-selection regression: scenario table not read from "
            f"LOOP_PROGRAM ({names!r})"
        )
    long_scratch = _SCRATCH_ROOT / ("long-scratch-" + "s" * 64)
    long_fixture = str(_SCRATCH_ROOT / ("long-fixture-root-" + "f" * 64))
    long_tmpdir = str(_SCRATCH_ROOT / ("long-tmpdir-" + "t" * 64))

    # 0. a short FIXTURE_ROOT inside the scratch root is honoured as-is.
    short_fixture = _SCRATCH_ROOT / "fx-selftest"
    proc = _probe_environment({
        "LE_AUDIO_ENGINE_SENDPIN_FIXTURE_ROOT": str(short_fixture),
        "LE_AUDIO_ENGINE_SENDPIN_SCRATCH_ROOT": str(_SCRATCH_ROOT),
    })
    if proc.returncode != 0:
        raise RuntimeError(
            "root-selection regression: short-root probe failed: "
            f"rc={proc.returncode} stderr={proc.stderr.strip()}"
        )
    fields = _parse_probe(proc.stdout)
    run_real = Path(fields["RUN_ROOT_REAL"])
    if _realpath(run_real).parent != _realpath(short_fixture):
        raise RuntimeError(
            f"root-selection regression: short FIXTURE_ROOT not honoured; run "
            f"root {run_real} is not under {short_fixture}"
        )
    shutil.rmtree(short_fixture, ignore_errors=True)

    # 1. long FIXTURE_ROOT + long TMPDIR -> short private path under scratch.
    proc = _probe_environment({
        "LE_AUDIO_ENGINE_SENDPIN_FIXTURE_ROOT": long_fixture,
        "LE_AUDIO_ENGINE_SENDPIN_SCRATCH_ROOT": str(_SCRATCH_ROOT),
        "TMPDIR": long_tmpdir,
    })
    if proc.returncode != 0:
        raise RuntimeError(
            "root-selection regression: long-root probe failed: "
            f"rc={proc.returncode} stderr={proc.stderr.strip()}"
        )
    fields = _parse_probe(proc.stdout)
    run_real = _realpath(fields["RUN_ROOT_REAL"])
    worst = int(fields["WORST_SOCKET_PATH_LEN"])
    if str(fields["SCRATCH_ROOT_REAL"]) != str(scratch_real):
        raise RuntimeError(
            "root-selection regression: probe used a different scratch root "
            f"{fields['SCRATCH_ROOT_REAL']} != {scratch_real}"
        )
    if not _under(run_real, _SCRATCH_ROOT):
        raise RuntimeError(
            f"root-selection regression: chosen run root {run_real} is outside "
            f"the scratch root {scratch_real}"
        )
    if _under(run_real, "/tmp"):
        raise RuntimeError(
            f"root-selection regression: chosen run root {run_real} is under /tmp"
        )
    if _under(run_real, long_fixture) or _under(run_real, long_tmpdir):
        raise RuntimeError(
            f"root-selection regression: chosen run root {run_real} used a "
            "rejected long candidate"
        )
    if run_real.parent != scratch_real:
        raise RuntimeError(
            f"root-selection regression: fallback {run_real} is not a direct "
            f"child of the scratch root {scratch_real}"
        )
    if worst > _SUN_PATH_MAX:
        raise RuntimeError(
            f"root-selection regression: real derived socket {worst} bytes > "
            f"{_SUN_PATH_MAX}"
        )
    if fields.get("DERIVED_BIND_OK") != "1":
        raise RuntimeError(
            "root-selection regression: the longest real derived socket did "
            f"not bind: {fields.get('WORST_DERIVED_SOCKET')}"
        )
    if len(fields["WORST_DERIVED_SOCKET"]) != worst:
        raise RuntimeError(
            "root-selection regression: modelled worst length "
            f"{worst} != bound socket length "
            f"{len(fields['WORST_DERIVED_SOCKET'])}"
        )
    expected = max(
        len(str(run_real)) + 1 + len(_LOOP_ROOT_NAME) + 1 + len(name) + 1
        + len(leaf)
        for name in names for leaf in _SOCKET_LEAVES
    )
    if worst != expected:
        raise RuntimeError(
            "root-selection regression: scenario-derived length model drifted "
            f"(probe={worst}, scenarios={expected})"
        )
    shutil.rmtree(long_fixture, ignore_errors=True)
    shutil.rmtree(long_tmpdir, ignore_errors=True)

    # 2. unusable scratch root -> named fail-closed error, never /tmp.
    proc = _probe_environment({
        "LE_AUDIO_ENGINE_SENDPIN_SCRATCH_ROOT": str(long_scratch),
        "TMPDIR": long_tmpdir,
    })
    shutil.rmtree(long_scratch, ignore_errors=True)
    shutil.rmtree(long_tmpdir, ignore_errors=True)
    if proc.returncode == 0:
        raise RuntimeError(
            "root-selection regression: unusable scratch root did not fail "
            "closed"
        )
    if "no scratch directory under" not in (proc.stderr + proc.stdout):
        raise RuntimeError(
            "root-selection regression: fail-closed error is not named; got "
            f"{proc.stderr.strip()!r}"
        )


def main() -> None:
    cc = os.environ.get("CC", "cc")
    _selftest_root_selection()
    with _make_run_dir() as tmp:
        root = Path(tmp)
        binary = compile_fixture(cc, root, SOURCE_DIR, sanitize=True)
        socket_path = root / "sendspin.sock"
        env = dict(os.environ)
        env.setdefault("ASAN_OPTIONS", "detect_leaks=1:abort_on_error=1")
        env.setdefault("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1")
        subprocess.run([str(binary), str(socket_path)], check=True,
                       timeout=120, env=env)

        # Outer-loop harness: the real run_engine()/main loop in forked
        # children against fake PCM+mixer + real socket + private FIFOs.
        loop_base = root / "loop"
        loop_base.mkdir(parents=True, exist_ok=True)
        loop_binary = compile_loop_fixture(cc, root, SOURCE_DIR, sanitize=True)
        subprocess.run([str(loop_binary), str(loop_base)], check=True,
                       timeout=180, env=env)
    print("test_audio_engine_sendspin: real engine/fake PCM integration PASS")


if __name__ == "__main__":
    if "--fixture-root-probe" in sys.argv[1:]:
        raise SystemExit(probe_fixture_root())
    if "--selftest-root-selection" in sys.argv[1:]:
        _selftest_root_selection()
        print("test_audio_engine_sendspin: scratch-root selection PASS")
        raise SystemExit(0)
    main()
