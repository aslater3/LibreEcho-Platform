#ifndef LIBREECHO_AEC_REFERENCE_H
#define LIBREECHO_AEC_REFERENCE_H

#include <stddef.h>
#include <stdint.h>
#include <sys/socket.h>
#include <sys/un.h>

#define LE_AEC_REFERENCE_MAGIC 0x5241454cU
#define LE_AEC_REFERENCE_VERSION 1U
#define LE_AEC_REFERENCE_RATE 48000U
#define LE_AEC_REFERENCE_MAX_FRAMES 2048U
#define LE_AEC_REFERENCE_SOCKET "aec-reference.sock"

struct le_aec_reference_header {
	uint32_t magic;
	uint16_t version;
	uint16_t header_bytes;
	uint32_t sequence;
	uint32_t sample_rate;
	uint16_t channels;
	uint16_t frames;
	uint32_t activity_mask;
	uint64_t render_sample;
	uint64_t monotonic_ns;
};

struct le_aec_reference_packet {
	struct le_aec_reference_header header;
	int16_t samples[LE_AEC_REFERENCE_MAX_FRAMES];
};

struct le_aec_reference_sender {
	int fd;
	uint32_t sequence;
	uint64_t render_sample;
	struct sockaddr_un address;
	socklen_t address_size;
};

int le_aec_reference_init(struct le_aec_reference_sender *sender,
			  const char *root);

/*
 * Publish the exact mono programme sent to the PCM device.
 *
 * Returns 0 when delivered, 1 when deliberately dropped because no receiver
 * is present or its socket buffer is full, and -1 for a permanent error.
 * The function never blocks the playback thread.
 */
int le_aec_reference_publish(struct le_aec_reference_sender *sender,
			     const int16_t *interleaved,
			     size_t frames,
			     unsigned int channels,
			     unsigned int activity_mask);

void le_aec_reference_close(struct le_aec_reference_sender *sender);

#endif
