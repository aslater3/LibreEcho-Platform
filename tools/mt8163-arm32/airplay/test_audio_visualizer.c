#define _DEFAULT_SOURCE

#include "audio_visualizer.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define TEST_FRAMES 2048U
#define TEST_PI 3.14159265358979323846

static const unsigned int centres[AUDIO_VISUALIZER_BANDS] = {
	63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6500, 11000
};

static void make_tone(int16_t *samples, unsigned int frequency,
		      unsigned int period, int amplitude)
{
	size_t frame;
	size_t offset = (size_t)period * TEST_FRAMES;

	for (frame = 0; frame < TEST_FRAMES; ++frame) {
		double phase = 2.0 * TEST_PI * frequency *
			(offset + frame) / AUDIO_VISUALIZER_RATE;

		samples[frame] = (int16_t)lround(sin(phase) * amplitude);
	}
}

static void make_dense_mix(int16_t *samples, unsigned int period,
			   unsigned int emphasis)
{
	size_t frame;
	size_t offset = (size_t)period * TEST_FRAMES;

	for (frame = 0; frame < TEST_FRAMES; ++frame) {
		double value = 0.0;
		unsigned int band;

		for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band) {
			double amplitude = band == emphasis ? 2400.0 : 1200.0;
			double phase = 2.0 * TEST_PI * centres[band] *
				(offset + frame) / AUDIO_VISUALIZER_RATE;

			value += sin(phase + (double)band * 0.37) * amplitude;
		}
		if (value > 30000.0)
			value = 30000.0;
		if (value < -30000.0)
			value = -30000.0;
		samples[frame] = (int16_t)lround(value);
	}
}

static unsigned int strongest_band(const uint8_t *levels)
{
	unsigned int strongest = 0;
	unsigned int band;

	for (band = 1; band < AUDIO_VISUALIZER_BANDS; ++band)
		if (levels[band] > levels[strongest])
			strongest = band;
	return strongest;
}

static int test_silence(void)
{
	struct audio_visualizer visualizer;
	int16_t samples[TEST_FRAMES] = { 0 };
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	unsigned int period;
	unsigned int band;

	audio_visualizer_init(&visualizer);
	memset(levels, 0xff, sizeof(levels));
	for (period = 0; period < 8; ++period)
		audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 1,
					 levels);
	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
		if (levels[band] != 0) {
			fprintf(stderr, "silence leaked into band %u: %u\n",
				band, levels[band]);
			return 1;
		}
	return 0;
}

static int test_band_centres(void)
{
	struct audio_visualizer visualizer;
	int16_t samples[TEST_FRAMES];
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	unsigned int expected;
	unsigned int period;
	unsigned int strongest;

	for (expected = 0; expected < AUDIO_VISUALIZER_BANDS; ++expected) {
		audio_visualizer_init(&visualizer);
		memset(levels, 0, sizeof(levels));
		for (period = 0; period < 8; ++period) {
			make_tone(samples, centres[expected], period, 12000);
			audio_visualizer_process(&visualizer, samples,
						 TEST_FRAMES, 1, levels);
		}
		strongest = strongest_band(levels);
		if ((strongest + 1U < expected || strongest > expected + 1U) ||
		    levels[expected] < 140) {
			unsigned int band;

			fprintf(stderr,
				"%u Hz selected band %u level %u, expected %u\n",
				centres[expected], strongest, levels[strongest],
				expected);
			for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
				fprintf(stderr, " %u", levels[band]);
			fputc('\n', stderr);
			return 1;
		}
	}
	return 0;
}

static int test_attack_and_decay(void)
{
	struct audio_visualizer visualizer;
	int16_t samples[TEST_FRAMES];
	uint8_t levels[AUDIO_VISUALIZER_BANDS] = { 0 };
	uint8_t peak;
	unsigned int period;

	audio_visualizer_init(&visualizer);
	make_tone(samples, 1000, 0, 8000);
	audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 1, levels);
	peak = levels[6];
	if (peak < 100) {
		fprintf(stderr, "attack too slow: %u\n", peak);
		return 1;
	}
	memset(samples, 0, sizeof(samples));
	audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 1, levels);
	if (levels[6] == 0 || levels[6] >= peak) {
		fprintf(stderr, "decay is not gradual: %u -> %u\n",
			peak, levels[6]);
		return 1;
	}
	if (levels[6] > (unsigned int)peak * 17U / 20U) {
		fprintf(stderr, "decay is too slow for a reactive display: %u -> %u\n",
			peak, levels[6]);
		return 1;
	}
	for (period = 0; period < 96; ++period)
		audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 1,
					 levels);
	if (levels[6] != 0) {
		fprintf(stderr, "decay did not reach zero: %u\n", levels[6]);
		return 1;
	}
	return 0;
}

