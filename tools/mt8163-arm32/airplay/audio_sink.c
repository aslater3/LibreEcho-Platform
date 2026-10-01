/*
 * LE_AUDIO_SINK/1 engine-side server and bounded source queue.
 *
 * See audio_sink_protocol.h for the frozen wire contract and audio_sink.h for
 * the renderer-facing API.  This translation unit is deliberately
 * single-threaded and non-blocking: no locks, no unbounded reads or writes,
 * and every rejection is local to the offending source.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>

#if defined(__has_include)
#  if __has_include(<sys/random.h>)
#    include <sys/random.h>
#    define LE_AUDIO_SINK_HAVE_GETRANDOM 1
#  endif
#endif

#include "audio_sink.h"

#define LE_AUDIO_SINK_ACCEPT_BUDGET 2
#define LE_AUDIO_SINK_READ_BUDGET 8
#define LE_AUDIO_SINK_REPLY_BYTES 128

/* Bounded retry budget for the startup entropy read: a signal storm may
 * interrupt the read repeatedly, so give up (and fail closed) after a small
 * number of consecutive interruptions instead of spinning forever. */
#define LE_AUDIO_SINK_ENTROPY_MAX_ATTEMPTS 64

/* One advisory lock file per socket path serializes create/destroy so the
 * stale-node test-and-reclaim is atomic against a cooperating engine. */
#define LE_AUDIO_SINK_LOCK_SUFFIX ".lock"
#define LE_AUDIO_SINK_PATH_BYTES sizeof(((struct sockaddr_un *)0)->sun_path)

struct le_audio_sink {
    int listen_fd;
    int client_fd;
    int lock_fd;
    char path[LE_AUDIO_SINK_PATH_BYTES];
    char lock_path[LE_AUDIO_SINK_PATH_BYTES + sizeof(LE_AUDIO_SINK_LOCK_SUFFIX)];

    /* Filesystem identity of the node this instance bound, captured with
     * lstat(2) under the lifetime lock.  fstat(2) of a unix socket reports a
     * sockfs inode that must never be compared with the dirent identity. */
    dev_t path_dev;
    ino_t path_ino;
    int path_bound;

    uint32_t epoch;
    uint32_t capacity_frames;
    uid_t allowed_uid;
    int allow_root;

    /* Session state. */
    int active;
    int finished;
    int completed;
    uint32_t generation;
    uint32_t last_generation;
    uint32_t sequence;
    int have_sequence;
    uint64_t next_cursor;
    uint64_t accepted_frames;
    uint64_t submitted_frames;
    uint64_t played_frames;
    uint64_t finish_total;
    /* Published hardware-timing observation for the live generation (Task 4b).
     * Zero-init deliberately means TIMING_INVALID / finish 0 (fail closed) with
     * no explicit initialisation step.  Cleared on every generation change so a
     * stale sample can never leak onto a successor. */
    uint32_t timing_flags;
    uint64_t timing_finish_us;
    uint32_t last_error;

    /* Bounded frame queue (interleaved S16_LE, OUTPUT_CHANNELS per frame). */
    int16_t *queue;
    uint32_t queue_head;
    uint32_t queue_frames;

    uint8_t rx[LE_AUDIO_SINK_MAX_DATAGRAM_BYTES];
    uint8_t tx[LE_AUDIO_SINK_REPLY_BYTES];
};

/* --- small helpers -------------------------------------------------------- */

/* --- startup epoch -------------------------------------------------------- */

/*
 * The engine epoch must be FRESH across process restarts: a restarted engine
 * that happens to reuse its PID (or start within the same monotonic
 * microsecond) must never derive the same epoch, or a client holding the
 * previous epoch would be admitted.  The epoch is therefore drawn from the
 * operating system CSPRNG and creation fails closed when no reliable random
 * source is available.  There is deliberately NO clock/PID pseudo-random
 * fallback: a guessable epoch is a fail-open hole, not a degraded epoch.
 *
 * Source order is getrandom(2) (non-blocking) when the platform exposes it,
 * then /dev/urandom.  Either source is read to completion with a bounded,
 * checked loop: forward progress resets the budget, but a run of consecutive
 * interruptions is capped at LE_AUDIO_SINK_ENTROPY_MAX_ATTEMPTS so a signal
 * storm cannot spin the startup path.  A short read or an exhausted budget
 * fails closed rather than yielding a partially initialised epoch.  The bytes
 * are consumed locally and never logged.
 */
static int read_os_entropy(uint8_t *out, size_t length)
{
#ifdef LE_AUDIO_SINK_TEST_FORCE_EPOCH_ENTROPY_FAILURE
    /* Test-only seam: forces the "no reliable epoch source" path so the
     * fail-closed creation contract can be exercised on a real instance.
     * Never defined in a production build. */
    (void)out;
    (void)length;
    return -1;
#else
    size_t got;
    unsigned attempts;
    int fd;

#if defined(LE_AUDIO_SINK_HAVE_GETRANDOM)
    got = 0;
    attempts = 0;
    while (got < length) {
        ssize_t n = getrandom(out + got, length - got, GRND_NONBLOCK);

        if (n > 0) {
            got += (size_t)n;
            attempts = 0;
            continue;
        }
        if (n < 0 && errno == EINTR) {
            if (++attempts >= LE_AUDIO_SINK_ENTROPY_MAX_ATTEMPTS)
                break;  /* bounded: fall back after a signal storm */
            continue;
        }
        break;  /* ENOSYS/EAGAIN/short: fall back to /dev/urandom */
    }
    if (got == length)
        return 0;
#endif /* LE_AUDIO_SINK_HAVE_GETRANDOM */

    fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    got = 0;
    attempts = 0;
    while (got < length) {
        ssize_t n = read(fd, out + got, length - got);

        if (n > 0) {
            got += (size_t)n;
            attempts = 0;
            continue;
        }
        if (n < 0 && errno == EINTR) {
            if (++attempts >= LE_AUDIO_SINK_ENTROPY_MAX_ATTEMPTS)
                break;  /* bounded: fail closed after a signal storm */
            continue;
        }
        break;  /* short read (0) or hard error: fail closed */
    }
    if (got < length) {
        close(fd);
        return -1;  /* short read, hard error or exhausted budget: fail closed */
    }
    close(fd);
    return 0;
#endif /* LE_AUDIO_SINK_TEST_FORCE_EPOCH_ENTROPY_FAILURE */
}

