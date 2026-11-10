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
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "airplay_delay.h"

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
#define DEFAULT_AIRPLAY_STATUS_FILE "/run/libreecho-audio/airplay.status"
#define DEFAULT_AIRPLAY_ENGINE_DELAY_FILE "/run/libreecho-audio/airplay.engine-delay"
#define BUFFER_SIZE 8192
/* S16_LE stereo. The unsent remainder and every FIFO write stay a whole
 * number of frames, so a stall can never split a frame. */
#define FRAME_BYTES 4U
/* PIPE_BUF: a non-blocking FIFO write of this size is all-or-nothing. */
#define WRITE_CHUNK 4096U
#define STATUS_SIZE 768U
/* Diagnostics are RAM only: events go to stderr (the RAM-resident
 * libreecho-airplayd log) at most once per kind per interval, and the status
 * file is overwritten in place at the same cadence while a session is live. */
#ifndef EVENT_INTERVAL_MS
#define EVENT_INTERVAL_MS 10000ULL
#endif
#ifndef STATUS_INTERVAL_MS
#define STATUS_INTERVAL_MS 10000ULL
#endif
#define OUTPUT_REOPEN_MS 100ULL
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

enum event_kind {
	EV_DISCARD,
	EV_REOPEN,
	EV_SESSION_START,
	EV_SESSION_END,
	EV_DELAY_FALLBACK,
	EV_COUNT
};

static const char *const event_names[EV_COUNT] = {
	"discard", "reopen", "session-start", "session-end", "delay-fallback"
};

struct forwarder {
	int input;
	int output;
	const char *input_path;
	const char *output_path;
	/* The unsent remainder: always starts on a frame boundary and is a whole
	 * number of frames.  carry holds a trailing partial frame read from the
	 * pipe; it is joined to the next read, never forwarded alone. */
	unsigned char pending[BUFFER_SIZE];
	size_t pending_off, pending_len;
	unsigned char carry[FRAME_BYTES];
	size_t carry_len;
	/* The session the held bytes belong to. */
	struct stat data_marker;
	int have_marker;
	/* Session tracking for events and the status file. */
	int session_active;
	uint64_t session_frames;
	uint64_t last_status_ms;
	int status_fd;
	uint64_t next_output_try_ms;
	int output_opened_once;
	/* Cumulative bytes written into airplay-media.pcm (delay channel). */
	uint64_t written_bytes;
	/* Diagnostics. */
	uint64_t discards, discarded_bytes, reopens;
	uint32_t seen_fallbacks;
	struct le_ad_bridge_ctx delay;
	struct le_ad_parts parts;
	unsigned int delay_reason;
	struct le_ad_reader shairport;
	uint64_t ev_pending[EV_COUNT];
	uint64_t ev_last_ms[EV_COUNT];
	char ev_detail[EV_COUNT][96];
};

static uint64_t now_ms(void)
{
	return le_ad_now_ns() / 1000000ULL;
}

static void event_emit(struct forwarder *f, enum event_kind kind, uint64_t now)
{
	fprintf(stderr, "airplay-audio: %s x%llu (%s)\n", event_names[kind],
		(unsigned long long)f->ev_pending[kind], f->ev_detail[kind]);
	f->ev_pending[kind] = 0;
	f->ev_last_ms[kind] = now;
}

/* At most one line per kind per EVENT_INTERVAL_MS; events inside the window
 * are counted and reported with the next line (or by event_tick). */
static void event_note(struct forwarder *f, enum event_kind kind, uint64_t count,
		       const char *format, ...)
{
	uint64_t now = now_ms();
	va_list ap;

	if (count == 0)
		return;
	f->ev_pending[kind] += count;
	va_start(ap, format);
	(void)vsnprintf(f->ev_detail[kind], sizeof(f->ev_detail[kind]), format, ap);
	va_end(ap);
	if (f->ev_last_ms[kind] == 0 || now - f->ev_last_ms[kind] >= EVENT_INTERVAL_MS)
		event_emit(f, kind, now);
}

static void event_tick(struct forwarder *f, uint64_t now)
{
	int kind;

	for (kind = 0; kind < EV_COUNT; ++kind)
		if (f->ev_pending[kind] &&
		    now - f->ev_last_ms[kind] >= EVENT_INTERVAL_MS)
			event_emit(f, (enum event_kind)kind, now);
}

static int marker_present(struct stat *marker)
{
	return lstat(DEFAULT_AIRPLAY_ACTIVE_FILE, marker) == 0 && S_ISREG(marker->st_mode);
}

static int same_marker(const struct stat *a, const struct stat *b)
{
	return a->st_dev == b->st_dev && a->st_ino == b->st_ino &&
	       a->st_ctim.tv_sec == b->st_ctim.tv_sec &&
	       a->st_ctim.tv_nsec == b->st_ctim.tv_nsec;
}