static int test_dense_mix_keeps_motion(void)
{
	struct audio_visualizer visualizer;
	int16_t samples[TEST_FRAMES];
	uint8_t levels[AUDIO_VISUALIZER_BANDS] = { 0 };
	uint8_t previous[AUDIO_VISUALIZER_BANDS];
	unsigned int period;
	unsigned int band;
	unsigned int minimum = 255;
	unsigned int maximum = 0;
	unsigned int delta = 0;

	audio_visualizer_init(&visualizer);
	for (period = 0; period < 10; ++period) {
		make_dense_mix(samples, period, 4);
		audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 1,
					 levels);
	}
	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band) {
		if (levels[band] < minimum)
			minimum = levels[band];
		if (levels[band] > maximum)
			maximum = levels[band];
		previous[band] = levels[band];
	}
	if (maximum < minimum + 32U) {
		fprintf(stderr, "dense mix collapsed to a flat display:");
		for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
			fprintf(stderr, " %u", levels[band]);
		fputc('\n', stderr);
		return 1;
	}

	for (period = 10; period < 14; ++period) {
		make_dense_mix(samples, period, 8);
		audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 1,
					 levels);
	}
	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
		delta += levels[band] > previous[band]
			? levels[band] - previous[band]
			: previous[band] - levels[band];
	if (delta < 96U) {
		fprintf(stderr, "dense mix changed too little: delta=%u\n",
			delta);
		return 1;
	}
	return 0;
}

static int levels_equal(const uint8_t *a, const uint8_t *b)
{
	unsigned int band;

	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
		if (a[band] != b[band])
			return 0;
	return 1;
}

/* Fill the engine's interleaved stereo layout (stride = OUTPUT_CHANNELS) with
 * the same sample in both channels. */
static void fill_constant(int16_t *samples, int value)
{
	unsigned int frame;

	for (frame = 0; frame < TEST_FRAMES; ++frame) {
		samples[frame * 2] = (int16_t)value;
		samples[frame * 2 + 1] = (int16_t)value;
	}
}

static int process_constant(const uint8_t *expected, int value,
			    const char *label)
{
	struct audio_visualizer visualizer;
	int16_t samples[TEST_FRAMES * 2];
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	unsigned int period;
	unsigned int band;

	audio_visualizer_init(&visualizer);
	memset(levels, 0, sizeof(levels));
	fill_constant(samples, value);
	for (period = 0; period < 8; ++period)
		audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 2,
					 levels);
	if (!levels_equal(levels, expected)) {
		fprintf(stderr, "%s levels changed:", label);
		for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
			fprintf(stderr, " %u", levels[band]);
		fputc('\n', stderr);
		return 1;
	}
	return 0;
}

/*
 * REGRESSION: the hot path scaled each sample with
 * ``(int32_t)sample << FILTER_INPUT_SHIFT``.  Left-shifting a negative signed
 * value is undefined in C, so any negative media sample made the shared engine
 * exhibit UB on the media-only visualizer path (the legacy scenario hid it by
 * keeping a higher-priority bus live).  The scaling is now a bounded multiply;
 * this test drives bipolar and full-scale extreme vectors in the engine's real
 * interleaved layout under UBSan (halt_on_error=1), so reintroducing the
 * negative shift aborts here.  The expected levels were captured from the
 * intended (numerically identical) pre-fix behaviour, so the fix cannot
 * silently change amplitudes.
 */
static int test_bipolar_extreme(void)
{
	static const uint8_t pos12000[AUDIO_VISUALIZER_BANDS] =
		{ 59, 19, 19, 14, 12, 11, 7, 5, 0, 0, 0, 0 };
	static const uint8_t neg12000[AUDIO_VISUALIZER_BANDS] =
		{ 59, 19, 19, 14, 12, 11, 7, 5, 0, 0, 0, 0 };
	static const uint8_t extreme[AUDIO_VISUALIZER_BANDS] =
		{ 65, 25, 25, 21, 19, 14, 14, 11, 11, 5, 4, 0 };
	struct audio_visualizer visualizer;
	int16_t samples[TEST_FRAMES * 2];
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	unsigned int frame;
	unsigned int band;

	/* A negative constant must scale to exactly the same magnitude as its
	 * positive twin: the front end is linear and the input scaling is a
	 * defined multiply, never a signed left shift. */
	if (process_constant(pos12000, 12000, "positive tone") ||
	    process_constant(neg12000, -12000, "negative tone"))
		return 1;
	if (!levels_equal(pos12000, neg12000)) {
		fprintf(stderr, "bipolar amplitudes are not symmetric\n");
		return 1;
	}

	/* Both signed extremes and the full-scale magnitude edge. */
	if (process_constant(extreme, -32768, "INT16_MIN") ||
	    process_constant(extreme, 32767, "INT16_MAX") ||
	    process_constant(extreme, -32767, "INT16_MIN+1"))
		return 1;

	/* A dense vector alternating the signed extremes: every frame is a
	 * negative or positive full-scale edge, the worst case for the old
	 * negative shift. */
	audio_visualizer_init(&visualizer);
	memset(levels, 0, sizeof(levels));
	for (frame = 0; frame < TEST_FRAMES; ++frame) {
		int value = (frame & 1u) ? INT16_MIN : INT16_MAX;

		samples[frame * 2] = (int16_t)value;
		samples[frame * 2 + 1] = (int16_t)value;
	}
	for (frame = 0; frame < 8; ++frame)
		audio_visualizer_process(&visualizer, samples, TEST_FRAMES, 2,
					 levels);
	{
		unsigned int maximum = 0;

		for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
			if (levels[band] > maximum)
				maximum = levels[band];
		if (maximum == 0) {
			fprintf(stderr, "extreme vector produced no energy\n");
			return 1;
		}
	}
	return 0;
}

int main(void)
{
	if (test_silence() || test_band_centres() || test_attack_and_decay() ||
	    test_dense_mix_keeps_motion() || test_bipolar_extreme())
		return 1;
	puts("audio visualizer analyzer: ok");
	return 0;
}