static int derive_epoch(uint32_t *out)
{
    uint8_t bytes[4];

    if (!out)
        return LE_AUDIO_SINK_ERR_LENGTH;
    if (read_os_entropy(bytes, sizeof(bytes)) != 0)
        return -1;
    *out = le_audio_sink_get_u32(bytes);
    if (*out == 0u)   /* keep the historical non-zero epoch contract */
        *out = 1u;
    return LE_AUDIO_SINK_OK;
}

static void reset_session(struct le_audio_sink *sink)
{
    sink->active = 0;
    sink->finished = 0;
    sink->completed = 0;
    sink->have_sequence = 0;
    sink->sequence = 0;
    sink->next_cursor = 0;
    sink->accepted_frames = 0;
    sink->submitted_frames = 0;
    sink->played_frames = 0;
    sink->finish_total = 0;
    sink->queue_head = 0;
    sink->queue_frames = 0;
    sink->timing_flags = LE_AUDIO_SINK_PROGRESS_TIMING_INVALID;
    sink->timing_finish_us = 0;
}

static void disconnect_peer(struct le_audio_sink *sink)
{
    if (sink->client_fd >= 0) {
        close(sink->client_fd);
        sink->client_fd = -1;
    }
    /* A vanished peer fails closed for this source only: discard its unread
     * frames and forget the session, but keep the epoch and any hardware
     * progress already reported. */
    sink->active = 0;
    sink->finished = 0;
    sink->completed = 0;
    sink->have_sequence = 0;
    sink->sequence = 0;
    sink->queue_head = 0;
    sink->queue_frames = 0;
    sink->timing_flags = LE_AUDIO_SINK_PROGRESS_TIMING_INVALID;
    sink->timing_finish_us = 0;
}

/*
 * Shared session fence for every message that touches a live generation.
 * Returns LE_AUDIO_SINK_OK only when `epoch` matches the engine epoch and
 * `generation` names the currently active session; otherwise the exact
 * fail-closed error.  Keeping one predicate makes the API and the socket
 * layer reject stale epochs/generations identically.
 */
static int require_active(const struct le_audio_sink *sink, uint32_t epoch,
                          uint32_t generation)
{
    if (epoch != sink->epoch)
        return LE_AUDIO_SINK_ERR_STALE_EPOCH;
    if (sink->active)
        return (generation == sink->generation)
                   ? LE_AUDIO_SINK_OK
                   : LE_AUDIO_SINK_ERR_STALE_GENERATION;
    return (generation <= sink->last_generation)
               ? LE_AUDIO_SINK_ERR_STALE_GENERATION
               : LE_AUDIO_SINK_ERR_NO_SESSION;
}

/* Effective timing flags for the reported generation: exactly one of the
 * three PROGRESS bits, defaulting to explicit invalid (fail closed). */
static uint32_t effective_timing_flags(const struct le_audio_sink *sink)
{
    return sink->timing_flags ? sink->timing_flags
                              : LE_AUDIO_SINK_PROGRESS_TIMING_INVALID;
}

/* finish_us is published only when the timing is genuinely valid. */
static uint64_t effective_timing_finish_us(const struct le_audio_sink *sink)
{
    return (effective_timing_flags(sink) & LE_AUDIO_SINK_PROGRESS_TIMING_VALID)
               ? sink->timing_finish_us
               : 0u;
}

static void send_reply(struct le_audio_sink *sink, size_t length)
{
    ssize_t written;

    if (sink->client_fd < 0)
        return;
    written = send(sink->client_fd, sink->tx, length,
                   MSG_DONTWAIT | MSG_NOSIGNAL);
    if (written < 0 || (size_t)written != length)
        disconnect_peer(sink);
}

static void send_error(struct le_audio_sink *sink, int status, uint8_t detail)
{
    uint8_t *p = sink->tx;

    sink->last_error = (uint32_t)status;
    le_audio_sink_encode_header(p, LE_AUDIO_SINK_TYPE_ERROR,
                                LE_AUDIO_SINK_ERROR_PAYLOAD_BYTES);
    le_audio_sink_put_u32(p + 16, (uint32_t)status);
    le_audio_sink_put_u32(p + 20, sink->epoch);
    le_audio_sink_put_u32(p + 24, sink->generation);
    le_audio_sink_put_u32(p + 28, detail);
    le_audio_sink_put_u32(p + 32, 0);
    send_reply(sink, LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_ERROR_PAYLOAD_BYTES);
}

