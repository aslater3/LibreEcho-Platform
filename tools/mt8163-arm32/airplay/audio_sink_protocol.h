/*
 * LE_AUDIO_SINK/1 - canonical framed sink ABI for the LibreEcho shared PCM
 * engine.
 *
 * This header freezes the wire contract between a local producer (the
 * Sendspin companion) and the Platform-owned shared audio engine.  It is a
 * byte-exact, C99-compatible description of the messages; it is deliberately
 * usable from C, C++ and any other language.  It is the single source of
 * truth: a mirror in another repository must be checked byte-for-byte against
 * this file, never hand-maintained.
 *
 * The frozen interface is split in two layers:
 *
 *   - audio_sink_protocol.h (this file): constants, message layouts and the
 *     pure little-endian codec.  No sockets, no allocation, no side effects.
 *   - audio_sink.h / audio_sink.c: the engine-side server, bounded queue and
 *     renderer-facing API.  The socket layer is a thin codec over the same
 *     operations exposed by the API, so the wire behaviour is unit-testable
 *     without a socket and the API is reusable for an in-process producer.
 *
 * ---------------------------------------------------------------------------
 * Transport
 * ---------------------------------------------------------------------------
 *
 *   AF_UNIX, SOCK_SEQPACKET.  Default path /run/libreecho-audio/sendspin.sock.
 *   The listener creates the node with mode 0600 inside the audio runtime
 *   directory; the engine also verifies SO_PEERCRED against an allow-list (the
 *   engine's own euid, optionally uid 0).  Exactly one active client is
 *   admitted at a time; additional connections are accepted only to be closed
 *   immediately.  Message boundaries are preserved: one datagram is one
 *   message, so a truncated or oversized datagram is rejected rather than
 *   streamed.
 *
 * ---------------------------------------------------------------------------
 * Serialization
 * ---------------------------------------------------------------------------
 *
 *   Every multi-byte integer is little-endian with an explicit fixed width.
 *   Native C structs, pointers, padding and `packed` types are never sent.
 *   Reserved fields are written as zero and must be zero on receipt.  All
 *   frame counters and all time counters are 64-bit even on 32-bit targets
 *   (ARM32).
 *
 *   Common header (16 bytes), present on every message:
 *
 *     offset  size  field
 *     0       4     magic       LE_AUDIO_SINK_MAGIC
 *     4       1     version     LE_AUDIO_SINK_PROTOCOL_VERSION
 *     5       1     type        message opcode (le_audio_sink_type)
 *     6       2     flags       message-level flags; zero in version 1
 *     8       4     length      payload byte count after this header
 *     12      4     reserved    must be zero
 *
 * ---------------------------------------------------------------------------
 * Lifecycle
 * ---------------------------------------------------------------------------
 *
 *   OPEN -> OPEN_ACK -> DATA* -> (FINISH -> FINISH_ACK) | (CANCEL/RESET)
 *
 *   - OPEN carries the protocol version, source id, a monotonically
 *     increasing session generation and the requested geometry.  The engine
 *     returns a fresh engine epoch, the accepted geometry, the queue capacity
 *     and a readiness bitmap.  The engine epoch changes whenever the engine
 *     process restarts; the session generation is chosen by the client and
 *     must strictly increase for the lifetime of an epoch.
 *   - DATA carries the epoch, generation, a strictly increasing sequence
 *     (first DATA of a generation is sequence 0), the cumulative first
 *     source-frame cursor and a frame-aligned PCM payload.  A message carries
 *     at most one hardware period (LE_AUDIO_SINK_MAX_DATA_FRAMES).  The engine
 *     replies with CREDIT reporting the cumulative accepted frame count and
 *     the remaining bounded capacity.  A successful socket write is not
 *     application acceptance; only CREDIT is.
 *   - PROGRESS is emitted on request (and may be pushed by the engine in a
 *     later task).  It reports cumulative source frames handed to the
 *     hardware timeline, the estimated monotonic finish time of the last such
 *     frame, the hardware-played cursor, the queued frame count and timing
 *     validity/error flags.  Task 3 ships no DAC timing model: the engine
 *     reports invalid timing (finish_us = 0, TIMING_INVALID set) until the
 *     engine timing task supplies one.  The hardware-played cursor is
 *     per-generation: it counts frames of the reported generation the DAC has
 *     finished and restarts at zero on OPEN, never a raw global device count.
 *   - FINISH declares that no further DATA will arrive for the generation and
 *     supplies the exact total source-frame count (an exact short tail is
 *     allowed).  It must name the live generation and the last accepted
 *     sequence; a FINISH for a fenced/stale generation, or one that carries a
 *     replayed sequence, is rejected before any state changes.  Repeating a
 *     FINISH with the same exact total is idempotent and re-acked.
 *     Completion is reported only when the externally supplied physical
 *     playhead has passed that final frame; an empty software queue is not
 *     completion.
 *   - CANCEL/RESET discards only the unrendered frames of the current
 *     generation and fences it.  The reply names the fenced generation and the
 *     earliest clean hardware horizon; frames already rendered to the hardware
 *     timeline are not removed.  Only the engine's timing task can supply a
 *     valid horizon, so the flag is clear until then.
 *
 * Fail-closed rule: a stale epoch or generation, invalid geometry, malformed
 * length/reserved field, unknown or duplicate sequence, frame-cursor or
 * alignment mismatch, counter overflow, queue overload or peer disappearance
 * rejects the offending source only.  The engine keeps running and every
 * other bus is untouched.  Rejection is reported with ERROR and the session is
 * left intact so a valid client can recover or open a new generation.
 */

