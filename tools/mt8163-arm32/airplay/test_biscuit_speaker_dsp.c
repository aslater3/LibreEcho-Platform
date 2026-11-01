/*
 * Unit tests for the Echo Dot (biscuit) speaker chain (biscuit_speaker_dsp.h).
 * Behavioural assertions only: EQ shape where it matters, the volume ladder,
 * unknown-volume bypass, ceiling, compression direction, and stability.
 */
#define _POSIX_C_SOURCE 200809L

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "biscuit_speaker_dsp.h"

#define TEST_TWO_PI 6.28318530717958647692
#define RATE 48000.0

static int failures;

static void check(int cond, const char *what, double got, double want, double tol)
{
	printf("  %s %-56s %+8.2f (want %+7.2f +-%.2f)\n", cond ? "ok  " : "FAIL",
	       what, got, want, tol);
	if (!cond)
		failures++;
}

static void check_near(const char *what, double got, double want, double tol)
{
	check(fabs(got - want) <= tol, what, got, want, tol);
}

/* EQ-only gain at f (no dynamics), dB relative to input. */
static double eq_gain_db(int volume, double f)
{
	static struct biscuit_dsp d;
	double acc = 0.0, w = TEST_TWO_PI * f / RATE, a = 100.0;
	long n, settle = (long)(RATE * 0.5), meas = (long)(RATE * 0.5);
	int i;

	biscuit_dsp_init(&d, volume);
	for (n = 0; n < settle + meas; ++n) {
		double x = a * sin(w * (double)n);
		if (d.eq_active)
			for (i = 0; i < BISCUIT_EQ_SECTIONS; ++i)
				x = biscuit_step(&d.eq[i], x);
		x *= d.eq_gain;
		if (n >= settle)
			acc += x * x;
	}
	return 20.0 * log10(sqrt(acc / (double)meas) / (a / sqrt(2.0)));
}

/* Full chain steady-state output level of a sine, dBFS (RMS of a full-scale sine = 0). */
static double chain_level_makeup_dbfs(int volume, double f, double in_dbfs,
				      double makeup_db, double *peak);

static double chain_level_dbfs(int volume, double f, double in_dbfs, double *peak)
{
	return chain_level_makeup_dbfs(volume, f, in_dbfs, 0.0, peak);
}

static double chain_level_makeup_dbfs(int volume, double f, double in_dbfs,
				      double makeup_db, double *peak)
{
	static struct biscuit_dsp d;
	double acc = 0.0, w = TEST_TWO_PI * f / RATE;
	double a = 32768.0 * pow(10.0, in_dbfs / 20.0);
	long n, settle = (long)(RATE * 4.0), meas = (long)(RATE * 1.0);

	biscuit_dsp_init(&d, volume);
	biscuit_dsp_configure(&d, (float)makeup_db);
	*peak = 0.0;
	for (n = 0; n < settle + meas; ++n) {
		float y = biscuit_dsp_process(&d, (float)(a * sin(w * (double)n)));
		if (!isfinite(y)) {
			*peak = 1e9;
			return 1e9;
		}
		if (n >= settle) {
			acc += (double)y * y;
			if (fabs(y) > *peak)
				*peak = fabs(y);
		}
	}
	return 20.0 * log10(sqrt(2.0 * acc / (double)meas) / 32768.0);
}

/* EQ-plus-tone gain at f (no dynamics), dB relative to input.  Same order as
 * biscuit_dsp_process: loudness EQ, owner shelves, then the volume gain. */
static double tone_eq_gain_db(int volume, int tone, double bass_db, double treble_db, double f)
{
	static struct biscuit_dsp d;
	double acc = 0.0, w = TEST_TWO_PI * f / RATE, a = 100.0;
	long n, settle = (long)(RATE * 0.5), meas = (long)(RATE * 0.5);
	int i;

	biscuit_dsp_init(&d, volume);
	if (tone)
		biscuit_dsp_set_tone(&d, (float)bass_db, (float)treble_db);
	for (n = 0; n < settle + meas; ++n) {
		double x = a * sin(w * (double)n);
		if (d.eq_active)
			for (i = 0; i < BISCUIT_EQ_SECTIONS; ++i)
				x = biscuit_step(&d.eq[i], x);
		if (d.tone_on)
			for (i = 0; i < 2; ++i)
				x = biscuit_step(&d.tone[i], x);
		x *= d.eq_gain;
		if (n >= settle)
			acc += x * x;
	}
	return 20.0 * log10(sqrt(acc / (double)meas) / (a / sqrt(2.0)));
}

