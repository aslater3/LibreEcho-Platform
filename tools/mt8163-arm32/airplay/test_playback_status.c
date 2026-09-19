#define _GNU_SOURCE

#include "playback_status.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int read_status(const char *path, char *buffer, size_t size,
		       struct stat *metadata)
{
	ssize_t length;
	int fd = open(path, O_RDONLY | O_CLOEXEC);

	if (fd < 0)
		return -1;
	length = read(fd, buffer, size - 1);
	if (length < 0 || fstat(fd, metadata) < 0) {
		(void)close(fd);
		return -1;
	}
	buffer[length] = '\0';
	return close(fd);
}

int main(void)
{
	char root[] = "/tmp/libreecho-playback-status.XXXXXX";
	char document[256];
	struct playback_status status;
	struct stat first;
	struct stat unchanged;
	struct stat announcing;
	uint64_t pending[PLAYBACK_STATUS_BUS_COUNT] = {0, 85, 0, 0};
	uint64_t last_frame[PLAYBACK_STATUS_BUS_COUNT] = {4096, 2048, 0, 0};
	int result = 1;

	if (!mkdtemp(root)) {
		perror("mkdtemp");
		return 1;
	}
	if (playback_status_init(&status, root) < 0 ||
	    playback_status_publish(&status, 0) < 0 ||
	    read_status(status.path, document, sizeof(document), &first) < 0) {
		perror("publish idle");
		goto out;
	}
	if ((first.st_mode & 0777) != 0644 ||
	    strcmp(document,
		"{\"state\":\"idle\",\"active\":null,\"buses\":{"
		"\"media\":false,\"system\":false,\"announcement\":false,"
		"\"alarm\":false}}\n") != 0) {
		fprintf(stderr, "invalid idle status: %s", document);
		goto out;
	}
	if (playback_status_publish(&status, 0) < 0 ||
	    read_status(status.path, document, sizeof(document), &unchanged) < 0 ||
	    first.st_ino != unchanged.st_ino) {
		fprintf(stderr, "unchanged state replaced the status file\n");
		goto out;
	}
	if (playback_status_publish(
		    &status, PLAYBACK_BUS_MEDIA | PLAYBACK_BUS_ANNOUNCEMENT) < 0 ||
	    read_status(status.path, document, sizeof(document), &announcing) < 0) {
		perror("publish announcing");
		goto out;
	}
	if (first.st_ino == announcing.st_ino ||
	    !strstr(document, "\"state\":\"announcing\"") ||
	    !strstr(document, "\"active\":\"announcement\"") ||
	    !strstr(document, "\"media\":true") ||
	    !strstr(document, "\"announcement\":true") ||
	    !strstr(document, "\"system\":false") ||
	    !strstr(document, "\"alarm\":false")) {
		fprintf(stderr, "invalid announcing status: %s", document);
		goto out;
	}
	if (playback_status_publish_drain(
		    &status, PLAYBACK_BUS_MEDIA | PLAYBACK_BUS_SYSTEM,
		    PLAYBACK_BUS_MEDIA | PLAYBACK_BUS_ANNOUNCEMENT |
		        PLAYBACK_BUS_ALARM,
		    pending, last_frame) < 0 ||
	    read_status(status.path, document, sizeof(document), &announcing) < 0 ||
	    !strstr(document, "\"system\":{\"drained\":false") ||
	    !strstr(document, "\"pending_frames\":85") ||
	    !strstr(document, "\"last_frame\":2048")) {
		fprintf(stderr, "invalid per-bus drain status: %s", document);
		goto out;
	}
	puts("atomic playback status: ok");
	result = 0;
out:
	(void)unlink(status.path);
	(void)unlink(status.temporary_path);
	if (rmdir(root) < 0 && errno != ENOENT)
		perror("rmdir");
	return result;
}