#ifndef LIBREECHO_AUDIO_SINK_PROTOCOL_H
#define LIBREECHO_AUDIO_SINK_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Human-readable identity of this frozen revision. */
#define LE_AUDIO_SINK_PROTOCOL_ID "LE_AUDIO_SINK/1"

/* magic: the four bytes 'S','N','K','1' read little-endian. */
#define LE_AUDIO_SINK_MAGIC 0x314B4E53u
#define LE_AUDIO_SINK_PROTOCOL_VERSION 1u
#define LE_AUDIO_SINK_HEADER_BYTES 16u

/* Never accept or emit a datagram larger than this, header included. */
#define LE_AUDIO_SINK_MAX_DATAGRAM_BYTES 16384u
#define LE_AUDIO_SINK_MAX_PAYLOAD_BYTES \
    (LE_AUDIO_SINK_MAX_DATAGRAM_BYTES - LE_AUDIO_SINK_HEADER_BYTES)

/* Canonical output geometry advertised by this version. */
#define LE_AUDIO_SINK_OUTPUT_RATE 48000u
#define LE_AUDIO_SINK_OUTPUT_CHANNELS 2u
#define LE_AUDIO_SINK_BYTES_PER_FRAME 4u   /* 2 channels * S16_LE */
#define LE_AUDIO_SINK_PERIOD_FRAMES 2048u  /* one hardware period */
#define LE_AUDIO_SINK_MAX_CAPACITY_FRAMES 4096u
#define LE_AUDIO_SINK_MAX_DATA_FRAMES LE_AUDIO_SINK_PERIOD_FRAMES

/* Default socket path and restrictive node mode. */
#define LE_AUDIO_SINK_DEFAULT_SOCKET_PATH \
    "/run/libreecho-audio/sendspin.sock"
#define LE_AUDIO_SINK_SOCKET_MODE 0600

/* Source identifiers. */
enum le_audio_sink_source {
    LE_AUDIO_SINK_SOURCE_SENDPIN = 1
};

/* Sample formats.  Only the canonical geometry is accepted in version 1. */
enum le_audio_sink_format {
    LE_AUDIO_SINK_FORMAT_S16_LE = 1
};

/* Message opcodes.  Client -> engine are 0x0n, engine -> client are 0x8n. */
enum le_audio_sink_type {
    LE_AUDIO_SINK_TYPE_OPEN = 0x01,
    LE_AUDIO_SINK_TYPE_DATA = 0x02,
    LE_AUDIO_SINK_TYPE_PROGRESS_REQ = 0x03,
    LE_AUDIO_SINK_TYPE_FINISH = 0x04,
    LE_AUDIO_SINK_TYPE_CANCEL = 0x05,
    LE_AUDIO_SINK_TYPE_RESET = 0x06,
    LE_AUDIO_SINK_TYPE_OPEN_ACK = 0x81,
    LE_AUDIO_SINK_TYPE_CREDIT = 0x82,
    LE_AUDIO_SINK_TYPE_PROGRESS = 0x83,
    LE_AUDIO_SINK_TYPE_FINISH_ACK = 0x84,
    LE_AUDIO_SINK_TYPE_RESET_ACK = 0x85,
    LE_AUDIO_SINK_TYPE_ERROR = 0x86
};

