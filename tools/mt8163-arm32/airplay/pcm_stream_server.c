#define _GNU_SOURCE
#include "pcm_stream_server.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static void close_source(struct le_pcm_source *c)
{
    if (c->fd >= 0) close(c->fd);
    memset(c, 0, sizeof(*c)); c->fd = -1;
}
void le_pcm_server_init(struct le_pcm_server *s)
{
    unsigned int i;
    memset(s, 0, sizeof(*s)); s->listener = -1;
    for (i = 0; i < LE_PCM_CLIENTS; ++i) s->source[i].fd = -1;
}
int le_pcm_server_open(struct le_pcm_server *s, const char *root)
{
    struct sockaddr_un address;
    struct stat st;
    int length;
    le_pcm_server_init(s);
    length = snprintf(s->path, sizeof(s->path), "%s/%s", root, LE_PCM_SOCKET);
    if (length <= 0 || (size_t)length >= sizeof(s->path)) return -1;
    /* Do not replace an arbitrary node in the runtime directory. */
    if (lstat(s->path, &st) == 0) {
        if (!S_ISSOCK(st.st_mode) || unlink(s->path) < 0) return -1;
    } else if (errno != ENOENT) return -1;
    s->listener = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (s->listener < 0) return -1;
    memset(&address, 0, sizeof(address)); address.sun_family = AF_UNIX;
    memcpy(address.sun_path, s->path, (size_t)length + 1U);
    if (bind(s->listener, (struct sockaddr *)&address, sizeof(address)) < 0 ||
        chmod(s->path, 0660) < 0 || listen(s->listener, LE_PCM_CLIENTS) < 0) {
        close(s->listener); s->listener = -1; return -1;
    }
    return 0;
}
void le_pcm_server_close(struct le_pcm_server *s)
{
    unsigned int i;
    for (i = 0; i < LE_PCM_CLIENTS; ++i) close_source(&s->source[i]);
    if (s->listener >= 0) { close(s->listener); unlink(s->path); }
    s->listener = -1;
}
nfds_t le_pcm_server_pollfds(const struct le_pcm_server *s, struct pollfd *fds)
{
    unsigned int i;
    nfds_t n = 0;
    if (s->listener >= 0) fds[n++] = (struct pollfd){s->listener, POLLIN, 0};
    for (i = 0; i < LE_PCM_CLIENTS; ++i) if (s->source[i].fd >= 0) {
        /* A full producer queue supplies backpressure, not a busy POLLIN loop.
         * HUP/RDHUP still wakes cancellation while DATA is queued. */
        short events = POLLRDHUP;
        if (s->source[i].queued <= LE_PCM_QUEUE_FRAMES - LE_PCM_PACKET_FRAMES)
            events |= POLLIN;
        fds[n++] = (struct pollfd){s->source[i].fd, events, 0};
    }
    return n;
}
static void publish(struct le_pcm_source *c)
{
    unsigned char message[LE_PCM_HEADER];
    if (c->fd < 0 || !c->opened) return;
    if (c->reported_state == c->state && c->reported_accepted == c->accepted &&
        c->reported_played == c->played) return;
    le_pcm_header(message, LE_PCM_STATE, c->state, c->role);
    le_pcm_put64(message + 16, c->accepted); le_pcm_put64(message + 24, c->played);
    if (send(c->fd, message, sizeof(message), MSG_DONTWAIT | MSG_NOSIGNAL) ==
        (ssize_t)sizeof(message)) {
        c->reported_state = c->state; c->reported_accepted = c->accepted;
        c->reported_played = c->played;
    }
    /* A slow status reader never blocks render. Its next update is coalesced. */
}
static void cancel_source(struct le_pcm_server *s, struct le_pcm_source *c,
                           unsigned int state)
{
    c->state = state; c->queued = 0; c->focus = 0;
    if (state == LE_PCM_CANCELLED) ++s->cancellations;
    else ++s->failures;
    publish(c);
}
static int process(struct le_pcm_server *s, struct le_pcm_source *c,
                     const unsigned char *message, size_t bytes)
{
    unsigned int kind, frames, role;
    size_t i;
    if (bytes < LE_PCM_HEADER || le_pcm_get32(message) != LE_PCM_MAGIC ||
        le_pcm_get64(message + 16) || le_pcm_get64(message + 24)) return -1;
    kind = le_pcm_get32(message + 4); frames = le_pcm_get32(message + 8);
    role = le_pcm_get32(message + 12);
    if (kind == LE_PCM_OPEN) {
        if (c->opened || bytes != LE_PCM_HEADER || frames ||
            (role & ~LE_PCM_FOCUS) > 3U) return -1;
        c->opened = 1; c->role = role & ~LE_PCM_FOCUS;
        c->focus = !!(role & LE_PCM_FOCUS); c->state = LE_PCM_ACCEPTING;
        publish(c); return 0;
    }
    if (!c->opened || role) return -1;
    if (kind == LE_PCM_DATA) {
        if (c->state != LE_PCM_ACCEPTING || !frames || frames > LE_PCM_PACKET_FRAMES ||
            bytes != LE_PCM_HEADER + frames * LE_PCM_FRAME_BYTES ||
            frames > LE_PCM_QUEUE_FRAMES - c->queued ||
            c->accepted > UINT64_MAX - frames) return -1;
        for (i = 0; i < frames * 2U; ++i) {
            const unsigned char *v = message + LE_PCM_HEADER + i * 2U;
            c->samples[c->queued * 2U + i] = (int16_t)((uint16_t)v[0] | ((uint16_t)v[1] << 8));
        }
        c->queued += frames; c->accepted += frames; s->warm_periods = 8U; return 0;
    }
    if (bytes != LE_PCM_HEADER || frames) return -1;
    if (kind == LE_PCM_FINISH) {
        if (c->state != LE_PCM_ACCEPTING && c->state != LE_PCM_FINISHING) return -1;
        c->state = LE_PCM_FINISHING;
        if (c->played == c->accepted) { c->state = LE_PCM_DRAINED; c->focus = 0; }
    } else if (kind == LE_PCM_CANCEL) {
        cancel_source(s, c, LE_PCM_CANCELLED);
    } else if (kind == LE_PCM_QUERY) {
        c->reported_state = 0;
    } else return -1;
    publish(c); return 0;
}
void le_pcm_server_service(struct le_pcm_server *s)
{
    unsigned int i, budget;
    if (s->listener >= 0) for (budget = 0; budget < LE_PCM_CLIENTS; ++budget) {
        int fd = accept4(s->listener, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (fd < 0) break;
        for (i = 0; i < LE_PCM_CLIENTS && s->source[i].fd >= 0; ++i) {}
        if (i == LE_PCM_CLIENTS) { close(fd); ++s->failures; break; }
        s->source[i].fd = fd;
    }
    for (i = 0; i < LE_PCM_CLIENTS; ++i) {
        struct le_pcm_source *c = &s->source[i];
        struct pollfd descriptor;
        if (c->fd < 0) continue;
        descriptor = (struct pollfd){c->fd, POLLRDHUP, 0};
        /* Producers retain the connection until DRAINED. Early disconnect is
         * cancellation, even when unsent DATA/FINISH remains in the socket. */
        if (poll(&descriptor, 1, 0) > 0 &&
            (descriptor.revents & (POLLRDHUP | POLLHUP | POLLERR | POLLNVAL))) {
            if (c->state != LE_PCM_DRAINED) ++s->cancellations;
            close_source(c); continue;
        }
        for (budget = 0; budget < 8U; ++budget) {
            unsigned char message[LE_PCM_PACKET_BYTES];
            ssize_t bytes;
            if (c->queued > LE_PCM_QUEUE_FRAMES - LE_PCM_PACKET_FRAMES) {
                /* Permit FINISH/CANCEL/QUERY through a full audio queue. */
                bytes = recv(c->fd, message, LE_PCM_HEADER, MSG_PEEK | MSG_DONTWAIT);
                if (bytes < (ssize_t)LE_PCM_HEADER ||
                    le_pcm_get32(message + 4) == LE_PCM_DATA) break;
            }
            bytes = recv(c->fd, message, sizeof(message), MSG_DONTWAIT | MSG_TRUNC);
            if (bytes < 0 && errno == EINTR) continue;
            if (bytes < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
            if (bytes <= 0) { close_source(c); break; }
            if ((size_t)bytes > sizeof(message) || process(s, c, message, (size_t)bytes) < 0) {
                cancel_source(s, c, LE_PCM_FAILED);
                /* Leave the queued failure reply readable, then disconnect. */
                close_source(c); break;
            }
        }
        publish(c);
    }
}
static size_t ready_frames(const struct le_pcm_source *c, unsigned int period)
{
    if (c->fd < 0 || !c->opened ||
        (c->state != LE_PCM_ACCEPTING && c->state != LE_PCM_FINISHING)) return 0;
    if (c->queued >= period) return period;
    return c->state == LE_PCM_FINISHING ? c->queued : 0;
}
int le_pcm_server_ready(const struct le_pcm_server *s, unsigned int period)
{
    unsigned int i;
    for (i = 0; i < LE_PCM_CLIENTS; ++i) if (ready_frames(&s->source[i], period)) return 1;
    return 0;
}
int le_pcm_server_focus(const struct le_pcm_server *s)
{
    unsigned int i;
    for (i = 0; i < LE_PCM_CLIENTS; ++i)
        if (s->source[i].fd >= 0 && s->source[i].opened && s->source[i].focus) return 1;
    return 0;
}
unsigned int le_pcm_server_mask(const struct le_pcm_server *s, unsigned int period)
{
    unsigned int i, mask = 0;
    for (i = 0; i < LE_PCM_CLIENTS; ++i)
        if (ready_frames(&s->source[i], period)) mask |= 1U << s->source[i].role;
    return mask;
}
int32_t le_pcm_server_mix(const struct le_pcm_server *s, size_t frame, unsigned int period, int32_t media_gain_q15)
{
    unsigned int i;
    int32_t result = 0;
    for (i = 0; i < LE_PCM_CLIENTS; ++i) {
        const struct le_pcm_source *c = &s->source[i];
        if (frame < ready_frames(c, period)) {
            int32_t mono = ((int32_t)c->samples[frame * 2U] + c->samples[frame * 2U + 1U]) / 2;
            result += c->role == 0U ? (int32_t)(((int64_t)mono * media_gain_q15) >> 15) : mono;
        }
    }
    return result;
}
void le_pcm_server_submit(struct le_pcm_server *s, uint64_t hardware_first, unsigned int period)
{
    unsigned int i;
    for (i = 0; i < LE_PCM_CLIENTS; ++i) {
        struct le_pcm_source *c = &s->source[i];
        size_t frames = ready_frames(c, period);
        struct le_pcm_segment *segment;
        if (!frames) continue;
        if (c->segment_count == LE_PCM_SEGMENTS) {
            cancel_source(s, c, LE_PCM_FAILED); continue;
        }
        segment = &c->segments[c->segment_count++];
        segment->hardware_first = hardware_first;
        segment->stream_first = c->submitted;
        segment->frames = (unsigned int)frames;
        c->submitted += frames; c->queued -= frames;
        memmove(c->samples, c->samples + frames * 2U, c->queued * LE_PCM_FRAME_BYTES);
    }
}
void le_pcm_server_progress(struct le_pcm_server *s, uint64_t hardware_played)
{
    unsigned int i, j;
    for (i = 0; i < LE_PCM_CLIENTS; ++i) {
        struct le_pcm_source *c = &s->source[i];
        unsigned int retired = 0;
        if (c->fd < 0) continue;
        for (j = 0; j < c->segment_count; ++j) {
            struct le_pcm_segment *segment = &c->segments[j];
            uint64_t count = hardware_played > segment->hardware_first
                ? hardware_played - segment->hardware_first : 0;
            uint64_t played;
            if (count > segment->frames) count = segment->frames;
            played = segment->stream_first + count;
            if (count && played > c->played) c->played = played;
            if (count == segment->frames) ++retired; else break;
        }
        if (retired) {
            c->segment_count -= retired;
            memmove(c->segments, c->segments + retired, c->segment_count * sizeof(c->segments[0]));
        }
        if (c->state == LE_PCM_FINISHING && c->played == c->accepted) {
            c->state = LE_PCM_DRAINED; c->focus = 0;
        }
        publish(c);
    }
}
void le_pcm_server_fail(struct le_pcm_server *s)
{
    unsigned int i;
    for (i = 0; i < LE_PCM_CLIENTS; ++i)
        if (s->source[i].fd >= 0 && s->source[i].state != LE_PCM_DRAINED)
            cancel_source(s, &s->source[i], LE_PCM_FAILED);
}
