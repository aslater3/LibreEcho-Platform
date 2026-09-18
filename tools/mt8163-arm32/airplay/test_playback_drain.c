#include "playback_drain.h"

#include <stdio.h>

#define CHECK(x) do { if (!(x)) { \
    fprintf(stderr, "check failed line %d: %s\n", __LINE__, #x); return 1; \
} } while (0)

int main(void)
{
    struct playback_drain drain;

    playback_drain_init(&drain);
    CHECK(playback_drain_bus_drained(&drain, PLAYBACK_DRAIN_SYSTEM, 0, 0));

    playback_drain_submit(&drain,
                          PLAYBACK_DRAIN_MEDIA | PLAYBACK_DRAIN_SYSTEM,
                          2048);
    CHECK(!playback_drain_bus_drained(&drain, PLAYBACK_DRAIN_SYSTEM, 0, 2048));
    CHECK(playback_drain_pending_frames(&drain, PLAYBACK_DRAIN_SYSTEM,
                                        0, 2048) == 2048);

    /* Media continues into a second hardware period. Once one period remains
       queued, the first period (and therefore the final system sample) has
       played even though the global PCM cannot be drained. */
    playback_drain_submit(&drain, PLAYBACK_DRAIN_MEDIA, 2048);
    CHECK(playback_drain_bus_drained(&drain, PLAYBACK_DRAIN_SYSTEM, 0, 2048));
    CHECK(!playback_drain_bus_drained(&drain, PLAYBACK_DRAIN_MEDIA, 0, 2048));

    /* Unread source data keeps the bus non-drained even after its last mixed
       period passes the hardware cursor. Selective cancellation clears only
       this pending count; it never rewinds media or the shared PCM. */
    CHECK(!playback_drain_bus_drained(&drain, PLAYBACK_DRAIN_SYSTEM, 4096, 0));
    CHECK(playback_drain_bus_drained(&drain, PLAYBACK_DRAIN_SYSTEM, 0, 0));

    printf("playback_drain: per-bus cursor PASS\n");
    return 0;
}