/* Status / error codes.  LE_AUDIO_SINK_OK is success; every other value is a
 * bounded, fail-closed rejection of one message or one source. */
enum le_audio_sink_status {
    LE_AUDIO_SINK_OK = 0,
    LE_AUDIO_SINK_ERR_MAGIC = 1,
    LE_AUDIO_SINK_ERR_VERSION = 2,
    LE_AUDIO_SINK_ERR_LENGTH = 3,
    LE_AUDIO_SINK_ERR_RESERVED = 4,
    LE_AUDIO_SINK_ERR_TYPE = 5,
    LE_AUDIO_SINK_ERR_SOURCE = 6,
    LE_AUDIO_SINK_ERR_GEOMETRY = 7,
    LE_AUDIO_SINK_ERR_NO_SESSION = 8,
    LE_AUDIO_SINK_ERR_ALREADY_OPEN = 9,
    LE_AUDIO_SINK_ERR_STALE_EPOCH = 10,
    LE_AUDIO_SINK_ERR_STALE_GENERATION = 11,
    LE_AUDIO_SINK_ERR_SEQUENCE = 12,
    LE_AUDIO_SINK_ERR_FRAME_CURSOR = 13,
    LE_AUDIO_SINK_ERR_FRAME_COUNT = 14,
    LE_AUDIO_SINK_ERR_ALIGNMENT = 15,
    LE_AUDIO_SINK_ERR_CAPACITY = 16,
    LE_AUDIO_SINK_ERR_COUNTER_OVERFLOW = 17,
    LE_AUDIO_SINK_ERR_FINISHED = 18,
    LE_AUDIO_SINK_ERR_PEER = 19
};

/* OPEN_ACK.readiness bitmap. */
#define LE_AUDIO_SINK_OPEN_READY 0x00000001u

/* PROGRESS.flags bitmap.  Task 3 sets TIMING_INVALID and never VALID: no DAC
 * timing model is implemented yet. */
#define LE_AUDIO_SINK_PROGRESS_TIMING_VALID 0x00000001u
#define LE_AUDIO_SINK_PROGRESS_TIMING_ERROR 0x00000002u
#define LE_AUDIO_SINK_PROGRESS_TIMING_INVALID 0x00000004u

/* RESET_ACK.flags bitmap.  HORIZON_VALID is clear until the timing task can
 * supply a hardware horizon. */
#define LE_AUDIO_SINK_RESET_HORIZON_VALID 0x00000001u

/* Payload sizes (excluding the 16-byte common header). */
#define LE_AUDIO_SINK_OPEN_PAYLOAD_BYTES 20u
#define LE_AUDIO_SINK_OPEN_ACK_PAYLOAD_BYTES 36u
#define LE_AUDIO_SINK_DATA_PREFIX_BYTES 32u
#define LE_AUDIO_SINK_CREDIT_PAYLOAD_BYTES 32u
#define LE_AUDIO_SINK_PROGRESS_REQ_PAYLOAD_BYTES 8u
#define LE_AUDIO_SINK_PROGRESS_PAYLOAD_BYTES 48u
#define LE_AUDIO_SINK_FINISH_PAYLOAD_BYTES 24u
#define LE_AUDIO_SINK_FINISH_ACK_PAYLOAD_BYTES 28u
#define LE_AUDIO_SINK_RESET_PAYLOAD_BYTES 16u
#define LE_AUDIO_SINK_RESET_ACK_PAYLOAD_BYTES 32u
#define LE_AUDIO_SINK_ERROR_PAYLOAD_BYTES 20u

