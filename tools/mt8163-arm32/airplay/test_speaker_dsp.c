/*
 * Unit tests for the Radar-Puffin speaker tuning stage (speaker_dsp.h).
 *
 * These assert behaviour, not snapshots: filter shapes respond where they
 * should, the loudness ladder runs in the physically correct direction, volume
 * clamping matches the stock boundary selection, and the stage is bit
 * transparent when inactive.
 */
#define _POSIX_C_SOURCE 200809L

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

#include "speaker_dsp.h"
#include "puffin_downmix.h"

/* M_PI is a POSIX extension, not exposed under strict -std=c99. */
#define TEST_TWO_PI 6.28318530717958647692

#define RATE 48000.0
#define AMPL  8000.0

static int failures;

static void check(int cond, const char *what, double got, double want, double tol)
{
	if (cond) {
		printf("  ok   %-52s %+8.2f (want %+7.2f +-%.2f)\n", what, got, want, tol);
		return;
	}
	printf("  FAIL %-52s %+8.2f (want %+7.2f +-%.2f)\n", what, got, want, tol);
	failures++;
}

/* steady-state gain in dB of a single biquad driven with a sine at f */
static double biquad_gain_db(const struct speaker_dsp_biquad *bq, double f)
{
	struct speaker_dsp_biquad b = *bq;
	double w = TEST_TWO_PI * f / RATE;
	double acc = 0.0;
	long n, settle = (long)(RATE * 0.25), measure = (long)(RATE * 0.25);

	for (n = 0; n < settle + measure; ++n) {
		double x = AMPL * sin(w * (double)n);
		double y = b.b0 * x + b.z1;

		b.z1 = b.b1 * x - b.a1 * y + b.z2;
		b.z2 = b.b2 * x - b.a2 * y;
		if (n >= settle)
			acc += y * y;
	}
	{
		double rms = sqrt(acc / (double)measure);
		return 20.0 * log10(rms / (AMPL / sqrt(2.0)));
	}
}

/* steady-state gain in dB of the whole chain */
static double chain_gain_db(int volume, double f)
{
	struct speaker_dsp dsp;
	double w = TEST_TWO_PI * f / RATE;
	double acc = 0.0;
	long n, settle = (long)(RATE * 0.25), measure = (long)(RATE * 0.25);

	speaker_dsp_init(&dsp, volume);
	for (n = 0; n < settle + measure; ++n) {
		int32_t x = (int32_t)lrint(AMPL * sin(w * (double)n));
		float y = speaker_dsp_equalize(&dsp, x);

		if (n >= settle)
			acc += (double)y * (double)y;
	}
	{
		double rms = sqrt(acc / (double)measure);
		return 20.0 * log10(rms / (AMPL / sqrt(2.0)));
	}
}

static void test_biquad_shapes(void)
{
	struct speaker_dsp_section s;
	struct speaker_dsp_biquad bq;
	double dc, at_fc, hf;

	printf("biquad design (RBJ, our own coefficients)\n");

	s.shape = SPEAKER_SHAPE_LOWSHELF;
	s.fc_hz = 150.0f; s.q = 0.9f; s.gain_db = 6.0f;
	speaker_biquad_design(&bq, &s);
	dc = biquad_gain_db(&bq, 20.0);
	hf = biquad_gain_db(&bq, 10000.0);
	check(fabs(dc - 6.0) < 0.5, "lowshelf +6 dB boosts DC", dc, 6.0, 0.5);
	check(fabs(hf) < 0.5, "lowshelf +6 dB leaves HF unity", hf, 0.0, 0.5);

	s.shape = SPEAKER_SHAPE_HIGHSHELF;
	s.fc_hz = 3000.0f; s.q = 0.7f; s.gain_db = -4.0f;
	speaker_biquad_design(&bq, &s);
	dc = biquad_gain_db(&bq, 50.0);
	hf = biquad_gain_db(&bq, 16000.0);
	check(fabs(dc) < 0.5, "highshelf -4 dB leaves DC unity", dc, 0.0, 0.5);
	check(fabs(hf + 4.0) < 0.8, "highshelf -4 dB cuts HF", hf, -4.0, 0.8);

	s.shape = SPEAKER_SHAPE_PEAK;
	s.fc_hz = 80.0f; s.q = 0.9f; s.gain_db = 2.0f;
	speaker_biquad_design(&bq, &s);
	at_fc = biquad_gain_db(&bq, 80.0);
	dc = biquad_gain_db(&bq, 20.0);
	hf = biquad_gain_db(&bq, 8000.0);
	check(fabs(at_fc - 2.0) < 0.4, "peak +2 dB at Fc", at_fc, 2.0, 0.4);
	check(fabs(dc) < 0.6 && fabs(hf) < 0.6, "peak +2 dB skirts near unity",
	      dc > hf ? dc : hf, 0.0, 0.6);
}

