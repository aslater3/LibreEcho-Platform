/*
 * Host tests for the producer session-id derivation (music_session_id.h).
 *
 * The frozen frame carries a nonzero uint32 "session" that must change on every
 * producer start even when the supervisor restarts the engine twice inside one
 * millisecond.  The previous millisecond-granularity derivation collided there;
 * these tests pin the collision resistance and the nonzero contract.
 *
 * The real clock and the real entropy source are used, so the same-millisecond
 * case is the actual restart boundary a consumer branches on.
 */
#define _GNU_SOURCE

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>

/*
 * Detect the getrandom syscall exactly as the header does, so the failure
 * injection below is only compiled on a target that would actually use it.
 */
#if defined(__has_include)
#  if __has_include(<sys/syscall.h>)
#    include <sys/syscall.h>
#    if defined(SYS_getrandom)
#      define TEST_HAVE_GETRANDOM 1
#    endif
#  endif
#endif

#ifdef TEST_HAVE_GETRANDOM
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>

/*
 * Compile-time replacement for the header's entropy hooks.  Both are
 * runtime-toggled so one translation unit can exercise the real path and the
 * unavailable-entropy path:
 *   - the getrandom wrapper records the flags it was called with (the fix must
 *     be non-blocking) and can return EAGAIN, modelling the pre-CRNG-init
 *     window where a blocking call would have stalled the engine;
 *   - the /dev/urandom open can be forced to fail, so the final clock/pid
 *     fallback is reached without touching the host's real entropy.
 */
static int force_getrandom_eagain = 0;
static int force_urandom_fail = 0;
static unsigned int seen_getrandom_flags = 0U;

static long test_getrandom(void *buf, size_t len, unsigned int flags)
{
	seen_getrandom_flags = flags;
	if (force_getrandom_eagain) {
		errno = EAGAIN;
		return -1L;
	}
	return (long)syscall(SYS_getrandom, buf, len, flags);
}

static int test_open_urandom(void)
{
	if (force_urandom_fail) {
		errno = EAGAIN;
		return -1;
	}
	return open("/dev/urandom", O_RDONLY);
}

#define MUSIC_SESSION_ID_GETRANDOM(buf, len, flags) \
	test_getrandom((buf), (len), (flags))
#define MUSIC_SESSION_ID_OPEN_URANDOM() test_open_urandom()
#endif /* TEST_HAVE_GETRANDOM */

#include "music_session_id.h"

static uint64_t now_ms(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
		return 0ULL;
	return (uint64_t)now.tv_sec * 1000U + (uint64_t)(now.tv_nsec / 1000000L);
}

/* A fixed clock and pid with three different entropy values must not all fold
 * to the same id: entropy is what defeats a same-nanosecond restart. */
static int test_mix_separates_clocks_pids_and_entropy(void)
{
	uint32_t base = music_session_id_mix(1000ULL, 7U, 1U);
	uint32_t other_clock = music_session_id_mix(1001ULL, 7U, 1U);
	uint32_t other_pid = music_session_id_mix(1000ULL, 8U, 1U);
	uint32_t other_entropy = music_session_id_mix(1000ULL, 7U, 2U);

	if (base == 0U) {
		fprintf(stderr, "mix returned the forbidden zero session\n");
		return 1;
	}
	if (base == other_clock || base == other_pid || base == other_entropy ||
	    other_clock == other_pid || other_clock == other_entropy ||
	    other_pid == other_entropy) {
		fprintf(stderr, "mix collapsed distinct clock/pid/entropy inputs\n");
		return 1;
	}
	return 0;
}

/* The required regression: two seeds created within the same millisecond must
 * differ.  Two back-to-back calls complete far inside one millisecond, so the
 * fast path is exercised on the first attempt. */
static int test_two_sessions_in_same_millisecond_differ(void)
{
	int attempt;

	for (attempt = 0; attempt < 100000; ++attempt) {
		uint64_t before = now_ms();
		uint32_t first = music_session_id_seed();
		uint32_t second = music_session_id_seed();

		if (first == 0U || second == 0U) {
			fprintf(stderr, "seed returned the forbidden zero session\n");
			return 1;
		}
		if (now_ms() != before)
			continue;
		if (first == second) {
			fprintf(stderr,
				"two sessions in the same millisecond collided: %u\n",
				first);
			return 1;
		}
		return 0;
	}
	fprintf(stderr, "could not issue two seeds inside one millisecond\n");
	return 1;
}