static void test_owner_tone(void)
{
	static struct biscuit_dsp stock, flat, moved, ref;
	double ref60, ref9k, peak_flat = 0.0, peak_boost = 0.0;
	long n, differs = 0;
	int i;
	double w60 = TEST_TWO_PI * 80.0 / RATE;

	printf("biscuit_speaker_dsp: owner bass and treble shelves\n");
	biscuit_dsp_init(&stock, 50);
	biscuit_dsp_init(&flat, 50);
	biscuit_dsp_set_tone(&flat, 0.0f, 0.0f);
	for (n = 0; n < (long)RATE / 2; ++n) {
		float x = (float)(9000.0 * sin(w60 * (double)n) + 3000.0 * sin(TEST_TWO_PI * 7000.0 * (double)n / RATE));
		if (biscuit_dsp_process(&stock, x) != biscuit_dsp_process(&flat, x))
			differs++;
	}
	check(differs == 0, "flat owner tone equals the stock Dot chain sample for sample",
	      (double)differs, 0.0, 0.0);

	ref60 = tone_eq_gain_db(50, 0, 0.0, 0.0, 60.0);
	check_near("bass +6 lifts 60 Hz by about 6 dB",
		   tone_eq_gain_db(50, 1, 6.0, 0.0, 60.0) - ref60, 6.0, 1.0);
	check_near("bass +6 leaves 9 kHz alone",
		   tone_eq_gain_db(50, 1, 6.0, 0.0, 9000.0) - tone_eq_gain_db(50, 0, 0.0, 0.0, 9000.0),
		   0.0, 0.5);
	ref9k = tone_eq_gain_db(50, 0, 0.0, 0.0, 9000.0);
	check_near("treble +6 lifts 9 kHz by about 6 dB",
		   tone_eq_gain_db(50, 1, 0.0, 6.0, 9000.0) - ref9k, 6.0, 1.0);
	check_near("treble -6 cuts 9 kHz by about 6 dB",
		   tone_eq_gain_db(50, 1, 0.0, -6.0, 9000.0) - ref9k, -6.0, 1.0);

	/* Max boost at full volume: the full-band limiter still holds its ceiling. */
	biscuit_dsp_init(&moved, 100);
	biscuit_dsp_set_tone(&moved, 6.0f, 6.0f);
	for (n = 0; n < (long)RATE * 2; ++n) {
		double t = (double)n / RATE;
		float x = (float)(30000.0 * sin(TEST_TWO_PI * 80.0 * t) +
				  8000.0 * sin(TEST_TWO_PI * 9000.0 * t));
		float y = biscuit_dsp_process(&moved, x);
		if (n < (long)RATE)
			continue;
		if (fabs(y) > peak_boost)
			peak_boost = fabs(y);
	}
	biscuit_dsp_init(&ref, 100);
	for (n = 0; n < (long)RATE * 2; ++n) {
		double t = (double)n / RATE;
		float x = (float)(30000.0 * sin(TEST_TWO_PI * 80.0 * t) +
				  8000.0 * sin(TEST_TWO_PI * 9000.0 * t));
		float y = biscuit_dsp_process(&ref, x);
		if (n < (long)RATE)
			continue;
		if (fabs(y) > peak_flat)
			peak_flat = fabs(y);
	}
	check(peak_boost <= 32768.0 * pow(10.0, -0.1 / 20.0) + 1.0,
	      "max boost at full volume is held at the -0.1 dBFS ceiling",
	      peak_boost, 32396.0, 1.0);
	check(fabs(peak_boost - peak_flat) < 0.01 * 32768.0,
	      "max boost peak equals the stock protected peak", peak_boost - peak_flat, 0.0,
	      0.01 * 32768.0);

	/* Unknown<->known volume transitions re-init the chain; the tone survives. */
	biscuit_dsp_init(&moved, 50);
	biscuit_dsp_set_tone(&moved, -3.0f, 2.0f);
	biscuit_dsp_set_volume(&moved, -1);
	biscuit_dsp_set_volume(&moved, 60);
	check(moved.tone_on == 1, "owner tone survives unknown-volume re-init",
	      (double)moved.tone_on, 1.0, 0.0);
	for (i = 0; i < 2; ++i) {
		biscuit_dsp_init(&ref, 60);
		biscuit_dsp_set_tone(&ref, -3.0f, 2.0f);
		check(moved.tone[i].b0 == ref.tone[i].b0 && moved.tone[i].a1 == ref.tone[i].a1,
		      i ? "treble shelf coefficients kept across re-init" :
			  "bass shelf coefficients kept across re-init",
		      moved.tone[i].b0 - ref.tone[i].b0, 0.0, 0.0);
	}
}

