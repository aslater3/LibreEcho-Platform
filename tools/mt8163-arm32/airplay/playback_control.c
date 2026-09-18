#include "playback_control.h"

#include <string.h>

int playback_control_parse_cancel(const char *message, unsigned int *bus)
{
    if (!message || !bus)
        return 0;
    if (!strcmp(message, "cancel system")) {
        *bus = PLAYBACK_CONTROL_SYSTEM;
        return 1;
    }
    if (!strcmp(message, "cancel announcement")) {
        *bus = PLAYBACK_CONTROL_ANNOUNCEMENT;
        return 1;
    }
    if (!strcmp(message, "cancel alarm")) {
        *bus = PLAYBACK_CONTROL_ALARM;
        return 1;
    }
    /* Media cancellation is deliberately not model-facing: barge-in must not
       interrupt music, AirPlay or Bluetooth playback. */
    return 0;
}
