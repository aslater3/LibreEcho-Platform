/*
 * Deterministic host tests for the version-2 musical feature producer.
 *
 * The tests drive the real audio_visualizer_process_features() path with
 * synthesized 48 kHz stereo PCM: a stable tempo click train, differentiated
 * low/mid/high bursts, a build/drop, a quiet gap and re-entry, a steady dense
 * false-positive, and a beatless confidence-decay tail.  They also assert the
 * bounded memory, the fixed size of the serialized frame and the deterministic
 * (bit-identical) replay guarantee.
 *
 * MF_DEBUG=1 dumps a per-period CSV trace for calibration.
 */
#define _DEFAULT_SOURCE

#include "audio_visualizer.h"
#include "music_features.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define TEST_FRAMES 2048U
#define TEST_MAX_PERIODS 1024
#define TEST_PI 3.14159265358979323846
#define TEST_RATE 48000.0

static int debug_enabled;
static struct music_features trace[TEST_MAX_PERIODS];
static uint8_t trace_levels[TEST_MAX_PERIODS][AUDIO_VISUALIZER_BANDS];

static void buffer_clear(int16_t *buffer)
{
	memset(buffer, 0, TEST_FRAMES * 2U * sizeof(int16_t));
}

static void buffer_add(int16_t *buffer, unsigned int frame, double value)
{
	double left = (double)buffer[frame * 2U] + value;
	double right = (double)buffer[frame * 2U + 1U] + value;

	if (left > 32767.0)
		left = 32767.0;
	if (left < -32768.0)
		left = -32768.0;
	if (right > 32767.0)
		right = 32767.0;
	if (right < -32768.0)
		right = -32768.0;
	buffer[frame * 2U] = (int16_t)lround(left);
	buffer[frame * 2U + 1U] = (int16_t)lround(right);
}

static void add_tone(int16_t *buffer, int period, double frequency,
		     double amplitude)
{
	double base = (double)period * TEST_FRAMES;
	unsigned int frame;

	for (frame = 0; frame < TEST_FRAMES; ++frame) {
		double phase = 2.0 * TEST_PI * frequency *
			(base + frame) / TEST_RATE;

		buffer_add(buffer, frame, sin(phase) * amplitude);
	}
}

static void add_click(int16_t *buffer, int period, double frequency,
		      double amplitude, double decay_seconds)
{
	double base = (double)period * TEST_FRAMES;
	unsigned int frame;

	for (frame = 0; frame < TEST_FRAMES; ++frame) {
		double seconds = (double)frame / TEST_RATE;
		double envelope = exp(-seconds / decay_seconds);
		double phase = 2.0 * TEST_PI * frequency *
			(base + frame) / TEST_RATE;

		buffer_add(buffer, frame, sin(phase) * amplitude * envelope);
	}
}

static const double dense_centres[AUDIO_VISUALIZER_BANDS] = {
	63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6500, 11000
};

static void add_dense(int16_t *buffer, int period, double amplitude)
{
	unsigned int band;

	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
		add_tone(buffer, period, dense_centres[band],
			 amplitude / (double)AUDIO_VISUALIZER_BANDS *
			 2.0);
}

typedef void (*generator_fn)(int period, int16_t *buffer, void *context);

static int run_scenario(generator_fn generator, void *context, int periods,
			int debug)
{
	struct audio_visualizer visualizer;
	int16_t buffer[TEST_FRAMES * 2U];
	int period;

	if (periods > TEST_MAX_PERIODS)
		periods = TEST_MAX_PERIODS;
	audio_visualizer_init(&visualizer);
	memset(trace, 0, sizeof(trace));
	for (period = 0; period < periods; ++period) {
		buffer_clear(buffer);
		generator(period, buffer, context);
		audio_visualizer_process_features(&visualizer, buffer,
						  TEST_FRAMES, 2,
						  trace_levels[period],
						  &trace[period]);
		if (debug) {
			printf("%d,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,"
			       "%u,%u,%u,%u,0x%03x\n",
			       period,
			       trace[period].energy,
			       trace[period].warmth,
			       trace[period].brightness_axis,
			       trace[period].density,
			       trace[period].transientness,
			       trace[period].groove,
			       trace[period].build,
			       trace[period].spaciousness,
			       trace[period].loudness_fast,
			       trace[period].loudness_slow,
			       trace[period].onset_low,
			       trace[period].onset_mid,
			       trace[period].onset_high,
			       trace[period].beat_strength,
			       trace[period].beat_confidence,
			       trace[period].novelty,
			       trace[period].event_strength,
			       trace[period].bpm_x100,
			       trace[period].events);
		}
	}
	return periods;
}

