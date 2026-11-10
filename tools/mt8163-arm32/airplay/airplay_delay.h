#ifndef LIBREECHO_AIRPLAY_DELAY_H
#define LIBREECHO_AIRPLAY_DELAY_H
/*
 * AirPlay output-delay channel (issue #356).
 *
 * Shairport Sync's pipe backend has no delay(), so it cannot see how much
 * audio it has written but nobody has played yet, and so it cannot correct
 * sender-vs-DAC drift or hold AirPlay group sync.  This header is the shared
 * definition of the three tiny fixed-size RAM pages that carry that number:
 *
 *   <audio root>/airplay.engine-delay   written by libreecho-audio-engine
 *       cumulative bytes the engine has read from airplay-media.pcm, the
 *       frames queued inside the engine for the AirPlay bus, the frames
 *       queued in ALSA, a monotonic timestamp and a "draining" flag.
 *   <airplay.pcm>.delay                 written by libreecho-airplay-audio
 *       downstream_frames = bridge remainder + airplay-media FIFO +
 *       engine queue + ALSA queue, at a monotonic timestamp.
 *   <airplay.pcm>.delay-read            written by Shairport's pipe backend
 *       the delay() value it last read plus a fallback counter, for the
 *       bridge's status file and event lines.
 *
 * Shairport's delay() then returns
 *     frames in airplay.pcm (FIONREAD, read at call time) + downstream
 * decayed by the time elapsed since the snapshot while the engine drains.
 * Anything stale, missing or inconsistent makes delay() fail so Shairport
 * falls back to its previous no-delay() behaviour instead of using garbage.
 *
 * Every page is a seqlock (seq is odd while its single writer updates) so a
 * reader never sees a torn snapshot.  All pages live on tmpfs under /run:
 * nothing here ever touches /data.  The including file must have enabled
 * POSIX/GNU feature macros (clock_gettime, mmap) before including this.
 */
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stddef.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define LE_AD_RATE 48000ULL
/* A snapshot older than this is not trusted.  The engine publishes at least
 * every ~45 ms while playing and every 20 ms while idle. */
#define LE_AD_STALE_NS 250000000ULL
#define LE_AD_PAGE_SIZE 64u
#define LE_AD_MAGIC_ENGINE 0x3145454cu /* "LEE1" */
#define LE_AD_MAGIC_BRIDGE 0x3142454cu /* "LEB1" */
#define LE_AD_MAGIC_READ 0x3152454cu   /* "LER1" */
/* A recomputed FIFO occupancy further than this from the live FIONREAD is
 * treated as a stale offset and re-based (64 KiB FIFO plus a safe margin). */
#define LE_AD_REBASE_BYTES 262144

enum le_ad_reason {
	LE_AD_OK = 0,
	LE_AD_NO_PAGE = 1,       /* page missing, short or wrong magic */
	LE_AD_STALE = 2,         /* bridge snapshot older than LE_AD_STALE_NS */
	LE_AD_NO_ENGINE = 3,     /* bridge could not read the engine page */
	LE_AD_ENGINE_STALE = 4,  /* engine snapshot older than LE_AD_STALE_NS */
	LE_AD_ALSA_UNKNOWN = 5,  /* engine is playing but ALSA level unknown */
	LE_AD_NO_PIPE = 6,       /* Shairport has no open pipe */
	LE_AD_RACE = 7,          /* page kept changing under the reader */
	LE_AD_FIONREAD_FAILED = 8,
	LE_AD_REASON_COUNT = 9
};

static inline const char *le_ad_reason_name(unsigned int reason)
{
	static const char *const names[LE_AD_REASON_COUNT] = {
		"ok", "no-page", "stale", "no-engine", "engine-stale",
		"alsa-unknown", "no-pipe", "race", "fionread-failed"
	};
	return reason < LE_AD_REASON_COUNT ? names[reason] : "unknown";
}

