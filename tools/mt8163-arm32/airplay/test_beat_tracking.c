/*
 * Beat-tracking host tests for the version-2 music feature producer.
 *
 * Synthetic kick/click tracks with sample-accurate beat times (not aligned to
 * the 2048-frame analysis period) run through the production analyser,
 * audio_visualizer_process_features().  For each track the published tempo
 * must sit within +/-1.5 BPM of the truth, and beat_phase must advance on
 * (nearly) every update and sit near zero when a beat lands, so beat-driven
 * LED motion follows the music instead of standing still.
 *
 * Tempos that are not a whole number of 42.7 ms periods (120 BPM is 11.72
 * periods) are the regression: a whole-period lag reported 117.2 BPM.  The
 * busy track adds off-grid hits so beat confidence sits at 32..63, where the
 * phase used to stand still; there the median tempo and the phase motion are
 * checked.
 *
 * BEAT_DEBUG=1 prints per-update tempo, confidence and phase.
 */
#define _DEFAULT_SOURCE

#include "audio_visualizer.h"
#include "music_features.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BT_FRAMES 2048U
#define BT_RATE 48000.0
#define BT_PI 3.14159265358979323846
#define BT_PERIODS 900		/* ~38 s */
#define BT_SETTLE 300		/* ~12.8 s to settle before measuring */
#define BT_TOLERANCE_X100 150	/* +/-1.5 BPM */

struct track {
	const char *name;
	double bpm;
	double swing;		/* 0: straight 8th hats; >0: off-beat delay, beats */
	double noise;		/* white noise amplitude */
	int hats;		/* 1: 8th-note hats */
	int snare;		/* 1: snare on beats 2 and 4 */
	unsigned int busy;	/* off-grid tom hits per 256 sixteenths */
};

static int compare_u16(const void *a, const void *b)
{
	return (int)*(const uint16_t *)a - (int)*(const uint16_t *)b;
}

static uint32_t lcg_state;

static double noise_sample(void)
{
	lcg_state = lcg_state * 1664525U + 1013904223U;
	return ((double)(lcg_state >> 8) / 8388608.0) - 1.0;
}

/* Deterministic hash of a sixteenth-note slot, for off-grid hits. */
static uint32_t slot_hash(long slot)
{
	uint32_t x = (uint32_t)slot * 2654435761U + 0x9e3779b9U;

	x ^= x >> 15;
	x *= 2246822519U;
	x ^= x >> 13;
	return x;
}

/* Kick: pitch-dropping sine with a 60 ms decay, starting at time zero. */
static double kick(double t)
{
	double f = 50.0 + 70.0 * exp(-t / 0.03);

	if (t < 0.0 || t > 0.35)
		return 0.0;
	return sin(2.0 * BT_PI * f * t) * exp(-t / 0.06);
}

/* Short tonal click for snares and hats, so the test needs no noise state. */
static double tick(double t, double freq, double decay)
{
	if (t < 0.0 || t > decay * 8.0)
		return 0.0;
	return sin(2.0 * BT_PI * freq * t) * exp(-t / decay);
}

static void render(const struct track *tr, int period, int16_t *buffer)
{
	double beat = 60.0 / tr->bpm;
	unsigned int frame;

	for (frame = 0; frame < BT_FRAMES; ++frame) {
		double t = ((double)period * BT_FRAMES + frame) / BT_RATE;
		long n = (long)floor(t / beat);
		double value = 0.0;
		long k;

		for (k = n - 1; k <= n; ++k) {
			double start = (double)k * beat;

			if (k < 0)
				continue;
			value += 18000.0 * kick(t - start);
			if (tr->snare && (k % 2) == 1)
				value += 7000.0 * tick(t - start, 1800.0, 0.02);
			if (tr->hats) {
				double off = start + beat * (0.5 + tr->swing);

				value += 2500.0 * tick(t - start, 7000.0, 0.008);
				value += 2500.0 * tick(t - off, 7000.0, 0.008);
			}
		}
		if (tr->busy) {
			double sixteenth = beat / 4.0;
			long slot = (long)floor(t / sixteenth);

			for (k = slot - 3; k <= slot; ++k) {
				uint32_t h = slot_hash(k);
				double at;

				if (k < 0 || (h & 0xffU) >= tr->busy)
					continue;
				/* Anywhere inside the slot: not on the grid. */
				at = ((double)k + (double)((h >> 8) & 0xffU) /
				      256.0) * sixteenth;
				value += 16000.0 * tick(t - at,
					140.0 + (double)((h >> 16) & 0x7fU),
					0.05);
			}
		}
		if (tr->noise > 0.0)
			value += tr->noise * noise_sample();
		if (value > 32767.0)
			value = 32767.0;
		if (value < -32768.0)
			value = -32768.0;
		buffer[frame * 2U] = (int16_t)lround(value);
		buffer[frame * 2U + 1U] = (int16_t)lround(value);
	}
}