static int has_event(int start, int periods, uint16_t mask)
{
	int period;

	for (period = start; period < start + periods &&
	     period < TEST_MAX_PERIODS; ++period)
		if ((trace[period].events & mask) != 0)
			return 1;
	return 0;
}

static unsigned int max_u8(int start, int periods,
			   uint8_t (*getter)(const struct music_features *))
{
	unsigned int value = 0;
	int period;

	for (period = start; period < start + periods &&
	     period < TEST_MAX_PERIODS; ++period) {
		unsigned int candidate = getter(&trace[period]);

		if (candidate > value)
			value = candidate;
	}
	return value;
}

static uint8_t get_confidence(const struct music_features *f)
{
	return f->beat_confidence;
}

static uint8_t get_build(const struct music_features *f)
{
	return f->build;
}

static uint8_t get_novelty(const struct music_features *f)
{
	return f->novelty;
}

/* ------------------------------------------------------------------ */

struct click_context {
	int period;
	double frequency;
	double amplitude;
};

static void gen_tempo_clicks(int period, int16_t *buffer, void *context)
{
	struct click_context *click = context;

	if ((period % click->period) == 0)
		add_click(buffer, period, click->frequency, click->amplitude,
			  0.05);
}

static int test_stable_tempo(void)
{
	struct click_context click = { 12, 90.0, 24000.0 };
	int periods = run_scenario(gen_tempo_clicks, &click, 320, debug_enabled);
	unsigned int confidence = max_u8(160, 160, get_confidence);
	uint16_t bpm = 0;
	int period;
	int kicks = 0;

	for (period = 160; period < periods; ++period) {
		if (trace[period].bpm_x100 != 0)
			bpm = trace[period].bpm_x100;
		if ((trace[period].events & MUSIC_EVENT_KICK) != 0)
			++kicks;
	}
	if (confidence < 150U) {
		fprintf(stderr, "tempo confidence too low: %u\n", confidence);
		return 1;
	}
	if (bpm < 11200U || bpm > 12300U) {
		fprintf(stderr, "tempo bpm_x100 out of band: %u\n", bpm);
		return 1;
	}
	if (kicks < 4) {
		fprintf(stderr, "tempo click train produced %d kicks\n",
			kicks);
		return 1;
	}
	return 0;
}