struct le_ad_engine {
	uint32_t magic;
	uint32_t seq;
	uint64_t epoch;            /* engine start; changes on a restart */
	uint64_t mono_ns;          /* CLOCK_MONOTONIC of the snapshot */
	uint64_t media_read_bytes; /* cumulative bytes read from airplay-media.pcm */
	uint32_t queue_frames;     /* AirPlay frames held inside the engine */
	uint32_t alsa_frames;      /* frames queued in the ALSA ring at mono_ns */
	uint32_t playing;          /* PCM open and draining the AirPlay bus */
	uint32_t alsa_valid;       /* alsa_frames is a real measurement */
};

struct le_ad_bridge {
	uint32_t magic;
	uint32_t seq;
	uint64_t mono_ns;
	uint32_t downstream_frames; /* everything past airplay.pcm, at mono_ns */
	uint32_t playing;           /* downstream drains at the DAC rate */
	uint32_t valid;
	uint32_t reason;            /* enum le_ad_reason when !valid */
	uint32_t pend_frames;       /* diagnostics: bridge remainder */
	uint32_t fifo_frames;       /* diagnostics: airplay-media FIFO */
	uint32_t queue_frames;      /* diagnostics: engine queue */
	uint32_t alsa_frames;       /* diagnostics: ALSA queue */
};

struct le_ad_read {
	uint32_t magic;
	uint32_t seq;
	uint64_t mono_ns;       /* when delay() last ran */
	int64_t delay_frames;   /* its last successful result */
	uint32_t reads;         /* successful calls */
	uint32_t fallbacks;     /* failed calls */
	uint32_t last_reason;
	uint32_t pad;
};

typedef char le_ad_engine_fits[sizeof(struct le_ad_engine) <= LE_AD_PAGE_SIZE ? 1 : -1];
typedef char le_ad_bridge_fits[sizeof(struct le_ad_bridge) <= LE_AD_PAGE_SIZE ? 1 : -1];
typedef char le_ad_read_fits[sizeof(struct le_ad_read) <= LE_AD_PAGE_SIZE ? 1 : -1];

static inline uint64_t le_ad_now_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
		return 0;
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* ---- seqlock primitives (word 1 of every page is the sequence) ---------- */

static inline uint32_t le_ad_seq_load(const void *page)
{
	return __atomic_load_n((const uint32_t *)page + 1, __ATOMIC_ACQUIRE);
}

/* Make the page's sequence odd.  An already-odd sequence (a writer that
 * died mid-update) is left odd; end() always lands on an even value. */
static inline void le_ad_write_begin(void *page)
{
	uint32_t *seq = (uint32_t *)page + 1;
	uint32_t value = __atomic_load_n(seq, __ATOMIC_RELAXED);

	if (!(value & 1u))
		__atomic_store_n(seq, value + 1u, __ATOMIC_RELAXED);
	__atomic_thread_fence(__ATOMIC_RELEASE);
}

static inline void le_ad_write_end(void *page)
{
	uint32_t *seq = (uint32_t *)page + 1;
	uint32_t value = __atomic_load_n(seq, __ATOMIC_RELAXED);

	__atomic_store_n(seq, (value | 1u) + 1u, __ATOMIC_RELEASE);
}

static inline int le_ad_write_in_progress(const void *page)
{
	return (le_ad_seq_load(page) & 1u) != 0;
}

/* Copy a page if it is stable and carries the expected magic. */
static inline int le_ad_read_page(const void *page, void *dst, size_t size,
				  uint32_t magic)
{
	unsigned int attempt;

	for (attempt = 0; attempt < 8; ++attempt) {
		uint32_t before = le_ad_seq_load(page), after;

		if (before & 1u)
			continue;
		memcpy(dst, page, size);
		__atomic_thread_fence(__ATOMIC_ACQUIRE);
		after = __atomic_load_n((const uint32_t *)page + 1, __ATOMIC_RELAXED);
		if (before != after)
			continue;
		return *(const uint32_t *)dst == magic ? 0 : -1;
	}
	return -1;
}

/* ---- mapped pages ------------------------------------------------------ */

