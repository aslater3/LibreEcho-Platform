/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Minimal native WMT launcher prototype.
 *
 * This is intentionally separate from wifi_native/wmt_init.c.  It handles the
 * userspace command/response protocol exposed by the conn_soc /dev/wmt node:
 *
 *   poll() -> read() queued command -> print it -> write() response
 *
 * The kernel currently queues "srh_patch" and treats only the exact,
 * case-insensitive string "ok" as success.  The safe default here is "fail";
 * pass --ok only when success is explicitly intended.
 */

#define _POSIX_C_SOURCE 200809L

#include "wmt_ioctl.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

#define DEFAULT_WMT_DEVICE "/dev/wmt"
#define WMT_COMMAND_SIZE 256 /* NAME_MAX + terminating byte in the driver */

struct launcher_options {
	const char *device;
	const char *response;
	int response_explicit;
	int ok;
	int once;
};

static void usage(const char *program)
{
	printf("Usage: %s [options]\n", program);
	printf("\n");
	printf("Poll /dev/wmt for a kernel command, print it, and write a response.\n");
	printf("Options:\n");
	printf("  -d, --device PATH     WMT device (default: %s)\n", DEFAULT_WMT_DEVICE);
	printf("      --response TEXT   Failure response (default: fail)\n");
	printf("      --ok              Explicitly respond with ok\n");
	printf("      --once            Handle one command, then exit (host review aid)\n");
	printf("  -h, --help            Show this help\n");
}

static int parse_options(int argc, char **argv, struct launcher_options *options)
{
	int i;

	options->device = DEFAULT_WMT_DEVICE;
	options->response = "fail";
	options->response_explicit = 0;
	options->ok = 0;
	options->once = 0;

	for (i = 1; i < argc; ++i) {
		if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
			usage(argv[0]);
			return 1;
		}
		if ((!strcmp(argv[i], "-d") || !strcmp(argv[i], "--device")) && i + 1 < argc) {
			options->device = argv[++i];
			continue;
		}
		if (!strcmp(argv[i], "--response") && i + 1 < argc) {
			options->response = argv[++i];
			options->response_explicit = 1;
			continue;
		}
		if (!strcmp(argv[i], "--ok")) {
			options->ok = 1;
			continue;
		}
		if (!strcmp(argv[i], "--once")) {
			options->once = 1;
			continue;
		}
		fprintf(stderr, "unknown or incomplete option: %s\n", argv[i]);
		return -1;
	}

	if (options->ok && options->response_explicit) {
		fprintf(stderr, "--ok and --response are mutually exclusive\n");
		return -1;
	}
	if (strlen(options->response) == 0 || strlen(options->response) >= WMT_COMMAND_SIZE) {
		fprintf(stderr, "response must be 1..255 bytes\n");
		return -1;
	}
	/* Do not permit an implicit success response; --ok is the explicit gate. */
	if (!options->ok && !strcasecmp(options->response, "ok")) {
		fprintf(stderr, "response \"ok\" requires explicit --ok\n");
		return -1;
	}
	return 0;
}

static int write_response(int fd, const char *response)
{
	ssize_t response_len = (ssize_t)strlen(response);
	ssize_t written = write(fd, response, (size_t)response_len);

	if (written < 0) {
		fprintf(stderr, "write response: %s\n", strerror(errno));
		return -1;
	}
	if (written != response_len) {
		fprintf(stderr, "short response write: %zd of %zd bytes\n", written, response_len);
		return -1;
	}
	return 0;
}

static int handle_command(int fd, const struct launcher_options *options)
{
	char command[WMT_COMMAND_SIZE];
	const char *response = options->ok ? "ok" : options->response;
	ssize_t command_len;

	command_len = read(fd, command, sizeof(command) - 1);
	if (command_len < 0) {
		if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
			return 0;
		fprintf(stderr, "read command: %s\n", strerror(errno));
		return -1;
	}
	if (command_len == 0) {
		fprintf(stderr, "WMT device returned EOF while command was expected\n");
		return -1;
	}

	/* WMT_read() returns a byte count and does not append a NUL terminator. */
	command[command_len] = '\0';
	printf("command: %s\n", command);
	fflush(stdout);

	/* No newline: WMT_write() accepts exactly "ok" as the success token. */
	return write_response(fd, response);
}

int main(int argc, char **argv)
{
	struct launcher_options options;
	struct pollfd pollfd = { 0 };
	int parse_result;
	int fd;

	parse_result = parse_options(argc, argv, &options);
	if (parse_result != 0)
		return parse_result > 0 ? EXIT_SUCCESS : EXIT_FAILURE;

	fd = open(options.device, O_RDWR);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", options.device, strerror(errno));
		return EXIT_FAILURE;
	}

	/* Tell the kernel this launcher is alive; arg 0 means not killed. */
	if (ioctl(fd, WMT_IOCTL_SET_LAUNCHER_KILL, 0) < 0) {
		fprintf(stderr, "ioctl SET_LAUNCHER_KILL(0): %s\n", strerror(errno));
		close(fd);
		return EXIT_FAILURE;
	}

	pollfd.fd = fd;
	pollfd.events = POLLIN;
	for (;;) {
		int poll_result = poll(&pollfd, 1, -1);

		if (poll_result < 0) {
			if (errno == EINTR)
				continue;
			fprintf(stderr, "poll %s: %s\n", options.device, strerror(errno));
			close(fd);
			return EXIT_FAILURE;
		}
		if (poll_result == 0)
			continue;
		if (pollfd.revents & POLLNVAL) {
			fprintf(stderr, "poll %s: invalid file descriptor\n", options.device);
			close(fd);
			return EXIT_FAILURE;
		}
		if (pollfd.revents & POLLERR) {
			fprintf(stderr, "poll %s: device error\n", options.device);
			close(fd);
			return EXIT_FAILURE;
		}
		if (pollfd.revents & POLLHUP) {
			fprintf(stderr, "poll %s: device hangup\n", options.device);
			close(fd);
			return EXIT_FAILURE;
		}
		if (pollfd.revents & POLLIN) {
			if (handle_command(fd, &options) != 0) {
				close(fd);
				return EXIT_FAILURE;
			}
			if (options.once)
				break;
		}
	}

	close(fd);
	return EXIT_SUCCESS;
}
