/*
 * Host regression for the producer clock wrap (music_features.h and the
 * audio_engine producer path).
 *
 * The wire timestamp_ms is a frozen uint32 derived from the cumulative
 * analysed-music clock.  Once that clock crosses 2^32 ms a consumer would see a
 * backward timestamp *inside a live session* and reject the frame, so the
 * producer must rotate the session -- fresh nonzero id, seq restarting at 0,
 * clock restarting near 0 -- before the crossing.
 *
 * This drives the real transport (music_feature_transport_begin_tick) at the
 * production period/rate, starting one period below the boundary, and the real
 * frame formatter, and checks:
 *
 *   - no timestamp ever moves backwards inside a session;
 *   - the rotation fires exactly on the wrapping tick, adopting a fresh
 *     nonzero session with seq reset and a clock that never wrapped;
 *   - the emitted frame keeps the frozen field set and carries the new values.
 */
#include "music_features.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* Mirror audio_engine.c's PERIOD_SIZE / DEFAULT_RATE so the boundary is the
 * one the shipped producer actually reaches. */
#define PRODUCER_FRAMES 2048U
#define PRODUCER_RATE 48000U
#define OLD_SESSION 0x3f3f0001U
#define NEW_SESSION 0x3f3f0002U

#define MAX_FIELDS 64

static uint32_t test_next_session(void)
{
	return NEW_SESSION;
}

/* The last update index whose own timestamp still fits the 32-bit wire field.
 * Computed with the producer's own elapsed-ms arithmetic. */
static uint64_t boundary_update_count(void)
{
	struct music_feature_transport probe;
	uint64_t low = 0;
	uint64_t high = (uint64_t)UINT32_MAX;

	music_feature_transport_init(&probe, OLD_SESSION, PRODUCER_FRAMES,
				     PRODUCER_RATE);
	while (low < high) {
		uint64_t mid = low + (high - low + 1U) / 2U;

		probe.update_count = (uint32_t)mid;
		if (music_feature_transport_elapsed_ms(&probe) <=
		    (uint64_t)UINT32_MAX)
			low = mid;
		else
			high = mid - 1U;
	}
	return low;
}

/* The clock must be monotonic inside a session and rotate exactly once at the
 * 32-bit boundary, with a fresh id and a restarted sequence. */
static int test_clock_never_moves_backwards_in_a_session(void)
{
	struct music_feature_transport transport;
	uint64_t boundary = boundary_update_count();
	uint64_t previous_timestamp;
	uint32_t previous_session;
	int rotated_once = 0;
	int step;

	if (boundary < 4U || boundary >= (uint64_t)UINT32_MAX) {
		fprintf(stderr, "implausible wrap boundary index %llu\n",
			(unsigned long long)boundary);
		return 1;
	}

	music_feature_transport_init(&transport, OLD_SESSION, PRODUCER_FRAMES,
				     PRODUCER_RATE);
	transport.update_count = (uint32_t)(boundary - 3U);
	previous_session = transport.session;
	previous_timestamp = music_feature_transport_elapsed_ms(&transport);

	for (step = 0; step < 8; ++step) {
		uint64_t timestamp;
		int rotated = music_feature_transport_begin_tick(
			&transport, test_next_session);

		if (rotated) {
			rotated_once = 1;
			if (transport.session == previous_session ||
			    transport.session == 0U) {
				fprintf(stderr,
					"wrap rotation did not adopt a fresh nonzero session\n");
				return 1;
			}
			if (transport.seq != 0U) {
				fprintf(stderr,
					"wrap rotation did not reset seq\n");
				return 1;
			}
			if (music_feature_transport_next_tick_wraps(&transport)) {
				fprintf(stderr,
					"fresh session is still at the wrap boundary\n");
				return 1;
			}
		} else if (transport.session != previous_session) {
			fprintf(stderr, "session changed without a rotation\n");
			return 1;
		}

		timestamp = music_feature_transport_elapsed_ms(&transport);
		if (timestamp > (uint64_t)UINT32_MAX) {
			fprintf(stderr,
				"producer clock overflowed the 32-bit wire field\n");
			return 1;
		}
		if (!rotated && timestamp < previous_timestamp) {
			fprintf(stderr,
				"timestamp moved backwards inside a session\n");
			return 1;
		}
		previous_session = transport.session;
		previous_timestamp = timestamp;
	}

	if (!rotated_once) {
		fprintf(stderr, "the producer never rotated before the wrap\n");
		return 1;
	}
	return 0;
}

/* Collect the object key tokens of a flat JSON object in order.  A key is a
 * quoted run immediately followed by ':'; quoted *values* are followed by a
 * delimiter or '}', so they are not collected. */