/* Create (or reopen) a fixed-size, world-readable page and map it writable.
 * The file never grows or shrinks: it is always LE_AD_PAGE_SIZE bytes.  A
 * sequence left odd by a dead writer is made even again. */
static inline void *le_ad_map_writer(const char *path, uint32_t magic)
{
	struct stat st;
	void *page;
	int fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0644);

	if (fd < 0)
		return NULL;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
	    (st.st_size != (off_t)LE_AD_PAGE_SIZE &&
	     ftruncate(fd, (off_t)LE_AD_PAGE_SIZE) < 0)) {
		close(fd);
		return NULL;
	}
	page = mmap(NULL, LE_AD_PAGE_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	close(fd);
	if (page == MAP_FAILED)
		return NULL;
	if (*(volatile uint32_t *)page != magic) {
		memset(page, 0, LE_AD_PAGE_SIZE);
		*(volatile uint32_t *)page = magic;
	} else if (le_ad_write_in_progress(page)) {
		le_ad_write_end(page);
	}
	return page;
}

struct le_ad_reader {
	const void *map;
	dev_t dev;
	ino_t ino;
};

static inline void le_ad_reader_close(struct le_ad_reader *reader)
{
	if (reader->map)
		munmap((void *)reader->map, LE_AD_PAGE_SIZE);
	memset(reader, 0, sizeof(*reader));
}

/* Return the mapping of a page, re-mapping when the file was replaced.  The
 * stat() makes replacement visible; the caller decides how often to pay it. */
static inline const void *le_ad_reader_get(struct le_ad_reader *reader,
					   const char *path, int revalidate)
{
	struct stat st;
	int fd;
	void *map;

	if (reader->map && !revalidate)
		return reader->map;
	if (stat(path, &st) < 0 || !S_ISREG(st.st_mode) ||
	    st.st_size < (off_t)LE_AD_PAGE_SIZE) {
		le_ad_reader_close(reader);
		return NULL;
	}
	if (reader->map && st.st_dev == reader->dev && st.st_ino == reader->ino)
		return reader->map;
	le_ad_reader_close(reader);
	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return NULL;
	map = mmap(NULL, LE_AD_PAGE_SIZE, PROT_READ, MAP_SHARED, fd, 0);
	close(fd);
	if (map == MAP_FAILED)
		return NULL;
	reader->map = map;
	reader->dev = st.st_dev;
	reader->ino = st.st_ino;
	return map;
}

/* ---- engine side ------------------------------------------------------- */

struct le_ad_engine_pub {
	struct le_ad_engine *page;
	uint64_t epoch;
};

static inline int le_ad_engine_open(struct le_ad_engine_pub *pub, const char *path)
{
	pub->page = (struct le_ad_engine *)le_ad_map_writer(path, LE_AD_MAGIC_ENGINE);
	pub->epoch = le_ad_now_ns() ^ ((uint64_t)getpid() << 40);
	if (pub->epoch == 0)
		pub->epoch = 1;
	return pub->page ? 0 : -1;
}

static inline void le_ad_engine_publish(struct le_ad_engine_pub *pub,
					uint64_t mono_ns, uint64_t media_read_bytes,
					uint32_t queue_frames, uint32_t alsa_frames,
					int alsa_valid, int playing)
{
	struct le_ad_engine *page = pub->page;

	if (!page)
		return;
	le_ad_write_begin(page);
	page->epoch = pub->epoch;
	page->mono_ns = mono_ns;
	page->media_read_bytes = media_read_bytes;
	page->queue_frames = queue_frames;
	page->alsa_frames = alsa_valid ? alsa_frames : 0u;
	page->alsa_valid = alsa_valid ? 1u : 0u;
	page->playing = playing ? 1u : 0u;
	le_ad_write_end(page);
}

static inline void le_ad_engine_close(struct le_ad_engine_pub *pub)
{
	if (pub->page)
		munmap(pub->page, LE_AD_PAGE_SIZE);
	pub->page = NULL;
}