static void test_stability(void)
{
	struct speaker_dsp dsp;
	long n;
	double peak = 0.0;
	int finite = 1;

	printf("stability and numerical safety\n");
	speaker_dsp_init(&dsp, 50);
	for (n = 0; n < (long)RATE * 2; ++n) {
		int32_t x = (n == 0) ? 32767 : 0;
		int32_t y = speaker_dsp_process(&dsp, x);

		if (!isfinite((double)y))
			finite = 0;
		if (fabs((double)y) > peak)
			peak = fabs((double)y);
	}
	check(finite, "impulse response stays finite", finite ? 0.0 : 1.0, 0.0, 0.0);
	check(peak < 200000.0, "impulse response does not diverge", peak, 0.0, 200000.0);
}

static void test_loudness_ladder(void)
{
	double bass50, bass100, treble50, treble100, mid50, mid100;

	printf("loudness ladder direction (more lift at lower volume)\n");
	bass50 = chain_gain_db(50, 120.0);
	bass100 = chain_gain_db(100, 120.0);
	treble50 = chain_gain_db(50, 9000.0);
	treble100 = chain_gain_db(100, 9000.0);
	mid50 = chain_gain_db(50, 700.0);
	mid100 = chain_gain_db(100, 700.0);

	check(bass50 > bass100 + 1.5, "bass lift is greater at volume 50 than 100",
	      bass50 - bass100, 0.0, 1e9);
	check(treble50 > treble100 + 0.5, "treble lift is greater at volume 50 than 100",
	      treble50 - treble100, 0.0, 1e9);
	check(fabs(mid50 - mid100) < 1.5, "mid band is essentially volume independent",
	      mid50 - mid100, 0.0, 1.5);
}

static void test_upper_boundary_selection(void)
{
	static const int input[] = {0, 50, 51, 59, 60, 61, 69, 70, 71, 79, 80, 81, 99, 100};
	static const int expected[] = {50, 50, 60, 60, 60, 70, 70, 70, 80, 80, 80, 100, 100, 100};
	int i;
	printf("stock first-upper-boundary preset selection\n");
	for (i = 0; i < (int)(sizeof(input) / sizeof(input[0])); ++i) {
		double bass = chain_gain_db(input[i], 120.0);
		double anchor_bass = chain_gain_db(expected[i], 120.0);
		double treble = chain_gain_db(input[i], 9000.0);
		double anchor_treble = chain_gain_db(expected[i], 9000.0);
		char label[80];
		(void)snprintf(label, sizeof(label), "index %d uses preset %d at bass", input[i], expected[i]);
		check(fabs(bass - anchor_bass) < 0.01, label, bass - anchor_bass, 0.0, 0.01);
		(void)snprintf(label, sizeof(label), "index %d uses preset %d at treble", input[i], expected[i]);
		check(fabs(treble - anchor_treble) < 0.01, label, treble - anchor_treble, 0.0, 0.01);
	}
}

static void test_volume_clamping(void)
{
	double a, b;

	printf("volume boundary clamping\n");
	a = chain_gain_db(10, 120.0);   /* below the first boundary */
	b = chain_gain_db(50, 120.0);
	check(fabs(a - b) < 0.01, "volume below 50 uses the 50 step", a - b, 0.0, 0.01);
	a = chain_gain_db(127, 120.0);  /* above 100 percent */
	b = chain_gain_db(100, 120.0);
	check(fabs(a - b) < 0.01, "volume above 100 uses the 100 step", a - b, 0.0, 0.01);
}