static int test_differentiated_onsets(void)
{
	struct click_context low = { 100, 80.0, 26000.0 };
	struct click_context mid = { 100, 1000.0, 26000.0 };
	struct click_context high = { 100, 6000.0, 26000.0 };
	int failures = 0;

	/* Low burst: KICK dominates, MID carries less onset energy. */
	run_scenario(gen_tempo_clicks, &low, 40, 0);
	if (!has_event(0, 40, MUSIC_EVENT_KICK)) {
		fprintf(stderr, "low burst produced no KICK\n");
		++failures;
	}
	{
		unsigned int low_peak = 0, mid_peak = 0;
		int p;

		for (p = 0; p < 40; ++p) {
			if (trace[p].onset_low > low_peak)
				low_peak = trace[p].onset_low;
			if (trace[p].onset_mid > mid_peak)
				mid_peak = trace[p].onset_mid;
		}
		if (low_peak < 60U || low_peak <= mid_peak) {
			fprintf(stderr,
				"low onset not dominant: low=%u mid=%u\n",
				low_peak, mid_peak);
			++failures;
		}
	}

	/* Mid burst: SNARE, mid onset dominant. */
	run_scenario(gen_tempo_clicks, &mid, 40, 0);
	if (!has_event(0, 40, MUSIC_EVENT_SNARE)) {
		fprintf(stderr, "mid burst produced no SNARE\n");
		++failures;
	}
	{
		unsigned int mid_peak = 0, low_peak = 0, high_peak = 0;
		int p;

		for (p = 0; p < 40; ++p) {
			if (trace[p].onset_mid > mid_peak)
				mid_peak = trace[p].onset_mid;
			if (trace[p].onset_low > low_peak)
				low_peak = trace[p].onset_low;
			if (trace[p].onset_high > high_peak)
				high_peak = trace[p].onset_high;
		}
		if (mid_peak < 60U || mid_peak < low_peak ||
		    mid_peak < high_peak) {
			fprintf(stderr,
				"mid onset not dominant: low=%u mid=%u high=%u\n",
				low_peak, mid_peak, high_peak);
			++failures;
		}
	}

	/* High burst: HIGH, high onset dominant. */
	run_scenario(gen_tempo_clicks, &high, 40, 0);
	if (!has_event(0, 40, MUSIC_EVENT_HIGH)) {
		fprintf(stderr, "high burst produced no HIGH\n");
		++failures;
	}
	{
		unsigned int high_peak = 0, mid_peak = 0;
		int p;

		for (p = 0; p < 40; ++p) {
			if (trace[p].onset_high > high_peak)
				high_peak = trace[p].onset_high;
			if (trace[p].onset_mid > mid_peak)
				mid_peak = trace[p].onset_mid;
		}
		if (high_peak < 60U || high_peak <= mid_peak) {
			fprintf(stderr,
				"high onset not dominant: high=%u mid=%u\n",
				high_peak, mid_peak);
			++failures;
		}
	}
	return failures;
}

struct balance_context {
	int switch_period;
};

static void gen_balance(int period, int16_t *buffer, void *context)
{
	struct balance_context *balance = context;

	if (period < balance->switch_period)
		add_tone(buffer, period, 100.0, 16000.0);
	else
		add_tone(buffer, period, 6000.0, 16000.0);
}

static int test_balance_axes(void)
{
	struct balance_context balance = { 60 };
	int periods = run_scenario(gen_balance, &balance, 120, 0);
	unsigned int warm_low = 0, warm_high = 0;
	unsigned int bright_low = 0, bright_high = 0;
	int period;

	for (period = 40; period < 60; ++period) {
		if (trace[period].warmth > warm_low)
			warm_low = trace[period].warmth;
		if (trace[period].brightness_axis > bright_low)
			bright_low = trace[period].brightness_axis;
	}
	for (period = periods - 20; period < periods; ++period) {
		if (trace[period].warmth > warm_high)
			warm_high = trace[period].warmth;
		if (trace[period].brightness_axis > bright_high)
			bright_high = trace[period].brightness_axis;
	}
	if (warm_low <= warm_high + 32U) {
		fprintf(stderr, "warmth does not track bass: low=%u high=%u\n",
			warm_low, warm_high);
		return 1;
	}
	if (bright_high <= bright_low + 32U) {
		fprintf(stderr,
			"brightness_axis does not track treble: low=%u high=%u\n",
			bright_low, bright_high);
		return 1;
	}
	return 0;
}

struct build_context {
	int warmup_end;
	int ramp_end;
	int dip_end;
	int impact;
};

static void gen_build_drop(int period, int16_t *buffer, void *context)
{
	struct build_context *build = context;

	if (period < build->warmup_end) {
		add_dense(buffer, period, 8000.0);
	} else if (period < build->ramp_end) {
		double span = (double)(build->ramp_end - build->warmup_end);
		double t = (double)(period - build->warmup_end) / span;

		add_dense(buffer, period, 8000.0 + 22000.0 * t);
	} else if (period < build->dip_end) {
		add_dense(buffer, period, 4000.0);
	} else if (period == build->impact) {
		add_dense(buffer, period, 30000.0);
		add_click(buffer, period, 70.0, 28000.0, 0.12);
		add_click(buffer, period, 5000.0, 16000.0, 0.03);
	} else {
		add_dense(buffer, period, 26000.0);
	}
}

