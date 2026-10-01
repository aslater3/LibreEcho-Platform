/*
 * Bounded TinyALSA hardware output-timing model.
 *
 * See audio_timing.h for the time model, the dropped-frame accounting rule and
 * the fail-closed contract.  This file uses only the pinned public pcm_*
 * accessors -- pcm_is_ready(), pcm_get_htimestamp() and pcm_get_buffer_size()
 * -- and never inspects an opaque struct or issues a guessed ioctl.
 */

#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif

#include "audio_timing.h"

#include <stdlib.h>
#include <string.h>

struct le_audio_timing_entry {
    uint64_t output_first_frame;
    uint64_t source_first_frame;
    uint32_t frames;
    uint32_t epoch;
    uint32_t generation;
    int generated;
};

struct le_audio_timing_completed_event {
    uint64_t output_first_frame;
    uint64_t output_end_frame;
    uint64_t source_first_frame;
    uint64_t source_end_frame;
    uint32_t epoch;
    uint32_t generation;
    int generated;
};

struct le_audio_timing {
    struct pcm *pcm;
    uint32_t rate;
    uint32_t buffer_frames;
    enum le_audio_timing_clock clock;
    uint32_t output_latency_us;
    int latency_calibrated;

    /* Monotone object-lifetime counters. */
    uint64_t accepted_frames; /* frames a pcm_writei() accepted */
    uint64_t dropped_frames;  /* frames a reset confirmed will never play */

    /* Highest physical playhead ever verified.  Never moves backwards; used to
     * bound a drop request and to reject a fabricated backwards cursor. */
    uint64_t played_floor;

    /* Bounded ledger: entries tile [0, accepted - dropped) with no growth. */
    struct le_audio_timing_entry entries[LE_AUDIO_LEDGER_CAPACITY];
    size_t start;
    size_t count;

    /* Bounded completed-event ring (exactly-once drain). */
    struct le_audio_timing_completed_event completed[LE_AUDIO_LEDGER_CAPACITY];
    size_t completed_start;
    size_t completed_count;
    int drain_overflow;

    /* Monotone high-water source progress (survives silence and reaping). */
    int high_source_valid;
    uint64_t high_source_frame;
    uint64_t high_source_output_end;
    uint32_t high_source_epoch;
    uint32_t high_source_generation;

    /* Output endpoint of the highest source range (for the source finish). */
    int source_end_valid;
    uint64_t source_end_output_frame;

    /* Last verified sample (the monotonic anchor); have_anchor is 0 after any
     * invalidation so the next consistent query re-anchors. */
    int have_anchor;
    uint64_t last_playhead;
    uint64_t last_sample_us;

    /* One-shot forced-invalid status consumed by the next query. */
    int pending_status;

    /* Persistent fence: stays invalid until explicitly reconciled. */
    int fenced;
    int fence_status;
};

static void reset_anchor(struct le_audio_timing *timing)
{
    timing->have_anchor = 0;
    timing->last_playhead = 0u;
    timing->last_sample_us = 0u;
}

static void mark_invalid(struct le_audio_timing_sample *out, int status)
{
    out->valid = 0;
    out->status = status;
    out->playhead_frames = 0u;
    out->sample_us = 0u;
}

static uint64_t physical_submitted(const struct le_audio_timing *timing)
{
    return timing->accepted_frames - timing->dropped_frames;
}

struct le_audio_timing *le_audio_timing_create(
    const struct le_audio_timing_config *config)
{
    struct le_audio_timing *timing;

    if (!config || !config->pcm || config->rate == 0u)
        return NULL;
    /* An undeclared clock domain can never produce trustworthy DAC timing. */
    if (config->clock != LE_AUDIO_TIMING_CLOCK_MONOTONIC)
        return NULL;

    timing = calloc(1u, sizeof(*timing));
    if (!timing)
        return NULL;
    timing->pcm = config->pcm;
    timing->rate = config->rate;
    timing->clock = config->clock;
    timing->output_latency_us = config->output_latency_us;
    timing->latency_calibrated = config->latency_calibrated ? 1 : 0;
    timing->buffer_frames = config->buffer_frames;
    if (timing->buffer_frames == 0u) {
        if (!pcm_is_ready(config->pcm)) {
            free(timing);
            return NULL;
        }
        timing->buffer_frames = pcm_get_buffer_size(config->pcm);
    }
    if (timing->buffer_frames == 0u) {
        free(timing);
        return NULL;
    }
    return timing;
}

