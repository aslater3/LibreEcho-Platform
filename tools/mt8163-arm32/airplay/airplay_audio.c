/* LibreEcho AirPlay PCM producer.
 *
 * Shairport Sync writes decoded S16_LE/48 kHz/stereo PCM to its private FIFO.
 * This process forwards it to the dedicated LibreEcho AirPlay bus. It never opens
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
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#define DEFAULT_INPUT_FIFO "/run/libreecho/airplay.pcm"
#define DEFAULT_MEDIA_FIFO "/run/libreecho-audio/airplay-media.pcm"
#define DEFAULT_AIRPLAY_VOLUME_FILE "/run/libreecho-audio/airplay.volume"
#define DEFAULT_AIRPLAY_MASTER_FILE "/run/libreecho-audio/airplay.master"
#define DEFAULT_AIRPLAY_ACTIVE_FILE "/run/libreecho-audio/airplay.active"
#define DEFAULT_AIRPLAY_LAST_START_FILE "/run/libreecho-audio/airplay.last-start"
#define DEFAULT_AIRPLAY_LOCK_FILE "/run/libreecho-audio/airplay.lock"
#define DEFAULT_AIRPLAY_WAITERS_FILE "/run/libreecho-audio/airplay.waiters"
#define DEFAULT_AIRPLAY_RESET_FILE "/run/libreecho-audio/airplay.reset"
#define DEFAULT_AIRPLAY_RESET_ACK_FILE "/run/libreecho-audio/airplay.reset-ack"
#define BUFFER_SIZE 8192
#define SESSION_TOKEN_LENGTH 32

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

/* Hook tokens are exactly 128-bit lowercase hexadecimal, not paths or flags. */
static int valid_session_token(const char *token)
{
	size_t i;
	if (!token || strnlen(token, SESSION_TOKEN_LENGTH + 1) != SESSION_TOKEN_LENGTH)
		return 0;
	for (i = 0; i < SESSION_TOKEN_LENGTH; ++i)
		if (!((token[i] >= '0' && token[i] <= '9') ||
		      (token[i] >= 'a' && token[i] <= 'f')))
			return 0;
	return 1;
}

/* Called with the hook lock held. No symlink, truncated or extended marker
 * can authorize a command; the whole content must match the caller's token. */
static int matching_session(const char *token)
{
	char content[SESSION_TOKEN_LENGTH + 2];
	struct stat st;
	int fd, n;
	if (!valid_session_token(token)) return 0;
	fd = open(DEFAULT_AIRPLAY_ACTIVE_FILE, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) return 0;
	n = fstat(fd, &st);
	if (n == 0 && S_ISREG(st.st_mode) && st.st_size == SESSION_TOKEN_LENGTH + 1)
		n = (int)read(fd, content, SESSION_TOKEN_LENGTH + 1);
	else
		n = -1;
	close(fd);
	return n == SESSION_TOKEN_LENGTH + 1 &&
	       !memcmp(content, token, SESSION_TOKEN_LENGTH) &&
	       content[SESSION_TOKEN_LENGTH] == '\n';
}

/* The first 16 hex digits encode the strictly increasing source start tick.
 * Keep its high-water mark through stops, so an old delayed start cannot
 * resurrect a session after the newer one has already ended. Called locked. */
static int newer_start(const char *token)
{
	char content[SESSION_TOKEN_LENGTH + 1];
	struct stat st;
	int fd, n;
	fd = open(DEFAULT_AIRPLAY_LAST_START_FILE, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) return errno == ENOENT ? 1 : -1;
	n = fstat(fd, &st);
	if (n == 0 && S_ISREG(st.st_mode) && st.st_size == SESSION_TOKEN_LENGTH + 1)
		n = (int)read(fd, content, sizeof(content));
	else
		n = -1;
	close(fd);
	if (n != (int)sizeof(content) || content[SESSION_TOKEN_LENGTH] != '\n') return -1;
	content[SESSION_TOKEN_LENGTH] = '\0';
	if (!valid_session_token(content)) return -1;
	return memcmp(token, content, 16) > 0 ? 1 : 0;
}

