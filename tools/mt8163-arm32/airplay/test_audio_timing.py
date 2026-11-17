#!/usr/bin/env python3
"""Behavioral regression tests for the bounded TinyALSA output-timing model.

The fixture compiles the real audio_timing.c against a fake TinyALSA PCM
backend (a link-time substitute for the pinned public pcm_* symbols) and drives
the public API.  It proves, with real C execution rather than source grepping:

  * fail-closed create for an undeclared clock domain;
  * startup settle / zero timestamp / wrong clock domain / stale / future
    samples are rejected instead of inventing DAC evidence;
  * the accepted/submitted/played distinction and the absolute timeline never
    leaking a modular ring wrap;
  * partial-segment placement, engine-generated silence not credited while real
    SDK silence is, per-generation fencing and the calibrated finish estimate;
  * an XRUN fences the timeline (no auto re-anchor on the stale submitted
    baseline) and `drop_unplayed()`/`reset()` reconciles dropped frames without
    manufacturing played credit or crediting a successor;
  * bounded, checked drop accounting (partial consumption, repeated resets,
    invalid drop requests) and ledger overflow recovery;
  * the completed-progress drain ring (exactly once, multiple generations),
    the source high-water surviving trailing silence / skipped queries / a
    drained timeline, and the source-endpoint finish staying independent of
    queued engine silence.

Only the generated stub <tinyalsa/pcm.h> and the real audio_timing.c are used;
no device, no production path, no privileged operation.

An optional ARM32 cross-compile check runs when the pinned toolchain is
present; it fails the test only when explicitly required.
"""

import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
STUB_HEADER = r'''#ifndef TINYALSA_PCM_H
#define TINYALSA_PCM_H
#include <time.h>

#define PCM_OUT 0x00000000U
#define PCM_MONOTONIC 0x00000008U

enum pcm_format { PCM_FORMAT_S16_LE = 0 };

struct pcm_config {
    unsigned int channels;
    unsigned int rate;
    unsigned int period_size;
    unsigned int period_count;
    enum pcm_format format;
    unsigned long start_threshold;
    unsigned long stop_threshold;
    unsigned long silence_threshold;
    unsigned long silence_size;
    unsigned long avail_min;
};

struct pcm;

struct pcm *pcm_open(unsigned int card, unsigned int device, unsigned int flags,
                     const struct pcm_config *config);
int pcm_is_ready(const struct pcm *pcm);
const char *pcm_get_error(const struct pcm *pcm);
int pcm_close(struct pcm *pcm);
unsigned int pcm_get_buffer_size(const struct pcm *pcm);
int pcm_get_htimestamp(struct pcm *pcm, unsigned int *avail,
                       struct timespec *tstamp);
#endif
'''

