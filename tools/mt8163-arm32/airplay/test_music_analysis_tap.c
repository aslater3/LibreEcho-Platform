/*
 * Host test: the music analysis tap (#44) in the real engine translation unit.
 *
 * audio_engine.c is #included (its main renamed) so music_analysis_period()
 * and render_period() run unchanged; tinyalsa is stubbed by the shell wrapper.
 * Proves that
 *
 *   1. the analysed signal is the media/AirPlay programme at unity gain, so a
 *      sender-volume change moves the speaker output but not the features;
 *   2. a bus at zero gain, or AirPlay before its first volume, is left out;
 *   3. media and AirPlay are summed with saturation.
 */
#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define main engine_program_main
#include "audio_engine.c"
#undef main

#define TAP_PERIODS 400U
#define TAP_SAMPLES (PERIOD_SIZE * INPUT_CHANNELS)
#define GAIN_MINUS_20DB_Q15 3277

static int failures;

#define CHECK(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
		++failures; \
	} \
} while (0)

/* A 120 BPM click train: a decaying 90 Hz burst every twelve periods. */
static void click_period(int16_t *pcm, unsigned int period)
{
	unsigned int frame;

	for (frame = 0; frame < PERIOD_SIZE; ++frame) {
		double t = (double)frame / DEFAULT_RATE;
		double v = (period % 12U) == 0 ?
			sin(2.0 * 3.14159265358979323846 * 90.0 * t) *
			24000.0 * exp(-t / 0.05) : 0.0;

		pcm[frame * 2U] = (int16_t)lrint(v);
		pcm[frame * 2U + 1U] = (int16_t)lrint(v);
	}
}

static void ready_bus(struct source_bus *bus, int16_t *pcm, int32_t gain)
{
	bus->samples = pcm;
	bus->capacity = TAP_SAMPLES * sizeof(int16_t);
	bus->received = bus->capacity;
	bus->gain_q15 = gain;
}

/* Runs the engine tap and analyser over the click train at one sender gain. */
static void analyse_at(int32_t gain, struct music_features *features,
		       uint8_t *levels, int *output_peak)
{
	struct source_bus sources[SOURCE_COUNT];
	struct audio_visualizer analyzer;
	struct puffin_dynamics dynamics;
	struct speaker_dsp speaker;
	static int16_t media[TAP_SAMPLES], analysis[TAP_SAMPLES];
	static int16_t output[PERIOD_SIZE * OUTPUT_CHANNELS];
	int32_t master = 32768;
	unsigned int period, i;

	memset(sources, 0, sizeof(sources));
	memset(features, 0, sizeof(*features));
	audio_visualizer_init(&analyzer);
	puffin_dynamics_init(&dynamics);
	speaker_dsp_init(&speaker, 80);
	*output_peak = 0;
	for (period = 0; period < TAP_PERIODS; ++period) {
		click_period(media, period);
		ready_bus(&sources[SOURCE_MEDIA], media, gain);
		render_period(sources, NULL, 0, output, &dynamics, &speaker,
			      master, &master);
		for (i = 0; i < PERIOD_SIZE * OUTPUT_CHANNELS; ++i)
			if (abs(output[i]) > *output_peak)
				*output_peak = abs(output[i]);
		music_analysis_period(sources, analysis);
		CHECK(memcmp(analysis, media, sizeof(media)) == 0);
		audio_visualizer_process_features(&analyzer, analysis,
						  PERIOD_SIZE, INPUT_CHANNELS,
						  levels, features);
	}
}

int main(void)
{
	struct source_bus sources[SOURCE_COUNT];
	struct music_features loud, quiet;
	uint8_t loud_levels[AUDIO_VISUALIZER_BANDS];
	uint8_t quiet_levels[AUDIO_VISUALIZER_BANDS];
	static int16_t media[TAP_SAMPLES], airplay[TAP_SAMPLES];
	static int16_t analysis[TAP_SAMPLES];
	int loud_peak, quiet_peak;
	unsigned int i;

	/* 1. Sender volume reaches the speaker, not the analyser. */
	analyse_at(32768, &loud, loud_levels, &loud_peak);
	analyse_at(GAIN_MINUS_20DB_Q15, &quiet, quiet_levels, &quiet_peak);
	CHECK(loud_peak > 0 && quiet_peak > 0);
	CHECK(quiet_peak * 2 < loud_peak);
	CHECK(memcmp(&loud, &quiet, sizeof(loud)) == 0);
	CHECK(memcmp(loud_levels, quiet_levels, sizeof(loud_levels)) == 0);
	CHECK(loud.beat_confidence > 0);

	/* 2. Muted media and AirPlay without a volume yet are not analysed. */
	for (i = 0; i < TAP_SAMPLES; ++i) {
		media[i] = 1000;
		airplay[i] = 30000;
	}
	memset(sources, 0, sizeof(sources));
	ready_bus(&sources[SOURCE_MEDIA], media, 0);
	ready_bus(&sources[SOURCE_AIRPLAY], airplay, 32768);
	sources[SOURCE_AIRPLAY].airplay_volume_missing = 1;
	music_analysis_period(sources, analysis);
	for (i = 0; i < TAP_SAMPLES; ++i)
		CHECK(analysis[i] == 0);

	/* 3. Media and AirPlay are summed and saturate at full scale. */
	sources[SOURCE_MEDIA].gain_q15 = 16384;
	sources[SOURCE_AIRPLAY].airplay_volume_missing = 0;
	music_analysis_period(sources, analysis);
	for (i = 0; i < TAP_SAMPLES; ++i)
		CHECK(analysis[i] == 31000);
	for (i = 0; i < TAP_SAMPLES; ++i)
		media[i] = 5000;
	music_analysis_period(sources, analysis);
	for (i = 0; i < TAP_SAMPLES; ++i)
		CHECK(analysis[i] == 32767);

	if (failures)
		return 1;
	printf("music analysis tap: unity-gain programme, volume-independent "
	       "features (speaker peak %d vs %d): ok\n", loud_peak, quiet_peak);
	return 0;
}