static int set_volume(const char *path, const char *token, const char *text)
{
	char *end;
	double db = strtod(text, &end);
	char temporary[256];
	char value[64];
	int fd;
	int length;

	if (end == text || *end != '\0' || !isfinite(db) ||
	    (db != -144.0 && (db < -30.0 || db > 0.0)))
		return 2;
	if (!matching_session(token)) return 2;
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

static int clear_session_state(void)
{
	int result = 0;

	if (unlink(DEFAULT_AIRPLAY_ACTIVE_FILE) < 0 && errno != ENOENT)
		result = 1;
	/* The volume file is session state.  Do not let a new connection
	 * inherit the previous phone's volume before its first callback. */
	if (unlink(DEFAULT_AIRPLAY_VOLUME_FILE) < 0 && errno != ENOENT)
		result = 1;
	if (unlink(DEFAULT_AIRPLAY_MASTER_FILE) < 0 && errno != ENOENT)
		result = 1;
	return result;
}

/* Hooks register shared waiter locks before contending for the session lock.
 * The bridge must pass an exclusive admission gate, held only for a single
 * nonblocking session-lock attempt, never across PCM I/O or a retry sleep.
 * Thus a registered hook prevents the next PCM chunk from barging ahead.
 * Process death releases registration automatically; no stale waiter counter.
 * The session lock still covers the complete read/write/reset/token operation.
 */
static int lock_session_mode(int forwarding)
{
	struct stat st;
	int fd = open(DEFAULT_AIRPLAY_LOCK_FILE,
		      O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0640);
	int waiters;
	unsigned int attempt;
	if (fd < 0)
		return -1;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode)) {
		close(fd);
		return -1;
	}
	waiters = open(DEFAULT_AIRPLAY_WAITERS_FILE,
		       O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0640);
	if (waiters < 0) { close(fd); return -1; }
	if (fstat(waiters, &st) < 0 || !S_ISREG(st.st_mode)) {
		close(waiters); close(fd); return -1;
	}
	for (attempt = 0; attempt < 100; ++attempt) {
		if (flock(waiters, (forwarding ? LOCK_EX : LOCK_SH) | LOCK_NB) == 0) {
			if (flock(fd, LOCK_EX | LOCK_NB) == 0) {
				close(waiters);
				return fd;
			}
			/* Hooks retain shared registration while waiting. The bridge
			 * must release admission before sleeping, even when a hook
			 * has already acquired the session lock and left the gate. */
			if (forwarding) {
				int saved = errno;
				(void)flock(waiters, LOCK_UN);
				errno = saved;
			}
		}
		if (errno != EWOULDBLOCK && errno != EINTR)
			break;
		usleep(10000);
	}
	close(waiters);
	close(fd);
	return -1;
}

static int lock_session(void)
{
	return lock_session_mode(0);
}

static int set_active(const char *path, const char *token)
{
	int fd;
	char temporary[256];

	if (snprintf(temporary, sizeof(temporary), "%s.tmp.XXXXXX", path) >=
	    (int)sizeof(temporary))
		return 1;
	fd = mkstemp(temporary);
	if (fd < 0)
		return 1;
	if (fchmod(fd, 0640) < 0 || write(fd, token, SESSION_TOKEN_LENGTH) != SESSION_TOKEN_LENGTH ||
	    write(fd, "\n", 1) != 1) {
		(void)close(fd);
		(void)unlink(temporary);
		return 1;
	}
	if (close(fd) < 0) { (void)unlink(temporary); return 1; }
	if (rename(temporary, path) < 0) {
		(void)unlink(temporary);
		return 1;
	}
	return 0;
}

/* The bridge may not yet have read the predecessor's input bytes. Its read
 * is serialized by the hook lock, so empty this FIFO before engine reset. */
static int drain_input(void)
{
	unsigned char buffer[4096];
	size_t total = 0;
	int fd = open(DEFAULT_INPUT_FIFO, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) return errno == ENOENT ? 0 : 1;
	for (;;) {
		ssize_t n = read(fd, buffer, sizeof(buffer));
		if (n > 0) {
			total += (size_t)n;
			if (total > 262144) { close(fd); return 1; }
			continue;
		}
		if (n < 0 && errno == EINTR) continue;
		if (n == 0 || (n < 0 && errno == EAGAIN)) { close(fd); return 0; }
		close(fd); return 1;
	}
}

/* Called under the shared forwarding/hook lock. A unique request is published
 * only after the marker is removed. The engine acknowledges after draining. */