/*
 * OPEN payload (20 bytes), client -> engine:
 *   0   4  source           enum le_audio_sink_source
 *   4   4  generation       client session generation (> previous)
 *   8   4  rate             requested output rate (48000)
 *   12  2  channels         requested channels (2)
 *   14  2  sample_format    enum le_audio_sink_format (S16_LE)
 *   16  4  reserved         must be zero
 *
 * OPEN_ACK payload (36 bytes), engine -> client:
 *   0   4  status           LE_AUDIO_SINK_OK or an error code
 *   4   4  epoch            engine epoch
 *   8   4  generation       echo of the accepted generation
 *   12  4  rate             accepted rate
 *   16  2  channels         accepted channels
 *   18  2  sample_format    accepted format
 *   20  4  period_frames    one hardware period (2048)
 *   24  4  capacity_frames  bounded source queue capacity (<=4096)
 *   28  4  readiness        OPEN_READY bitmap
 *   32  4  reserved         must be zero
 *
 * DATA payload (32-byte prefix followed by the PCM frames):
 *   0   4  epoch            engine epoch
 *   4   4  generation       session generation
 *   8   4  sequence         strictly increasing; first is 0
 *   12  4  reserved         must be zero
 *   16  8  first_frame      cumulative first source-frame cursor
 *   24  4  frame_count      1..LE_AUDIO_SINK_MAX_DATA_FRAMES
 *   28  4  reserved         must be zero
 *   32  4*frame_count       interleaved S16_LE PCM, frame-aligned
 *
 * CREDIT payload (32 bytes), engine -> client, replies to DATA:
 *   0   4  status           LE_AUDIO_SINK_OK or an error code
 *   4   4  epoch
 *   8   4  generation
 *   12  4  sequence         echo of the accepted sequence
 *   16  8  accepted_frames  cumulative accepted source frames
 *   24  4  capacity_remaining frames still queueable (capacity - queued)
 *   28  4  reserved         must be zero
 *
 * PROGRESS_REQ payload (8 bytes), client -> engine:
 *   0   4  epoch
 *   4   4  generation
 *
 * PROGRESS payload (48 bytes), engine -> client:
 *   0   4  epoch
 *   4   4  generation
 *   8   4  sequence         last accepted sequence (0 when none)
 *   12  4  flags            TIMING_VALID | TIMING_ERROR | TIMING_INVALID
 *   16  8  submitted_frames cumulative frames handed to the hardware timeline
 *   24  8  finish_us        monotonic estimated finish of the last submitted
 *                           source frame; 0 when timing is invalid
 * 32  8  played_frames    hardware-played cursor of this generation (resets on
 *                          OPEN); a stale generation's value is never applied
 *   40  4  queued_frames    frames buffered but not yet submitted
 *   44  4  reserved         must be zero
 *
 * FINISH payload (24 bytes), client -> engine:
 *   0   4  epoch
 *   4   4  generation
 *   8   8  exact_total_frames  exact cumulative total for the generation
 *   16  4  sequence             must equal the last accepted sequence
 *                               (0 when no DATA was accepted)
 *   20  4  reserved             must be zero
 *
 * FINISH_ACK payload (28 bytes), engine -> client:
 *   0   4  status
 *   4   4  epoch
 *   8   4  generation
 *   12  8  total_frames
 *   20  4  completed        1 only after the physical playhead passes the tail
 *   24  4  reserved         must be zero
 *
 * CANCEL/RESET payload (16 bytes), client -> engine:
 *   0   4  epoch
 *   4   4  generation
 *   8   4  reason
 *   12  4  reserved         must be zero
 *
 * RESET_ACK payload (32 bytes), engine -> client:
 *   0   4  status
 *   4   4  old_epoch
 *   8   4  old_generation    the generation that was fenced
 *   12  4  new_epoch
 *   16  8  horizon_frames    earliest clean hardware horizon; valid only when
 *                            RESET_HORIZON_VALID is set
 *   24  4  flags             RESET_HORIZON_VALID bitmap
 *   28  4  reserved          must be zero
 *
 * ERROR payload (20 bytes), engine -> client:
 *   0   4  status          error code
 *   4   4  epoch
 *   8   4  generation
 *   12  4  detail          opcode the error refers to (0 when unknown)
 *   16  4  reserved        must be zero
 */

struct le_audio_sink_header {
    uint8_t version;
    uint8_t type;
    uint16_t flags;
    uint32_t length;
};

/* --- explicit little-endian codec ---------------------------------------- */

static inline void le_audio_sink_put_u16(uint8_t *out, uint16_t value)
{
    out[0] = (uint8_t)(value & 0xffu);
    out[1] = (uint8_t)((value >> 8) & 0xffu);
}

