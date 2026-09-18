#ifndef LIBREECHO_PLAYBACK_DRAIN_H
#define LIBREECHO_PLAYBACK_DRAIN_H

#include <stddef.h>
#include <stdint.h>

#define PLAYBACK_DRAIN_MEDIA        (1U << 0)
#define PLAYBACK_DRAIN_SYSTEM       (1U << 1)
#define PLAYBACK_DRAIN_ANNOUNCEMENT (1U << 2)
#define PLAYBACK_DRAIN_ALARM        (1U << 3)
#define PLAYBACK_DRAIN_BUS_COUNT 4U

struct playback_drain {
    uint64_t submitted_frames;
    uint64_t last_frame[PLAYBACK_DRAIN_BUS_COUNT];
};

void playback_drain_init(struct playback_drain *drain);
void playback_drain_submit(struct playback_drain *drain,
                           unsigned int bus_mask, unsigned int frames);
uint64_t playback_drain_played_frames(const struct playback_drain *drain,
                                      long hardware_delay_frames);
uint64_t playback_drain_pending_frames(const struct playback_drain *drain,
                                       unsigned int bus,
                                       size_t source_pending_bytes,
                                       long hardware_delay_frames);
int playback_drain_bus_drained(const struct playback_drain *drain,
                               unsigned int bus,
                               size_t source_pending_bytes,
                               long hardware_delay_frames);

#endif