static int run_track(const struct track *tr)
{
	static struct music_features trace[BT_PERIODS];
	struct audio_visualizer visualizer;
	int16_t buffer[BT_FRAMES * 2U];
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	double beat_periods = 60.0 / tr->bpm * BT_RATE / BT_FRAMES;
	int debug = getenv("BEAT_DEBUG") != NULL;
	static uint16_t published[BT_PERIODS];
	int period, advanced = 0, measured = 0, with_bpm = 0;
	int beats = 0, aligned = 0, worst_bpm = 0;
	int mid = 0, mid_advanced = 0, median_error;
	long bpm_sum = 0;
	double mean_bpm, median_bpm;
	int failed = 0;

	lcg_state = 0x1234567U;
	audio_visualizer_init(&visualizer);
	for (period = 0; period < BT_PERIODS; ++period) {
		render(tr, period, buffer);
		audio_visualizer_process_features(&visualizer, buffer,
						  BT_FRAMES, 2, levels,
						  &trace[period]);
		if (debug)
			printf("%s,%d,%u,%u,%u\n", tr->name, period,
			       trace[period].bpm_x100,
			       trace[period].beat_confidence,
			       trace[period].beat_phase);
	}

	for (period = BT_SETTLE; period < BT_PERIODS; ++period) {
		const struct music_features *f = &trace[period];
		double start = (double)period;
		double next_beat;
		int error;

		++measured;
		if (f->beat_phase != trace[period - 1].beat_phase)
			++advanced;
		/* Real-music confidence often sits here; the phase must run. */
		if (f->beat_confidence >= 32U && f->beat_confidence < 64U) {
			++mid;
			if (f->beat_phase != trace[period - 1].beat_phase)
				++mid_advanced;
		}
		if (f->bpm_x100 != 0) {
			published[with_bpm] = f->bpm_x100;
			++with_bpm;
			bpm_sum += f->bpm_x100;
			error = abs((int)f->bpm_x100 -
				    (int)lround(tr->bpm * 100.0));
			if (error > worst_bpm)
				worst_bpm = error;
		}
		/*
		 * Phase at the update in which a beat lands.  The onset is seen
		 * in the period that contains the kick, so compare there.
		 */
		next_beat = ceil(start / beat_periods) * beat_periods;
		if (next_beat < start + 1.0) {
			int phase = f->beat_phase;
			int distance = phase > 32768 ? 65536 - phase : phase;

			++beats;
			if (distance <= 65536 / 5)
				++aligned;
		}
	}
	mean_bpm = with_bpm ? (double)bpm_sum / with_bpm / 100.0 : 0.0;
	qsort(published, (size_t)with_bpm, sizeof(published[0]), compare_u16);
	median_bpm = with_bpm ? published[with_bpm / 2] / 100.0 : 0.0;
	median_error = abs((int)lround(median_bpm * 100.0) -
			   (int)lround(tr->bpm * 100.0));
	printf("beat: %-10s truth %6.2f BPM  median %6.2f  mean %6.2f  "
	       "worst %+6.2f  bpm %3d%%  phase-advancing %3d%% "
	       "(conf 32..63: %d/%d)  on-beat %3d%% (%d beats)\n",
	       tr->name, tr->bpm, median_bpm, mean_bpm, worst_bpm / 100.0,
	       100 * with_bpm / measured, 100 * advanced / measured,
	       mid_advanced, mid, beats ? 100 * aligned / beats : 0, beats);

	if (tr->busy) {
		/*
		 * Off-grid hits keep confidence low and occasionally pull the
		 * tempo to another metrical level, so hold the median and the
		 * phase motion, not every update.
		 */
		if (median_error > BT_TOLERANCE_X100) {
			fprintf(stderr, "%s: median tempo off by %.2f BPM\n",
				tr->name, median_error / 100.0);
			failed = 1;
		}
		if (mid < 50 || mid_advanced * 20 < mid * 19) {
			fprintf(stderr, "%s: phase advanced on %d of %d "
				"low-confidence updates\n", tr->name,
				mid_advanced, mid);
			failed = 1;
		}
		return failed;
	}
	if (with_bpm * 10 < measured * 9) {
		fprintf(stderr, "%s: tempo published on only %d of %d updates\n",
			tr->name, with_bpm, measured);
		failed = 1;
	}
	if (worst_bpm > BT_TOLERANCE_X100) {
		fprintf(stderr, "%s: tempo off by %.2f BPM (limit 1.5)\n",
			tr->name, worst_bpm / 100.0);
		failed = 1;
	}
	if (advanced * 20 < measured * 19) {
		fprintf(stderr, "%s: phase advanced on only %d of %d updates\n",
			tr->name, advanced, measured);
		failed = 1;
	}
	if (beats == 0 || aligned * 5 < beats * 4) {
		fprintf(stderr, "%s: phase near zero on only %d of %d beats\n",
			tr->name, aligned, beats);
		failed = 1;
	}
	return failed;
}

int main(void)
{
	static const struct track tracks[] = {
		{ "kick-100", 100.0, 0.0, 0.0, 0, 0, 0 },
		{ "kick-120", 120.0, 0.0, 0.0, 0, 0, 0 },
		{ "kick-128", 128.0, 0.0, 0.0, 0, 0, 0 },
		{ "kit-120", 120.0, 0.0, 0.0, 1, 1, 0 },
		{ "swing-112", 112.0, 1.0 / 6.0, 0.0, 1, 1, 0 },
		{ "noise-124", 124.0, 0.0, 3000.0, 1, 0, 0 },
		{ "busy-118", 118.0, 0.0, 0.0, 1, 0, 110 },
	};
	unsigned int i;
	int failed = 0;

	for (i = 0; i < sizeof(tracks) / sizeof(tracks[0]); ++i)
		failed |= run_track(&tracks[i]);
	if (failed)
		return 1;
	puts("beat tracking: ok");
	return 0;
}