TIMING_PROGRAM = r'''
#define _POSIX_C_SOURCE 200809L

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "audio_timing.h"

/* --- fake TinyALSA backend ------------------------------------------------ */

static unsigned int fake_buffer = 4096;
static unsigned int fake_avail = 4096;
static struct timespec fake_ts;
static int fake_rc = 0;

int pcm_is_ready(const struct pcm *pcm)
{
    (void)pcm;
    return 1;
}

const char *pcm_get_error(const struct pcm *pcm)
{
    (void)pcm;
    return "fake";
}

unsigned int pcm_get_buffer_size(const struct pcm *pcm)
{
    (void)pcm;
    return fake_buffer;
}

int pcm_get_htimestamp(struct pcm *pcm, unsigned int *avail,
                       struct timespec *tstamp)
{
    (void)pcm;
    if (fake_rc != 0)
        return fake_rc;
    *avail = fake_avail;
    *tstamp = fake_ts;
    return 0;
}

/* depth = frames written but not yet played -> avail = buffer - depth. */
static void set_depth(unsigned int depth)
{
    if (depth > fake_buffer)
        depth = fake_buffer;
    fake_avail = fake_buffer - depth;
}

static void ts_now_minus_us(uint64_t age_us)
{
    long sub_sec = (long)(age_us / 1000000u);
    long sub_ns = (long)((age_us % 1000000u) * 1000u);

    clock_gettime(CLOCK_MONOTONIC, &fake_ts);
    fake_ts.tv_sec -= sub_sec;
    fake_ts.tv_nsec -= sub_ns;
    while (fake_ts.tv_nsec < 0) {
        fake_ts.tv_nsec += 1000000000L;
        fake_ts.tv_sec -= 1;
    }
}

static void ts_now_plus_us(uint64_t ahead_us)
{
    long add_sec = (long)(ahead_us / 1000000u);
    long add_ns = (long)((ahead_us % 1000000u) * 1000u);

    clock_gettime(CLOCK_MONOTONIC, &fake_ts);
    fake_ts.tv_sec += add_sec;
    fake_ts.tv_nsec += add_ns;
    while (fake_ts.tv_nsec >= 1000000000L) {
        fake_ts.tv_nsec -= 1000000000L;
        fake_ts.tv_sec += 1;
    }
}

static void ts_realtime(void)
{
    clock_gettime(CLOCK_REALTIME, &fake_ts);
}

static uint64_t ts_us(void)
{
    return (uint64_t)fake_ts.tv_sec * 1000000u +
           (uint64_t)fake_ts.tv_nsec / 1000u;
}

/* --- shared helpers ------------------------------------------------------- */

#define CHECK(cond, code)                                                     \
    do {                                                                      \
        if (!(cond)) {                                                        \
            fprintf(stderr, "FAIL line %d code %d\n", __LINE__, (code));      \
            return (code);                                                    \
        }                                                                     \
    } while (0)

static int dummy_pcm;

static struct le_audio_timing *mk_timing(int calibrated)
{
    struct le_audio_timing_config c;

    memset(&c, 0, sizeof(c));
    c.pcm = (struct pcm *)&dummy_pcm;
    c.rate = 48000u;
    c.buffer_frames = 4096u;
    c.clock = LE_AUDIO_TIMING_CLOCK_MONOTONIC;
    c.output_latency_us = 1500u;
    c.latency_calibrated = calibrated;
    return le_audio_timing_create(&c);
}

static void seed_anchor(struct le_audio_timing *t)
{
    struct le_audio_timing_sample s;

    set_depth(0u);
    ts_now_minus_us(1000u);
    (void)le_audio_timing_query(t, &s);
}

static int do_submit(struct le_audio_timing *t, uint64_t src, uint32_t frames,
                     int generated, uint32_t gen)
{
    struct le_audio_timing_submission s;

    memset(&s, 0, sizeof(s));
    s.epoch = 7u;
    s.generation = gen;
    s.source_first_frame = src;
    s.frames = frames;
    s.generated = generated;
    return le_audio_timing_submit(t, &s);
}

int main(void)
{
    struct le_audio_timing_config config;
    struct le_audio_timing *t;
    struct le_audio_timing *t2;
    struct le_audio_timing_sample sample;
    struct le_audio_timing_progress progress;
    struct le_audio_timing_completed completed;
    uint64_t expected;

    /* 1: undeclared clock domain fails closed. */
    memset(&config, 0, sizeof(config));
    config.pcm = (struct pcm *)&dummy_pcm;
    config.rate = 48000u;
    config.clock = LE_AUDIO_TIMING_CLOCK_UNKNOWN;
    CHECK(le_audio_timing_create(&config) == NULL, 1);

    /* 2: declared monotonic clock with buffer_frames 0 pulls the public size. */
    memset(&config, 0, sizeof(config));
    config.pcm = (struct pcm *)&dummy_pcm;
    config.rate = 48000u;
    config.buffer_frames = 0u;
    config.clock = LE_AUDIO_TIMING_CLOCK_MONOTONIC;
    config.output_latency_us = 1500u;
    config.latency_calibrated = 1;
    t = le_audio_timing_create(&config);
    CHECK(t != NULL, 2);

    /* 3: a zero-length submission is rejected and does not advance. */
    CHECK(do_submit(t, 0u, 0u, 0, 1u) == LE_AUDIO_TIMING_ERR_RANGE, 3);
    CHECK(le_audio_timing_submitted(t) == 0u, 3);

    /* 32: quality O2 - an overflowing source_first_frame + frames must fail
     * closed (ERR_RANGE) with no phantom accepted/submitted credit, while the
     * generated=1 padding path ignores the (unused) source cursor and is
     * unaffected. */
    {
        struct le_audio_timing *to = mk_timing(0);
        struct le_audio_timing_submission so;

        memset(&so, 0, sizeof(so));
        so.epoch = 7u;
        so.generation = 1u;
        so.source_first_frame = UINT64_MAX - 50u;
        so.frames = 100u;
        so.generated = 0;
        CHECK(le_audio_timing_submit(to, &so) == LE_AUDIO_TIMING_ERR_RANGE, 32);
        CHECK(le_audio_timing_submitted(to) == 0u, 32);
        so.generated = 1;
        CHECK(le_audio_timing_submit(to, &so) == LE_AUDIO_TIMING_OK, 32);
        CHECK(le_audio_timing_submitted(to) == 100u, 32);
        le_audio_timing_destroy(to);
    }

    /* 4: startup settle - a zero hardware timestamp is not DAC evidence. */
    set_depth(0u);
    fake_ts.tv_sec = 0;
    fake_ts.tv_nsec = 0;
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 4);
    CHECK(!sample.valid && sample.status == LE_AUDIO_TIMING_ERR_SKEW, 4);

    /* 5: wrong clock domain (REALTIME) is rejected by the skew window. */
    ts_realtime();
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 5);
    CHECK(!sample.valid && sample.status == LE_AUDIO_TIMING_ERR_SKEW, 5);

    /* 6: a stale timestamp beyond the age bound is rejected. */
    ts_now_minus_us(500000u);
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 6);
    CHECK(!sample.valid && sample.status == LE_AUDIO_TIMING_ERR_SKEW, 6);

    /* 7: a future timestamp beyond the skew bound is rejected. */
    ts_now_plus_us(50000u);
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 7);
    CHECK(!sample.valid && sample.status == LE_AUDIO_TIMING_ERR_SKEW, 7);

    /* 8: a failing backend query is reported, not guessed. */
    fake_rc = -5;
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 8);
    CHECK(!sample.valid && sample.status == LE_AUDIO_TIMING_ERR_QUERY, 8);
    fake_rc = 0;

    /* 9: a settled, fresh, empty stream yields a valid zero playhead. */
    set_depth(0u);
    ts_now_minus_us(1000u);
    expected = ts_us();
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 9);
    CHECK(sample.valid, 9);
    CHECK(sample.playhead_frames == 0u, 9);
    CHECK(sample.in_flight_frames == 0u && sample.avail_frames == 4096u, 9);
    CHECK(sample.sample_us == expected, 9);

    /* 10: submitted/played distinction - one period fully in flight. */
    CHECK(do_submit(t, 0u, 2048u, 0, 1u) == LE_AUDIO_TIMING_OK, 10);
    CHECK(le_audio_timing_submitted(t) == 2048u, 10);
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 10);
    CHECK(sample.valid && sample.playhead_frames == 0u, 10);
    CHECK(sample.in_flight_frames == 2048u, 10);

    /* 11: progress maps the playhead onto the source cursor and estimates the
     *     calendar finish with the calibrated latency term. */
    expected = ts_us();
    CHECK(le_audio_timing_progress(t, &progress) == LE_AUDIO_TIMING_OK, 11);
    CHECK(progress.valid && progress.source_valid, 11);
    CHECK(progress.epoch == 7u && progress.generation == 1u, 11);
    CHECK(progress.submitted_frames == 2048u && progress.dropped_frames == 0u, 11);
    CHECK(progress.played_frames == 0u && progress.source_frame == 0u, 11);
    CHECK(progress.finish_valid, 11);
    CHECK(progress.finish_us ==
              expected + (2048u * 1000000u) / 48000u + 1500u, 11);

    /* 12: fully drained - the tail source cursor is reported, not fabricated. */
    set_depth(0u);
    ts_now_minus_us(1000u);
    expected = ts_us();
    CHECK(le_audio_timing_progress(t, &progress) == LE_AUDIO_TIMING_OK, 12);
    CHECK(progress.valid && progress.source_valid, 12);
    CHECK(progress.played_frames == 2048u && progress.source_frame == 2048u, 12);
    CHECK(progress.finish_valid &&
              progress.finish_us == expected + 1500u, 12);

    /* 13: partial-period placement - a sub-period range maps at its offset. */
    CHECK(do_submit(t, 2048u, 500u, 0, 1u) == LE_AUDIO_TIMING_OK, 13);
    set_depth(200u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t, &progress) == LE_AUDIO_TIMING_OK, 13);
    CHECK(progress.valid && progress.source_valid, 13);
    CHECK(progress.submitted_frames == 2548u, 13);
    CHECK(progress.played_frames == 2348u, 13);
    CHECK(progress.source_frame == 2348u, 13);
    CHECK(progress.finish_us ==
              ts_us() + (200u * 1000000u) / 48000u + 1500u, 13);

    /* 14: engine-generated silence occupies the timeline but is not credited as
     *     source data; the just-completed SOURCE tail is still surfaced once
     *     (Finding 2) while the playhead region is flagged generated. */
    CHECK(do_submit(t, 0u, 2048u, 1, 1u) == LE_AUDIO_TIMING_OK, 14);
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t, &progress) == LE_AUDIO_TIMING_OK, 14);
    CHECK(progress.valid, 14);
    CHECK(progress.generated, 14);
    CHECK(progress.source_valid && progress.source_frame == 2548u, 14);
    CHECK(progress.submitted_frames == 4596u, 14);
    CHECK(progress.played_frames == 2548u, 14);

    /* 15: real SDK silence is an ordinary source range and DOES count. */
    CHECK(do_submit(t, 2548u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 15);
    set_depth(50u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t, &progress) == LE_AUDIO_TIMING_OK, 15);
    CHECK(progress.valid && progress.source_valid && !progress.generated, 15);
    CHECK(progress.played_frames == 4646u, 15);
    CHECK(progress.source_frame == 2598u, 15);

    /* 16: a non-monotonic playhead invalidates and resets the anchor. */
    set_depth(4000u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_query(t, &sample) == LE_AUDIO_TIMING_OK, 16);
    CHECK(!sample.valid && sample.status == LE_AUDIO_TIMING_ERR_NONMONOTONIC, 16);

    /* 17: finish estimate fails closed behind the playhead / NULL arguments. */
    {
        uint64_t finish = 0u;
        struct le_audio_timing_progress ignored;

        CHECK(le_audio_timing_progress(t, &progress) == LE_AUDIO_TIMING_OK, 17);
        CHECK(le_audio_timing_finish_us(t, 0u, &finish) ==
                  LE_AUDIO_TIMING_ERR_INVALID, 17);
        CHECK(le_audio_timing_query(NULL, &sample) ==
                  LE_AUDIO_TIMING_ERR_ARGUMENT, 17);
        CHECK(le_audio_timing_progress(NULL, &ignored) ==
                  LE_AUDIO_TIMING_ERR_ARGUMENT, 17);
        CHECK(le_audio_timing_take_completed(NULL, &completed) ==
                  LE_AUDIO_TIMING_ERR_ARGUMENT, 17);
        CHECK(le_audio_timing_drop_unplayed(NULL, 1u) ==
                  LE_AUDIO_TIMING_ERR_ARGUMENT, 17);
    }
    le_audio_timing_destroy(t);

    /* 18: CANCEL/RESET that leaves the hardware DRAINING does not fence: the
     *     status is surfaced once and the next consistent sample re-anchors,
     *     while the still-draining generation keeps being reported. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 18);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 2048u, 0, 5u) == LE_AUDIO_TIMING_OK, 18);
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 18);
    CHECK(progress.valid && progress.generation == 5u, 18);
    le_audio_timing_invalidate(t2, LE_AUDIO_TIMING_ERR_CANCEL);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 18);
    CHECK(!progress.valid && !progress.fenced &&
              progress.status == LE_AUDIO_TIMING_ERR_CANCEL, 18);
    set_depth(1024u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 18);
    CHECK(progress.valid && !progress.fenced, 18);
    CHECK(progress.generation == 5u && progress.played_frames == 1024u, 18);
    le_audio_timing_destroy(t2);

    /* 19: XRUN fences.  No auto re-anchor on the stale submitted baseline and
     *     no manufactured credit; the caller reconciles BEFORE resuming. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 19);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 2048u, 0, 1u) == LE_AUDIO_TIMING_OK, 19);
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_query(t2, &sample) == LE_AUDIO_TIMING_OK, 19);
    CHECK(sample.valid && sample.playhead_frames == 0u, 19);

    le_audio_timing_invalidate(t2, LE_AUDIO_TIMING_ERR_XRUN);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 19);
    CHECK(!progress.valid && progress.fenced &&
              progress.status == LE_AUDIO_TIMING_ERR_XRUN, 19);
    /* repeated sampling stays fenced (no re-anchor on stale submitted) */
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 19);
    CHECK(!progress.valid && progress.fenced, 19);
    /* explicit reconcile first: drop everything unplayed (2048) */
    CHECK(le_audio_timing_unplayed(t2) == 2048u, 19);
    CHECK(le_audio_timing_reset(t2) == LE_AUDIO_TIMING_OK, 19);
    CHECK(le_audio_timing_dropped(t2) == 2048u, 19);
    /* then the recovery write plays from the reconciled baseline */
    CHECK(do_submit(t2, 0u, 2048u, 0, 2u) == LE_AUDIO_TIMING_OK, 19);
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 19);
    CHECK(progress.valid && !progress.fenced, 19);
    CHECK(progress.played_frames == 0u, 19);
    CHECK(progress.submitted_frames == 4096u, 19);
    CHECK(progress.dropped_frames == 2048u, 19);
    CHECK(progress.generation == 2u && progress.source_frame == 0u, 19);
    le_audio_timing_destroy(t2);

    /* 19b: a write while fenced is recorded but does not re-anchor or credit;
     *      reconciling afterwards drops the whole unplayed backlog. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 19);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 2048u, 0, 1u) == LE_AUDIO_TIMING_OK, 19);
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_query(t2, &sample) == LE_AUDIO_TIMING_OK, 19);
    CHECK(sample.valid, 19);
    le_audio_timing_fence(t2, LE_AUDIO_TIMING_ERR_XRUN);
    CHECK(do_submit(t2, 0u, 2048u, 0, 2u) == LE_AUDIO_TIMING_OK, 19);
    set_depth(2048u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 19);
    CHECK(!progress.valid && progress.fenced, 19);
    CHECK(le_audio_timing_unplayed(t2) == 4096u, 19);
    CHECK(le_audio_timing_reset(t2) == LE_AUDIO_TIMING_OK, 19);
    CHECK(le_audio_timing_dropped(t2) == 4096u, 19);
    le_audio_timing_destroy(t2);

    /* 20: partial consumption + drop: played credit is preserved, the dropped
     *     frames are neither credited to the source nor to the successor. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 20);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 2048u, 0, 1u) == LE_AUDIO_TIMING_OK, 20);
    set_depth(1024u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_query(t2, &sample) == LE_AUDIO_TIMING_OK, 20);
    CHECK(sample.valid && sample.playhead_frames == 1024u, 20);

    le_audio_timing_fence(t2, LE_AUDIO_TIMING_ERR_XRUN);
    CHECK(le_audio_timing_unplayed(t2) == 1024u, 20);
    /* invalid drop request: more than unplayed -> RANGE, no state change */
    CHECK(le_audio_timing_drop_unplayed(t2, 2048u) ==
              LE_AUDIO_TIMING_ERR_RANGE, 20);
    CHECK(le_audio_timing_dropped(t2) == 0u && le_audio_timing_unplayed(t2) == 1024u, 20);
    CHECK(le_audio_timing_submitted(t2) == 2048u, 20);
    /* exact partial drop */
    CHECK(le_audio_timing_drop_unplayed(t2, 1024u) == LE_AUDIO_TIMING_OK, 20);
    CHECK(le_audio_timing_dropped(t2) == 1024u, 20);
    /* successor write; the dropped generation is not credited to it */
    CHECK(do_submit(t2, 0u, 2048u, 0, 2u) == LE_AUDIO_TIMING_OK, 20);
    set_depth(1024u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 20);
    CHECK(progress.valid, 20);
    CHECK(progress.played_frames == 2048u, 20);
    CHECK(progress.generation == 2u && progress.source_frame == 1024u, 20);
    le_audio_timing_destroy(t2);

    /* 21: repeated resets stay bounded and do not accumulate phantom credit. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 21);
    seed_anchor(t2);
    {
        unsigned int i;
        for (i = 0u; i < 3u; ++i) {
            CHECK(do_submit(t2, 0u, 1024u, 0, (uint32_t)(i + 1u)) ==
                      LE_AUDIO_TIMING_OK, 21);
            set_depth(1024u);
            ts_now_minus_us(1000u);
            CHECK(le_audio_timing_query(t2, &sample) == LE_AUDIO_TIMING_OK, 21);
            CHECK(sample.valid && sample.playhead_frames == 0u, 21);
            le_audio_timing_fence(t2, LE_AUDIO_TIMING_ERR_XRUN);
            CHECK(le_audio_timing_reset(t2) == LE_AUDIO_TIMING_OK, 21);
        }
    }
    CHECK(le_audio_timing_dropped(t2) == 3072u, 21);
    CHECK(le_audio_timing_submitted(t2) == 3072u, 21);
    set_depth(0u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 21);
    CHECK(progress.valid && progress.played_frames == 0u, 21);
    /* a further normal write plays correctly */
    CHECK(do_submit(t2, 0u, 1024u, 0, 9u) == LE_AUDIO_TIMING_OK, 21);
    set_depth(0u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 21);
    CHECK(progress.valid && progress.played_frames == 1024u &&
              progress.generation == 9u, 21);
    le_audio_timing_destroy(t2);

    /* 22: a full ledger recovers after an explicit drop reconcile. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 22);
    {
        unsigned int i;
        for (i = 0u; i < LE_AUDIO_LEDGER_CAPACITY; ++i) {
            CHECK(do_submit(t2, (uint64_t)i, 1u, 0, 1u) ==
                      LE_AUDIO_TIMING_OK, 22);
        }
        CHECK(le_audio_timing_dropped(t2) == 0u, 22);
        CHECK(do_submit(t2, 99u, 1u, 0, 1u) == LE_AUDIO_TIMING_ERR_RANGE, 22);
        CHECK(le_audio_timing_submitted(t2) == LE_AUDIO_LEDGER_CAPACITY, 22);
        le_audio_timing_fence(t2, LE_AUDIO_TIMING_ERR_XRUN);
        CHECK(le_audio_timing_reset(t2) == LE_AUDIO_TIMING_OK, 22);
        CHECK(le_audio_timing_dropped(t2) == LE_AUDIO_LEDGER_CAPACITY, 22);
        CHECK(do_submit(t2, 0u, 1u, 0, 2u) == LE_AUDIO_TIMING_OK, 22);
    }
    le_audio_timing_destroy(t2);

    /* 23: completed-progress drain is exactly once and ordered; multiple
     *     completed generations cannot overwrite each other. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 23);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 23);
    CHECK(do_submit(t2, 0u, 200u, 1, 2u) == LE_AUDIO_TIMING_OK, 23);
    CHECK(do_submit(t2, 100u, 100u, 0, 3u) == LE_AUDIO_TIMING_OK, 23);
    set_depth(0u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 23);
    CHECK(progress.valid && progress.source_valid, 23);
    CHECK(progress.source_frame == 200u && progress.generation == 3u, 23);
    CHECK(le_audio_timing_take_completed(t2, &completed) == LE_AUDIO_TIMING_OK, 23);
    CHECK(completed.valid && completed.generation == 1u && !completed.generated, 23);
    CHECK(completed.source_end_frame == 100u, 23);
    CHECK(le_audio_timing_take_completed(t2, &completed) == LE_AUDIO_TIMING_OK, 23);
    CHECK(completed.valid && completed.generation == 2u && completed.generated, 23);
    CHECK(le_audio_timing_take_completed(t2, &completed) == LE_AUDIO_TIMING_OK, 23);
    CHECK(completed.valid && completed.generation == 3u && !completed.generated, 23);
    CHECK(completed.source_end_frame == 200u, 23);
    CHECK(le_audio_timing_take_completed(t2, &completed) == LE_AUDIO_TIMING_OK, 23);
    CHECK(!completed.valid, 23);
    le_audio_timing_destroy(t2);

    /* 24: source tail survives a skipped query landing inside trailing engine
     *     silence, and repeated calls are idempotent (exactly-once delta). */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 24);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 24);
    CHECK(do_submit(t2, 0u, 200u, 1, 1u) == LE_AUDIO_TIMING_OK, 24);
    set_depth(100u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 24);
    CHECK(progress.valid && progress.source_valid && progress.generated, 24);
    CHECK(progress.source_frame == 100u, 24);
    {
        uint64_t first = progress.source_frame;
        CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 24);
        CHECK(progress.valid && progress.source_frame == first, 24);
    }
    le_audio_timing_destroy(t2);

    /* 25: a query landing exactly on the source/generated boundary delivers the
     *     final source tail once. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 25);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 25);
    CHECK(do_submit(t2, 0u, 200u, 1, 1u) == LE_AUDIO_TIMING_OK, 25);
    set_depth(200u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 25);
    CHECK(progress.valid && progress.source_valid && progress.source_frame == 100u, 25);
    le_audio_timing_destroy(t2);

    /* 26: the source-endpoint finish tracks the mapped source endpoint, not the
     *     engine silence queued after it. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 26);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 26);
    CHECK(do_submit(t2, 0u, 200u, 1, 1u) == LE_AUDIO_TIMING_OK, 26);
    set_depth(250u);
    ts_now_minus_us(1000u);
    expected = ts_us();
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 26);
    CHECK(progress.valid && progress.source_finish_valid, 26);
    CHECK(progress.source_finish_us ==
              expected + (50u * 1000000u) / 48000u + 1500u, 26);
    CHECK(progress.finish_valid, 26);
    CHECK(progress.finish_us ==
              expected + (250u * 1000000u) / 48000u + 1500u, 26);
    CHECK(progress.source_finish_us < progress.finish_us, 26);
    le_audio_timing_destroy(t2);

    /* 27: a past source endpoint is reconstructed only inside the bounded fresh
     *     window; beyond it the source finish is explicitly invalid. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 27);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 27);
    CHECK(do_submit(t2, 0u, 2000u, 1, 1u) == LE_AUDIO_TIMING_OK, 27);
    set_depth(0u);
    ts_now_minus_us(1000u);
    expected = ts_us();
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 27);
    CHECK(progress.valid && progress.source_finish_valid, 27);
    CHECK(progress.source_finish_us ==
              expected - (2000u * 1000000u) / 48000u + 1500u, 27);
    le_audio_timing_destroy(t2);

    t2 = mk_timing(1);
    CHECK(t2 != NULL, 27);
    seed_anchor(t2);
    CHECK(do_submit(t2, 0u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 27);
    CHECK(do_submit(t2, 0u, 20000u, 1, 1u) == LE_AUDIO_TIMING_OK, 27);
    set_depth(0u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 27);
    CHECK(progress.valid, 27);
    CHECK(!progress.source_finish_valid && progress.source_finish_us == 0u, 27);
    CHECK(progress.finish_valid, 27);
    le_audio_timing_destroy(t2);

    /* 28: a full completed ring fails closed (ERR_RANGE) instead of silently
     *     dropping a completion; draining recovers it. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 28);
    seed_anchor(t2);
    {
        unsigned int i;
        for (i = 0u; i < LE_AUDIO_LEDGER_CAPACITY; ++i) {
            CHECK(do_submit(t2, 0u, 1u, 0, (uint32_t)(i + 1u)) ==
                      LE_AUDIO_TIMING_OK, 28);
        }
    }
    set_depth(0u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 28);
    CHECK(progress.valid && !progress.drain_overflow, 28);
    /* one more distinct generation cannot be recorded -> fail closed */
    CHECK(do_submit(t2, 0u, 1u, 0, 100u) == LE_AUDIO_TIMING_OK, 28);
    set_depth(0u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 28);
    CHECK(!progress.valid && progress.drain_overflow &&
              progress.status == LE_AUDIO_TIMING_ERR_RANGE, 28);
    /* drain one -> the retained entry reaps and overflow clears */
    CHECK(le_audio_timing_take_completed(t2, &completed) == LE_AUDIO_TIMING_OK, 28);
    CHECK(completed.valid, 28);
    set_depth(0u);
    ts_now_minus_us(1000u);
    CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 28);
    CHECK(progress.valid && !progress.drain_overflow, 28);
    le_audio_timing_destroy(t2);

    /* 29: the absolute timeline never leaks the modular ring wrap. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 29);
    seed_anchor(t2);
    {
        unsigned int i;
        uint64_t cumulative = 0u;
        uint64_t source = 0u;

        for (i = 0u; i < 6u; ++i) {
            CHECK(do_submit(t2, source, 2048u, 0, 1u) == LE_AUDIO_TIMING_OK, 29);
            cumulative += 2048u;
            source += 2048u;
            set_depth(0u);
            ts_now_minus_us(1000u);
            CHECK(le_audio_timing_progress(t2, &progress) ==
                      LE_AUDIO_TIMING_OK, 29);
            CHECK(progress.valid && progress.played_frames == cumulative, 29);
            CHECK(progress.source_frame == source, 29);
        }
        CHECK(cumulative > 4096u, 29);
        CHECK(do_submit(t2, source, 2048u, 0, 1u) == LE_AUDIO_TIMING_OK, 29);
        set_depth(2048u);
        ts_now_minus_us(1000u);
        CHECK(le_audio_timing_query(t2, &sample) == LE_AUDIO_TIMING_OK, 29);
        CHECK(sample.valid && sample.playhead_frames == cumulative, 29);
        set_depth(1000u);
        ts_now_minus_us(1000u);
        CHECK(le_audio_timing_query(t2, &sample) == LE_AUDIO_TIMING_OK, 29);
        CHECK(sample.valid &&
                  sample.playhead_frames == cumulative + 2048u - 1000u, 29);
    }
    le_audio_timing_destroy(t2);

    /* 30: the ledger is bounded - overflow fails closed, reaping recovers it. */
    t2 = mk_timing(1);
    CHECK(t2 != NULL, 30);
    {
        unsigned int i;
        for (i = 0u; i < LE_AUDIO_LEDGER_CAPACITY; ++i) {
            CHECK(do_submit(t2, (uint64_t)i, 1u, 0, 1u) ==
                      LE_AUDIO_TIMING_OK, 30);
        }
        CHECK(do_submit(t2, 99u, 1u, 0, 1u) == LE_AUDIO_TIMING_ERR_RANGE, 30);
        CHECK(le_audio_timing_submitted(t2) == LE_AUDIO_LEDGER_CAPACITY, 30);
        set_depth(0u);
        ts_now_minus_us(1000u);
        CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 30);
        CHECK(progress.valid, 30);
        CHECK(do_submit(t2, 0u, 1u, 0, 2u) == LE_AUDIO_TIMING_OK, 30);
    }
    le_audio_timing_destroy(t2);

    /* 31: an uncalibrated latency keeps the sample but refuses every finish. */
    t2 = mk_timing(0);
    CHECK(t2 != NULL, 31);
    {
        uint64_t finish = 123u;

        CHECK(do_submit(t2, 0u, 100u, 0, 1u) == LE_AUDIO_TIMING_OK, 31);
        set_depth(0u);
        ts_now_minus_us(1000u);
        CHECK(le_audio_timing_progress(t2, &progress) == LE_AUDIO_TIMING_OK, 31);
        CHECK(progress.valid && !progress.finish_valid &&
                  !progress.source_finish_valid, 31);
        CHECK(progress.finish_us == 0u && progress.source_finish_us == 0u, 31);
        CHECK(le_audio_timing_finish_us(t2, 100u, &finish) ==
                  LE_AUDIO_TIMING_ERR_INVALID, 31);
        CHECK(finish == 123u, 31);
        CHECK(le_audio_timing_source_finish_us(t2, &finish) ==
                  LE_AUDIO_TIMING_ERR_INVALID, 31);
    }
    le_audio_timing_destroy(t2);

    puts("audio_timing: timeline, drop-accounting, drain and fail-closed checks PASS");
    return 0;
}
'''


