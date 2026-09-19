#ifndef LIBREECHO_PLAYBACK_STATUS_H
#define LIBREECHO_PLAYBACK_STATUS_H

#include <limits.h>
#include <stdint.h>

#define PLAYBACK_BUS_MEDIA (1U << 0)
#define PLAYBACK_BUS_SYSTEM (1U << 1)
#define PLAYBACK_BUS_ANNOUNCEMENT (1U << 2)
#define PLAYBACK_BUS_ALARM (1U << 3)
#define PLAYBACK_STATUS_BUS_COUNT 4U

struct playback_status {
	char path[PATH_MAX];
	char temporary_path[PATH_MAX];
	unsigned int last_mask;
	unsigned int last_drained_mask;
	uint64_t last_pending[PLAYBACK_STATUS_BUS_COUNT];
	uint64_t last_frame[PLAYBACK_STATUS_BUS_COUNT];
	int published;
};

int playback_status_init(struct playback_status *status, const char *root);
int playback_status_publish(struct playback_status *status,
			    unsigned int bus_mask);
int playback_status_publish_drain(
	struct playback_status *status, unsigned int bus_mask,
	unsigned int drained_mask,
	const uint64_t pending_frames[PLAYBACK_STATUS_BUS_COUNT],
	const uint64_t last_frame[PLAYBACK_STATUS_BUS_COUNT]);

#endif