static int test_build_and_drop(void)
{
	struct build_context build = { 100, 200, 208, 208 };
	int periods = run_scenario(gen_build_drop, &build, 300, debug_enabled);
	unsigned int peak_build = max_u8(100, 100, get_build);

	if (peak_build < 96U) {
		fprintf(stderr, "build axis never crossed the gate: %u\n",
			peak_build);
		return 1;
	}
	if (!has_event(100, 100, MUSIC_EVENT_BUILD)) {
		fprintf(stderr, "ramp produced no BUILD event\n");
		return 1;
	}
	if (!has_event(200, 40, MUSIC_EVENT_DROP)) {
		fprintf(stderr, "drop boundary produced no DROP event\n");
		return 1;
	}
	(void)periods;
	return 0;
}

static void gen_gap_reentry(int period, int16_t *buffer, void *context)
{
	(void)context;
	if (period >= 40 && period < 72)
		return;	/* silent gap */
	add_tone(buffer, period, 220.0, 18000.0);
	add_tone(buffer, period, 1000.0, 12000.0);
}

static int test_gap_and_reentry(void)
{
	int periods = run_scenario(gen_gap_reentry, NULL, 140, debug_enabled);

	if (!has_event(40, 32, MUSIC_EVENT_BREAKDOWN)) {
		fprintf(stderr, "quiet gap produced no BREAKDOWN\n");
		return 1;
	}
	if (!has_event(72, 30, MUSIC_EVENT_REENTRY)) {
		fprintf(stderr, "gap return produced no REENTRY\n");
		return 1;
	}
	if (max_u8(72, 20, get_novelty) == 0) {
		fprintf(stderr, "re-entry did not raise novelty\n");
		return 1;
	}
	(void)periods;
	return 0;
}

static void gen_steady_dense(int period, int16_t *buffer, void *context)
{
	(void)context;
	add_dense(buffer, period, 22000.0);
}

static void gen_section(int period, int16_t *buffer, void *context);

static int test_section_change(void)
{
	struct balance_context section = { 120 };
	int periods;
	unsigned int peak_novelty;

	/* Two distinct sustained spectra; the switch is a section change. */
	periods = run_scenario(gen_section, &section, 260, debug_enabled);
	peak_novelty = max_u8(120, 80, get_novelty);
	if (peak_novelty < 80U) {
		fprintf(stderr, "section change did not raise novelty: %u\n",
			peak_novelty);
		return 1;
	}
	if (!has_event(120, 100, MUSIC_EVENT_SECTION)) {
		fprintf(stderr, "section change produced no SECTION event\n");
		return 1;
	}
	(void)periods;
	return 0;
}

static void gen_section(int period, int16_t *buffer, void *context)
{
	struct balance_context *section = context;
	unsigned int band;

	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band) {
		int low_side = band < 6;
		int before = period < section->switch_period;

		if (low_side == before)
			add_tone(buffer, period, dense_centres[band], 2600.0);
		else
			add_tone(buffer, period, dense_centres[band], 500.0);
	}
}

static void gen_fill(int period, int16_t *buffer, void *context)
{
	(void)context;
	if ((period % 8) == 0)
		add_dense(buffer, period, 30000.0);
}

static int test_fill_accent(void)
{
	int periods = run_scenario(gen_fill, NULL, 340, debug_enabled);

	if (!has_event(140, 200, MUSIC_EVENT_FILL)) {
		fprintf(stderr, "broadband accents produced no FILL event\n");
		return 1;
	}
	(void)periods;
	return 0;
}