int main(void)
{
	double peak, lo, hi;
	static struct biscuit_dsp d;
	long n;

	printf("biscuit_speaker_dsp: loudness EQ shape (relative to 1 kHz)\n");
	{
		double ref = eq_gain_db(50, 1000.0);
		check_near("150 Hz upper-bass emphasis", eq_gain_db(50, 150.0) - ref, 26.0, 2.0);
		check(eq_gain_db(50, 65.0) - ref < eq_gain_db(50, 150.0) - ref - 15.0,
		      "65 Hz notch sits >15 dB under the 150 Hz emphasis",
		      eq_gain_db(50, 65.0) - ref, eq_gain_db(50, 150.0) - ref - 15.0, 0.0);
		check_near("2.7 kHz presence dip", eq_gain_db(50, 2700.0) - ref, -13.0, 2.0);
		check_near("6.4 kHz dip", eq_gain_db(50, 6400.0) - ref, -11.5, 2.0);
	}

	printf("biscuit_speaker_dsp: volume ladder is broadband and monotonic\n");
	check_near("volume 50 == volume 70", eq_gain_db(70, 1000.0) - eq_gain_db(50, 1000.0), 0.0, 0.01);
	check_near("volume 80 step", eq_gain_db(80, 1000.0) - eq_gain_db(50, 1000.0), 3.52, 0.05);
	check_near("volume 90 step", eq_gain_db(90, 1000.0) - eq_gain_db(50, 1000.0), 7.54, 0.05);
	check_near("volume 100 step", eq_gain_db(100, 1000.0) - eq_gain_db(50, 1000.0), 14.54, 0.05);
	check_near("ladder does not change shape (150 Hz rel. 1 kHz @100)",
		   (eq_gain_db(100, 150.0) - eq_gain_db(100, 1000.0)) -
		   (eq_gain_db(50, 150.0) - eq_gain_db(50, 1000.0)), 0.0, 0.01);

	printf("biscuit_speaker_dsp: unknown volume bypasses the EQ\n");
	check_near("volume -1 at 150 Hz is flat", eq_gain_db(-1, 150.0), 0.0, 0.01);

	printf("biscuit_speaker_dsp: protection\n");
	lo = chain_level_dbfs(100, 150.0, -40.0, &peak);
	hi = chain_level_dbfs(100, 150.0, -10.0, &peak);
	check(hi - lo < 15.0, "150 Hz +30 dB input step is compressed at volume 100", hi - lo, 15.0, 0.0);
	check(peak <= 32768.0 * pow(10.0, -0.1 / 20.0) + 1.0,
	      "full-scale bass held at the -0.1 dBFS ceiling", peak, 32768.0 * pow(10.0, -0.1 / 20.0), 1.0);
	(void)chain_level_dbfs(100, 1000.0, 0.0, &peak);
	check(peak <= 32768.0 * pow(10.0, -0.1 / 20.0) + 1.0,
	      "full-scale 1 kHz held at the ceiling", peak, 32768.0 * pow(10.0, -0.1 / 20.0), 1.0);

	printf("biscuit_speaker_dsp: per-target bass makeup\n");
	check_near("init resets makeup to stock 0 dB",
		   (biscuit_dsp_init(&d, 50), (double)d.makeup[0]), 1.0, 0.0);
	check_near("+2.5 dB makeup lifts quiet 150 Hz by 2.5 dB",
		   chain_level_makeup_dbfs(50, 150.0, -50.0, 2.5, &peak) -
		   chain_level_dbfs(50, 150.0, -50.0, &peak), 2.5, 0.15);
	check_near("+2.5 dB makeup leaves quiet 2 kHz alone",
		   chain_level_makeup_dbfs(50, 2000.0, -40.0, 2.5, &peak) -
		   chain_level_dbfs(50, 2000.0, -40.0, &peak), 0.0, 0.05);
	(void)chain_level_makeup_dbfs(100, 150.0, 0.0, 6.0, &peak);
	check(peak <= 32768.0 * pow(10.0, -0.1 / 20.0) + 1.0,
	      "max makeup full-scale bass still held at the ceiling",
	      peak, 32768.0 * pow(10.0, -0.1 / 20.0), 1.0);
	biscuit_dsp_init(&d, 50);
	biscuit_dsp_configure(&d, 99.0f);
	check_near("makeup clamps to 6 dB", (double)d.bass_makeup_db, 6.0, 0.0);
	biscuit_dsp_configure(&d, NAN);
	check_near("NaN makeup selects stock", (double)d.bass_makeup_db, 0.0, 0.0);
	biscuit_dsp_configure(&d, 2.5f);
	biscuit_dsp_set_volume(&d, -1);
	biscuit_dsp_set_volume(&d, 50);
	check_near("makeup survives unknown-volume re-init",
		   20.0 * log10((double)d.makeup[1]), 2.5, 0.001);

	printf("biscuit_speaker_dsp: stability\n");
	biscuit_dsp_init(&d, 100);
	peak = 0.0;
	for (n = 0; n < (long)RATE * 2; ++n) {
		/* full-scale square wave with volume changes every 100 ms */
		float x = (n / 240) % 2 ? 32767.0f : -32768.0f;
		float y;
		if (n % 4800 == 0)
			biscuit_dsp_set_volume(&d, (int)((n / 4800) % 2 ? 100 : 50));
		y = biscuit_dsp_process(&d, x);
		if (!isfinite(y) || fabs(y) > peak)
			peak = isfinite(y) ? fabs(y) : 1e9;
	}
	check(peak <= 32768.0, "square wave + volume churn stays finite and in range", peak, 32768.0, 0.0);
	biscuit_dsp_init(&d, 50);
	check(biscuit_dsp_process(&d, NAN) == 0.0f, "NaN input is dropped", 0.0, 0.0, 0.0);

	test_owner_tone();

	if (failures) {
		printf("biscuit_speaker_dsp: %d FAILED\n", failures);
		return 1;
	}
	printf("biscuit_speaker_dsp: PASS\n");
	return 0;
}
