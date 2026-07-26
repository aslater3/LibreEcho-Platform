#define _POSIX_C_SOURCE 200809L

#include "aec_reference.h"

#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

typedef char le_aec_reference_header_size_must_be_40[
	sizeof(struct le_aec_reference_header) == 40 ? 1 : -1];

static uint64_t monotonic_nanoseconds(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
		return 0;
	return (uint64_t)now.tv_sec * 1000000000ULL +
	       (uint64_t)now.tv_nsec;
}

int le_aec_reference_init(struct le_aec_reference_sender *sender,
			  const char *root)
{
	int flags;
	int length;

	if (!sender || !root)
		return -1;
	memset(sender, 0, sizeof(*sender));
	sender->fd = -1;
	memset(&sender->address, 0, sizeof(sender->address));
	sender->address.sun_family = AF_UNIX;
	length = snprintf(sender->address.sun_path,
			  sizeof(sender->address.sun_path), "%s/%s",
			  root, LE_AEC_REFERENCE_SOCKET);
	if (length < 0 ||
	    (size_t)length >= sizeof(sender->address.sun_path)) {
		errno = ENAMETOOLONG;
		return -1;
	}
	sender->address_size =
		(socklen_t)(offsetof(struct sockaddr_un, sun_path) +
			    (size_t)length + 1U);
	sender->fd = socket(AF_UNIX, SOCK_DGRAM, 0);
	if (sender->fd < 0)
		return -1;
	flags = fcntl(sender->fd, F_GETFL, 0);
	if (flags < 0 || fcntl(sender->fd, F_SETFL, flags | O_NONBLOCK) < 0) {
		close(sender->fd);
		sender->fd = -1;
		return -1;
	}
	flags = fcntl(sender->fd, F_GETFD, 0);
	if (flags >= 0)
		(void)fcntl(sender->fd, F_SETFD, flags | FD_CLOEXEC);
	return 0;
}

int le_aec_reference_publish(struct le_aec_reference_sender *sender,
			     const int16_t *interleaved,
			     size_t frames,
			     unsigned int channels,
			     unsigned int activity_mask)
{
	struct le_aec_reference_packet packet;
	size_t packet_size;
	size_t frame;
	ssize_t sent;

	if (!sender || sender->fd < 0 || !interleaved ||
	    frames == 0 || frames > LE_AEC_REFERENCE_MAX_FRAMES ||
	    channels == 0 || channels > UINT16_MAX) {
		errno = EINVAL;
		return -1;
	}
	memset(&packet.header, 0, sizeof(packet.header));
	packet.header.magic = LE_AEC_REFERENCE_MAGIC;
	packet.header.version = LE_AEC_REFERENCE_VERSION;
	packet.header.header_bytes = sizeof(packet.header);
	packet.header.sequence = sender->sequence++;
	packet.header.sample_rate = LE_AEC_REFERENCE_RATE;
	packet.header.channels = 1;
	packet.header.frames = (uint16_t)frames;
	packet.header.activity_mask = activity_mask;
	packet.header.render_sample = sender->render_sample;
	packet.header.monotonic_ns = monotonic_nanoseconds();
	sender->render_sample += frames;
	for (frame = 0; frame < frames; ++frame)
		packet.samples[frame] = interleaved[frame * channels];
	packet_size = sizeof(packet.header) +
		      frames * sizeof(packet.samples[0]);
	sent = sendto(sender->fd, &packet, packet_size, MSG_DONTWAIT,
		      (const struct sockaddr *)&sender->address,
		      sender->address_size);
	if (sent == (ssize_t)packet_size)
		return 0;
	if (sent < 0 &&
	    (errno == ENOENT || errno == ECONNREFUSED || errno == EAGAIN ||
	     errno == EWOULDBLOCK || errno == ENOBUFS))
		return 1;
	return -1;
}

void le_aec_reference_close(struct le_aec_reference_sender *sender)
{
	if (!sender)
		return;
	if (sender->fd >= 0)
		close(sender->fd);
	sender->fd = -1;
}