static int test_steady_dense_no_false_positive(void)
{
	int periods = run_scenario(gen_steady_dense, NULL, 560, debug_enabled);
	unsigned int peak_novelty = max_u8(280, periods - 280, get_novelty);
	unsigned int peak_build = max_u8(280, periods - 280, get_build);
	int period;
	int structural = 0;

	for (period = 280; period < periods; ++period)
		if ((trace[period].events & MUSIC_EVENT_STRUCTURAL) != 0) {
			fprintf(stderr,
				"steady dense fired structural event at %d: 0x%03x\n",
				period, trace[period].events);
			++structural;
		}
	if (structural != 0)
		return 1;
	if (peak_build >= 96U) {
		fprintf(stderr, "steady dense build gate reached %u\n",
			peak_build);
		return 1;
	}
	if (peak_novelty >= 110U) {
		fprintf(stderr, "steady dense novelty gate reached %u\n",
			peak_novelty);
		return 1;
	}
	return 0;
}

static void gen_beats_then_flat(int period, int16_t *buffer, void *context)
{
	(void)context;
	if (period < 180) {
		if ((period % 12) == 0)
			add_click(buffer, period, 90.0, 24000.0, 0.05);
		return;
	}
	add_dense(buffer, period, 20000.0);
}

static int test_beatless_confidence_decay(void)
{
	int periods = run_scenario(gen_beats_then_flat, NULL, 400,
				   debug_enabled);
	unsigned int confident = 0, decayed = 0;
	int period;
	uint16_t tail_bpm = 1;

	for (period = 140; period < 180; ++period)
		if (trace[period].beat_confidence > confident)
			confident = trace[period].beat_confidence;
	for (period = periods - 20; period < periods; ++period) {
		if (trace[period].beat_confidence > decayed)
			decayed = trace[period].beat_confidence;
		tail_bpm = trace[period].bpm_x100;
	}
	if (confident < 150U) {
		fprintf(stderr, "click train did not build confidence: %u\n",
			confident);
		return 1;
	}
	if (decayed >= 32U) {
		fprintf(stderr, "confidence did not decay: %u\n", decayed);
		return 1;
	}
	if (tail_bpm != 0) {
		fprintf(stderr, "stale bpm retained after decay: %u\n",
			tail_bpm);
		return 1;
	}
	return 0;
}

static int test_determinism(void)
{
	struct click_context click = { 12, 90.0, 24000.0 };
	struct music_features first[TEST_MAX_PERIODS];
	int periods;
	int copy;

	periods = run_scenario(gen_tempo_clicks, &click, 200, 0);
	memcpy(first, trace, sizeof(struct music_features) * (size_t)periods);
	run_scenario(gen_tempo_clicks, &click, 200, 0);
	for (copy = 0; copy < periods; ++copy)
		if (memcmp(&first[copy], &trace[copy],
			   sizeof(struct music_features)) != 0) {
			fprintf(stderr, "non-deterministic frame at %d\n", copy);
			return 1;
		}
	return 0;
}

static int test_packet_contract(void)
{
	struct music_features feature;
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	struct music_feature_transport transport;
	char buffer[MUSIC_FEATURES_FRAME_MAX];
	unsigned int version;
	int length;
	int v1_length;

	memset(&feature, 0, sizeof(feature));
	memset(levels, 0, sizeof(levels));
	feature.energy = 200;
	feature.beat_phase = 40000;
	feature.bpm_x100 = 11719;
	feature.events = MUSIC_EVENT_KICK | MUSIC_EVENT_BUILD;
	music_feature_transport_init(&transport, 0, TEST_FRAMES, 48000U);
	if (transport.session == 0) {
		fprintf(stderr, "transport session must be nonzero\n");
		return 1;
	}

	for (version = 1; version <= MUSIC_FEATURES_VERSION; ++version) {
		length = music_features_format_frame(buffer, sizeof(buffer),
						     &feature, levels, 70,
						     version, transport.session,
						     7, 1234);
		if (length <= 0 || (size_t)length >= MUSIC_FEATURES_FRAME_MAX) {
			fprintf(stderr, "frame v%u length invalid: %d\n",
				version, length);
			return 1;
		}
		if (version == MUSIC_FEATURES_VERSION) {
			if (strstr(buffer, "\"feature_version\":2") == NULL ||
			    strstr(buffer, "\"owner\":\"music\"") == NULL ||
			    strstr(buffer, "\"levels\":\"") == NULL ||
			    strstr(buffer, "\"beat_phase\":40000") == NULL ||
			    strstr(buffer, "\"bpm_x100\":11719") == NULL ||
			    strstr(buffer, "\"events\":17") == NULL) {
				fprintf(stderr,
					"v2 frame missing a contract field:\n%s",
					buffer);
				return 1;
			}
		}
	}
	/* Legacy compatibility mode must remain a valid frame. */
	v1_length = music_features_format_frame(buffer, sizeof(buffer), NULL,
						levels, 70, 1, 1, 1, 1);
	if (v1_length <= 0 || strstr(buffer, "feature_version") != NULL) {
		fprintf(stderr, "v1 compatibility frame regressed\n");
		return 1;
	}
	return 0;
}