void le_audio_timing_destroy(struct le_audio_timing *timing)
{
    free(timing);
}

uint64_t le_audio_timing_submitted(const struct le_audio_timing *timing)
{
    return timing ? timing->accepted_frames : 0u;
}

uint64_t le_audio_timing_dropped(const struct le_audio_timing *timing)
{
    return timing ? timing->dropped_frames : 0u;
}

uint64_t le_audio_timing_unplayed(const struct le_audio_timing *timing)
{
    uint64_t physical;

    if (!timing)
        return 0u;
    physical = physical_submitted(timing);
    if (timing->played_floor >= physical)
        return 0u;
    return physical - timing->played_floor;
}

void le_audio_timing_fence(struct le_audio_timing *timing, int status)
{
    if (!timing)
        return;
    if (status == LE_AUDIO_TIMING_OK)
        status = LE_AUDIO_TIMING_ERR_RECONCILE;
    timing->fenced = 1;
    timing->fence_status = status;
    timing->pending_status = LE_AUDIO_TIMING_OK;
    reset_anchor(timing);
}

void le_audio_timing_invalidate(struct le_audio_timing *timing, int status)
{
    if (!timing)
        return;
    /* XRUN is a dropping discontinuity: never re-anchor on the stale submitted
     * baseline.  The caller must reconcile explicitly. */
    if (status == LE_AUDIO_TIMING_ERR_XRUN) {
        le_audio_timing_fence(timing, status);
        return;
    }
    if (status == LE_AUDIO_TIMING_OK)
        status = LE_AUDIO_TIMING_ERR_INVALID;
    timing->pending_status = status;
    reset_anchor(timing);
}

int le_audio_timing_query(struct le_audio_timing *timing,
                          struct le_audio_timing_sample *out)
{
    struct timespec tstamp;
    struct timespec now;
    unsigned int avail = 0u;
    uint32_t in_flight;
    uint64_t physical;
    uint64_t playhead;
    uint64_t sample_us;
    uint64_t now_us;
    int64_t lead;

    if (!timing || !out)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    memset(out, 0, sizeof(*out));

    if (timing->fenced) {
        out->valid = 0;
        out->status = timing->fence_status;
        return LE_AUDIO_TIMING_OK;
    }

    if (timing->pending_status != LE_AUDIO_TIMING_OK) {
        out->valid = 0;
        out->status = timing->pending_status;
        timing->pending_status = LE_AUDIO_TIMING_OK;
        return LE_AUDIO_TIMING_OK;
    }