def compile_fixture(cc, name, program, root, include_root, extra_flags=()):
    source = root / (name + ".c")
    binary = root / name
    source.write_text(program, encoding="ascii")
    command = [
        cc, "-std=c99", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
        "-D_POSIX_C_SOURCE=200809L",
        "-I", str(include_root), "-I", str(SOURCE_DIR),
        *extra_flags,
        str(source), str(SOURCE_DIR / "audio_timing.c"),
        "-o", str(binary),
    ]
    subprocess.run(command, check=True, timeout=120)
    return binary


def run_host_suite(root, include_root):
    cc = os.environ.get("CC", "cc")
    sanitizers = ("-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-g")
    try:
        binary = compile_fixture(cc, "test_audio_timing_unit", TIMING_PROGRAM,
                                 root, include_root, extra_flags=sanitizers)
    except subprocess.CalledProcessError:
        print("note: host sanitizers unsupported; retrying without them")
        binary = compile_fixture(cc, "test_audio_timing_unit", TIMING_PROGRAM,
                                 root, include_root)
    env = dict(os.environ)
    env["ASAN_OPTIONS"] = "detect_leaks=1"
    env["UBSAN_OPTIONS"] = "halt_on_error=1"
    subprocess.run([str(binary)], check=True, timeout=60, env=env)