static int fifo_bytes(int fd)
{
	int bytes = 0;

	if (fd < 0 || ioctl(fd, FIONREAD, &bytes) < 0 || bytes < 0)
		return 0;
	return bytes;
}

/* Drop everything this process holds for a session that is over. Only called
 * when the session marker is gone or replaced. */
static void discard_held(struct forwarder *f, const char *why)
{
	size_t bytes = f->pending_len + f->carry_len;

	f->pending_off = f->pending_len = f->carry_len = 0;
	if (bytes) {
		f->discards++;
		f->discarded_bytes += bytes;
		event_note(f, EV_DISCARD, 1, "%s; %zu bytes (%zu frames), %llu discards total",
			   why, bytes, bytes / FRAME_BYTES,
			   (unsigned long long)f->discards);
	}
}

static void write_status(struct forwarder *f, int active)
{
	char text[STATUS_SIZE];
	struct le_ad_read shairport;
	uint64_t now = now_ms();
	long long age_ms = -1, shairport_delay = -1;
	unsigned int fallbacks = 0, last_reason = 0;
	int n;

	if (f->shairport.map &&
	    le_ad_read_page(f->shairport.map, &shairport, sizeof(shairport),
			    LE_AD_MAGIC_READ) == 0) {
		uint64_t t = shairport.mono_ns / 1000000ULL;

		age_ms = now >= t ? (long long)(now - t) : 0;
		shairport_delay = (long long)shairport.delay_frames;
		fallbacks = shairport.fallbacks;
		last_reason = shairport.last_reason;
	}
	n = snprintf(text, sizeof(text),
		     "airplay-status v1\n"
		     "mono_ms=%llu\n"
		     "active=%d\n"
		     "session_frames=%llu\n"
		     "pipe_fill_frames=%d\n"
		     "bridge_remainder_frames=%zu\n"
		     "media_fifo_frames=%d\n"
		     "engine_queue_frames=%u\n"
		     "alsa_delay_frames=%u\n"
		     "downstream_frames=%u\n"
		     "delay_state=%s\n"
		     "shairport_delay_frames=%lld\n"
		     "shairport_delay_age_ms=%lld\n"
		     "delay_fallbacks=%u\n"
		     "delay_last_fallback=%s\n"
		     "discards=%llu\n"
		     "discarded_bytes=%llu\n"
		     "reopens=%llu\n",
		     (unsigned long long)now, active, (unsigned long long)f->session_frames,
		     (int)(fifo_bytes(f->input) / (int)FRAME_BYTES),
		     f->pending_len / FRAME_BYTES,
		     (int)(fifo_bytes(f->output) / (int)FRAME_BYTES),
		     f->parts.queue_frames, f->parts.alsa_frames,
		     f->parts.downstream_frames, le_ad_reason_name(f->delay_reason),
		     shairport_delay, age_ms, fallbacks, le_ad_reason_name(last_reason),
		     (unsigned long long)f->discards, (unsigned long long)f->discarded_bytes,
		     (unsigned long long)f->reopens);
	if (n < 0)
		return;
	/* Fixed size, rewritten in place: pad with spaces, end with a newline. */
	if ((size_t)n > STATUS_SIZE - 1)
		n = (int)(STATUS_SIZE - 1);
	memset(text + n, ' ', STATUS_SIZE - (size_t)n);
	text[STATUS_SIZE - 1] = '\n';
	if (f->status_fd < 0) {
		struct stat st;

		f->status_fd = open(DEFAULT_AIRPLAY_STATUS_FILE,
				    O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0644);
		if (f->status_fd >= 0 &&
		    (fstat(f->status_fd, &st) < 0 || !S_ISREG(st.st_mode) ||
		     (st.st_size != (off_t)STATUS_SIZE &&
		      ftruncate(f->status_fd, (off_t)STATUS_SIZE) < 0))) {
			close(f->status_fd);
			f->status_fd = -1;
		}
	}
	if (f->status_fd >= 0 &&
	    pwrite(f->status_fd, text, STATUS_SIZE, 0) != (ssize_t)STATUS_SIZE) {
		close(f->status_fd);
		f->status_fd = -1;
	}
	f->last_status_ms = now;
}

/* Reconcile the bridge's view of the session with the marker.  The one place
 * where held audio may be discarded: the session ended (marker gone) or was
 * replaced by a new one.  Safe with or without the session lock. */
