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
static double owner_eq_gain_db(int volume, int tone, const float *db, double f)
{
	static struct biscuit_dsp d;
	double acc = 0.0, w = TEST_TWO_PI * f / RATE, a = 100.0;
	long n, settle = (long)(RATE * 0.5), meas = (long)(RATE * 0.5);

	biscuit_dsp_init(&d, volume);
	if (tone)
		biscuit_dsp_set_tone(&d, db[0], db[1], db[2], db[3]);
	for (n = 0; n < settle + meas; ++n) {
		double x = biscuit_dsp_bus(&d, a * sin(w * (double)n));
		if (n >= settle)
			acc += x * x;
	}
	return 20.0 * log10(sqrt(acc / (double)meas) / (a / sqrt(2.0)));
}

static double tone_eq_gain_db(int volume, int tone, double bass_db, double treble_db, double f)
{
	const float db[BISCUIT_OWNER_BANDS] = { (float)bass_db, 0.0f, 0.0f, (float)treble_db };

	return owner_eq_gain_db(volume, tone, db, f);
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
	biscuit_dsp_set_tone(&flat, 0.0f, 0.0f, 0.0f, 0.0f);
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
	biscuit_dsp_set_tone(&moved, 6.0f, 6.0f, 6.0f, 6.0f);
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
	biscuit_dsp_set_tone(&moved, -3.0f, 1.0f, -1.0f, 2.0f);
	biscuit_dsp_set_volume(&moved, -1);
	biscuit_dsp_set_volume(&moved, 60);
	check(moved.owner.on == 0xfu, "owner tone survives unknown-volume re-init",
	      (double)moved.owner.on, 15.0, 0.0);
	for (i = 0; i < BISCUIT_OWNER_BANDS; ++i) {
		biscuit_dsp_init(&ref, 60);
		biscuit_dsp_set_tone(&ref, -3.0f, 1.0f, -1.0f, 2.0f);
		static const char *const label[BISCUIT_OWNER_BANDS] = {
			"bass shelf coefficients kept across re-init",
			"low-mid peak coefficients kept across re-init",
			"high-mid peak coefficients kept across re-init",
			"treble shelf coefficients kept across re-init"
		};

		check(moved.owner.band[i].b0 == ref.owner.band[i].b0 &&
		      moved.owner.band[i].a1 == ref.owner.band[i].a1, label[i],
		      moved.owner.band[i].b0 - ref.owner.band[i].b0, 0.0, 0.0);
	}
}

/* Steady-state RMS of the full Dot chain output for a sine, dBFS. */
static double boost_out_level_dbfs(struct biscuit_dsp *d, double f, double in_dbfs)
{
	double amp = 32768.0 * pow(10.0, in_dbfs / 20.0), w = TEST_TWO_PI * f / RATE, acc = 0.0;
	long n, settle = (long)(RATE * 0.5), meas = (long)(RATE * 0.25);

	for (n = 0; n < settle + meas; ++n) {
		float y = biscuit_dsp_process(d, (float)(amp * sin(w * (double)n)));
		if (n >= settle)
			acc += (double)y * (double)y;
	}
	return 20.0 * log10(sqrt(acc / (double)meas) / (32768.0 / sqrt(2.0)));
}