static void reply_open_ack(struct le_audio_sink *sink, int status,
                           uint32_t generation, uint32_t rate,
                           uint16_t channels, uint16_t format)
{
    uint8_t *p = sink->tx;

    le_audio_sink_encode_header(p, LE_AUDIO_SINK_TYPE_OPEN_ACK,
                                LE_AUDIO_SINK_OPEN_ACK_PAYLOAD_BYTES);
    le_audio_sink_put_u32(p + 16, (uint32_t)status);
    le_audio_sink_put_u32(p + 20, sink->epoch);
    le_audio_sink_put_u32(p + 24, generation);
    le_audio_sink_put_u32(p + 28, rate);
    le_audio_sink_put_u16(p + 32, channels);
    le_audio_sink_put_u16(p + 34, format);
    le_audio_sink_put_u32(p + 36, LE_AUDIO_SINK_PERIOD_FRAMES);
    le_audio_sink_put_u32(p + 40, sink->capacity_frames);
    le_audio_sink_put_u32(p + 44, LE_AUDIO_SINK_OPEN_READY);
    le_audio_sink_put_u32(p + 48, 0);
    send_reply(sink, LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_OPEN_ACK_PAYLOAD_BYTES);
}

static void reply_credit(struct le_audio_sink *sink, int status,
                         uint32_t generation, uint32_t sequence)
{
    uint8_t *p = sink->tx;

    le_audio_sink_encode_header(p, LE_AUDIO_SINK_TYPE_CREDIT,
                                LE_AUDIO_SINK_CREDIT_PAYLOAD_BYTES);
    le_audio_sink_put_u32(p + 16, (uint32_t)status);
    le_audio_sink_put_u32(p + 20, sink->epoch);
    le_audio_sink_put_u32(p + 24, generation);
    le_audio_sink_put_u32(p + 28, sequence);
    le_audio_sink_put_u64(p + 32, sink->accepted_frames);
    le_audio_sink_put_u32(p + 40, sink->capacity_frames - sink->queue_frames);
    le_audio_sink_put_u32(p + 44, 0);
    send_reply(sink, LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_CREDIT_PAYLOAD_BYTES);
}

static void reply_progress(struct le_audio_sink *sink)
{
    uint8_t *p = sink->tx;

    le_audio_sink_encode_header(p, LE_AUDIO_SINK_TYPE_PROGRESS,
                                LE_AUDIO_SINK_PROGRESS_PAYLOAD_BYTES);
    le_audio_sink_put_u32(p + 16, sink->epoch);
    le_audio_sink_put_u32(p + 20, sink->generation);
    le_audio_sink_put_u32(p + 24, sink->sequence);
    /* Publish the timing observation supplied for this live generation; with
     * none published this is explicit TIMING_INVALID / finish 0. */
    le_audio_sink_put_u32(p + 28, effective_timing_flags(sink));
    le_audio_sink_put_u64(p + 32, sink->submitted_frames);
    le_audio_sink_put_u64(p + 40, effective_timing_finish_us(sink));
    le_audio_sink_put_u64(p + 48, sink->played_frames);
    le_audio_sink_put_u32(p + 56, sink->queue_frames);
    le_audio_sink_put_u32(p + 60, 0);
    send_reply(sink, LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_PROGRESS_PAYLOAD_BYTES);
}

static void reply_finish_ack(struct le_audio_sink *sink, int status,
                             uint32_t generation, uint64_t total)
{
    uint8_t *p = sink->tx;

    le_audio_sink_encode_header(p, LE_AUDIO_SINK_TYPE_FINISH_ACK,
                                LE_AUDIO_SINK_FINISH_ACK_PAYLOAD_BYTES);
    le_audio_sink_put_u32(p + 16, (uint32_t)status);
    le_audio_sink_put_u32(p + 20, sink->epoch);
    le_audio_sink_put_u32(p + 24, generation);
    le_audio_sink_put_u64(p + 28, total);
    le_audio_sink_put_u32(p + 36, (uint32_t)(sink->completed ? 1 : 0));
    le_audio_sink_put_u32(p + 40, 0);
    send_reply(sink, LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_FINISH_ACK_PAYLOAD_BYTES);
}

static void reply_reset_ack(struct le_audio_sink *sink, int status,
                            uint32_t generation)
{
    uint8_t *p = sink->tx;

    le_audio_sink_encode_header(p, LE_AUDIO_SINK_TYPE_RESET_ACK,
                                LE_AUDIO_SINK_RESET_ACK_PAYLOAD_BYTES);
    le_audio_sink_put_u32(p + 16, (uint32_t)status);
    le_audio_sink_put_u32(p + 20, sink->epoch);
    le_audio_sink_put_u32(p + 24, generation);
    le_audio_sink_put_u32(p + 28, sink->epoch);
    le_audio_sink_put_u64(p + 32, 0);   /* horizon unknown until timing task */
    le_audio_sink_put_u32(p + 40, 0);   /* RESET_HORIZON_VALID clear */
    le_audio_sink_put_u32(p + 44, 0);
    send_reply(sink, LE_AUDIO_SINK_HEADER_BYTES + LE_AUDIO_SINK_RESET_ACK_PAYLOAD_BYTES);
}

/* --- protocol handlers ---------------------------------------------------- */

static void handle_open(struct le_audio_sink *sink, const uint8_t *payload,
                        uint32_t length)
{
    uint32_t source;
    uint32_t generation;
    uint32_t rate;
    uint16_t channels;
    uint16_t format;
    int status;

    if (length != LE_AUDIO_SINK_OPEN_PAYLOAD_BYTES) {
        send_error(sink, LE_AUDIO_SINK_ERR_LENGTH, LE_AUDIO_SINK_TYPE_OPEN);
        return;
    }
    if (le_audio_sink_get_u32(payload + 16) != 0u) {
        send_error(sink, LE_AUDIO_SINK_ERR_RESERVED, LE_AUDIO_SINK_TYPE_OPEN);
        return;
    }
    source = le_audio_sink_get_u32(payload);
    generation = le_audio_sink_get_u32(payload + 4);
    rate = le_audio_sink_get_u32(payload + 8);
    channels = le_audio_sink_get_u16(payload + 12);
    format = le_audio_sink_get_u16(payload + 14);
    status = le_audio_sink_session_open(sink, source, generation, rate,
                                       channels, format);
    if (status != LE_AUDIO_SINK_OK) {
        send_error(sink, status, LE_AUDIO_SINK_TYPE_OPEN);
        return;
    }
    reply_open_ack(sink, LE_AUDIO_SINK_OK, generation, rate, channels, format);
}