static inline void le_audio_sink_put_u32(uint8_t *out, uint32_t value)
{
    out[0] = (uint8_t)(value & 0xffu);
    out[1] = (uint8_t)((value >> 8) & 0xffu);
    out[2] = (uint8_t)((value >> 16) & 0xffu);
    out[3] = (uint8_t)((value >> 24) & 0xffu);
}

static inline void le_audio_sink_put_u64(uint8_t *out, uint64_t value)
{
    int i;

    for (i = 0; i < 8; ++i)
        out[i] = (uint8_t)((value >> (8 * i)) & 0xffu);
}

static inline uint16_t le_audio_sink_get_u16(const uint8_t *in)
{
    return (uint16_t)((uint16_t)in[0] | ((uint16_t)in[1] << 8));
}

static inline uint32_t le_audio_sink_get_u32(const uint8_t *in)
{
    return (uint32_t)in[0] | ((uint32_t)in[1] << 8) |
           ((uint32_t)in[2] << 16) | ((uint32_t)in[3] << 24);
}

static inline uint64_t le_audio_sink_get_u64(const uint8_t *in)
{
    uint64_t value = 0;
    int i;

    for (i = 7; i >= 0; --i)
        value = (value << 8) | (uint64_t)in[i];
    return value;
}

static inline void le_audio_sink_encode_header(uint8_t *out, uint8_t type,
                                               uint32_t length)
{
    le_audio_sink_put_u32(out, LE_AUDIO_SINK_MAGIC);
    out[4] = (uint8_t)LE_AUDIO_SINK_PROTOCOL_VERSION;
    out[5] = type;
    le_audio_sink_put_u16(out + 6, 0);
    le_audio_sink_put_u32(out + 8, length);
    le_audio_sink_put_u32(out + 12, 0);
}

/*
 * Decode and validate the common header plus the datagram/declared-length
 * relationship.  Returns LE_AUDIO_SINK_OK or the first failing error code.
 * The caller has already received the whole datagram (SOCK_SEQPACKET), so
 * `size` is the real datagram length and must equal header + payload.
 */
static inline int le_audio_sink_decode_header(const uint8_t *data, size_t size,
                                              struct le_audio_sink_header *out)
{
    uint32_t reserved;

    if (!data || !out || size < LE_AUDIO_SINK_HEADER_BYTES)
        return LE_AUDIO_SINK_ERR_LENGTH;
    if (le_audio_sink_get_u32(data) != LE_AUDIO_SINK_MAGIC)
        return LE_AUDIO_SINK_ERR_MAGIC;
    out->version = data[4];
    out->type = data[5];
    out->flags = le_audio_sink_get_u16(data + 6);
    out->length = le_audio_sink_get_u32(data + 8);
    reserved = le_audio_sink_get_u32(data + 12);
    if (out->version != LE_AUDIO_SINK_PROTOCOL_VERSION)
        return LE_AUDIO_SINK_ERR_VERSION;
    if (out->flags != 0u || reserved != 0u)
        return LE_AUDIO_SINK_ERR_RESERVED;
    if (out->length > LE_AUDIO_SINK_MAX_PAYLOAD_BYTES)
        return LE_AUDIO_SINK_ERR_LENGTH;
    if ((size_t)out->length != size - LE_AUDIO_SINK_HEADER_BYTES)
        return LE_AUDIO_SINK_ERR_LENGTH;
    return LE_AUDIO_SINK_OK;
}

/*
 * Bounded cumulative frame arithmetic: add `frames` to `cursor`, failing
 * closed on 64-bit wrap.  All frame counters crossing the ABI are 64-bit so
 * the fold is identical on 32- and 64-bit targets.
 */
static inline int le_audio_sink_cursor_add(uint64_t cursor, uint32_t frames,
                                           uint64_t *out)
{
    if (!out)
        return LE_AUDIO_SINK_ERR_LENGTH;
    if ((uint64_t)frames > UINT64_MAX - cursor)
        return LE_AUDIO_SINK_ERR_COUNTER_OVERFLOW;
    *out = cursor + (uint64_t)frames;
    return LE_AUDIO_SINK_OK;
}

#ifdef __cplusplus
}
#endif

#endif /* LIBREECHO_AUDIO_SINK_PROTOCOL_H */