static void test_owner_boost(void)
{
	static struct biscuit_dsp stock, off, plus6, moved, ref;
	double base, b6, peak = 0.0, ceiling = 32768.0 * pow(10.0, -0.1 / 20.0);
	long n, differs = 0;
	float x;

	printf("biscuit_speaker_dsp: owner boost\n");
	biscuit_dsp_init(&stock, 50);
	biscuit_dsp_init(&off, 50);
	biscuit_dsp_set_boost(&off, 0.0f);
	for (n = 0; n < (long)RATE / 2; ++n) {
		x = (float)(9000.0 * sin(TEST_TWO_PI * 80.0 * (double)n / RATE) +
			    3000.0 * sin(TEST_TWO_PI * 7000.0 * (double)n / RATE));
		if (biscuit_dsp_process(&stock, x) != biscuit_dsp_process(&off, x))
			differs++;
	}
	check(differs == 0, "boost 0 dB equals the stock Dot chain sample for sample",
	      (double)differs, 0.0, 0.0);

	biscuit_dsp_init(&stock, 50);
	biscuit_dsp_init(&plus6, 50);
	biscuit_dsp_set_boost(&plus6, 6.0f);
	base = boost_out_level_dbfs(&stock, 1000.0, -40.0);
	b6 = boost_out_level_dbfs(&plus6, 1000.0, -40.0);
	check_near("+6 dB boost raises a quiet 1 kHz tone by about 6 dB", b6 - base, 6.0, 0.5);

	/* Full scale at max boost and volume: the Dot limiters still hold the ceiling. */
	biscuit_dsp_init(&plus6, 100);
	biscuit_dsp_set_boost(&plus6, 6.0f);
	for (n = 0; n < (long)RATE * 2; ++n) {
		double t = (double)n / RATE;
		float y;

		x = (float)(30000.0 * sin(TEST_TWO_PI * 80.0 * t) +
			    30000.0 * sin(TEST_TWO_PI * 1000.0 * t));
		y = biscuit_dsp_process(&plus6, x);
		if (n >= (long)RATE && fabs(y) > peak)
			peak = fabs(y);
	}
	check(peak <= ceiling + 1.0, "+6 dB boost at full scale held at the Dot ceiling",
	      peak, ceiling, 1.0);

	biscuit_dsp_init(&moved, 50);
	biscuit_dsp_set_boost(&moved, 6.0f);
	biscuit_dsp_set_volume(&moved, -1);
	biscuit_dsp_set_volume(&moved, 60);
	biscuit_dsp_init(&ref, 60);
	biscuit_dsp_set_boost(&ref, 6.0f);
	check(fabs(moved.boost - ref.boost) < 1e-12,
	      "boost survives unknown-volume re-init", moved.boost, ref.boost, 1e-12);
}

/* Four-band owner equaliser: band frequencies (shelves on their plateau). */
static const double owner_hz[BISCUIT_OWNER_BANDS] = { 40.0, 400.0, 2500.0, 16000.0 };
static const char *const owner_name[BISCUIT_OWNER_BANDS] = { "bass", "low-mid", "high-mid", "treble" };

static void test_owner_eq_response(void)
{
	static const float flat[BISCUIT_OWNER_BANDS] = { 0.0f, 0.0f, 0.0f, 0.0f };
	int band, at, sign, cross_ok = 1;

	printf("biscuit_speaker_dsp: four-band owner EQ frequency response (delta vs flat, volume 50)\n");
	for (sign = -1; sign <= 1; sign += 2)
		for (band = 0; band < BISCUIT_OWNER_BANDS; ++band) {
			float db[BISCUIT_OWNER_BANDS] = { 0.0f, 0.0f, 0.0f, 0.0f };
			char label[96];

			db[band] = 6.0f * (float)sign;
			printf("  %-8s %+d dB:", owner_name[band], 6 * sign);
			for (at = 0; at < BISCUIT_OWNER_BANDS; ++at) {
				double delta = owner_eq_gain_db(50, 1, db, owner_hz[at]) -
					owner_eq_gain_db(50, 0, flat, owner_hz[at]);

				printf(" %5.0f Hz %+6.2f", owner_hz[at], delta);
				if (at != band && fabs(delta) >= 1.0)
					cross_ok = 0;
				if (at == band) {
					printf("\n");
					(void)snprintf(label, sizeof(label), "%s %+d dB moves %.0f Hz by its dB",
						       owner_name[band], 6 * sign, owner_hz[at]);
					check_near(label, delta, 6.0 * sign, 0.5);
					printf("           ");
				}
			}
			printf("\n");
		}
	check(cross_ok, "every band stays under 1 dB at the other bands' frequencies",
	      cross_ok ? 0.0 : 1.0, 0.0, 0.0);
}

static float owner_programme(long n, double amp)
{
	double t = (double)n / RATE;

	return (float)(amp * (0.62 * sin(TEST_TWO_PI * 90.0 * t) +
			      0.30 * sin(TEST_TWO_PI * 400.0 * t + 0.3) +
			      0.08 * sin(TEST_TWO_PI * 2500.0 * t + 1.1)));
}

