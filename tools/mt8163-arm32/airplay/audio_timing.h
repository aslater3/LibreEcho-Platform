/*
 * Bounded TinyALSA hardware output-timing model for the LibreEcho shared PCM
 * engine (Task 4a helper + tests; the engine loop wiring is Task 4b).
 *
 * This module turns the pinned TinyALSA public timestamp/status API into a
 * verified absolute hardware output-frame timeline, and keeps a bounded ledger
 * that maps successful hardware submissions back onto
 * (epoch, generation, source-frame cursor) ranges.  It never pokes at opaque
 * library internals and never guesses an ioctl ABI.
 *
 * ---------------------------------------------------------------------------
 * Time model
 * ---------------------------------------------------------------------------
 * The only public timing query used is:
 *
 *     int pcm_get_htimestamp(struct pcm *, unsigned int *avail,
 *                            struct timespec *tstamp);
 *
 * For an output stream `avail` is the number of EMPTY frames that can still be
 * written, i.e. buffer_size - (appl_ptr - hw_ptr); `tstamp` is the mmap status
 * timestamp for that hw_ptr.  The pinned API therefore exposes a
 * buffer-relative depth, NOT a modular hardware pointer, so this module does
 * not invent a modular unwrap: it anchors the absolute timeline to the
 * engine's cumulative accepted-frame counter:
 *
 *     physical_playhead = accepted_frames - dropped_frames - in_flight
 *
 * `accepted_frames` counts only frames a real pcm_writei() accepted and is
 * monotone for the whole object lifetime.  `dropped_frames` counts frames a
 * hardware reset confirmed will never play (see "Dropped-frame accounting").
 * `in_flight = buffer_frames - avail`.  The engine must never add priming
 * silence it generated itself, so silence the engine manufactures occupies the
 * output timeline (through the ledger) but is never credited to a source;
 * silence that arrived as real SDK frames is recorded as an ordinary source
 * range and does count.
 *
 * The returned `tstamp` is only trusted after it has been checked against a
 * freshly sampled local clock of the DECLARED domain (CLOCK_MONOTONIC).  A
 * timestamp that is zero (stream not settled), implausibly stale, in the
 * future, or that does not belong to the declared clock domain makes the
 * sample invalid rather than producing invented DAC evidence.
 *
 * ---------------------------------------------------------------------------
 * Dropped-frame accounting (why this is NOT a submitted_frames rewind)
 * ---------------------------------------------------------------------------
 * When a hardware reset (TinyALSA underrun / SNDRV_PCM_IOCTL_PREPARE) discards
 * buffered frames, those frames were counted as accepted but never physically
 * played.  Rewinding `accepted_frames` would make a monotone published cursor
 * jump backwards, so instead this module keeps a separate monotone
 * `dropped_frames` offset: the effective output baseline is
 * `accepted_frames - dropped_frames`, and
 * `physical_playhead = accepted_frames - dropped_frames - in_flight`.
 *
 * An unknown discontinuity must never re-anchor using the stale submitted
 * baseline.  `le_audio_timing_fence()` quarantines the timeline: while fenced
 * every query/progress reports the discontinuity status and no playhead, and
 * it stays that way until the caller explicitly reconciles the timeline with
 * `le_audio_timing_drop_unplayed()`/`le_audio_timing_reset()` or recreates the
 * object.  `le_audio_timing_drop_unplayed()` is the bounded, checked reconcile:
 * it removes `frames` unplayed frames from the tail of the timeline, prunes the
 * matching ledger entries (truncating a straddling one), and never credits a
 * dropped source frame or its successor generation.
 *
 * The two discontinuity classes are deliberately distinct:
 *   * CANCEL / RESET / OPEN that leaves the hardware draining -> use
 *     `le_audio_timing_invalidate()`: the anchor is dropped, the ledger fence
 *     is kept, and the still-draining ranges keep being reported under their
 *     own (stale) generation; the next consistent query re-anchors.
 *   * a real XRUN / PCM reset that drops buffered frames -> use
 *     `le_audio_timing_fence()` (or `invalidate()` with ERR_XRUN, which fences)
 *     followed by `drop_unplayed()`/`reset()`.
 *
 * ---------------------------------------------------------------------------
 * Completed-progress drain (bounded, exactly-once)
 * ---------------------------------------------------------------------------
 * Reaped (fully played) entries are appended to a bounded completed-event ring
 * keyed by (epoch, generation) instead of overwriting a single tail, so
 * multiple completed generations cannot overwrite each other unreported.
 * `le_audio_timing_take_completed()` drains that ring oldest-first, each event
 * exactly once.  When the ring is full the model fails closed
 * (ERR_RANGE / valid clear) rather than silently dropping a completion, and
 * the entry stays in the ledger until the caller drains.
 *
 * `le_audio_timing_progress()` additionally publishes a source high-water
 * cursor, monotone within the reported `(epoch, generation)` (it re-primes to
 * the successor's own lower cursor when a new generation OPENs; the object
 * lifetime is not monotone), so the final source tail survives trailing
 * engine-generated silence, a skipped query, or a fully drained timeline
 * (source_valid=1, source_frame=end-of-source).  Repeated calls at the same
 * playhead return the same value, so a delta-based consumer emits each
 * completed tail exactly once.  `progress.generated` still reports whether the
 * current playhead lies inside engine-generated silence.
 *
 * The absolute output-tail finish stays separate: `progress.finish_us` is the
 * finish of the OUTPUT tail.  `progress.source_finish_valid`/`source_finish_us`
 * (and `le_audio_timing_source_finish_us()`) instead estimate the finish of the
 * mapped SOURCE hardware endpoint, ignoring subsequently queued engine
 * silence.  Both fail closed when the latency constant is uncalibrated; a past
 * source endpoint is only extrapolated when the bounded fresh-sample window
 * justifies it, never fabricated.
 *
 * No drift servo lives here; Sendspin owns synchronisation, this module only
 * measures.
 */