static int test_bounds_and_memory(void)
{
	int periods = run_scenario(gen_tempo_clicks,
				   &(struct click_context){ 12, 90.0, 24000.0 },
				   200, 0);
	int period;

	for (period = 0; period < periods; ++period) {
		const struct music_features *f = &trace[period];

		if (f->events > 0x1ffU) {
			fprintf(stderr, "events out of range at %d: 0x%x\n",
				period, f->events);
			return 1;
		}
		if (f->bpm_x100 > 30000U) {
			fprintf(stderr, "bpm out of range at %d: %u\n",
				period, f->bpm_x100);
			return 1;
		}
	}
	if (sizeof(struct music_features_state) > 512U) {
		fprintf(stderr, "feature state grew beyond bound: %zu\n",
			sizeof(struct music_features_state));
		return 1;
	}
	if (sizeof(struct audio_visualizer) > 2048U) {
		fprintf(stderr, "analyzer grew beyond bound: %zu\n",
			sizeof(struct audio_visualizer));
		return 1;
	}
	return 0;
}

static int test_host_runtime(void)
{
	struct audio_visualizer visualizer;
	int16_t buffer[TEST_FRAMES * 2U];
	struct music_features feature;
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	struct timespec start, end;
	int period;
	const int periods = 400;
	double elapsed_us;
	double per_period_us;

	audio_visualizer_init(&visualizer);
	add_dense(buffer, 0, 20000.0);
	clock_gettime(CLOCK_MONOTONIC, &start);
	for (period = 0; period < periods; ++period)
		audio_visualizer_process_features(&visualizer, buffer,
						  TEST_FRAMES, 2, levels,
						  &feature);
	clock_gettime(CLOCK_MONOTONIC, &end);
	elapsed_us = (double)(end.tv_sec - start.tv_sec) * 1.0e6 +
		(double)(end.tv_nsec - start.tv_nsec) / 1.0e3;
	per_period_us = elapsed_us / (double)periods;
	printf("music_features: host runtime %.2f us/period (%d periods)\n",
	       per_period_us, periods);
	if (per_period_us > 5000.0) {
		fprintf(stderr, "host analysis budget exceeded: %.2f us\n",
			per_period_us);
		return 1;
	}
	return 0;
}

int main(void)
{
	debug_enabled = getenv("MF_DEBUG") != NULL;
	printf("music_features: state=%zu analyzer=%zu frame_max=%u\n",
	       sizeof(struct music_features_state),
	       sizeof(struct audio_visualizer),
	       (unsigned int)MUSIC_FEATURES_FRAME_MAX);
	if (test_stable_tempo() || test_differentiated_onsets() ||
	    test_balance_axes() || test_build_and_drop() ||
	    test_gap_and_reentry() || test_section_change() ||
	    test_fill_accent() ||
	    test_steady_dense_no_false_positive() ||
	    test_beatless_confidence_decay() || test_determinism() ||
	    test_packet_contract() || test_bounds_and_memory() ||
	    test_host_runtime())
		return 1;
	puts("music features: ok");
	return 0;
}