static void test_owner_eq_flat_is_stock(void)
{
	static struct biscuit_dsp stock, flat, back;
	long n, differs = 0;

	printf("biscuit_speaker_dsp: four-band owner EQ all flat is the stock chain\n");
	biscuit_dsp_init(&stock, 50);
	biscuit_dsp_init(&flat, 50);
	biscuit_dsp_init(&back, 50);
	biscuit_dsp_set_tone(&flat, 0.0f, 0.0f, 0.0f, 0.0f);
	biscuit_dsp_set_boost(&flat, 0.0f);
	biscuit_dsp_set_tone(&back, 3.0f, -2.0f, 2.0f, -1.0f);
	biscuit_dsp_set_boost(&back, 6.0f);
	for (n = 0; n < (long)RATE; ++n) {
		float x = (float)(9000.0 * sin(TEST_TWO_PI * 80.0 * (double)n / RATE) +
				  4000.0 * sin(TEST_TWO_PI * 410.0 * (double)n / RATE) +
				  3000.0 * sin(TEST_TWO_PI * 7000.0 * (double)n / RATE));

		if (n == (long)RATE / 4) {
			biscuit_dsp_fade_tone(&flat, 0.0f, 0.0f, 0.0f, 0.0f);
			biscuit_dsp_fade_boost(&flat, 0.0f);
			biscuit_dsp_fade_tone(&back, 0.0f, 0.0f, 0.0f, 0.0f);
			biscuit_dsp_fade_boost(&back, 0.0f);
		}
		if (biscuit_dsp_process(&stock, x) != biscuit_dsp_process(&flat, x))
			differs++;
		(void)biscuit_dsp_process(&back, x);
	}
	check(differs == 0, "all-flat four-band EQ equals the stock Dot chain sample for sample",
	      (double)differs, 0.0, 0.0);
	check(back.owner.on == 0 && back.owner.fade == 0 && back.boost == 1.0 && back.boost_ramp == 0,
	      "a fade back to flat ends with no band on and the boost exactly unity",
	      back.boost, 1.0, 0.0);
}

static double max_step(const float *y, long from, long to)
{
	double m = 0.0;
	long n;

	for (n = from + 1; n < to; ++n)
		if (fabs((double)y[n] - (double)y[n - 1]) > m)
			m = fabs((double)y[n] - (double)y[n - 1]);
	return m;
}

#define CLICK_LEN 96000L
/* A quarter period of the 90 Hz component after a whole number of its
 * cycles: the change lands on a programme peak, where a hard switch shows. */
#define CLICK_AT (48000L + 133L)

/* Change at CLICK_AT, live (crossfade/ramp) or hard; measure the bus into the
 * compressors or the limited output.  Ratio of the largest step in the change
 * window to the larger natural step of the steady stretches around it. */
static double owner_change_step_ratio(int live, int limited, double amp,
				      double *window, double *natural)
{
	static float y[CLICK_LEN];
	static struct biscuit_dsp d;
	double before, after;
	long n;

	biscuit_dsp_init(&d, 50);
	biscuit_dsp_set_tone(&d, -4.0f, 0.0f, 2.0f, 0.0f);
	for (n = 0; n < CLICK_LEN; ++n) {
		float x = owner_programme(n, amp);

		if (n == CLICK_AT) {
			if (live) {
				biscuit_dsp_fade_tone(&d, 6.0f, 6.0f, -6.0f, 6.0f);
				biscuit_dsp_fade_boost(&d, 6.0f);
			} else {
				biscuit_dsp_set_tone(&d, 6.0f, 6.0f, -6.0f, 6.0f);
				biscuit_dsp_set_boost(&d, 6.0f);
			}
		}
		y[n] = limited ? biscuit_dsp_process(&d, x) : (float)biscuit_dsp_bus(&d, x);
	}
	before = max_step(y, CLICK_AT - 12000, CLICK_AT);
	after = max_step(y, CLICK_LEN - 24000, CLICK_LEN);
	*window = max_step(y, CLICK_AT - 1, CLICK_AT + 2 * BISCUIT_OWNER_FADE_FRAMES + 2 * BISCUIT_LA);
	*natural = before > after ? before : after;
	return *window / *natural;
}