    if (!pcm_is_ready(timing->pcm)) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_QUERY);
        return LE_AUDIO_TIMING_OK;
    }
    if (pcm_get_htimestamp(timing->pcm, &avail, &tstamp) != 0) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_QUERY);
        return LE_AUDIO_TIMING_OK;
    }
    out->avail_frames = avail;
    if (avail > timing->buffer_frames) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_RANGE);
        return LE_AUDIO_TIMING_OK;
    }

    in_flight = timing->buffer_frames - avail;
    out->in_flight_frames = in_flight;
    physical = physical_submitted(timing);
    if ((uint64_t)in_flight > physical) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_RANGE);
        return LE_AUDIO_TIMING_OK;
    }
    playhead = physical - (uint64_t)in_flight;

    /* Startup settle: a zero timestamp means the stream has not produced a
     * hardware sample yet; it is never treated as time zero. */
    if (tstamp.tv_sec == 0 && tstamp.tv_nsec == 0) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_SKEW);
        return LE_AUDIO_TIMING_OK;
    }
    sample_us = (uint64_t)tstamp.tv_sec * 1000000u +
                (uint64_t)tstamp.tv_nsec / 1000u;

    /* Bind the timestamp to a freshly sampled clock in the DECLARED domain.
     * A timestamp in another domain (e.g. REALTIME) or a stale/future one
     * fails the bounded window instead of becoming invented DAC evidence. */
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_SKEW);
        return LE_AUDIO_TIMING_OK;
    }
    now_us = (uint64_t)now.tv_sec * 1000000u + (uint64_t)now.tv_nsec / 1000u;
    lead = (int64_t)sample_us - (int64_t)now_us;
    if (lead > (int64_t)LE_AUDIO_TIMING_MAX_SAMPLE_FUTURE_US ||
        lead < -(int64_t)LE_AUDIO_TIMING_MAX_SAMPLE_AGE_US) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_SKEW);
        return LE_AUDIO_TIMING_OK;
    }

    /* The physical DAC cursor can never move backwards.  A playhead below the
     * highest verified value means frames were dropped without reconciliation
     * or the ring was reset behind our back: fail closed, do not publish. */
    if (playhead < timing->played_floor) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_NONMONOTONIC);
        return LE_AUDIO_TIMING_OK;
    }

    if (timing->have_anchor &&
        (playhead < timing->last_playhead ||
         sample_us < timing->last_sample_us)) {
        reset_anchor(timing);
        mark_invalid(out, LE_AUDIO_TIMING_ERR_NONMONOTONIC);
        return LE_AUDIO_TIMING_OK;
    }

    out->valid = 1;
    out->status = LE_AUDIO_TIMING_OK;
    out->playhead_frames = playhead;
    out->sample_us = sample_us;
    timing->have_anchor = 1;
    timing->last_playhead = playhead;
    timing->last_sample_us = sample_us;
    if (playhead > timing->played_floor)
        timing->played_floor = playhead;
    return LE_AUDIO_TIMING_OK;
}

/* Absolute finish time of hardware output frame `h_end`; handles a past
 * endpoint with checked signed arithmetic. */
static int finish_at(const struct le_audio_timing *timing, uint64_t h_end,
                     uint64_t *out_finish_us)
{
    uint64_t delta_us;
    uint64_t finish_us;

    if (!timing->have_anchor || !timing->latency_calibrated)
        return LE_AUDIO_TIMING_ERR_INVALID;

    if (h_end >= timing->last_playhead) {
        uint64_t remaining = h_end - timing->last_playhead;

        if (remaining > UINT64_MAX / 1000000u)
            return LE_AUDIO_TIMING_ERR_INVALID;
        delta_us = remaining * 1000000u / timing->rate;
        if (delta_us > UINT64_MAX - timing->last_sample_us)
            return LE_AUDIO_TIMING_ERR_INVALID;
        finish_us = timing->last_sample_us + delta_us;
    } else {
        uint64_t back = timing->last_playhead - h_end;

        if (back > UINT64_MAX / 1000000u)
            return LE_AUDIO_TIMING_ERR_INVALID;
        delta_us = back * 1000000u / timing->rate;
        if (delta_us > timing->last_sample_us)
            return LE_AUDIO_TIMING_ERR_INVALID;
        finish_us = timing->last_sample_us - delta_us;
    }

    if ((uint64_t)timing->output_latency_us > UINT64_MAX - finish_us)
        return LE_AUDIO_TIMING_ERR_INVALID;
    finish_us += (uint64_t)timing->output_latency_us;

    *out_finish_us = finish_us;
    return LE_AUDIO_TIMING_OK;
}

static int finish_from(const struct le_audio_timing *timing, uint64_t h_end_frame,
                       uint64_t *out_finish_us)
{
    if (!timing || !out_finish_us)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    if (!timing->have_anchor || !timing->latency_calibrated)
        return LE_AUDIO_TIMING_ERR_INVALID;
    if (h_end_frame < timing->last_playhead)
        return LE_AUDIO_TIMING_ERR_INVALID;
    return finish_at(timing, h_end_frame, out_finish_us);
}