static void sync_session(struct forwarder *f, const struct stat *marker, int present)
{
	if (!present || (f->have_marker && !same_marker(marker, &f->data_marker))) {
		if (f->session_active)
			event_note(f, EV_SESSION_END, 1,
				   "%llu frames forwarded; %llu discards, %llu reopens",
				   (unsigned long long)f->session_frames,
				   (unsigned long long)f->discards,
				   (unsigned long long)f->reopens);
		discard_held(f, present ? "session replaced" : "session ended");
		if (f->session_active)
			write_status(f, 0);
		f->session_active = 0;
		f->have_marker = 0;
	}
	if (present && !f->have_marker) {
		f->data_marker = *marker;
		f->have_marker = 1;
		f->session_active = 1;
		f->session_frames = 0;
		event_note(f, EV_SESSION_START, 1, "media fifo %d frames, pipe %d frames",
			   fifo_bytes(f->output) / (int)FRAME_BYTES,
			   fifo_bytes(f->input) / (int)FRAME_BYTES);
		write_status(f, 1);
	}
}

/* Session events and the status cadence. No lock: this only observes. */
static void track_session(struct forwarder *f)
{
	struct stat marker;
	uint64_t now = now_ms();
	int present = marker_present(&marker);

	sync_session(f, &marker, present);
	if (f->session_active && now - f->last_status_ms >= STATUS_INTERVAL_MS)
		write_status(f, 1);
	event_tick(f, now);
}

/* Recompute and publish the delay Shairport reads. Called every tick; the
 * page was already marked "updating" by delay_begin when bytes moved. */
static void publish_delay(struct forwarder *f)
{
	uint64_t now = le_ad_now_ns();
	unsigned int before = f->delay_reason;
	struct le_ad_read shairport;
	char path[320];

	f->delay_reason = le_ad_bridge_update(&f->delay, now, f->written_bytes,
		(int64_t)fifo_bytes(f->output), (uint32_t)(f->pending_len / FRAME_BYTES),
		&f->parts);
	/* Shairport counts its own failed delay() calls; report them as events. */
	if (snprintf(path, sizeof(path), "%s.delay-read", f->input_path) < (int)sizeof(path)) {
		const void *map = le_ad_reader_get(&f->shairport, path, 0);

		if (map && le_ad_read_page(map, &shairport, sizeof(shairport),
					   LE_AD_MAGIC_READ) == 0) {
			if (shairport.fallbacks > f->seen_fallbacks)
				event_note(f, EV_DELAY_FALLBACK,
					   shairport.fallbacks - f->seen_fallbacks,
					   "Shairport delay() unavailable (%s); bridge: %s",
					   le_ad_reason_name(shairport.last_reason),
					   le_ad_reason_name(f->delay_reason));
			f->seen_fallbacks = shairport.fallbacks;
		}
	}
	if (f->delay_reason != LE_AD_OK && before == LE_AD_OK && f->session_active)
		event_note(f, EV_DELAY_FALLBACK, 1, "bridge cannot publish delay: %s",
			   le_ad_reason_name(f->delay_reason));
}