/* Rapid successive seeds must stay distinct and nonzero. */
static int test_rapid_seeds_are_distinct(void)
{
	enum { COUNT = 256 };
	uint32_t ids[COUNT];
	unsigned int i, j;

	for (i = 0; i < COUNT; ++i) {
		ids[i] = music_session_id_seed();
		if (ids[i] == 0U) {
			fprintf(stderr, "seed %u was zero\n", i);
			return 1;
		}
	}
	for (i = 0; i < COUNT; ++i) {
		for (j = i + 1; j < COUNT; ++j) {
			if (ids[i] == ids[j]) {
				fprintf(stderr, "seeds %u and %u collided: %u\n",
					i, j, ids[i]);
				return 1;
			}
		}
	}
	return 0;
}

#ifdef TEST_HAVE_GETRANDOM
/* The entropy-unavailable regression.  getrandom must be asked for non-blocking
 * entropy, and when it reports EAGAIN (before the CRNG is initialised) and
 * /dev/urandom cannot be opened, the clock/pid fallback must still yield a
 * nonzero, restart-varying id without blocking. */
static int test_unavailable_entropy_is_nonblocking_and_still_distinct(void)
{
	struct timespec start, end, gap;
	uint32_t first, second;
	double elapsed;

	/* The real path first, to capture the flags the header passes. */
	force_getrandom_eagain = 0;
	force_urandom_fail = 0;
	(void)music_session_id_seed();
	if ((seen_getrandom_flags & (unsigned int)GRND_NONBLOCK) == 0U) {
		fprintf(stderr,
			"getrandom called with blocking flags 0x%x (pre-CRNG stall)\n",
			seen_getrandom_flags);
		return 1;
	}
	if (seen_getrandom_flags != (unsigned int)GRND_NONBLOCK) {
		fprintf(stderr, "unexpected getrandom flags 0x%x\n",
			seen_getrandom_flags);
		return 1;
	}

	/* Now both entropy sources are unavailable. */
	force_getrandom_eagain = 1;
	force_urandom_fail = 1;
	gap.tv_sec = 0;
	gap.tv_nsec = 1000000L; /* 1 ms: the clock/pid fallback must vary */
	if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
		fprintf(stderr, "clock_gettime failed\n");
		return 1;
	}
	first = music_session_id_seed();
	nanosleep(&gap, NULL);
	second = music_session_id_seed();
	if (clock_gettime(CLOCK_MONOTONIC, &end) != 0) {
		fprintf(stderr, "clock_gettime failed\n");
		return 1;
	}
	force_getrandom_eagain = 0;
	force_urandom_fail = 0;

	if (first == 0U || second == 0U) {
		fprintf(stderr, "unavailable-entropy seed returned the forbidden zero\n");
		return 1;
	}
	if (first == second) {
		fprintf(stderr, "unavailable-entropy seeds collided: %u\n", first);
		return 1;
	}
	elapsed = (double)(end.tv_sec - start.tv_sec) +
		  (double)(end.tv_nsec - start.tv_nsec) / 1e9;
	if (elapsed > 1.0) {
		fprintf(stderr,
			"unavailable-entropy path took %.3fs (blocked on entropy?)\n",
			elapsed);
		return 1;
	}
	return 0;
}
#endif /* TEST_HAVE_GETRANDOM */

int main(void)
{
	if (test_mix_separates_clocks_pids_and_entropy() ||
	    test_two_sessions_in_same_millisecond_differ() ||
	    test_rapid_seeds_are_distinct()
#ifdef TEST_HAVE_GETRANDOM
	    || test_unavailable_entropy_is_nonblocking_and_still_distinct()
#endif
	    )
		return 1;
	printf("music session-id: ok (same-millisecond sessions differ)\n");
	return 0;
}