static void handle_data(struct le_audio_sink *sink, const uint8_t *payload,
                        uint32_t length)
{
    uint32_t epoch;
    uint32_t generation;
    uint32_t sequence;
    uint32_t frame_count;
    uint64_t first_frame;
    int status;

    if (length < LE_AUDIO_SINK_DATA_PREFIX_BYTES) {
        send_error(sink, LE_AUDIO_SINK_ERR_LENGTH, LE_AUDIO_SINK_TYPE_DATA);
        return;
    }
    if (le_audio_sink_get_u32(payload + 12) != 0u ||
        le_audio_sink_get_u32(payload + 28) != 0u) {
        send_error(sink, LE_AUDIO_SINK_ERR_RESERVED, LE_AUDIO_SINK_TYPE_DATA);
        return;
    }
    epoch = le_audio_sink_get_u32(payload);
    generation = le_audio_sink_get_u32(payload + 4);
    sequence = le_audio_sink_get_u32(payload + 8);
    first_frame = le_audio_sink_get_u64(payload + 16);
    frame_count = le_audio_sink_get_u32(payload + 24);
    if (frame_count == 0u || frame_count > LE_AUDIO_SINK_MAX_DATA_FRAMES) {
        send_error(sink, LE_AUDIO_SINK_ERR_FRAME_COUNT, LE_AUDIO_SINK_TYPE_DATA);
        return;
    }
    if (length != LE_AUDIO_SINK_DATA_PREFIX_BYTES +
                  frame_count * LE_AUDIO_SINK_BYTES_PER_FRAME) {
        send_error(sink, LE_AUDIO_SINK_ERR_ALIGNMENT, LE_AUDIO_SINK_TYPE_DATA);
        return;
    }
    /* Byte-oriented: the wire payload carries S16_LE bytes with no alignment
     * guarantee, so never form a typed sample pointer from it. */
    status = le_audio_sink_submit(sink, payload + 32, frame_count, first_frame,
                                  epoch, generation, sequence);
    if (status != LE_AUDIO_SINK_OK) {
        send_error(sink, status, LE_AUDIO_SINK_TYPE_DATA);
        return;
    }
    reply_credit(sink, LE_AUDIO_SINK_OK, generation, sequence);
}

static void handle_progress_request(struct le_audio_sink *sink,
                                    const uint8_t *payload, uint32_t length)
{
    uint32_t epoch;
    uint32_t generation;

    if (length != LE_AUDIO_SINK_PROGRESS_REQ_PAYLOAD_BYTES) {
        send_error(sink, LE_AUDIO_SINK_ERR_LENGTH,
                   LE_AUDIO_SINK_TYPE_PROGRESS_REQ);
        return;
    }
    epoch = le_audio_sink_get_u32(payload);
    generation = le_audio_sink_get_u32(payload + 4);
    if (epoch != sink->epoch) {
        send_error(sink, LE_AUDIO_SINK_ERR_STALE_EPOCH,
                   LE_AUDIO_SINK_TYPE_PROGRESS_REQ);
        return;
    }
    if (!sink->active || generation != sink->generation) {
        send_error(sink, LE_AUDIO_SINK_ERR_STALE_GENERATION,
                   LE_AUDIO_SINK_TYPE_PROGRESS_REQ);
        return;
    }
    reply_progress(sink);
}

static void handle_finish(struct le_audio_sink *sink, const uint8_t *payload,
                          uint32_t length)
{
    uint32_t epoch;
    uint32_t generation;
    uint32_t sequence;
    uint64_t total;
    int status;

    if (length != LE_AUDIO_SINK_FINISH_PAYLOAD_BYTES) {
        send_error(sink, LE_AUDIO_SINK_ERR_LENGTH, LE_AUDIO_SINK_TYPE_FINISH);
        return;
    }
    if (le_audio_sink_get_u32(payload + 20) != 0u) {
        send_error(sink, LE_AUDIO_SINK_ERR_RESERVED, LE_AUDIO_SINK_TYPE_FINISH);
        return;
    }
    epoch = le_audio_sink_get_u32(payload);
    generation = le_audio_sink_get_u32(payload + 4);
    total = le_audio_sink_get_u64(payload + 8);
    sequence = le_audio_sink_get_u32(payload + 16);
    /* A terminal message must name the live generation and the last accepted
     * sequence; a stale/fenced generation or a replayed sequence is rejected
     * before any state changes. */
    status = require_active(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK) {
        send_error(sink, status, LE_AUDIO_SINK_TYPE_FINISH);
        return;
    }
    if (sequence != sink->sequence) {
        send_error(sink, LE_AUDIO_SINK_ERR_SEQUENCE, LE_AUDIO_SINK_TYPE_FINISH);
        return;
    }
    status = le_audio_sink_finish(sink, epoch, generation, total);
    if (status != LE_AUDIO_SINK_OK) {
        send_error(sink, status, LE_AUDIO_SINK_TYPE_FINISH);
        return;
    }
    reply_finish_ack(sink, LE_AUDIO_SINK_OK, generation, total);
}