static void test_inactive_is_transparent(void)
{
	struct speaker_dsp dsp;
	int i, same = 1;

	printf("unknown-volume loudness EQ bypass\n");
	speaker_dsp_init(&dsp, -1);
	for (i = -32768; i < 32768; i += 379) {
		if (speaker_dsp_equalize(&dsp, i) != i)
			same = 0;
	}
	check(same, "volume -1 bypasses EQ but keeps MBCL available", same ? 0.0 : 1.0, 0.0, 0.0);
}

static void test_midband_shaping(void)
{
	double g;

	printf("parametric EQ and overall shaping\n");
	g = chain_gain_db(70, 80.0);
	check(g > 3.0, "80 Hz region is lifted (parametric peak plus shelf)", g, 0.0, 1e9);
	g = chain_gain_db(70, 150.0);
	check(g > 2.0, "150 Hz region is lifted (parametric shelf)", g, 0.0, 1e9);
	g = chain_gain_db(100, 12000.0);
	check(g > -6.0 && g < 12.0, "12 kHz stays within a sane range", g, 0.0, 1e9);
}

static void test_bass_boost_reaches_limiter_without_preclipping(void)
{
	struct speaker_dsp raw_eq, protected;
	struct puffin_dynamics dynamics;
	int32_t peak_before_mbcl = 0;
	int32_t peak_after_limiter = 0;
	long n;

	printf("bass boost headroom before MBCL and output limiter\n");
	speaker_dsp_init(&raw_eq, 50);
	speaker_dsp_init(&protected, 50);
	puffin_dynamics_init(&dynamics);
	for (n = 0; n < (long)RATE / 2; ++n) {
		int32_t input = (int32_t)lrint(4000.0 *
			sin(TEST_TWO_PI * 80.0 * (double)n / RATE));
		int32_t wide = (int32_t)lrintf(speaker_dsp_equalize(&raw_eq, input));
		int32_t protected_sample = speaker_dsp_process(&protected, input);
		int16_t output = puffin_render_mono(&dynamics, protected_sample);
		int32_t magnitude = wide < 0 ? -wide : wide;
		int32_t final_magnitude = output < 0 ? -(int32_t)output : output;

		if (n < (long)RATE / 4)
			continue;
		if (magnitude > peak_before_mbcl)
			peak_before_mbcl = magnitude;
		if (final_magnitude > peak_after_limiter)
			peak_after_limiter = final_magnitude;
	}
	check(peak_before_mbcl > 45000 && peak_before_mbcl < 80000,
	      "80 Hz EQ remains wide until multiband protection",
	      peak_before_mbcl, 55000, 25000);
	check(peak_after_limiter <= 32767,
	      "output stays within signed 16-bit PCM",
	      peak_after_limiter, 32767, 0);
}

static void test_bass_band_protection_keeps_full_bus_limiter_idle(void)
{
	struct speaker_dsp dsp;
	struct puffin_dynamics dynamics;
	int32_t peak = 0;
	int limited = 0;
	int rail = 0;
	long n;

	speaker_dsp_init(&dsp, 60);
	puffin_dynamics_init(&dynamics);
	for (n = 0; n < (long)RATE; ++n) {
		int32_t sample = (int32_t)lrint(4000.0 *
			sin(TEST_TWO_PI * 80.0 * (double)n / RATE));
		int32_t protected = speaker_dsp_process(&dsp, sample);
		int16_t output = puffin_render_mono(&dynamics, protected);

		if (n < (long)RATE / 2)
			continue;
		if (abs(protected) > peak)
			peak = abs(protected);
		if (dynamics.gain_q15 < PUFFIN_OUTPUT_TRIM_Q15)
			limited++;
		if (output == 32767 || output == -32767)
			rail++;
	}
	printf("80 Hz sustained bass: pre-trim peak=%d full-bus-limited=%d rail=%d\n",
	       peak, limited, rail);
	check(peak < 23000 && peak > 1000,
	      "bass-band protection acts before output trim",
	      peak, 0, 23000);
	check(limited < RATE / 20 && rail < RATE / 200,
	      "bass no longer pumps or rails the full-band limiter",
	      limited, 0, RATE / 20);
}