/* ---- bridge side ------------------------------------------------------- */

struct le_ad_parts {
	uint32_t pend_frames;
	uint32_t fifo_frames;
	uint32_t queue_frames;
	uint32_t alsa_frames;
	uint32_t downstream_frames;
	uint32_t playing;
};

struct le_ad_bridge_ctx {
	struct le_ad_reader engine;
	struct le_ad_bridge *page;
	uint64_t epoch;
	int64_t offset;
	int have_epoch;
	char engine_path[256];
};

static inline uint64_t le_ad_frames_for_ns(uint64_t ns)
{
	if (ns > 4ULL * LE_AD_STALE_NS)
		ns = 4ULL * LE_AD_STALE_NS;
	return ns * LE_AD_RATE / 1000000000ULL;
}

/* Combine an engine snapshot with the bridge's own counters.
 *
 * Bytes the bridge has written into airplay-media.pcm minus bytes the engine
 * has read (as of the snapshot) are the FIFO content; both counters only ever
 * grow, so no timing window can lose or double-count a chunk.  The offset
 * re-bases the two counters whenever the engine restarts or the bridge starts
 * after it.  Everything past the pipe drains at the DAC rate while the engine
 * is playing the AirPlay bus, so the sum is decayed by the snapshot's age.
 * Returns LE_AD_OK or the reason the number cannot be trusted. */
static inline unsigned int le_ad_downstream(struct le_ad_bridge_ctx *ctx,
					    const struct le_ad_engine *e,
					    uint64_t now_ns, uint64_t written_bytes,
					    int64_t media_fifo_bytes,
					    uint32_t pend_frames,
					    struct le_ad_parts *parts)
{
	int64_t fifo_bytes;
	uint64_t elapsed, total, consumed;

	memset(parts, 0, sizeof(*parts));
	elapsed = now_ns > e->mono_ns ? now_ns - e->mono_ns : 0;
	if (elapsed > LE_AD_STALE_NS)
		return LE_AD_ENGINE_STALE;
	if (e->playing && !e->alsa_valid)
		return LE_AD_ALSA_UNKNOWN;
	if (!ctx->have_epoch || ctx->epoch != e->epoch) {
		ctx->epoch = e->epoch;
		ctx->offset = (int64_t)written_bytes - (int64_t)e->media_read_bytes -
			      media_fifo_bytes;
		ctx->have_epoch = 1;
	}
	fifo_bytes = (int64_t)written_bytes - (int64_t)e->media_read_bytes - ctx->offset;
	if (fifo_bytes < 0 || fifo_bytes - media_fifo_bytes > LE_AD_REBASE_BYTES) {
		/* A counter jumped (e.g. the FIFO was drained behind our back):
		 * trust the live FIONREAD and re-base from it. */
		ctx->offset = (int64_t)written_bytes - (int64_t)e->media_read_bytes -
			      media_fifo_bytes;
		fifo_bytes = media_fifo_bytes;
	}
	parts->pend_frames = pend_frames;
	parts->fifo_frames = (uint32_t)(fifo_bytes / 4);
	parts->queue_frames = e->queue_frames;
	parts->alsa_frames = e->alsa_frames;
	parts->playing = e->playing ? 1u : 0u;
	total = (uint64_t)pend_frames + parts->fifo_frames + e->queue_frames +
		e->alsa_frames;
	consumed = e->playing ? le_ad_frames_for_ns(elapsed) : 0;
	if (consumed > total)
		consumed = total;
	total -= consumed;
	parts->downstream_frames = total > 0xffffffffULL ? 0xffffffffu : (uint32_t)total;
	return LE_AD_OK;
}

static inline void le_ad_bridge_open(struct le_ad_bridge_ctx *ctx,
				     const char *page_path, const char *engine_path)
{
	memset(ctx, 0, sizeof(*ctx));
	ctx->page = (struct le_ad_bridge *)le_ad_map_writer(page_path, LE_AD_MAGIC_BRIDGE);
	strncpy(ctx->engine_path, engine_path, sizeof(ctx->engine_path) - 1);
}

