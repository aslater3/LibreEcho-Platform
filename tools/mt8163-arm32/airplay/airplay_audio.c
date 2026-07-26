/* LibreEcho AirPlay PCM producer.
 *
 * Shairport Sync writes decoded S16_LE/48 kHz/stereo PCM to its private FIFO.
 * This process forwards it to the shared LibreEcho media bus.  It never opens
 * ALSA or touches the codec/amplifier; libreecho-audio-engine is the sole
 * hardware owner for every playback source.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define DEFAULT_INPUT_FIFO "/run/libreecho/airplay.pcm"
#define DEFAULT_MEDIA_FIFO "/run/libreecho-audio/media.pcm"
#define DEFAULT_VOLUME_FILE "/run/libreecho-audio/media.volume"
#define DEFAULT_AIRPLAY_VOLUME_FILE "/run/libreecho-audio/airplay.volume"
#define DEFAULT_AIRPLAY_ACTIVE_FILE "/run/libreecho-audio/airplay.active"
#define BUFFER_SIZE 8192

static volatile sig_atomic_t stopping;

static void on_signal(int signo)
{
	if (signo == SIGTERM || signo == SIGINT)
		stopping = 1;
}

static int ensure_fifo(const char *path)
{
	struct stat st;

	if (mkfifo(path, 0660) == 0)
		return 0;
	if (errno != EEXIST)
		return -1;
	if (stat(path, &st) < 0 || !S_ISFIFO(st.st_mode)) {
		errno = EEXIST;
		return -1;
	}
	return 0;
}

static int write_all(int fd, const unsigned char *buffer, size_t length)
{
	size_t sent = 0;

	while (sent < length && !stopping) {
		ssize_t n = write(fd, buffer + sent, length - sent);

		if (n > 0) {
			sent += (size_t)n;
			continue;
		}
		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
			struct pollfd pfd = { fd, POLLOUT, 0 };
			int rc = poll(&pfd, 1, 250);

			if (rc >= 0)
				continue;
		}
		return -1;
	}
	return sent == length ? 0 : -1;
}

static int set_volume(const char *path, const char *text)
{
	char *end;
	double db = strtod(text, &end);
	char temporary[256];
	char value[64];
	int fd;
	int length;

	if (end == text || *end != '\0' || !isfinite(db))
		return 2;
	if (db < -144.0)
		db = -144.0;
	if (db > 0.0)
		db = 0.0;
	length = snprintf(value, sizeof(value), "%.6f\n", db);
	if (length < 0 || (size_t)length >= sizeof(value) ||
	    snprintf(temporary, sizeof(temporary), "%s.tmp", path) < 0)
		return 1;
	fd = open(temporary, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0640);
	if (fd < 0)
		return 1;
	if (write_all(fd, (const unsigned char *)value, (size_t)length) < 0 ||
	    fsync(fd) < 0 || close(fd) < 0) {
		(void)close(fd);
		(void)unlink(temporary);
		return 1;
	}
	if (rename(temporary, path) < 0) {
		(void)unlink(temporary);
		return 1;
	}
	return 0;
}

static int set_active(const char *path, int active)
{
	int fd;

	if (!active)
		return unlink(path) < 0 && errno != ENOENT ? 1 : 0;
	fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0640);
	if (fd < 0)
		return 1;
	return close(fd) < 0 ? 1 : 0;
}

static int forward_stream(const char *input_path, const char *output_path)
{
	unsigned char buffer[BUFFER_SIZE];
	int input = -1;
	int output = -1;
	int result = -1;

	if (ensure_fifo(input_path) < 0)
		return -1;
	input = open(input_path, O_RDONLY | O_CLOEXEC);
	if (input < 0)
		goto out;
	output = open(output_path, O_WRONLY | O_CLOEXEC);
	if (output < 0)
		goto out;
	while (!stopping) {
		ssize_t n = read(input, buffer, sizeof(buffer));

		if (n > 0) {
			if (write_all(output, buffer, (size_t)n) < 0)
				goto out;
			continue;
		}
		if (n == 0) {
			result = 0;
			break;
		}
		if (errno != EINTR)
			goto out;
	}
	if (stopping)
		result = 0;
out:
	if (output >= 0)
		close(output);
	if (input >= 0)
		close(input);
	return result;
}

int main(int argc, char **argv)
{
	const char *input_path = DEFAULT_INPUT_FIFO;
	const char *output_path = DEFAULT_MEDIA_FIFO;
	struct sigaction action;

	if (argc == 2 && (!strcmp(argv[1], "--start") ||
			 !strcmp(argv[1], "--stop"))) {
		int active = !strcmp(argv[1], "--start");

		if (active && access(DEFAULT_MEDIA_FIFO, F_OK) != 0)
			return 1;
		return set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, active);
	}
	if (argc == 3 && !strcmp(argv[1], "--set-volume")) {
		if (set_volume(DEFAULT_VOLUME_FILE, argv[2]) != 0)
			return 1;
		return set_volume(DEFAULT_AIRPLAY_VOLUME_FILE, argv[2]);
	}
	if (argc > 1)
		input_path = argv[1];
	if (argc > 2)
		output_path = argv[2];
	if (argc > 3) {
		fprintf(stderr, "Usage: %s [input-fifo] [media-fifo]\n", argv[0]);
		return 2;
	}

	memset(&action, 0, sizeof(action));
	action.sa_handler = on_signal;
	sigemptyset(&action.sa_mask);
	(void)sigaction(SIGTERM, &action, NULL);
	(void)sigaction(SIGINT, &action, NULL);
	signal(SIGPIPE, SIG_IGN);
	if (set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 1) != 0)
		return 1;

	while (!stopping) {
		if (forward_stream(input_path, output_path) < 0 && !stopping)
			usleep(250000);
	}
	(void)set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 0);
	return 0;
}