static void handle_cancel(struct le_audio_sink *sink, const uint8_t *payload,
                          uint32_t length, uint8_t opcode)
{
    uint32_t epoch;
    uint32_t generation;
    int status;

    if (length != LE_AUDIO_SINK_RESET_PAYLOAD_BYTES) {
        send_error(sink, LE_AUDIO_SINK_ERR_LENGTH, opcode);
        return;
    }
    if (le_audio_sink_get_u32(payload + 12) != 0u) {
        send_error(sink, LE_AUDIO_SINK_ERR_RESERVED, opcode);
        return;
    }
    epoch = le_audio_sink_get_u32(payload);
    generation = le_audio_sink_get_u32(payload + 4);
    status = le_audio_sink_cancel(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK) {
        send_error(sink, status, opcode);
        return;
    }
    reply_reset_ack(sink, LE_AUDIO_SINK_OK, generation);
}

static void handle_datagram(struct le_audio_sink *sink, size_t size)
{
    struct le_audio_sink_header header;
    int status;

    if (size > LE_AUDIO_SINK_MAX_DATAGRAM_BYTES) {
        send_error(sink, LE_AUDIO_SINK_ERR_LENGTH, 0);
        return;
    }
    status = le_audio_sink_decode_header(sink->rx, size, &header);
    if (status != LE_AUDIO_SINK_OK) {
        send_error(sink, status, 0);
        return;
    }
    switch (header.type) {
    case LE_AUDIO_SINK_TYPE_OPEN:
        handle_open(sink, sink->rx + LE_AUDIO_SINK_HEADER_BYTES, header.length);
        break;
    case LE_AUDIO_SINK_TYPE_DATA:
        handle_data(sink, sink->rx + LE_AUDIO_SINK_HEADER_BYTES, header.length);
        break;
    case LE_AUDIO_SINK_TYPE_PROGRESS_REQ:
        handle_progress_request(sink, sink->rx + LE_AUDIO_SINK_HEADER_BYTES,
                                header.length);
        break;
    case LE_AUDIO_SINK_TYPE_FINISH:
        handle_finish(sink, sink->rx + LE_AUDIO_SINK_HEADER_BYTES, header.length);
        break;
    case LE_AUDIO_SINK_TYPE_CANCEL:
    case LE_AUDIO_SINK_TYPE_RESET:
        handle_cancel(sink, sink->rx + LE_AUDIO_SINK_HEADER_BYTES, header.length,
                      header.type);
        break;
    default:
        send_error(sink, LE_AUDIO_SINK_ERR_TYPE, header.type);
        break;
    }
}

/* --- socket lifecycle ----------------------------------------------------- */

static int peer_allowed_fd(struct le_audio_sink *sink, int fd)
{
    struct ucred cred;
    socklen_t length = sizeof(cred);

    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &length) != 0)
        return 0;
    return le_audio_sink_peer_allowed(cred.uid, sink->allowed_uid,
                                      sink->allow_root);
}

/* Returns 1 when a process is accepting connections on `path`, 0 when the
 * node is a socket with no live listener (safely reclaimable), and -1 when
 * liveness cannot be established -- in which case the caller must fail closed
 * without unlinking. */
static int listener_is_live(const char *path)
{
    struct sockaddr_un address;
    size_t path_length = strlen(path);
    int fd;
    int connected;
    int err;

    if (path_length == 0 || path_length >= sizeof(address.sun_path))
        return -1;
    fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, path, path_length + 1);
    connected = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (connected == 0) {
        close(fd);
        return 1;   /* connected: a live listener owns the node */
    }
    err = errno;
    close(fd);
    if (err == ECONNREFUSED || err == ENOENT)
        return 0;   /* no listener (or node gone): stale */
    if (err == EAGAIN || err == EWOULDBLOCK || err == EINPROGRESS)
        return 1;   /* connection accepted/busy: a listener is present */
    return -1;      /* unknown: never reclaim a node we cannot classify */
}

/*
 * Remove the socket node only when it is still the exact filesystem object
 * this instance bound.  A successor that rebound the same path is left
 * untouched; a non-socket or vanished node is never unlinked.
 */
static void unlink_owned_path(struct le_audio_sink *sink)
{
    struct stat st;

    if (!sink->path_bound || sink->path[0] == '\0')
        return;
    if (lstat(sink->path, &st) != 0)
        return;
    if (!S_ISSOCK(st.st_mode))
        return;
    if (st.st_dev != sink->path_dev || st.st_ino != sink->path_ino)
        return;
    unlink(sink->path);
}

/*
 * Take the per-path advisory lifetime lock.  flock(2) is released by the
 * kernel when the owning process dies, so a crashed engine leaves no stale
 * lock; a live engine holds it for the whole instance lifetime and every
 * other create fails closed here.  Bounded and non-blocking: LOCK_NB never
 * waits.
 */
static int acquire_lifetime_lock(struct le_audio_sink *sink)
{
    struct stat st;

    sink->lock_fd = open(sink->lock_path,
                         O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW,
                         LE_AUDIO_SINK_SOCKET_MODE);
    if (sink->lock_fd < 0)
        return -1;
    if (fstat(sink->lock_fd, &st) != 0 || !S_ISREG(st.st_mode)) {
        close(sink->lock_fd);
        sink->lock_fd = -1;
        return -1;
    }
    if (flock(sink->lock_fd, LOCK_EX | LOCK_NB) != 0) {
        close(sink->lock_fd);
        sink->lock_fd = -1;
        return -1;   /* a live engine already owns this path: fail closed */
    }
    return 0;
}