static void test_live_volume_change_updates_loudness_without_stream_restart(void)
{
	struct speaker_dsp dsp;
	double rms50 = 0.0, rms100 = 0.0;
	long n;

	speaker_dsp_init(&dsp, 50);
	for (n = 0; n < (long)RATE; ++n) {
		int32_t sample = (int32_t)lrint(100.0 *
			sin(TEST_TWO_PI * 80.0 * (double)n / RATE));
		int32_t output = speaker_dsp_process(&dsp, sample);
		if (n >= (long)RATE * 3 / 4)
			rms50 += (double)output * output;
	}
	/* The PCM stays open while the AirPlay sender changes volume. */
	speaker_dsp_set_volume(&dsp, 100);
	for (n = 0; n < (long)RATE; ++n) {
		int32_t sample = (int32_t)lrint(100.0 *
			sin(TEST_TWO_PI * 80.0 * (double)n / RATE));
		int32_t output = speaker_dsp_process(&dsp, sample);
		if (n >= (long)RATE * 3 / 4)
			rms100 += (double)output * output;
	}
	rms50 = sqrt(rms50 / (RATE / 4));
	rms100 = sqrt(rms100 / (RATE / 4));
	check(rms50 > rms100 * 1.6,
	      "live phone volume changes the bass EQ without reopening PCM",
	      20.0 * log10(rms50 / rms100), 4.0, 2.0);
}

static void test_mid_ramp_volume_retarget_is_continuous(void)
{
	struct speaker_dsp dsp, unchanged, target;
	long n;
	float actual, expected;
	double candidate_rms = 0.0, target_rms = 0.0;

	speaker_dsp_init(&dsp, 50);
	for (n = 0; n < RATE; ++n)
		(void)speaker_dsp_equalize(&dsp, (int32_t)lrint(4000.0 *
			sin(TEST_TWO_PI * 80.0 * (double)n / RATE)));
	speaker_dsp_set_volume(&dsp, 100);
	for (n = 0; n < 2048; ++n)
		(void)speaker_dsp_equalize(&dsp, (int32_t)lrint(4000.0 *
			sin(TEST_TWO_PI * 80.0 * (double)n / RATE)));
	unchanged = dsp;
	speaker_dsp_set_volume(&dsp, 70);
	actual = speaker_dsp_equalize(&dsp, 4000);
	expected = speaker_dsp_equalize(&unchanged, 4000);
	check(fabs(actual - expected) < 100.0,
	      "mid-ramp retarget does not jump to the old cascade",
	      actual - expected, 0.0, 100.0);
	/* Multiple phone callbacks during one transition must still converge. */
	speaker_dsp_set_volume(&dsp, 80);
	speaker_dsp_init(&target, 80);
	for (n = 0; n < RATE; ++n) {
		int32_t sample = (int32_t)lrint(100.0 *
			sin(TEST_TWO_PI * 80.0 * (double)n / RATE));
		float y = speaker_dsp_equalize(&dsp, sample);
		float z = speaker_dsp_equalize(&target, sample);
		if (n >= RATE * 3 / 4) {
			candidate_rms += (double)y * y;
			target_rms += (double)z * z;
		}
	}
	check(fabs(10.0 * log10(candidate_rms / target_rms)) < 0.5,
	      "queued volume callbacks converge on the latest EQ setting",
	      10.0 * log10(candidate_rms / target_rms), 0.0, 0.5);
}

int main(void)
{
	test_biquad_shapes();
	test_stability();
	test_loudness_ladder();
	test_upper_boundary_selection();
	test_volume_clamping();
	test_inactive_is_transparent();
	test_midband_shaping();
	test_bass_boost_reaches_limiter_without_preclipping();
	test_bass_band_protection_keeps_full_bus_limiter_idle();
	test_live_volume_change_updates_loudness_without_stream_restart();
	test_mid_ramp_volume_retarget_is_continuous();

	if (failures) {
		printf("\nspeaker_dsp: %d check(s) FAILED\n", failures);
		return 1;
	}
	printf("\nspeaker_dsp: all checks passed\n");
	return 0;
}