/* Mark the page "being updated" before the bridge moves bytes out of
 * airplay.pcm, so Shairport's FIONREAD can never be paired with a snapshot
 * that does not include those bytes yet. */
static inline void le_ad_bridge_begin(struct le_ad_bridge_ctx *ctx)
{
	if (ctx->page)
		le_ad_write_begin(ctx->page);
}

static inline void le_ad_bridge_publish(struct le_ad_bridge_ctx *ctx, uint64_t now_ns,
					unsigned int reason,
					const struct le_ad_parts *parts)
{
	struct le_ad_bridge *page = ctx->page;

	if (!page)
		return;
	le_ad_write_begin(page);
	page->mono_ns = now_ns;
	page->valid = reason == LE_AD_OK;
	page->reason = reason;
	page->downstream_frames = reason == LE_AD_OK ? parts->downstream_frames : 0;
	page->playing = reason == LE_AD_OK ? parts->playing : 0;
	page->pend_frames = parts->pend_frames;
	page->fifo_frames = parts->fifo_frames;
	page->queue_frames = parts->queue_frames;
	page->alsa_frames = parts->alsa_frames;
	le_ad_write_end(page);
}

/* One bridge tick: read the engine page, combine, publish.  Returns the
 * reason (LE_AD_OK when a trustworthy number was published). */
static inline unsigned int le_ad_bridge_update(struct le_ad_bridge_ctx *ctx,
					       uint64_t now_ns, uint64_t written_bytes,
					       int64_t media_fifo_bytes,
					       uint32_t pend_frames,
					       struct le_ad_parts *parts)
{
	struct le_ad_engine e;
	const void *map = le_ad_reader_get(&ctx->engine, ctx->engine_path, 0);
	unsigned int reason;

	memset(parts, 0, sizeof(*parts));
	parts->pend_frames = pend_frames;
	if (!map || le_ad_read_page(map, &e, sizeof(e), LE_AD_MAGIC_ENGINE) != 0) {
		(void)le_ad_reader_get(&ctx->engine, ctx->engine_path, 1);
		reason = LE_AD_NO_ENGINE;
	} else {
		reason = le_ad_downstream(ctx, &e, now_ns, written_bytes,
					  media_fifo_bytes, pend_frames, parts);
		if (reason == LE_AD_ENGINE_STALE)
			(void)le_ad_reader_get(&ctx->engine, ctx->engine_path, 1);
	}
	le_ad_bridge_publish(ctx, now_ns, reason, parts);
	return reason;
}

static inline void le_ad_bridge_close(struct le_ad_bridge_ctx *ctx)
{
	le_ad_reader_close(&ctx->engine);
	if (ctx->page)
		munmap(ctx->page, LE_AD_PAGE_SIZE);
	ctx->page = NULL;
}

/* ---- Shairport side ---------------------------------------------------- */

/* The delay a Shairport that wrote into the pipe would see.  Pure function of
 * a bridge snapshot, the pipe occupancy and the clock. */
static inline unsigned int le_ad_delay_from_snapshot(const struct le_ad_bridge *b,
						     uint64_t now_ns,
						     uint64_t pipe_frames, long *delay)
{
	uint64_t age, downstream, consumed;

	if (!b->valid)
		return b->reason ? b->reason : LE_AD_NO_ENGINE;
	age = now_ns > b->mono_ns ? now_ns - b->mono_ns : 0;
	if (age > LE_AD_STALE_NS)
		return LE_AD_STALE;
	downstream = b->downstream_frames;
	consumed = b->playing ? le_ad_frames_for_ns(age) : 0;
	if (consumed > downstream)
		consumed = downstream;
	*delay = (long)(pipe_frames + downstream - consumed);
	return LE_AD_OK;
}

struct le_ad_client {
	pthread_mutex_t lock;
	struct le_ad_reader bridge;
	struct le_ad_read *record;
	char bridge_path[320];
	char record_path[320];
	int ready;
	uint32_t reads;
	uint32_t fallbacks;
};

