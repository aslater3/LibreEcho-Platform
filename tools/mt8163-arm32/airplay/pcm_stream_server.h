#ifndef LIBREECHO_PCM_STREAM_SERVER_H
#define LIBREECHO_PCM_STREAM_SERVER_H
#include "pcm_stream_protocol.h"
#include <poll.h>
#define LE_PCM_CLIENTS 8U
#define LE_PCM_QUEUE_FRAMES 4096U
#define LE_PCM_SEGMENTS 8U
struct le_pcm_segment {
    uint64_t hardware_first, stream_first;
    unsigned int frames;
};
struct le_pcm_source {
    int fd, opened, focus;
    unsigned int role, state;
    uint64_t accepted, submitted, played;
    uint64_t reported_accepted, reported_played;
    unsigned int reported_state;
    size_t queued;
    int16_t samples[LE_PCM_QUEUE_FRAMES * 2U];
    struct le_pcm_segment segments[LE_PCM_SEGMENTS];
    unsigned int segment_count;
};
struct le_pcm_server {
    int listener;
    char path[108];
    struct le_pcm_source source[LE_PCM_CLIENTS];
    uint64_t failures, cancellations;
    unsigned int warm_periods;
};
void le_pcm_server_init(struct le_pcm_server *s);
int le_pcm_server_open(struct le_pcm_server *s, const char *root);
void le_pcm_server_close(struct le_pcm_server *s);
nfds_t le_pcm_server_pollfds(const struct le_pcm_server *s,
                              struct pollfd *fds);
void le_pcm_server_service(struct le_pcm_server *s);
int le_pcm_server_ready(const struct le_pcm_server *s, unsigned int period);
int le_pcm_server_focus(const struct le_pcm_server *s);
unsigned int le_pcm_server_mask(const struct le_pcm_server *s, unsigned int period);
/* Add one frame to the mono programme before the board dynamics/limiter. */
int32_t le_pcm_server_mix(const struct le_pcm_server *s, size_t frame,
                          unsigned int period, int32_t media_gain_q15);
/* Only call after a successful hardware write. */
void le_pcm_server_submit(struct le_pcm_server *s, uint64_t hardware_first,
                           unsigned int period);
/* A negative/unknown hardware delay must NOT be converted to played audio. */
void le_pcm_server_progress(struct le_pcm_server *s, uint64_t hardware_played);
void le_pcm_server_fail(struct le_pcm_server *s);
#endif
