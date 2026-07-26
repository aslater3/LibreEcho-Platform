#define _POSIX_C_SOURCE 200809L

#include "aec_reference.h"

#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define CHECK(x) do { \
	if (!(x)) { \
		fprintf(stderr, "FAIL %s:%d: %s (errno=%d)\n", \
			__FILE__, __LINE__, #x, errno); \
		result = 1; \
		goto out; \
	} \
} while (0)

int main(void)
{
	char root[] = "/tmp/libreecho-aec-reference.XXXXXX";
	char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
	struct le_aec_reference_sender sender;
	struct le_aec_reference_packet packet;
	struct sockaddr_un address;
	int16_t stereo[16];
	ssize_t received;
	int receiver = -1;
	int result = 0;
	int i;

	memset(&sender, 0, sizeof(sender));
	sender.fd = -1;
	CHECK(mkdtemp(root) != NULL);
	CHECK(snprintf(path, sizeof(path), "%s/%s", root,
		       LE_AEC_REFERENCE_SOCKET) > 0);
	receiver = socket(AF_UNIX, SOCK_DGRAM, 0);
	CHECK(receiver >= 0);
	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	strcpy(address.sun_path, path);
	CHECK(bind(receiver, (struct sockaddr *)&address,
		   (socklen_t)(offsetof(struct sockaddr_un, sun_path) +
			       strlen(address.sun_path) + 1)) == 0);
	CHECK(le_aec_reference_init(&sender, root) == 0);
	for (i = 0; i < 8; ++i) {
		stereo[i * 2] = (int16_t)(100 + i);
		stereo[i * 2 + 1] = (int16_t)(-100 - i);
	}
	CHECK(le_aec_reference_publish(&sender, stereo, 8, 2, 5) == 0);
	received = recv(receiver, &packet, sizeof(packet), 0);
	CHECK(received ==
	      (ssize_t)(sizeof(packet.header) + 8 * sizeof(int16_t)));
	CHECK(packet.header.magic == LE_AEC_REFERENCE_MAGIC);
	CHECK(packet.header.version == LE_AEC_REFERENCE_VERSION);
	CHECK(packet.header.header_bytes == sizeof(packet.header));
	CHECK(packet.header.sequence == 0);
	CHECK(packet.header.sample_rate == 48000);
	CHECK(packet.header.channels == 1);
	CHECK(packet.header.frames == 8);
	CHECK(packet.header.activity_mask == 5);
	CHECK(packet.header.render_sample == 0);
	for (i = 0; i < 8; ++i)
		CHECK(packet.samples[i] == 100 + i);

	close(receiver);
	receiver = -1;
	unlink(path);
	CHECK(le_aec_reference_publish(&sender, stereo, 8, 2, 0) == 1);
	CHECK(sender.sequence == 2);
	CHECK(sender.render_sample == 16);
	puts("aec_reference: exact mono packets and nonblocking drops ok");

out:
	le_aec_reference_close(&sender);
	if (receiver >= 0)
		close(receiver);
	unlink(path);
	rmdir(root);
	return result;
}
