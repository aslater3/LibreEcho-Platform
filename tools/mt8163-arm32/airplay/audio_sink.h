/*
 * Engine-side LE_AUDIO_SINK/1 server and bounded source queue.
 *
 * This is the narrow, reusable interface the shared PCM engine binds to.  The
 * wire format lives in audio_sink_protocol.h; this header exposes the same
 * operations at C level so the protocol state machine is testable without a
 * socket and reusable by an in-process producer.
 *
 * Concurrency rule: the sink is single-threaded.  Every function is
 * non-blocking and bounded -- there are no queue locks and no unbounded
 * reads or writes.  The engine calls le_audio_sink_service() from its existing
 * poll loop; a socket or API handler must never wait on renderer work.
 *
 * Renderer split: le_audio_sink_render() copies queued frames without
 * consuming them; le_audio_sink_commit() advances the hardware-timeline
 * counter only after the caller has actually accepted those bytes.  A failed
 * PCM write therefore does not silently lose frames or advance progress.
 *
 * Generation safety (frozen for engine integration): the physical playhead
 * cursor is PER-GENERATION and relative to the frames that generation
 * submitted.  Every renderer callback that mutates session progress
 * (commit, note_playhead) names its (epoch, generation); a callback that does
 * not name the live generation is rejected untouched.  The engine worker must:
 *   1. call le_audio_sink_render() to peek at queued frames for the live
 *      generation and obtain their first_frame index;
 *   2. hand those bytes to the hardware ring, and only on full acceptance call
 *      le_audio_sink_commit(sink, epoch, generation, frames) with the same
 *      generation it opened;
 *   3. remember, per hardware buffer, which generation it belongs to, and feed
 *      back the observed physical cursor as
 *      le_audio_sink_note_playhead(sink, epoch, generation, played_frames)
 *      where played_frames counts frames of THAT generation the DAC has
 *      finished.  A buffer still draining after a CANCEL/RESET or a successor
 *      OPEN reports under its own (now stale) generation and is dropped.
 * The engine must therefore use its own per-generation cursor, never a raw
 * global device counter, and must not convert a stale value onto the live
 * generation.
 */

#ifndef LIBREECHO_AUDIO_SINK_H
#define LIBREECHO_AUDIO_SINK_H

#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#include "audio_sink_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

struct le_audio_sink;

struct le_audio_sink_config {
    /* Required.  Created mode 0600 by le_audio_sink_create().  The engine also
     * creates and holds an advisory lock file `<socket_path>.lock` (mode 0600)
     * in the same directory for the instance lifetime; it serializes
     * create/destroy, so a stale socket node can be reclaimed safely and a live
     * path fails closed.  The lock file is intentionally never unlinked. */
    const char *socket_path;
    /* Bounded source queue capacity in frames.  0 selects the maximum;
     * values above LE_AUDIO_SINK_MAX_CAPACITY_FRAMES are clamped. */
    uint32_t capacity_frames;
    /* Peer uid allowed on the socket.  0 or (uid_t)-1 selects the engine euid;
     * a zero-initialized config therefore fails closed to the engine's own
     * uid rather than silently opening the listener. */
    uid_t allowed_uid;
    /* Also admit uid 0 (root).  Off unless an operator opts in. */
    int allow_root;
};

/* Snapshot of the sink state for the engine status surface and tests. */
struct le_audio_sink_progress {
    uint32_t epoch;
    uint32_t generation;
    uint32_t sequence;
    uint32_t flags;               /* LE_AUDIO_SINK_PROGRESS_* bitmap */
    uint64_t accepted_frames;     /* cumulative accepted from DATA */
    uint64_t submitted_frames;    /* cumulative handed to the hardware timeline */
    uint64_t played_frames;       /* external physical playhead cursor */
    uint64_t finish_us;           /* estimated finish; 0 while invalid */
    uint32_t queued_frames;       /* buffered but not yet committed */
    uint32_t capacity_frames;
    uint32_t capacity_remaining;
    uint32_t last_error;          /* most recent error code, 0 when none */
    int active;                   /* a session is open */
    int finished;                 /* FINISH received for the generation */
    int completed;                /* physical playhead passed the exact tail */
};

/* Create the listener and its bounded queue.  Returns NULL on failure,
 * including when no reliable OS-random engine epoch source is available:
 * creation fails closed rather than admit a client under a guessable or
 * restart-duplicated epoch, and there is no clock/PID fallback. */
struct le_audio_sink *le_audio_sink_create(const struct le_audio_sink_config *config);

/* Close the listener, drop the queue and release all resources. */
void le_audio_sink_destroy(struct le_audio_sink *sink);

/*
 * Service the socket once: accept at most one pending peer (closing extras),
 * then read and answer a bounded number of datagrams.  Never blocks.  Returns
 * the number of messages serviced (>=0), or -1 when the sink is NULL or its
 * listener is not open.
 */
int le_audio_sink_service(struct le_audio_sink *sink);

/* The engine epoch, constant for the lifetime of this sink instance. */
uint32_t le_audio_sink_epoch(const struct le_audio_sink *sink);

