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

#include <stdint.h>
#include <stdio.h>
#include <time.h>

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

int main(void)
{
	if (test_mix_separates_clocks_pids_and_entropy() ||
	    test_two_sessions_in_same_millisecond_differ() ||
	    test_rapid_seeds_are_distinct())
		return 1;
	printf("music session-id: ok (same-millisecond sessions differ)\n");
	return 0;
}
