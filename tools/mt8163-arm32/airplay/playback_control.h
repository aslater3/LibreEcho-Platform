#ifndef LIBREECHO_PLAYBACK_CONTROL_H
#define LIBREECHO_PLAYBACK_CONTROL_H

#define PLAYBACK_CONTROL_MEDIA 0U
#define PLAYBACK_CONTROL_SYSTEM 1U
#define PLAYBACK_CONTROL_ANNOUNCEMENT 2U
#define PLAYBACK_CONTROL_ALARM 3U

int playback_control_parse_cancel(const char *message, unsigned int *bus);

#endif