int le_audio_timing_finish_us(const struct le_audio_timing *timing,
                              uint64_t h_end_frame, uint64_t *out_finish_us)
{
    return finish_from(timing, h_end_frame, out_finish_us);
}

int le_audio_timing_source_finish_us(const struct le_audio_timing *timing,
                                     uint64_t *out_finish_us)
{
    uint64_t endpoint;

    if (!timing || !out_finish_us)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    if (!timing->have_anchor || !timing->latency_calibrated ||
        !timing->source_end_valid)
        return LE_AUDIO_TIMING_ERR_INVALID;

    endpoint = timing->source_end_output_frame;
    /* A source endpoint already behind the playhead may only be reconstructed
     * while the bounded fresh-sample window still justifies it; otherwise the
     * estimate is refused rather than fabricated. */
    if (endpoint < timing->last_playhead) {
        uint64_t back = timing->last_playhead - endpoint;
        uint64_t back_us;

        if (back > UINT64_MAX / 1000000u)
            return LE_AUDIO_TIMING_ERR_INVALID;
        back_us = back * 1000000u / timing->rate;
        if (back_us > LE_AUDIO_TIMING_MAX_SAMPLE_AGE_US)
            return LE_AUDIO_TIMING_ERR_INVALID;
    }
    return finish_at(timing, endpoint, out_finish_us);
}

int le_audio_timing_submit(struct le_audio_timing *timing,
                           const struct le_audio_timing_submission *submission)
{
    struct le_audio_timing_entry *entry;
    uint64_t output_first;

    if (!timing || !submission)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    if (submission->frames == 0u)
        return LE_AUDIO_TIMING_ERR_RANGE;
    if (timing->count >= (size_t)LE_AUDIO_LEDGER_CAPACITY)
        return LE_AUDIO_TIMING_ERR_RANGE;
    if ((uint64_t)submission->frames > UINT64_MAX - timing->accepted_frames)
        return LE_AUDIO_TIMING_ERR_RANGE;
    /* A real-source range carries its own cumulative cursor; refuse a
     * source_first_frame + frames overflow here (quality O2) so the checked
     * arithmetic is complete.  generated padding ignores the cursor. */
    if (!submission->generated &&
        submission->source_first_frame > UINT64_MAX - (uint64_t)submission->frames)
        return LE_AUDIO_TIMING_ERR_RANGE;

    output_first = physical_submitted(timing);
    if ((uint64_t)submission->frames > UINT64_MAX - output_first)
        return LE_AUDIO_TIMING_ERR_RANGE;

    entry = &timing->entries[(timing->start + timing->count) %
                             (size_t)LE_AUDIO_LEDGER_CAPACITY];
    entry->output_first_frame = output_first;
    entry->source_first_frame = submission->generated
                                    ? 0u
                                    : submission->source_first_frame;
    entry->frames = submission->frames;
    entry->epoch = submission->epoch;
    entry->generation = submission->generation;
    entry->generated = submission->generated ? 1 : 0;

    timing->count++;
    timing->accepted_frames += (uint64_t)submission->frames;

    if (!entry->generated) {
        uint64_t end = output_first + (uint64_t)submission->frames;

        if (!timing->source_end_valid || end > timing->source_end_output_frame)
            timing->source_end_output_frame = end;
        timing->source_end_valid = 1;
    }
    return LE_AUDIO_TIMING_OK;
}