/* Open the engine FIFO without blocking.  Never closes the input pipe. */
static int open_output(struct forwarder *f, int reopen)
{
	uint64_t now = now_ms();

	if (f->output >= 0)
		return 0;
	if (now < f->next_output_try_ms)
		return -1;
	f->next_output_try_ms = now + OUTPUT_REOPEN_MS;
	f->output = open(f->output_path, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	if (f->output < 0)
		return -1;
	reopen = reopen && f->output_opened_once;
	f->output_opened_once = 1;
	if (reopen) {
		f->reopens++;
		event_note(f, EV_REOPEN, 1, "media fifo reopened; %zu bytes retained, %llu reopens total",
			   f->pending_len, (unsigned long long)f->reopens);
	}
	return 0;
}

/* Called with the session lock held and the marker verified.  Writes as much
 * of the remainder as the engine FIFO accepts right now, never waiting.
 * Returns 0 (progress or full FIFO) or -1 when the reader end is gone. */
static int flush_pending(struct forwarder *f)
{
	while (f->pending_len > 0) {
		size_t chunk = f->pending_len < WRITE_CHUNK ? f->pending_len : WRITE_CHUNK;
		ssize_t n = write(f->output, f->pending + f->pending_off, chunk);

		if (n > 0) {
			f->pending_off += (size_t)n;
			f->pending_len -= (size_t)n;
			f->written_bytes += (uint64_t)n;
			f->session_frames += (uint64_t)n / FRAME_BYTES;
			continue;
		}
		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
			return 0;
		if (n < 0 && errno == EPIPE) {
			close(f->output);
			f->output = -1;
			f->next_output_try_ms = 0;
			return -1;
		}
		return -1;
	}
	f->pending_off = 0;
	return 0;
}

/* Verify, under the lock, that the held bytes still belong to the live
 * session. Returns 1 if they do. */
static int session_still_ours(struct forwarder *f)
{
	struct stat marker;
	int present = marker_present(&marker);

	if (present && f->have_marker && same_marker(&marker, &f->data_marker))
		return 1;
	sync_session(f, &marker, present);
	return 0;
}

static int forward_stream(struct forwarder *f)
{
	unsigned char *stage = NULL;
	int result = -1;

	if (ensure_fifo(f->input_path) < 0)
		return -1;
	/* Hold a local write end so poll does not spin on POLLHUP while
	 * Shairport has no writer. Never write to this descriptor. The input is
	 * not closed again until the process stops: a full engine FIFO must never
	 * take the only reader away from Shairport. */
	f->input = open(f->input_path, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	if (f->input < 0)
		goto out;
	stage = malloc(BUFFER_SIZE);
	if (!stage)
		goto out;
	(void)open_output(f, 0);
	while (!stopping) {
		struct pollfd pfd;
		int lock_fd;
		ssize_t n;

		track_session(f);
		if (f->output < 0 && open_output(f, 1) < 0) {
			/* No reader (engine restarting): wait, keep the remainder and
			 * keep Shairport's pipe intact. */
			publish_delay(f);
			(void)poll(NULL, 0, 20);
			continue;
		}
		if (f->pending_len > 0) {
			/* Wait for FIFO space without holding the hook lock. */
			pfd.fd = f->output;
			pfd.events = POLLOUT;
		} else {
			pfd.fd = f->input;
			pfd.events = POLLIN;
		}
		pfd.revents = 0;
		if (poll(&pfd, 1, 20) < 0) {
			if (errno == EINTR)
				continue;
			goto out;
		}
		if (f->pending_len > 0) {
			if (!(pfd.revents & (POLLOUT | POLLERR | POLLHUP))) {
				publish_delay(f);
				continue;
			}
			lock_fd = lock_session_mode(1);
			if (lock_fd < 0)
				continue;
			if (session_still_ours(f))
				(void)flush_pending(f);
			publish_delay(f);
			close(lock_fd);
			continue;
		}
		if (!(pfd.revents & POLLIN)) {
			publish_delay(f);
			continue;
		}
		lock_fd = lock_session_mode(1);
		if (lock_fd < 0)
			continue;
		/* Mark the delay page "updating" before bytes leave airplay.pcm. */
		le_ad_bridge_begin(&f->delay);
		n = read(f->input, stage, BUFFER_SIZE - f->carry_len);
		if (n > 0) {
			struct stat marker;
			size_t total, whole;
			int present = marker_present(&marker);

			/* A new session may have started since the last tick (the
			 * hook completed before we took the lock): adopt it so its
			 * first PCM is forwarded, not mistaken for a predecessor. */
			sync_session(f, &marker, present);
			if (present) {
				memcpy(f->pending, f->carry, f->carry_len);
				memcpy(f->pending + f->carry_len, stage, (size_t)n);
				total = f->carry_len + (size_t)n;
				whole = total - total % FRAME_BYTES;
				f->carry_len = total - whole;
				memcpy(f->carry, f->pending + whole, f->carry_len);
				f->pending_off = 0;
				f->pending_len = whole;
				(void)flush_pending(f);
			} else {
				/* No live session: this is a predecessor's tail or a
				 * stray write; the engine would discard it anyway. */
				f->carry_len = 0;
				f->discards++;
				f->discarded_bytes += (uint64_t)n;
				event_note(f, EV_DISCARD, 1,
					   "no active session; %zd bytes, %llu discards total", n,
					   (unsigned long long)f->discards);
			}
		}
		publish_delay(f);
		close(lock_fd);
		if (n < 0 && errno != EAGAIN && errno != EINTR)
			goto out;
	}
	result = 0;
out:
	free(stage);
	if (f->output >= 0)
		close(f->output);
	if (f->input >= 0)
		close(f->input);
	f->output = f->input = -1;
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

	{
		static struct forwarder f;
		char delay_path[320];

		f.input = f.output = f.status_fd = -1;
		f.input_path = input_path;
		f.output_path = output_path;
		if (snprintf(delay_path, sizeof(delay_path), "%s.delay", input_path) <
		    (int)sizeof(delay_path))
			le_ad_bridge_open(&f.delay, delay_path,
					  DEFAULT_AIRPLAY_ENGINE_DELAY_FILE);
		while (!stopping) {
			if (forward_stream(&f) < 0 && !stopping) {
				/* Only a hard input error gets here (the pipe could not be
				 * opened or read). A full FIFO never does. */
				f.reopens++;
				event_note(&f, EV_REOPEN, 1,
					   "airplay.pcm reopened after a hard error; %llu total",
					   (unsigned long long)f.reopens);
				discard_held(&f, "input pipe error");
				usleep(250000);
			}
		}
		le_ad_bridge_close(&f.delay);
	}
	return 0;
}