static int bind_listener(struct le_audio_sink *sink)
{
    struct sockaddr_un address;
    struct stat st;
    size_t path_length = strlen(sink->path);
    int fd;

    if (path_length == 0 || path_length >= sizeof(address.sun_path))
        return -1;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, sink->path, path_length + 1);

    fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;

    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        int err = errno;

        /* The lifetime lock is already held, so no cooperating engine owns
         * the path.  EADDRINUSE still covers a foreign live listener and any
         * non-socket object: reclaim only a socket with no live listener, and
         * otherwise fail closed without unlinking anything. */
        if (err != EADDRINUSE ||
            lstat(sink->path, &st) != 0 || !S_ISSOCK(st.st_mode) ||
            listener_is_live(sink->path) != 0) {
            close(fd);
            return -1;
        }
        if (unlink(sink->path) != 0) {
            close(fd);
            return -1;
        }
        if (bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
            close(fd);
            return -1;
        }
    }

    /* Capture the dirent identity under the lock.  lstat(2) is authoritative;
     * fstat(fd) of a unix socket reports a different (sockfs) inode and must
     * never be compared against the directory entry. */
    if (lstat(sink->path, &st) != 0 || !S_ISSOCK(st.st_mode)) {
        close(fd);
        return -1;
    }
    sink->path_dev = st.st_dev;
    sink->path_ino = st.st_ino;
    sink->path_bound = 1;

    if (chmod(sink->path, LE_AUDIO_SINK_SOCKET_MODE) != 0) {
        unlink_owned_path(sink);
        sink->path_bound = 0;
        close(fd);
        return -1;
    }
    if (listen(fd, 2) != 0) {
        unlink_owned_path(sink);
        sink->path_bound = 0;
        close(fd);
        return -1;
    }
    sink->listen_fd = fd;
    return 0;
}

struct le_audio_sink *le_audio_sink_create(const struct le_audio_sink_config *config)
{
    struct le_audio_sink *sink;
    uint32_t capacity;

    if (!config || !config->socket_path)
        return NULL;
    if (strlen(config->socket_path) == 0 ||
        strlen(config->socket_path) >= sizeof(sink->path))
        return NULL;
    sink = calloc(1, sizeof(*sink));
    if (!sink)
        return NULL;
    sink->listen_fd = -1;
    sink->client_fd = -1;
    sink->lock_fd = -1;
    sink->path_bound = 0;
    memcpy(sink->path, config->socket_path, strlen(config->socket_path) + 1);
    /* Derive the sibling lock path; the socket path is already bounded by
     * sizeof(sink->path), so appending the suffix always fits. */
    memcpy(sink->lock_path, sink->path, strlen(sink->path));
    memcpy(sink->lock_path + strlen(sink->path), LE_AUDIO_SINK_LOCK_SUFFIX,
           sizeof(LE_AUDIO_SINK_LOCK_SUFFIX));
    capacity = config->capacity_frames;
    if (capacity == 0u || capacity > LE_AUDIO_SINK_MAX_CAPACITY_FRAMES)
        capacity = LE_AUDIO_SINK_MAX_CAPACITY_FRAMES;
    sink->capacity_frames = capacity;
    /* No reliable OS-random epoch source: fail closed rather than admit a
     * client under a guessable or duplicated epoch. */
    if (derive_epoch(&sink->epoch) != LE_AUDIO_SINK_OK) {
        free(sink);
        return NULL;
    }
    /* Zero or (uid_t)-1 both mean "unset": default to the engine euid. */
    if (config->allowed_uid == (uid_t)-1 || config->allowed_uid == (uid_t)0)
        sink->allowed_uid = geteuid();
    else
        sink->allowed_uid = config->allowed_uid;
    sink->allow_root = config->allow_root ? 1 : 0;
    sink->queue = calloc((size_t)capacity * LE_AUDIO_SINK_OUTPUT_CHANNELS,
                         sizeof(int16_t));
    if (!sink->queue) {
        free(sink);
        return NULL;
    }
    /* Serialize create/destroy on this path for the whole instance lifetime:
     * the lock is held until destroy() so the stale-node reclaim below cannot
     * race a cooperating successor.  A live collision fails closed here. */
    if (acquire_lifetime_lock(sink) != 0) {
        free(sink->queue);
        free(sink);
        return NULL;
    }
    if (bind_listener(sink) != 0) {
        if (sink->listen_fd >= 0)
            close(sink->listen_fd);
        close(sink->lock_fd);
        free(sink->queue);
        free(sink);
        return NULL;
    }
    return sink;
}

void le_audio_sink_destroy(struct le_audio_sink *sink)
{
    if (!sink)
        return;
    if (sink->client_fd >= 0)
        close(sink->client_fd);
    if (sink->listen_fd >= 0)
        close(sink->listen_fd);
    /* Remove only the node this instance bound (verified by dirent identity),
     * never a successor's node or an unrelated file.  The lifetime lock is
     * still held, so no cooperating successor can have rebound the path. */
    unlink_owned_path(sink);
    sink->path_bound = 0;
    /* Release the lifetime lock but keep the lock inode: unlinking it could
     * race a successor that is about to acquire the same path. */
    if (sink->lock_fd >= 0)
        close(sink->lock_fd);
    free(sink->queue);
    free(sink);
}