def find_pinned_tinyalsa_include():
    explicit = os.environ.get("LE_AUDIO_TIMING_ARM_TINYALSA_INCLUDE")
    if explicit and (Path(explicit) / "tinyalsa" / "pcm.h").is_file():
        return Path(explicit)
    base = Path("/mnt/old-samsung/tmp")
    if base.is_dir():
        for candidate in sorted(base.glob("*/tinyalsa-*/include")):
            if (candidate / "tinyalsa" / "pcm.h").is_file():
                return candidate
    return None


def run_arm_compile_check(root, include_root):
    required = os.environ.get("LE_AUDIO_TIMING_REQUIRE_ARM") == "1"
    cc = Path(os.environ.get(
        "LE_AUDIO_TIMING_ARM_CC",
        "/mnt/old-samsung/usr/bin/arm-linux-gnueabihf-gcc"))
    sysroot = Path(os.environ.get("LE_AUDIO_TIMING_ARM_SYSROOT",
                                  "/mnt/old-samsung"))
    if not (cc.is_file() and os.access(cc, os.X_OK) and sysroot.is_dir()):
        message = f"note: ARM toolchain unavailable ({cc}), cross compile skipped"
        if required:
            raise SystemExit(message)
        print(message)
        return
    include = find_pinned_tinyalsa_include() or include_root
    if include is not include_root:
        print(f"note: ARM compile uses pinned TinyALSA headers at {include}")
    obj = root / "audio_timing_arm.o"
    command = [
        str(cc), f"--sysroot={sysroot}", "-std=c99", "-D_POSIX_C_SOURCE=200809L",
        "-Wall", "-Wextra", "-Wpedantic", "-Werror",
        "-I", str(include), "-I", str(SOURCE_DIR),
        "-c", str(SOURCE_DIR / "audio_timing.c"), "-o", str(obj),
    ]
    subprocess.run(command, check=True, timeout=120)
    header = obj.read_bytes()[:20]
    if header[:4] != b"\x7fELF" or header[4] != 1:
        raise SystemExit("ARM object is not a 32-bit ELF")
    machine = struct.unpack("<H", header[18:20])[0]
    if machine != 40:
        raise SystemExit(f"ARM object has unexpected e_machine {machine}")
    print(f"audio_timing: ARM32 ({cc.name}, e_machine=ARM) compile PASS")


def check_source_doc_precision():
    """O1: the published source high-water is generation-scoped, not lifetime."""
    header = (SOURCE_DIR / "audio_timing.h").read_text(encoding="utf-8")
    if "monotone within the reported `(epoch, generation)`" not in header:
        raise SystemExit("audio_timing.h must document source_frame as "
                         "monotone within (epoch, generation)")
    if "cumulative source cursor (monotone high-water)" in header:
        raise SystemExit("audio_timing.h must not call source_frame a lifetime "
                         "monotone high-water")


def main():
    with tempfile.TemporaryDirectory(prefix="libreecho-timing-test-") as temp:
        root = Path(temp)
        stub = root / "tinyalsa"
        stub.mkdir()
        (stub / "pcm.h").write_text(STUB_HEADER, encoding="ascii")
        check_source_doc_precision()
        run_host_suite(root, root)
        run_arm_compile_check(root, root)
    print("test_audio_timing: host behavior and target compile PASS")


if __name__ == "__main__":
    sys.exit(main())