static void test_owner_eq_live_change(void)
{
	static struct biscuit_dsp d, ref;
	double window, natural, ratio;
	int limited;
	long n;

	printf("biscuit_speaker_dsp: live owner EQ/boost change does not click\n");
	for (limited = 0; limited <= 1; ++limited) {
		const char *where = limited ? "limited output" : "bus into compressors";
		char label[112];

		ratio = owner_change_step_ratio(1, limited, 300.0, &window, &natural);
		printf("  %s, live change: max step %.1f, natural %.1f (ratio %.3f)\n",
		       where, window, natural, ratio);
		/* The bus must not step at all.  At the limited output the
		 * Dot's bass compressor (20:1 from -50 dB, always active here)
		 * lags a +12 dB bass rise inside one period, so the output runs
		 * a little above its later compressed steady state: a level
		 * overshoot, not a step (a hard switch is 20-30x).  Measured
		 * 1.03-1.13 from -50 to -15 dBFS; bound it. */
		(void)snprintf(label, sizeof(label), limited ?
			       "%s: crossfade overshoot stays within compressor attack" :
			       "%s: crossfaded change steps no more than the signal", where);
		check(ratio <= (limited ? 1.2 : 1.0 + 1e-6), label, ratio, 1.0, limited ? 0.2 : 0.0);
		ratio = owner_change_step_ratio(0, limited, 300.0, &window, &natural);
		printf("  %s, hard switch (control): max step %.1f, natural %.1f (ratio %.3f)\n",
		       where, window, natural, ratio);
		(void)snprintf(label, sizeof(label), "%s: control hard switch does step", where);
		check(ratio > 1.5, label, ratio, 2.0, 0.5);
	}
	ratio = owner_change_step_ratio(1, 0, 6000.0, &window, &natural);
	printf("  loud programme, bus into compressors: max step %.1f, natural %.1f (ratio %.3f)\n",
	       window, natural, ratio);
	check(ratio <= 1.0 + 1e-6, "loud programme: crossfaded change steps no more than the signal",
	      ratio, 1.0, 0.0);

	/* A change queued during a fade lands exactly on its setting. */
	biscuit_dsp_init(&d, 50);
	biscuit_dsp_fade_tone(&d, 6.0f, 0.0f, 0.0f, 0.0f);
	for (n = 0; n < 500; ++n)
		(void)biscuit_dsp_process(&d, owner_programme(n, 300.0));
	biscuit_dsp_fade_tone(&d, -3.0f, 2.0f, 5.0f, -1.0f);
	for (n = 0; n < 3 * BISCUIT_OWNER_FADE_FRAMES; ++n)
		(void)biscuit_dsp_process(&d, owner_programme(n, 300.0));
	biscuit_dsp_init(&ref, 50);
	biscuit_dsp_set_tone(&ref, -3.0f, 2.0f, 5.0f, -1.0f);
	for (n = 0; n < BISCUIT_OWNER_BANDS; ++n)
		if (d.owner.band[n].b0 != ref.owner.band[n].b0 || d.owner.band[n].a2 != ref.owner.band[n].a2)
			break;
	check(n == BISCUIT_OWNER_BANDS && d.owner.on == ref.owner.on && !d.owner.fade && !d.owner.pending,
	      "a change queued during a fade lands on its setting", (double)n, 4.0, 0.0);

	biscuit_dsp_init(&d, 50);
	biscuit_dsp_fade_boost(&d, 3.0f);
	for (n = 0; n < BISCUIT_OWNER_FADE_FRAMES; ++n)
		(void)biscuit_dsp_process(&d, owner_programme(n, 300.0));
	check(d.boost == pow(10.0, 3.0 / 20.0) && d.boost_ramp == 0,
	      "boost ramp reaches the +3 dB step in one period", d.boost, pow(10.0, 3.0 / 20.0), 0.0);
}

static void test_owner_eq_all_bands_max_still_limited(void)
{
	static struct biscuit_dsp d;
	double peak = 0.0, ceiling = 32768.0 * pow(10.0, -0.1 / 20.0);
	long n;

	printf("biscuit_speaker_dsp: all four bands +6 dB plus boost +6 dB stay under the limiter\n");
	biscuit_dsp_init(&d, 100);
	biscuit_dsp_set_tone(&d, 6.0f, 6.0f, 6.0f, 6.0f);
	biscuit_dsp_set_boost(&d, 6.0f);
	for (n = 0; n < (long)RATE * 2; ++n) {
		double t = (double)n / RATE;
		float y = biscuit_dsp_process(&d, (float)(14000.0 * sin(TEST_TWO_PI * 40.0 * t) +
							  9000.0 * sin(TEST_TWO_PI * 400.0 * t) +
							  6000.0 * sin(TEST_TWO_PI * 2500.0 * t) +
							  4000.0 * sin(TEST_TWO_PI * 16000.0 * t)));
		if (!isfinite(y))
			peak = 1e9;
		else if (fabs(y) > peak)
			peak = fabs(y);
	}
	check(peak <= ceiling + 1.0, "all bands +6 and boost +6 held at the Dot ceiling",
	      peak, ceiling, 1.0);
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
	test_owner_boost();
	test_owner_eq_response();
	test_owner_eq_flat_is_stock();
	test_owner_eq_live_change();
	test_owner_eq_all_bands_max_still_limited();

	if (failures) {
		printf("biscuit_speaker_dsp: %d FAILED\n", failures);
		return 1;
	}
	printf("biscuit_speaker_dsp: PASS\n");
	return 0;
}