int le_audio_sink_service(struct le_audio_sink *sink)
{
    int serviced = 0;
    int attempt;

    if (!sink || sink->listen_fd < 0)
        return -1;

    /* Service (and reap) the current client first so a peer that has gone
     * away releases ownership before a newcomer is judged "busy". */
    if (sink->client_fd >= 0) {
        int budget;
        for (budget = 0; budget < LE_AUDIO_SINK_READ_BUDGET; ++budget) {
            ssize_t received = recv(sink->client_fd, sink->rx, sizeof(sink->rx),
                                    MSG_DONTWAIT | MSG_TRUNC);
            if (received < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK)
                    break;
                if (errno == EINTR)
                    continue;
                disconnect_peer(sink);
                break;
            }
            if (received == 0) {
                disconnect_peer(sink);
                break;
            }
            serviced += 1;
            handle_datagram(sink, (size_t)received);
            if (sink->client_fd < 0)
                break;
        }
    }

    for (attempt = 0; attempt < LE_AUDIO_SINK_ACCEPT_BUDGET; ++attempt) {
        int fd = accept4(sink->listen_fd, NULL, NULL,
                         SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (fd < 0)
            break;
        if (sink->client_fd >= 0 || !peer_allowed_fd(sink, fd)) {
            close(fd);
            continue;
        }
        sink->client_fd = fd;
        serviced += 1;
    }

    return serviced;
}

uint32_t le_audio_sink_epoch(const struct le_audio_sink *sink)
{
    return sink ? sink->epoch : 0u;
}

int le_audio_sink_peer_allowed(uid_t peer_uid, uid_t allowed_uid, int allow_root)
{
    if (peer_uid == allowed_uid)
        return 1;
    if (allow_root && peer_uid == (uid_t)0)
        return 1;
    return 0;
}

int le_audio_sink_session_open(struct le_audio_sink *sink, uint32_t source,
                               uint32_t generation, uint32_t rate,
                               uint16_t channels, uint16_t sample_format)
{
    if (!sink)
        return LE_AUDIO_SINK_ERR_LENGTH;
    if (source != (uint32_t)LE_AUDIO_SINK_SOURCE_SENDPIN)
        return LE_AUDIO_SINK_ERR_SOURCE;
    if (rate != LE_AUDIO_SINK_OUTPUT_RATE ||
        channels != (uint16_t)LE_AUDIO_SINK_OUTPUT_CHANNELS ||
        sample_format != (uint16_t)LE_AUDIO_SINK_FORMAT_S16_LE)
        return LE_AUDIO_SINK_ERR_GEOMETRY;
    if (generation <= sink->last_generation)
        return LE_AUDIO_SINK_ERR_STALE_GENERATION;
    reset_session(sink);
    sink->active = 1;
    sink->generation = generation;
    sink->last_generation = generation;
    sink->last_error = 0;
    return LE_AUDIO_SINK_OK;
}

int le_audio_sink_submit(struct le_audio_sink *sink, const uint8_t *frames,
                         uint32_t frame_count, uint64_t first_frame,
                         uint32_t epoch, uint32_t generation, uint32_t sequence)
{
    uint64_t next;
    uint32_t i;
    int status;

    if (!sink)
        return LE_AUDIO_SINK_ERR_LENGTH;
    status = require_active(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK)
        return status;
    if (sink->finished)
        return LE_AUDIO_SINK_ERR_FINISHED;
    if (frame_count == 0u || frame_count > LE_AUDIO_SINK_MAX_DATA_FRAMES)
        return LE_AUDIO_SINK_ERR_FRAME_COUNT;
    if (!frames)
        return LE_AUDIO_SINK_ERR_LENGTH;
    /* The sequence is a 32-bit counter: fail closed instead of silently
     * wrapping past UINT32_MAX into a value that would alias the first DATA
     * of the generation. */
    if (sink->have_sequence && sink->sequence == UINT32_MAX)
        return LE_AUDIO_SINK_ERR_COUNTER_OVERFLOW;
    if (sink->have_sequence) {
        if (sequence != sink->sequence + 1u)
            return LE_AUDIO_SINK_ERR_SEQUENCE;
    } else if (sequence != 0u) {
        return LE_AUDIO_SINK_ERR_SEQUENCE;
    }
    if (first_frame != sink->next_cursor)
        return LE_AUDIO_SINK_ERR_FRAME_CURSOR;
    status = le_audio_sink_cursor_add(sink->next_cursor, frame_count, &next);
    if (status != LE_AUDIO_SINK_OK)
        return status;
    if (frame_count > sink->capacity_frames - sink->queue_frames)
        return LE_AUDIO_SINK_ERR_CAPACITY;

    /* Byte-oriented little-endian decode: `frames` points into the wire
     * receive buffer with no alignment guarantee, so assemble each sample
     * from bytes instead of forming a typed sample pointer. */
    for (i = 0; i < frame_count; ++i) {
        uint32_t index = (sink->queue_head + sink->queue_frames + i) %
                         sink->capacity_frames;
        int16_t *dst =
            &sink->queue[(size_t)index * LE_AUDIO_SINK_OUTPUT_CHANNELS];
        const uint8_t *src =
            frames + (size_t)i * LE_AUDIO_SINK_BYTES_PER_FRAME;
        uint32_t channel;

        for (channel = 0; channel < LE_AUDIO_SINK_OUTPUT_CHANNELS; ++channel)
            dst[channel] =
                (int16_t)le_audio_sink_get_u16(src + 2u * channel);
    }
    sink->queue_frames += frame_count;
    sink->accepted_frames += frame_count;
    sink->next_cursor = next;
    sink->sequence = sequence;
    sink->have_sequence = 1;
    return LE_AUDIO_SINK_OK;
}

int le_audio_sink_finish(struct le_audio_sink *sink, uint32_t epoch,
                         uint32_t generation, uint64_t exact_total_frames)
{
    int status;

    if (!sink)
        return LE_AUDIO_SINK_ERR_LENGTH;
    status = require_active(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK)
        return status;
    if (exact_total_frames != sink->accepted_frames)
        return LE_AUDIO_SINK_ERR_FRAME_CURSOR;
    sink->finished = 1;
    sink->finish_total = exact_total_frames;
    sink->completed = sink->played_frames >= sink->finish_total ? 1 : 0;
    return LE_AUDIO_SINK_OK;
}

