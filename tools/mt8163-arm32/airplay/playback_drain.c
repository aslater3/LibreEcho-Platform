#include "playback_drain.h"

#include <string.h>

static int bus_index(unsigned int bus)
{
    unsigned int index;

    if (!bus || (bus & (bus - 1U)))
        return -1;
    for (index = 0; index < PLAYBACK_DRAIN_BUS_COUNT; ++index)
        if (bus == (1U << index))
            return (int)index;
    return -1;
}

void playback_drain_init(struct playback_drain *drain)
{
    if (drain)
        memset(drain, 0, sizeof(*drain));
}

void playback_drain_submit(struct playback_drain *drain,
                           unsigned int bus_mask, unsigned int frames)
{
    uint64_t end;
    unsigned int index;

    if (!drain || !frames)
        return;
    end = drain->submitted_frames + frames;
    for (index = 0; index < PLAYBACK_DRAIN_BUS_COUNT; ++index)
        if (bus_mask & (1U << index))
            drain->last_frame[index] = end;
    drain->submitted_frames = end;
}

uint64_t playback_drain_played_frames(const struct playback_drain *drain,
                                      long hardware_delay_frames)
{
    uint64_t delay;

    if (!drain || hardware_delay_frames < 0)
        return 0;
    delay = hardware_delay_frames > 0 ? (uint64_t)hardware_delay_frames : 0U;
    return delay >= drain->submitted_frames
        ? 0U : drain->submitted_frames - delay;
}

uint64_t playback_drain_pending_frames(const struct playback_drain *drain,
                                       unsigned int bus,
                                       size_t source_pending_bytes,
                                       long hardware_delay_frames)
{
    int index = bus_index(bus);
    uint64_t played;
    uint64_t pending;

    if (!drain || index < 0)
        return 0;
    played = playback_drain_played_frames(drain, hardware_delay_frames);
    pending = drain->last_frame[index] > played
        ? drain->last_frame[index] - played : 0U;
    /* Every source bus is S16_LE stereo. Round a partial frame upward. */
    pending += (source_pending_bytes + 3U) / 4U;
    return pending;
}

int playback_drain_bus_drained(const struct playback_drain *drain,
                               unsigned int bus,
                               size_t source_pending_bytes,
                               long hardware_delay_frames)
{
    int index = bus_index(bus);

    if (!drain || index < 0)
        return 0;
    if (source_pending_bytes)
        return 0;
    if (!drain->last_frame[index])
        return 1;
    return playback_drain_pending_frames(drain, bus, 0,
                                         hardware_delay_frames) == 0;
}