/*
 * OPEN the given session generation for the canonical geometry.  A generation
 * must strictly exceed the previous one; opening a new generation discards the
 * previous generation's unrendered frames.  Fails closed with
 * LE_AUDIO_SINK_ERR_STALE_GENERATION or LE_AUDIO_SINK_ERR_GEOMETRY.
 */
int le_audio_sink_session_open(struct le_audio_sink *sink, uint32_t source,
                               uint32_t generation, uint32_t rate,
                               uint16_t channels, uint16_t sample_format);

/*
 * Submit one frame-aligned block for the current session.  Validates epoch,
 * generation, sequence, frame cursor and bounded capacity; rejects overload
 * instead of growing the queue.  `frames` points at
 * `frame_count * LE_AUDIO_SINK_BYTES_PER_FRAME` bytes of interleaved S16_LE
 * samples and carries no alignment requirement (the wire receive buffer is
 * byte-addressed), so it is decoded byte-for-byte.  Returns
 * LE_AUDIO_SINK_OK or a bounded error code.
 */
int le_audio_sink_submit(struct le_audio_sink *sink, const uint8_t *frames,
                         uint32_t frame_count, uint64_t first_frame,
                         uint32_t epoch, uint32_t generation, uint32_t sequence);

/*
 * Declare no further DATA for the generation and supply the exact cumulative
 * total (an exact short tail is allowed).  Does not by itself report
 * completion; the physical playhead does.
 */
int le_audio_sink_finish(struct le_audio_sink *sink, uint32_t epoch,
                         uint32_t generation, uint64_t exact_total_frames);

/*
 * Discard only the current generation's unrendered frames and fence it.
 * Frames already committed to the hardware timeline are untouched.
 */
int le_audio_sink_cancel(struct le_audio_sink *sink, uint32_t epoch,
                         uint32_t generation);

/*
 * Copy up to `max_frames` queued frames into `out` without consuming them.
 * Returns the copied frame count and sets `first_frame` to the cumulative
 * source-frame index of the first copied frame.
 */
size_t le_audio_sink_render(struct le_audio_sink *sink, int16_t *out,
                            size_t max_frames, uint64_t *first_frame);

/*
 * Consume `frames` from the head of the queue after the caller has accepted
 * them, advancing the hardware-timeline counter.  Must name the generation
 * whose frames were rendered: a commit for a fenced/superseded generation is
 * rejected untouched (LE_AUDIO_SINK_ERR_STALE_GENERATION), so a commit for
 * frames from before a reset can never advance the successor.  Returns
 * LE_AUDIO_SINK_ERR_FRAME_COUNT when more frames are requested than queued.
 */
int le_audio_sink_commit(struct le_audio_sink *sink, uint32_t epoch,
                         uint32_t generation, size_t frames);

/*
 * Supply the externally observed physical playhead cursor for one generation.
 * The cursor is per-generation (it counts frames of `generation` the DAC has
 * finished, starting from zero when that generation OPENed) and monotonic
 * within the generation: a lower value is ignored.  The (epoch, generation)
 * pair must name the live session; any other value is rejected untouched, so
 * a late callback from a superseded generation cannot move the successor.
 * A value above the frames that generation actually submitted is untrustworthy
 * and rejected (LE_AUDIO_SINK_ERR_FRAME_CURSOR) rather than clamped upward.
 * Completion of a FINISHed generation is reported only once this cursor
 * passes its exact tail.
 */
int le_audio_sink_note_playhead(struct le_audio_sink *sink, uint32_t epoch,
                                uint32_t generation, uint64_t played_frames);

/*
 * Publish a hardware-timing observation for the live generation (engine ->
 * sink).  Identity-checked and freshness-checked the same way as
 * note_playhead: a (epoch, generation) that does not name the current active
 * session is rejected untouched (LE_AUDIO_SINK_ERR_STALE_EPOCH /
 * _STALE_GENERATION), so a late sample can never mark a successor valid.
 * `flags` must be exactly one of LE_AUDIO_SINK_PROGRESS_TIMING_VALID,
 * _TIMING_ERROR or _TIMING_INVALID.  `finish_us` is recorded and published by
 * get_progress() and the PROGRESS reply only for TIMING_VALID; for any other
 * value it is reported as 0.  Opening/cancelling/losing a generation clears
 * any published timing, so it cannot leak across generations.
 */
int le_audio_sink_note_timing(struct le_audio_sink *sink, uint32_t epoch,
                              uint32_t generation, uint32_t flags,
                              uint64_t finish_us);

/* Snapshot the current progress.  `flags`/`finish_us` report the timing
 * observation last published by le_audio_sink_note_timing() for the live
 * generation; with none published (or after a generation change) they default
 * to explicit invalid timing: TIMING_INVALID set, TIMING_VALID clear,
 * finish_us == 0. */
int le_audio_sink_get_progress(const struct le_audio_sink *sink,
                              struct le_audio_sink_progress *out);

/* Pure allow-list predicate used by the accept path and tested directly. */
int le_audio_sink_peer_allowed(uid_t peer_uid, uid_t allowed_uid, int allow_root);

#ifdef __cplusplus
}
#endif

#endif /* LIBREECHO_AUDIO_SINK_H */