int le_audio_sink_cancel(struct le_audio_sink *sink, uint32_t epoch,
                         uint32_t generation)
{
    int status;

    if (!sink)
        return LE_AUDIO_SINK_ERR_LENGTH;
    status = require_active(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK)
        return status;
    /* Discard only this generation's unrendered frames; already committed
     * hardware frames are left alone. */
    sink->queue_head = 0;
    sink->queue_frames = 0;
    sink->active = 0;
    sink->finished = 0;
    sink->completed = 0;
    sink->finish_total = 0;
    sink->have_sequence = 0;
    sink->sequence = 0;
    sink->timing_flags = LE_AUDIO_SINK_PROGRESS_TIMING_INVALID;
    sink->timing_finish_us = 0;
    return LE_AUDIO_SINK_OK;
}

size_t le_audio_sink_render(struct le_audio_sink *sink, int16_t *out,
                            size_t max_frames, uint64_t *first_frame)
{
    size_t frames;
    size_t i;

    if (!sink || !out)
        return 0;
    frames = max_frames < sink->queue_frames ? max_frames : sink->queue_frames;
    for (i = 0; i < frames; ++i) {
        uint32_t index = (sink->queue_head + (uint32_t)i) % sink->capacity_frames;
        memcpy(&out[i * LE_AUDIO_SINK_OUTPUT_CHANNELS],
               &sink->queue[(size_t)index * LE_AUDIO_SINK_OUTPUT_CHANNELS],
               LE_AUDIO_SINK_OUTPUT_CHANNELS * sizeof(int16_t));
    }
    if (first_frame)
        *first_frame = sink->submitted_frames;
    return frames;
}

int le_audio_sink_commit(struct le_audio_sink *sink, uint32_t epoch,
                         uint32_t generation, size_t frames)
{
    int status;

    if (!sink)
        return LE_AUDIO_SINK_ERR_LENGTH;
    /* A commit belongs to the generation whose frames were rendered: after a
     * CANCEL/RESET/OPEN reset it must not advance a successor's
     * hardware-timeline counter. */
    status = require_active(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK)
        return status;
    if (frames > sink->queue_frames)
        return LE_AUDIO_SINK_ERR_FRAME_COUNT;
    sink->queue_head = (sink->queue_head + (uint32_t)frames) %
                       sink->capacity_frames;
    sink->queue_frames -= (uint32_t)frames;
    sink->submitted_frames += frames;
    return LE_AUDIO_SINK_OK;
}

int le_audio_sink_note_playhead(struct le_audio_sink *sink, uint32_t epoch,
                                uint32_t generation, uint64_t played_frames)
{
    int status;

    if (!sink)
        return LE_AUDIO_SINK_ERR_LENGTH;
    /* Attribute the callback to a generation before touching any state: a
     * late report from a superseded generation must be a no-op, not evidence
     * of the successor's physical progress. */
    status = require_active(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK)
        return status;
    /* The cursor is per-generation and relative to this generation's own
     * submitted frames.  A value above what this generation actually handed
     * to the hardware is untrustworthy: reject it without moving the cursor
     * rather than clamping it upward into a false completion. */
    if (played_frames > sink->submitted_frames)
        return LE_AUDIO_SINK_ERR_FRAME_CURSOR;
    if (played_frames > sink->played_frames)
        sink->played_frames = played_frames;
    if (sink->finished && sink->played_frames >= sink->finish_total)
        sink->completed = 1;
    return LE_AUDIO_SINK_OK;
}

int le_audio_sink_note_timing(struct le_audio_sink *sink, uint32_t epoch,
                              uint32_t generation, uint32_t flags,
                              uint64_t finish_us)
{
    int status;

    if (!sink)
        return LE_AUDIO_SINK_ERR_LENGTH;
    /* Exactly one of the three PROGRESS timing bits must be set: a caller
     * cannot publish an ambiguous or reserved-best state. */
    if (flags != LE_AUDIO_SINK_PROGRESS_TIMING_VALID &&
        flags != LE_AUDIO_SINK_PROGRESS_TIMING_ERROR &&
        flags != LE_AUDIO_SINK_PROGRESS_TIMING_INVALID)
        return LE_AUDIO_SINK_ERR_RESERVED;
    /* Identity/freshness: only the live active generation may be marked. */
    status = require_active(sink, epoch, generation);
    if (status != LE_AUDIO_SINK_OK)
        return status;
    sink->timing_flags = flags;
    /* Record the horizon only for a valid sample; every other state reports 0
     * so no reader can trust an unverified finish. */
    sink->timing_finish_us =
        (flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) ? finish_us : 0u;
    return LE_AUDIO_SINK_OK;
}

int le_audio_sink_get_progress(const struct le_audio_sink *sink,
                               struct le_audio_sink_progress *out)
{
    if (!sink || !out)
        return LE_AUDIO_SINK_ERR_LENGTH;
    out->epoch = sink->epoch;
    out->generation = sink->generation;
    out->sequence = sink->sequence;
    out->flags = effective_timing_flags(sink);
    out->accepted_frames = sink->accepted_frames;
    out->submitted_frames = sink->submitted_frames;
    out->played_frames = sink->played_frames;
    out->finish_us = effective_timing_finish_us(sink);
    out->queued_frames = sink->queue_frames;
    out->capacity_frames = sink->capacity_frames;
    out->capacity_remaining = sink->capacity_frames - sink->queue_frames;
    out->last_error = sink->last_error;
    out->active = sink->active;
    out->finished = sink->finished;
    out->completed = sink->completed;
    return LE_AUDIO_SINK_OK;
}