static size_t collect_fields(const char *json, char keys[][32], size_t max)
{
	const char *cursor = json;
	size_t count = 0;

	while ((cursor = strchr(cursor, '"')) != NULL && count < max) {
		const char *end = strchr(cursor + 1, '"');
		size_t length;
		const char *colon;

		if (!end)
			break;
		length = (size_t)(end - (cursor + 1));
		colon = end + 1;
		if (*colon == ':' && length > 0 && length < 32) {
			memcpy(keys[count], cursor + 1, length);
			keys[count][length] = '\0';
			++count;
		}
		cursor = end + 1;
	}
	return count;
}

static int same_field_set(const char *before, const char *after)
{
	char before_keys[MAX_FIELDS][32];
	char after_keys[MAX_FIELDS][32];
	size_t before_count = collect_fields(before, before_keys, MAX_FIELDS);
	size_t after_count = collect_fields(after, after_keys, MAX_FIELDS);
	size_t index;

	if (before_count == 0U || before_count != after_count) {
		fprintf(stderr, "wire field count changed: %u -> %u\n",
			(unsigned int)before_count, (unsigned int)after_count);
		return 0;
	}
	for (index = 0; index < before_count; ++index) {
		if (strcmp(before_keys[index], after_keys[index]) != 0) {
			fprintf(stderr, "wire field %u changed: %s -> %s\n",
				(unsigned int)index, before_keys[index],
				after_keys[index]);
			return 0;
		}
	}
	return 1;
}

static int expect_substring(const char *frame, const char *value,
			    const char *label)
{
	if (strstr(frame, value) == NULL) {
		fprintf(stderr, "frame is missing %s (%s)\n", label, value);
		return 1;
	}
	return 0;
}

/* A frame formatted one period below the boundary carries the old session and
 * an in-range timestamp; the first frame of the new session carries the new
 * session, seq 0 and a small clock, with an unchanged field set. */
static int test_frame_across_the_rotation(void)
{
	struct music_feature_transport transport;
	struct music_features features;
	uint8_t levels[MUSIC_FEATURES_BANDS];
	char before[MUSIC_FEATURES_FRAME_MAX];
	char after[MUSIC_FEATURES_FRAME_MAX];
	char expected[64];
	uint64_t boundary = boundary_update_count();
	uint32_t seq;
	int length;

	memset(&features, 0, sizeof(features));
	memset(levels, 0, sizeof(levels));

	music_feature_transport_init(&transport, OLD_SESSION, PRODUCER_FRAMES,
				     PRODUCER_RATE);
	transport.update_count = (uint32_t)boundary;
	seq = music_feature_transport_next_seq(&transport);
	length = music_features_format_frame(
		before, sizeof(before), &features, levels, 70,
		MUSIC_FEATURES_VERSION, transport.session, seq,
		music_feature_transport_timestamp_ms(&transport));
	if (length <= 0) {
		fprintf(stderr, "pre-rotation frame failed to format\n");
		return 1;
	}

	snprintf(expected, sizeof(expected), "\"session\":%u,", OLD_SESSION);
	if (expect_substring(before, expected, "old session"))
		return 1;
	snprintf(expected, sizeof(expected), "\"timestamp_ms\":%u,",
		 music_feature_transport_timestamp_ms(&transport));
	if (expect_substring(before, expected, "pre-wrap timestamp"))
		return 1;

	if (!music_feature_transport_begin_tick(&transport, test_next_session)) {
		fprintf(stderr, "the wrapping tick did not rotate the session\n");
		return 1;
	}

	seq = music_feature_transport_next_seq(&transport);
	length = music_features_format_frame(
		after, sizeof(after), &features, levels, 70,
		MUSIC_FEATURES_VERSION, transport.session, seq,
		music_feature_transport_timestamp_ms(&transport));
	if (length <= 0) {
		fprintf(stderr, "post-rotation frame failed to format\n");
		return 1;
	}

	snprintf(expected, sizeof(expected), "\"session\":%u,", NEW_SESSION);
	if (expect_substring(after, expected, "rotated session"))
		return 1;
	snprintf(expected, sizeof(expected), "\"seq\":%u,", 0U);
	if (expect_substring(after, expected, "restarted sequence"))
		return 1;
	snprintf(expected, sizeof(expected), "\"timestamp_ms\":%u,",
		 PRODUCER_FRAMES * 1000U / PRODUCER_RATE);
	if (expect_substring(after, expected, "restarted clock"))
		return 1;
	if (!same_field_set(before, after)) {
		fprintf(stderr, "the wire field set changed across the rotation\n");
		return 1;
	}
	return 0;
}

int main(void)
{
	if (test_clock_never_moves_backwards_in_a_session() ||
	    test_frame_across_the_rotation())
		return 1;
	printf("music transport wrap: ok (session rotates before the 32-bit clock wraps)\n");
	return 0;
}