static inline void le_ad_client_init(struct le_ad_client *client, const char *pipe_path)
{
	memset(client, 0, sizeof(*client));
	pthread_mutex_init(&client->lock, NULL);
	if (snprintf(client->bridge_path, sizeof(client->bridge_path), "%s.delay",
		     pipe_path) >= (int)sizeof(client->bridge_path) ||
	    snprintf(client->record_path, sizeof(client->record_path), "%s.delay-read",
		     pipe_path) >= (int)sizeof(client->record_path))
		return;
	client->ready = 1;
}

static inline void le_ad_client_record(struct le_ad_client *client, uint64_t now_ns,
				       unsigned int reason, long delay)
{
	struct le_ad_read *page;

	if (reason == LE_AD_OK)
		++client->reads;
	else
		++client->fallbacks;
	if (!client->record)
		client->record = (struct le_ad_read *)le_ad_map_writer(
			client->record_path, LE_AD_MAGIC_READ);
	page = client->record;
	if (!page)
		return;
	le_ad_write_begin(page);
	page->mono_ns = now_ns;
	if (reason == LE_AD_OK)
		page->delay_frames = delay;
	else
		page->last_reason = reason;
	page->reads = client->reads;
	page->fallbacks = client->fallbacks;
	le_ad_write_end(page);
}

/* Shairport's delay(): frames written to the pipe but not yet played.
 * Returns 0 and sets *delay, or a negative errno (-ENODEV) when the number is
 * unavailable, which makes Shairport use its no-delay() behaviour. */
static inline int le_ad_client_delay(struct le_ad_client *client, int pipe_fd,
				     unsigned int bytes_per_frame, long *delay)
{
	struct le_ad_bridge snapshot;
	unsigned int reason = LE_AD_NO_PAGE, attempt;
	uint64_t now = 0;
	long value = 0;
	int cancel_state;

	/* Shairport cancels its player threads; open()/usleep() are cancellation
	 * points, and dying with the lock held would wedge the next connection. */
	(void)pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
	pthread_mutex_lock(&client->lock);
	if (!client->ready || pipe_fd < 0 || bytes_per_frame == 0) {
		reason = client->ready ? LE_AD_NO_PIPE : LE_AD_NO_PAGE;
	} else {
		for (attempt = 0; attempt < 4; ++attempt) {
			const void *map = le_ad_reader_get(&client->bridge,
							   client->bridge_path,
							   attempt > 0);
			uint32_t before;
			int pending = 0;

			if (!map) {
				reason = LE_AD_NO_PAGE;
				break;
			}
			before = le_ad_seq_load(map);
			if (before & 1u) {
				reason = LE_AD_RACE;
				usleep(200);
				continue;
			}
			memcpy(&snapshot, map, sizeof(snapshot));
			__atomic_thread_fence(__ATOMIC_ACQUIRE);
			if (ioctl(pipe_fd, FIONREAD, &pending) < 0 || pending < 0) {
				reason = LE_AD_FIONREAD_FAILED;
				break;
			}
			now = le_ad_now_ns();
			/* The snapshot is only consistent with this FIONREAD if the
			 * bridge did not move bytes in between. */
			if (le_ad_seq_load(map) != before) {
				reason = LE_AD_RACE;
				continue;
			}
			if (snapshot.magic != LE_AD_MAGIC_BRIDGE) {
				reason = LE_AD_NO_PAGE;
				break;
			}
			reason = le_ad_delay_from_snapshot(&snapshot, now,
				(uint64_t)pending / bytes_per_frame, &value);
			break;
		}
	}
	if (now == 0)
		now = le_ad_now_ns();
	le_ad_client_record(client, now, reason, value);
	pthread_mutex_unlock(&client->lock);
	(void)pthread_setcancelstate(cancel_state, NULL);
	if (reason != LE_AD_OK)
		return -ENODEV;
	*delay = value;
	return 0;
}
#endif