int le_audio_timing_drop_unplayed(struct le_audio_timing *timing, uint64_t frames)
{
    uint64_t boundary;
    uint64_t new_boundary;

    if (!timing)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    if (frames > le_audio_timing_unplayed(timing))
        return LE_AUDIO_TIMING_ERR_RANGE;

    boundary = physical_submitted(timing);
    new_boundary = boundary - frames;

    /* The dropped frames are the tail of the timeline: prune/patch the ledger
     * from the back so no dropped source frame or successor is credited. */
    while (timing->count > 0u) {
        struct le_audio_timing_entry *entry =
            &timing->entries[(timing->start + timing->count - 1u) %
                             (size_t)LE_AUDIO_LEDGER_CAPACITY];

        if (entry->output_first_frame >= new_boundary) {
            timing->count--;
            continue;
        }
        if (entry->output_first_frame + (uint64_t)entry->frames > new_boundary)
            entry->frames = (uint32_t)(new_boundary - entry->output_first_frame);
        break;
    }

    timing->dropped_frames += frames;
    if (timing->source_end_valid &&
        timing->source_end_output_frame > new_boundary)
        timing->source_end_output_frame = new_boundary;

    timing->fenced = 0;
    timing->fence_status = LE_AUDIO_TIMING_OK;
    reset_anchor(timing);
    return LE_AUDIO_TIMING_OK;
}

int le_audio_timing_reset(struct le_audio_timing *timing)
{
    if (!timing)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    return le_audio_timing_drop_unplayed(timing, le_audio_timing_unplayed(timing));
}

int le_audio_timing_take_completed(struct le_audio_timing *timing,
                                   struct le_audio_timing_completed *out)
{
    const struct le_audio_timing_completed_event *event;

    if (!timing || !out)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    memset(out, 0, sizeof(*out));
    if (timing->completed_count == 0u)
        return LE_AUDIO_TIMING_OK;

    event = &timing->completed[timing->completed_start];
    out->valid = 1;
    out->epoch = event->epoch;
    out->generation = event->generation;
    out->generated = event->generated;
    out->output_first_frame = event->output_first_frame;
    out->output_end_frame = event->output_end_frame;
    out->source_first_frame = event->source_first_frame;
    out->source_end_frame = event->source_end_frame;

    timing->completed_start =
        (timing->completed_start + 1u) % (size_t)LE_AUDIO_LEDGER_CAPACITY;
    timing->completed_count--;
    return LE_AUDIO_TIMING_OK;
}

static void ledger_reap(struct le_audio_timing *timing, uint64_t playhead)
{
    int stuck = 0;

    while (timing->count > 0u) {
        const struct le_audio_timing_entry *entry =
            &timing->entries[timing->start];
        uint64_t end = entry->output_first_frame + (uint64_t)entry->frames;

        if (end > playhead)
            break;

        /* Per-generation coalescing: consecutive completions of the same
         * (epoch, generation, generated) extend one bounded event instead of
         * filling the ring every hardware period. */
        if (timing->completed_count > 0u) {
            struct le_audio_timing_completed_event *last =
                &timing->completed[(timing->completed_start +
                                    timing->completed_count - 1u) %
                                   (size_t)LE_AUDIO_LEDGER_CAPACITY];

            if (last->epoch == entry->epoch &&
                last->generation == entry->generation &&
                last->generated == entry->generated) {
                last->output_end_frame = end;
                last->source_end_frame = entry->source_first_frame +
                                         (uint64_t)entry->frames;
                if (!entry->generated) {
                    timing->high_source_valid = 1;
                    timing->high_source_frame = last->source_end_frame;
                    timing->high_source_output_end = end;
                    timing->high_source_epoch = entry->epoch;
                    timing->high_source_generation = entry->generation;
                }
                timing->start = (timing->start + 1u) %
                                (size_t)LE_AUDIO_LEDGER_CAPACITY;
                timing->count--;
                continue;
            }
        }

        /* Fail closed instead of silently dropping a completion: if the ring
         * is full the entry stays in the ledger (no loss) and submissions are
         * backpressured until the caller drains. */
        if (timing->completed_count >= (size_t)LE_AUDIO_LEDGER_CAPACITY) {
            stuck = 1;
            break;
        }

        {
            struct le_audio_timing_completed_event *event =
                &timing->completed[(timing->completed_start +
                                    timing->completed_count) %
                                   (size_t)LE_AUDIO_LEDGER_CAPACITY];

            event->output_first_frame = entry->output_first_frame;
            event->output_end_frame = end;
            event->source_first_frame = entry->source_first_frame;
            event->source_end_frame = entry->source_first_frame +
                                      (uint64_t)entry->frames;
            event->epoch = entry->epoch;
            event->generation = entry->generation;
            event->generated = entry->generated;

            if (!entry->generated) {
                timing->high_source_valid = 1;
                timing->high_source_frame = event->source_end_frame;
                timing->high_source_output_end = end;
                timing->high_source_epoch = entry->epoch;
                timing->high_source_generation = entry->generation;
            }
            timing->completed_count++;
        }

        timing->start = (timing->start + 1u) % (size_t)LE_AUDIO_LEDGER_CAPACITY;
        timing->count--;
    }

    timing->drain_overflow = stuck ? 1 : 0;
}