#ifndef LIBREECHO_AUDIO_TIMING_H
#define LIBREECHO_AUDIO_TIMING_H

#include <stddef.h>
#include <stdint.h>
#include <time.h>

#include <tinyalsa/pcm.h>

#ifdef __cplusplus
extern "C" {
#endif

/* --- status codes -------------------------------------------------------- */

enum le_audio_timing_status {
    LE_AUDIO_TIMING_OK = 0,
    LE_AUDIO_TIMING_ERR_ARGUMENT = 1,     /* NULL/zero argument */
    LE_AUDIO_TIMING_ERR_CLOCK = 2,        /* undeclared/unknown clock domain */
    LE_AUDIO_TIMING_ERR_QUERY = 3,        /* backend timestamp query failed */
    LE_AUDIO_TIMING_ERR_SKEW = 4,         /* timestamp stale/future/wrong domain */
    LE_AUDIO_TIMING_ERR_NONMONOTONIC = 5, /* playhead/time went backwards */
    LE_AUDIO_TIMING_ERR_RANGE = 6,        /* counter/anomaly/ledger/event overflow */
    LE_AUDIO_TIMING_ERR_XRUN = 7,         /* stream discontinuity reported */
    LE_AUDIO_TIMING_ERR_INVALID = 8,      /* no trustworthy sample yet */
    LE_AUDIO_TIMING_ERR_RECONCILE = 9,    /* fenced, awaiting explicit reconcile */
    LE_AUDIO_TIMING_ERR_CANCEL = 10,      /* producer cancel, hardware draining */
    LE_AUDIO_TIMING_ERR_RESET = 11,       /* generation reset, hardware draining */
    LE_AUDIO_TIMING_ERR_OPEN = 12         /* stream (re)open, hardware draining */
};

/* --- clock domain -------------------------------------------------------- */

/* The caller declares the clock domain its PCM was opened for.  UNKNOWN fails
 * closed: a timestamp in an unverified domain is never used as DAC evidence. */
enum le_audio_timing_clock {
    LE_AUDIO_TIMING_CLOCK_UNKNOWN = 0,
    LE_AUDIO_TIMING_CLOCK_MONOTONIC = 1
};

/* A queried hardware timestamp older than this relative to the local clock is
 * stale; a timestamp more than the future bound ahead of it is clock/query
 * skew.  Both make the sample invalid.  The age bound is also the window in
 * which a past source endpoint may still be extrapolated for a source finish. */
#define LE_AUDIO_TIMING_MAX_SAMPLE_AGE_US 200000u    /* 200 ms */
#define LE_AUDIO_TIMING_MAX_SAMPLE_FUTURE_US 20000u  /* 20 ms */

/* Fixed ledger / completed-event capacity.  With a two-period ring no more than
 * a handful of submissions are ever in flight, so this is comfortably
 * bounded. */
#define LE_AUDIO_LEDGER_CAPACITY 8u

/* --- timing object ------------------------------------------------------- */

struct le_audio_timing;

struct le_audio_timing_config {
    /* Borrowed, ready and running.  Only the public pcm_* accessors are used. */
    struct pcm *pcm;
    /* Output frames per second (must be non-zero). */
    uint32_t rate;
    /* Hardware ring capacity in frames.  0 selects pcm_get_buffer_size(pcm). */
    uint32_t buffer_frames;
    /* Declared clock domain of the PCM timestamps.  UNKNOWN fails create(). */
    enum le_audio_timing_clock clock;
    /* Calibrated constant unobserved output latency added to a finish estimate. */
    uint32_t output_latency_us;
    /* Set only once the latency constant above is actually calibrated. */
    int latency_calibrated;
};

/* Create a timing model.  Returns NULL when the config is unusable, including
 * UNKNOWN clock domain, no PCM, zero rate, or a zero/unknown buffer size. */
struct le_audio_timing *le_audio_timing_create(
    const struct le_audio_timing_config *config);
void le_audio_timing_destroy(struct le_audio_timing *timing);

struct le_audio_timing_sample {
    int valid;                 /* 1 only when the sample is trustworthy */
    int status;                /* enum le_audio_timing_status */
    uint64_t playhead_frames;  /* absolute physical DAC frames done */
    uint64_t sample_us;        /* monotonic microseconds (verified domain) */
    uint32_t avail_frames;     /* raw empty-frame count from the backend */
    uint32_t in_flight_frames; /* buffer_frames - avail, bounded */
};

/* Fold one public timestamp/status query into the absolute timeline.  A sample
 * that is stale, future, wrong-domain, non-monotonic or range-anomalous is
 * reported invalid and resets the monotonic anchor; it is never silently
 * clamped into a valid-looking value.  While the model is fenced the sample is
 * invalid (status = the fenced status) and the anchor is NOT re-established
 * from the stale submitted baseline. */
int le_audio_timing_query(struct le_audio_timing *timing,
                          struct le_audio_timing_sample *out);

/* Estimate the absolute monotonic finish time of hardware output frame
 * `h_end_frame` from the last valid sample.  Fails closed
 * (LE_AUDIO_TIMING_ERR_INVALID) when no sample is valid, the latency constant
 * is not calibrated, h_end is behind the playhead, or the microsecond
 * conversion overflows. */
int le_audio_timing_finish_us(const struct le_audio_timing *timing,
                              uint64_t h_end_frame, uint64_t *out_finish_us);

/* Estimate the finish time of the mapped SOURCE hardware endpoint (ignoring
 * any engine silence queued after it).  Fails closed when the latency constant
 * is uncalibrated, no source range is known, or a past endpoint is outside the
 * bounded fresh-sample window (never fabricated). */
int le_audio_timing_source_finish_us(const struct le_audio_timing *timing,
                                     uint64_t *out_finish_us);

/* Force the timing model invalid for a discontinuity that leaves the hardware
 * DRAINING (CANCEL/RESET/OPEN).  Any pre-discontinuity sample or anchor is
 * dropped and the next consistent query re-anchors; the ledger fence is kept,
 * so a range still draining keeps being reported under its own generation.
 * `status` is remembered and reported once.
 *
 * XRUN is special-cased: an XRUN drops buffered frames, so this fences instead
 * (see le_audio_timing_fence). */
void le_audio_timing_invalidate(struct le_audio_timing *timing, int status);

/* Fence the timeline after a discontinuity whose dropped-frame count is not yet
 * reconciled (an XRUN or a PCM reset).  Publishes `status`, drops the anchor
 * and refuses to re-anchor or report any playhead until the caller explicitly
 * reconciles with le_audio_timing_drop_unplayed()/le_audio_timing_reset() or
 * recreates the object.  Submitted-but-unplayed frames are NOT credited. */
void le_audio_timing_fence(struct le_audio_timing *timing, int status);

/* Cumulative frames the engine has reported as accepted by the PCM (monotone,
 * spans every generation of the object). */
uint64_t le_audio_timing_submitted(const struct le_audio_timing *timing);

/* Cumulative frames a hardware reset has confirmed will never play (monotone). */
uint64_t le_audio_timing_dropped(const struct le_audio_timing *timing);

/* Frames accepted but not proven played (accepted - dropped - highest playhead
 * verified).  This is the amount a full ring reset would drop; it is the
 * conservative upper bound because played progress after the last verified
 * sample is unknown. */
uint64_t le_audio_timing_unplayed(const struct le_audio_timing *timing);

/* --- submissions / ledger ------------------------------------------------ */

struct le_audio_timing_submission {
    uint32_t epoch;
    uint32_t generation;
    /* Cumulative first source frame of this range.  Ignored when generated. */
    uint64_t source_first_frame;
    uint32_t frames;      /* must be non-zero and fit the bounded ledger */
    int generated;        /* 1 = engine-generated priming silence, never
                           * credited to a source */
};

/* Record one fully-accepted hardware submission: advance the accepted cursor
 * and append its (output range -> source range) mapping.  Returns
 * LE_AUDIO_TIMING_ERR_RANGE when the bounded ledger is full or the range is
 * unusable, and the accepted cursor is left untouched on failure so nothing is
 * silently lost or double counted. */
int le_audio_timing_submit(struct le_audio_timing *timing,
                           const struct le_audio_timing_submission *submission);

/* Explicitly reconcile a hardware reset that dropped `frames` accepted but
 * unplayed frames.  Removes them from the tail of the physical timeline, prunes
 * the matching ledger entries (truncating a straddling one) and clears the
 * fence.  Bounded/checked: `frames` may not exceed le_audio_timing_unplayed();
 * on a bad request nothing changes and LE_AUDIO_TIMING_ERR_RANGE is returned,
 * so a source frame that actually played is never dropped and a dropped frame
 * is never credited.  `frames == 0` is a valid no-op reconcile (for example a
 * reset with an empty ring) that still clears the fence. */
int le_audio_timing_drop_unplayed(struct le_audio_timing *timing, uint64_t frames);

/* Convenience reconcile for a full ring reset: drops every currently unplayed
 * frame (le_audio_timing_unplayed()) and clears the fence. */
int le_audio_timing_reset(struct le_audio_timing *timing);

/* --- completed-progress drain -------------------------------------------- */

struct le_audio_timing_completed {
    int valid;                 /* 1 when this call produced an event */
    uint32_t epoch;
    uint32_t generation;
    int generated;             /* the completed range was engine silence */
    uint64_t output_first_frame;
    uint64_t output_end_frame;   /* one past the last output frame */
    uint64_t source_first_frame; /* meaningful only when !generated */
    uint64_t source_end_frame;   /* one past the last source frame */
};

/* Drain the oldest pending completed segment, exactly once, in completion
 * order.  Returns LE_AUDIO_TIMING_OK and sets out->valid=1 when an event was
 * produced, out->valid=0 when none is pending, or ERR_ARGUMENT on NULL.  A full
 * ring is surfaced through progress (ERR_RANGE, valid clear) rather than
 * silently dropped. */
int le_audio_timing_take_completed(struct le_audio_timing *timing,
                                   struct le_audio_timing_completed *out);

/* --- published progress -------------------------------------------------- */

struct le_audio_timing_progress {
    int valid;                 /* the playhead sample is trustworthy */
    int status;                /* enum le_audio_timing_status */
    int fenced;                /* awaiting explicit discontinuity reconcile */
    int drain_overflow;        /* completed ring full: drain before trusting */
    uint32_t epoch;            /* generation of the reported source progress */
    uint32_t generation;
    uint64_t submitted_frames; /* cumulative accepted by the PCM (monotone) */
    uint64_t dropped_frames;   /* cumulative dropped (monotone) */
    uint64_t played_frames;    /* absolute physical DAC frames done */
    uint64_t sample_us;        /* monotonic time of the sample */
    int source_valid;          /* a real source cursor is known */
    int generated;             /* current playhead is inside engine silence */
    uint64_t source_frame;     /* source cursor high-water, monotone within the
                                * reported (epoch, generation), not the object
                                * lifetime (a successor re-primes lower) */
    int finish_valid;          /* OUTPUT-tail finish_us is a calibrated estimate */
    uint64_t finish_us;        /* estimated finish of the output tail */
    int source_finish_valid;   /* mapped SOURCE-endpoint finish is valid */
    uint64_t source_finish_us; /* estimated finish of the source endpoint */
};

/* Sample, reap the ledger at the verified playhead and publish source-specific
 * cumulative progress.  The reported source cursor is the monotone high-water
 * reached, so the final source tail survives trailing engine silence, a skipped
 * query and a drained timeline; repeated calls at the same playhead return the
 * same value.  An unmapped playhead is never fabricated. */
int le_audio_timing_progress(struct le_audio_timing *timing,
                             struct le_audio_timing_progress *out);

#ifdef __cplusplus
}
#endif

#endif /* LIBREECHO_AUDIO_TIMING_H */