static int reset_engine(void)
{
	unsigned char nonce[16];
	char token[34], temporary[256], ack[34];
	static const char hex[] = "0123456789abcdef";
	int fd, i, n, result = 1;
	struct stat st;

	fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
	if (fd < 0) return 1;
	n = (int)read(fd, nonce, sizeof(nonce));
	close(fd);
	if (n != (int)sizeof(nonce)) return 1;
	for (i = 0; i < 16; ++i) {
		token[i * 2] = hex[nonce[i] >> 4];
		token[i * 2 + 1] = hex[nonce[i] & 15];
	}
	token[32] = '\n'; token[33] = '\0';
	/* A prior engine instance's on-disk reply is not evidence that the
	 * currently running instance drained this generation. */
	if (unlink(DEFAULT_AIRPLAY_RESET_ACK_FILE) < 0 && errno != ENOENT)
		return 1;
	if (snprintf(temporary, sizeof(temporary), "%s.tmp.XXXXXX", DEFAULT_AIRPLAY_RESET_FILE) >= (int)sizeof(temporary)) return 1;
	fd = mkstemp(temporary);
	if (fd < 0) return 1;
	if (fchmod(fd, 0640) < 0 || write(fd, token, 33) != 33) {
		close(fd); unlink(temporary); return 1;
	}
	if (close(fd) < 0 || rename(temporary, DEFAULT_AIRPLAY_RESET_FILE) < 0) {
		unlink(temporary); return 1;
	}
	for (i = 0; i < 100; ++i) {
		fd = open(DEFAULT_AIRPLAY_RESET_ACK_FILE,
			  O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
		if (fd >= 0) {
			n = fstat(fd, &st);
			if (n == 0 && S_ISREG(st.st_mode) && st.st_size == 33) {
				n = (int)read(fd, ack, 33);
				if (n == 33 && !memcmp(ack, token, 33)) result = 0;
			}
			close(fd);
			if (!result) break;
		}
		usleep(10000);
	}
	/* Retain a successful request for a replacement engine. If it restarts
	 * between our acknowledgement and marker publication, it must re-drain
	 * and arm this same session rather than treating the marker as foreign. */
	if (result)
		(void)unlink(DEFAULT_AIRPLAY_RESET_FILE);
	return result;
}

static int forward_stream(const char *input_path, const char *output_path)
{
	unsigned char buffer[BUFFER_SIZE];
	int input = -1;
	int output = -1;
	int result = -1;

	if (ensure_fifo(input_path) < 0)
		return -1;
	/* Hold a local write end so poll does not spin on POLLHUP while
	 * Shairport has no writer. Never write to this descriptor. */
	input = open(input_path, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	if (input < 0)
		goto out;
	output = open(output_path, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	if (output < 0)
		goto out;
	/* Poll outside the session lock. Reading and forwarding a chunk share
	 * the hook lock, so stop/start can drain without an in-flight writer. */
	while (!stopping) {
		struct pollfd pfd = { input, POLLIN, 0 };
		int lock_fd, attempts;
		ssize_t n;
		if (poll(&pfd, 1, 20) < 0) {
			if (errno == EINTR) continue;
			goto out;
		}
		if (!(pfd.revents & POLLIN)) continue;
		lock_fd = lock_session_mode(1);
		if (lock_fd < 0) continue;
		n = read(input, buffer, sizeof(buffer));
		if (n > 0) {
			struct stat marker;
			if (lstat(DEFAULT_AIRPLAY_ACTIVE_FILE, &marker) == 0 &&
			    S_ISREG(marker.st_mode)) {
				size_t sent = 0;
				/* A blocked engine cannot indefinitely own the hook lock. */
				for (attempts = 0; sent < (size_t)n && attempts < 5; ++attempts) {
					ssize_t wrote = write(output, buffer + sent, (size_t)n - sent);
					if (wrote > 0) { sent += (size_t)wrote; continue; }
					if (wrote < 0 && errno == EINTR) continue;
					if (wrote < 0 && errno == EAGAIN) {
						struct pollfd writable = { output, POLLOUT, 0 };
						(void)poll(&writable, 1, 20);
						continue;
					}
					break;
				}
				if (sent != (size_t)n) {
					close(lock_fd);
					goto out;
				}
			}
		}
		close(lock_fd);
		if (n < 0 && errno != EAGAIN && errno != EINTR) goto out;
	}
	result = 0;
out:
	/* Close the writer first: when the marker falls, the shared engine
	 * can discard all remaining queued bytes from this session safely. */
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
	int lock_fd, result;

	if (argc >= 2 && (!strcmp(argv[1], "--start") ||
			 !strcmp(argv[1], "--stop"))) {
		if (argc != 3 || !valid_session_token(argv[2])) return 2;
		lock_fd = lock_session();
		if (lock_fd < 0) return 1; /* Never revoke another session without a match. */
		if (!strcmp(argv[1], "--start")) {
			result = newer_start(argv[2]);
			if (result <= 0) { close(lock_fd); return result == 0 ? 2 : 1; }
			/* Reserve this source order before any reset can block or fail. */
			result = set_active(DEFAULT_AIRPLAY_LAST_START_FILE, argv[2]);
			if (result) { close(lock_fd); return result; }
		}
		if (!strcmp(argv[1], "--stop") && !matching_session(argv[2])) {
			close(lock_fd);
			return 2;
		}
		result = clear_session_state();
		if (!result)
			result = drain_input();
		if (!result)
			result = reset_engine();
		if (!result && !strcmp(argv[1], "--start"))
			result = set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, argv[2]);
		close(lock_fd);
		return result;
	}
	if (argc >= 2 && !strcmp(argv[1], "--set-volume")) {
		if (argc != 4 || !valid_session_token(argv[2])) return 2;
		lock_fd = lock_session();
		if (lock_fd < 0)
			return 1;
		result = set_volume(DEFAULT_AIRPLAY_VOLUME_FILE, argv[2], argv[3]);
		close(lock_fd);
		return result;
	}
	if (argc > 1)
		input_path = argv[1];
	if (argc > 2)
		output_path = argv[2];
	if (argc > 3) {
		fprintf(stderr, "Usage: %s [input-fifo] [airplay-media-fifo]\n", argv[0]);
		return 2;
	}

	memset(&action, 0, sizeof(action));
	action.sa_handler = on_signal;
	sigemptyset(&action.sa_mask);
	(void)sigaction(SIGTERM, &action, NULL);
	(void)sigaction(SIGINT, &action, NULL);
	signal(SIGPIPE, SIG_IGN);

	while (!stopping) {
		if (forward_stream(input_path, output_path) < 0 && !stopping)
			usleep(250000);
	}
	return 0;
}