static const struct le_audio_timing_entry *ledger_lookup(
    const struct le_audio_timing *timing, uint64_t playhead)
{
    size_t i;

    for (i = 0u; i < timing->count; ++i) {
        const struct le_audio_timing_entry *entry =
            &timing->entries[(timing->start + i) %
                             (size_t)LE_AUDIO_LEDGER_CAPACITY];

        if (playhead >= entry->output_first_frame &&
            playhead < entry->output_first_frame + (uint64_t)entry->frames)
            return entry;
    }
    return NULL;
}

int le_audio_timing_progress(struct le_audio_timing *timing,
                             struct le_audio_timing_progress *out)
{
    struct le_audio_timing_sample sample;
    const struct le_audio_timing_entry *entry;
    uint64_t finish_us;
    uint64_t source_finish_us;

    if (!timing || !out)
        return LE_AUDIO_TIMING_ERR_ARGUMENT;
    memset(out, 0, sizeof(*out));

    (void)le_audio_timing_query(timing, &sample);
    out->valid = sample.valid;
    out->status = sample.status;
    out->fenced = timing->fenced;
    out->submitted_frames = timing->accepted_frames;
    out->dropped_frames = timing->dropped_frames;
    out->played_frames = sample.playhead_frames;
    out->sample_us = sample.sample_us;
    if (!sample.valid)
        return LE_AUDIO_TIMING_OK;

    ledger_reap(timing, sample.playhead_frames);
    out->drain_overflow = timing->drain_overflow;
    if (timing->drain_overflow) {
        /* The completed ring is still full: surface it (fail closed) rather
         * than reporting a stale high-water as if nothing were pending. */
        out->valid = 0;
        out->status = LE_AUDIO_TIMING_ERR_RANGE;
        return LE_AUDIO_TIMING_OK;
    }

    entry = ledger_lookup(timing, sample.playhead_frames);
    if (entry) {
        if (!entry->generated) {
            out->epoch = entry->epoch;
            out->generation = entry->generation;
            out->generated = 0;
            out->source_valid = 1;
            out->source_frame = entry->source_first_frame +
                                (sample.playhead_frames -
                                 entry->output_first_frame);
        } else {
            out->generated = 1;
            if (timing->high_source_valid) {
                out->epoch = timing->high_source_epoch;
                out->generation = timing->high_source_generation;
                out->source_valid = 1;
                out->source_frame = timing->high_source_frame;
            } else {
                out->epoch = entry->epoch;
                out->generation = entry->generation;
            }
        }
    } else if (timing->high_source_valid) {
        /* Reaped/drained: surface the monotone completed source high-water so
         * the final source tail survives trailing engine silence. */
        out->epoch = timing->high_source_epoch;
        out->generation = timing->high_source_generation;
        out->source_valid = 1;
        out->source_frame = timing->high_source_frame;
    }

    if (finish_from(timing, physical_submitted(timing), &finish_us) ==
        LE_AUDIO_TIMING_OK) {
        out->finish_valid = 1;
        out->finish_us = finish_us;
    }
    if (le_audio_timing_source_finish_us(timing, &source_finish_us) ==
        LE_AUDIO_TIMING_OK) {
        out->source_finish_valid = 1;
        out->source_finish_us = source_finish_us;
    }
    return LE_AUDIO_TIMING_OK;
}
