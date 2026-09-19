#include "playback_control.h"

#include <stdio.h>

#define CHECK(x) do { if (!(x)) { \
    fprintf(stderr, "check failed line %d: %s\n", __LINE__, #x); return 1; \
} } while (0)

int main(void)
{
    unsigned int bus = 99;

    CHECK(playback_control_parse_cancel("cancel system", &bus) == 1);
    CHECK(bus == PLAYBACK_CONTROL_SYSTEM);
    CHECK(playback_control_parse_cancel("cancel announcement", &bus) == 1);
    CHECK(bus == PLAYBACK_CONTROL_ANNOUNCEMENT);
    CHECK(playback_control_parse_cancel("cancel media", &bus) == 0);
    CHECK(playback_control_parse_cancel("cancel system extra", &bus) == 0);
    CHECK(playback_control_parse_cancel("shell anything", &bus) == 0);
    puts("playback_control: bounded cancellation PASS");
    return 0;
}
