#ifndef LIBREECHO_BISCUIT_SPEAKER_DSP_H
#define LIBREECHO_BISCUIT_SPEAKER_DSP_H

/*
 * Echo Dot 2 (biscuit) speaker chain on the mono programme bus.
 *
 *   loudness EQ (one fixed shape, gain ladder by volume)
 *   -> 4-band compressor/limiter (115 / 500 / 7500 Hz) -> full-band limiter
 *
 * No OutputTrim stage: the Dot's stock chain has none.
 *
 * Provenance: the EQ is an independently designed biquad cascade fitted to a
 * measured response; the dynamics use the stock *parameters* (crossovers,
 * ratios, thresholds, releases) with our own filters and detectors.  No vendor
 * coefficient data is embedded.  Time constants marked FITTED were measured
 * against an offline reference because the stock configuration does not
 * specify them.
 */

#include <math.h>
#include <stdint.h>
#include <string.h>

/* M_PI is a POSIX extension and is not exposed under strict -std=c99. */
#define BISCUIT_TWO_PI 6.28318530717958647692
#define BISCUIT_RATE 48000.0
#define BISCUIT_PCM 32768.0f
#define BISCUIT_EQ_SECTIONS 16
#define BISCUIT_BANDS 4
#define BISCUIT_GROUP 48u
#define BISCUIT_VOLUME_RAMP_FRAMES 4800

enum { BISCUIT_HIGHPASS = 0, BISCUIT_LOWSHELF = 1, BISCUIT_PEAK = 2, BISCUIT_HIGHSHELF = 3 };

struct biscuit_section { int shape; double fc, q, gain_db; };

/* Loudness shape fitted to the measured stock response (0.60 dB rms,
 * 1.9 dB worst, 70 Hz-16 kHz held-out grid).  Gain in dB at volume <= 70. */
static const double biscuit_eq_gain0_db = -2.63;
static const struct biscuit_section biscuit_eq[BISCUIT_EQ_SECTIONS] = {
	{ BISCUIT_HIGHPASS,     34.6, 0.508,   0.00 },
	{ BISCUIT_LOWSHELF,    434.4, 1.459,  +7.82 },
	{ BISCUIT_PEAK,         65.1, 8.000, -30.00 },
	{ BISCUIT_PEAK,        189.4, 2.236,  +5.36 },
	{ BISCUIT_PEAK,        126.8, 0.531, +19.93 },
	{ BISCUIT_PEAK,        369.0, 4.694,  -2.12 },
	{ BISCUIT_PEAK,       1391.6, 0.569,  +7.94 },
	{ BISCUIT_PEAK,       2757.0, 0.441, -24.68 },
	{ BISCUIT_PEAK,       4970.8, 2.154,  +8.16 },
	{ BISCUIT_PEAK,       6490.3, 1.434, -29.70 },
	{ BISCUIT_PEAK,       8002.2, 0.377, +30.00 },
	{ BISCUIT_PEAK,      12532.5, 3.177, -13.84 },
	{ BISCUIT_PEAK,       6985.8, 8.000,  +5.89 },
	{ BISCUIT_PEAK,         65.0, 8.000,  -2.77 },
	{ BISCUIT_PEAK,      17117.7, 3.845,  -9.84 },
	{ BISCUIT_PEAK,         59.3, 1.115,  -3.45 }
};

/* Stock volume boundaries and the measured broadband step per boundary. */
#define BISCUIT_STEPS 6
static const int biscuit_volume_boundary[BISCUIT_STEPS] = { 50, 60, 70, 80, 90, 100 };
static const double biscuit_volume_gain_db[BISCUIT_STEPS] = { 0.0, 0.0, 0.0, 3.52, 7.54, 14.54 };

/* Dynamics parameters.  Configured values are from the stock MBCL settings;
 * time constants are FITTED (see report). */
struct biscuit_dyn_params {
	double fc[3];
	float comp_ratio[BISCUIT_BANDS];
	float comp_thresh_db[BISCUIT_BANDS];
	float comp_min_db[BISCUIT_BANDS];
	float lim_thresh_db[BISCUIT_BANDS];
	float lim_release_ms[BISCUIT_BANDS];
	float full_thresh_db, full_release_ms;
	/* FITTED detector/gain time constants, milliseconds */
	float t_fast[BISCUIT_BANDS], t_attack[BISCUIT_BANDS], t_release[BISCUIT_BANDS], t_gain[BISCUIT_BANDS];
	float det_offset_db[BISCUIT_BANDS];
};

static const struct biscuit_dyn_params biscuit_dyn_default = {
	{ 115.0, 500.0, 7500.0 },
	{ 20.0f, 2.0f, 2.0f, 2.0f },
	{ -50.0f, -10.0f, -10.0f, -10.0f },
	{ -40.0f, -40.0f, -40.0f, -40.0f },
	{ -8.0f, 0.0f, 0.0f, 0.0f },
	{ 200.0f, 1.0f, 1.0f, 1.0f },
	-0.1f, 200.0f,
	/* FITTED (v2): fast detector, attack, release, gain smoothing */
	{ 1.0f, 3.5f, 7.0f, 1.0f },
	{ 8.6f, 49.2f, 990.8f, 2.6f },
	{ 376.5f, 846.4f, 1296.7f, 800.8f },
	{ 554.3f, 9.2f, 8.7f, 5.2f },
	{ 0.0f, 0.0f, 0.0f, 0.0f }
};

struct biscuit_biquad { double b0, b1, b2, a1, a2, z1, z2; };

/* Look-ahead peak limiter: instant-attack/hold/release target gain, sliding
 * minimum over the look-ahead window, then a box average of the same length.
 * The box of the windowed minimum can never exceed the target at the sample
 * being output, so peaks are held at the ceiling without waveshaping. */
#define BISCUIT_LA 64
#define BISCUIT_HOLD 480 /* must exceed BISCUIT_LA, see window minimum */
struct biscuit_limiter {
	float delay[BISCUIT_LA];
	float envbuf[BISCUIT_LA];
	int pos, hold;
	float box[BISCUIT_LA];
	double box_sum;
	float env, rel, ceiling;
};
struct biscuit_lr4 { struct biscuit_biquad low[2], high[2]; };

struct biscuit_band {
	double acc, p_fast, p_env;
	float comp_gain;
	float c_fast, c_attack, c_release, c_gain;
	float thresh_db, slope, min_gain;
	struct biscuit_limiter lim;
};

struct biscuit_dsp {
	struct biscuit_biquad eq[BISCUIT_EQ_SECTIONS];
	double eq_gain, next_eq_gain;
	/* Post-compressor makeup on the bass bands (<115 Hz, 115-500 Hz), set
	 * from the per-target audio profile; 0 dB (the zero state) is stock. */
	float bass_makeup_db;
	float makeup[BISCUIT_BANDS];
	int eq_active;
	int ramp, volume;
	struct biscuit_lr4 split[3], ap_low1, ap_low2, ap_mid2;
	struct biscuit_band band[BISCUIT_BANDS];
	unsigned int group_pos;
	struct biscuit_limiter full;
};

static inline void biscuit_design(struct biscuit_biquad *bq, int shape, double fc,
				  double q, double gain_db)
{
	double a = pow(10.0, gain_db / 40.0), w = BISCUIT_TWO_PI * fc / BISCUIT_RATE;
	double c = cos(w), s = sin(w), al = s / (2.0 * q), sq = 2.0 * sqrt(a) * al;
	double b0, b1, b2, a0, a1, a2;

	switch (shape) {
	case BISCUIT_HIGHPASS:
		b0 = (1.0 + c) / 2.0; b1 = -(1.0 + c); b2 = b0;
		a0 = 1.0 + al; a1 = -2.0 * c; a2 = 1.0 - al;
		break;
	case BISCUIT_LOWSHELF:
		b0 = a * ((a + 1.0) - (a - 1.0) * c + sq);
		b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * c);
		b2 = a * ((a + 1.0) - (a - 1.0) * c - sq);
		a0 = (a + 1.0) + (a - 1.0) * c + sq;
		a1 = -2.0 * ((a - 1.0) + (a + 1.0) * c);
		a2 = (a + 1.0) + (a - 1.0) * c - sq;
		break;
	case BISCUIT_HIGHSHELF:
		b0 = a * ((a + 1.0) + (a - 1.0) * c + sq);
		b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * c);
		b2 = a * ((a + 1.0) + (a - 1.0) * c - sq);
		a0 = (a + 1.0) - (a - 1.0) * c + sq;
		a1 = 2.0 * ((a - 1.0) - (a + 1.0) * c);
		a2 = (a + 1.0) - (a - 1.0) * c - sq;
		break;
	default:
		b0 = 1.0 + al * a; b1 = -2.0 * c; b2 = 1.0 - al * a;
		a0 = 1.0 + al / a; a1 = -2.0 * c; a2 = 1.0 - al / a;
		break;
	}
	bq->b0 = b0 / a0; bq->b1 = b1 / a0; bq->b2 = b2 / a0;
	bq->a1 = a1 / a0; bq->a2 = a2 / a0;
	bq->z1 = bq->z2 = 0.0;
}

static inline double biscuit_step(struct biscuit_biquad *bq, double x)
{
	double y = bq->b0 * x + bq->z1;
	bq->z1 = bq->b1 * x - bq->a1 * y + bq->z2;
	bq->z2 = bq->b2 * x - bq->a2 * y;
	return y;
}

static inline void biscuit_lr4_design(struct biscuit_lr4 *p, double fc)
{
	int i;
	for (i = 0; i < 2; ++i) {
		/* Butterworth (Q = 1/sqrt2) low/high pass pairs */
		{
			double w = BISCUIT_TWO_PI * fc / BISCUIT_RATE, c = cos(w);
			double al = sin(w) / (2.0 * 0.70710678118654752), a0 = 1.0 + al;
			p->low[i].b0 = (1.0 - c) / 2.0 / a0; p->low[i].b1 = (1.0 - c) / a0;
			p->low[i].b2 = p->low[i].b0; p->low[i].a1 = -2.0 * c / a0;
			p->low[i].a2 = (1.0 - al) / a0; p->low[i].z1 = p->low[i].z2 = 0.0;
			p->high[i] = p->low[i];
			p->high[i].b0 = (1.0 + c) / 2.0 / a0; p->high[i].b1 = -(1.0 + c) / a0;
			p->high[i].b2 = p->high[i].b0;
		}
	}
}

static inline void biscuit_lr4_step(struct biscuit_lr4 *p, double x, double *lo, double *hi)
{
	*lo = biscuit_step(&p->low[1], biscuit_step(&p->low[0], x));
	*hi = biscuit_step(&p->high[1], biscuit_step(&p->high[0], x));
}

static inline double biscuit_allpass(struct biscuit_lr4 *p, double x)
{
	double lo, hi;
	biscuit_lr4_step(p, x, &lo, &hi);
	return lo + hi;
}

/* One-pole coefficient for a time constant, evaluated once per detector group. */
static inline float biscuit_group_coef(float ms)
{
	return 1.0f - expf(-(float)BISCUIT_GROUP * 1000.0f / ((float)BISCUIT_RATE * ms));
}

static inline void biscuit_limiter_init(struct biscuit_limiter *l, float thresh_db,
					float release_ms)
{
	int i;
	memset(l, 0, sizeof(*l));
	l->ceiling = BISCUIT_PCM * powf(10.0f, thresh_db / 20.0f);
	/* Configured releases as short as 1 ms would re-open inside one bass
	 * cycle; the hold keeps the gain for 10 ms before releasing. */
	l->rel = expf(-1000.0f / ((float)BISCUIT_RATE * release_ms));
	l->env = 1.0f;
	for (i = 0; i < BISCUIT_LA; ++i)
		l->box[i] = l->envbuf[i] = 1.0f;
	l->box_sum = BISCUIT_LA;
}

static inline float biscuit_limiter_step(struct biscuit_limiter *l, float x)
{
	float mag = fabsf(x), target = 1.0f, out, mn;
	int slot = l->pos;

	if (mag > l->ceiling)
		target = l->ceiling / mag;
	if (target <= l->env) {
		l->env = target;
		l->hold = BISCUIT_HOLD;
	} else if (l->hold > 0) {
		--l->hold;
	} else {
		l->env = 1.0f - (1.0f - l->env) * l->rel;
		if (l->env > target)
			l->env = target;
	}
	/* Window minimum.  A drop always starts a hold longer than the window,
	 * so inside any window the envelope is at most one release segment and
	 * one hold segment: its minimum is at one of the two ends. */
	mn = l->envbuf[slot] < l->env ? l->envbuf[slot] : l->env;
	l->envbuf[slot] = l->env;
	/* box average of the minimum */
	l->box_sum += (double)mn - (double)l->box[slot];
	l->box[slot] = mn;
	out = l->delay[slot] * (float)(l->box_sum / BISCUIT_LA);
	l->delay[slot] = x;
	l->pos = (l->pos + 1) % BISCUIT_LA;
	return fmaxf(-l->ceiling, fminf(l->ceiling, out));
}

static inline double biscuit_volume_gain(int volume)
{
	int i;
	if (volume < 0)
		return 1.0; /* unknown level: EQ bypassed, see eq_active */
	for (i = 0; i < BISCUIT_STEPS - 1; ++i)
		if (volume <= biscuit_volume_boundary[i])
			break;
	return pow(10.0, (biscuit_eq_gain0_db + biscuit_volume_gain_db[i]) / 20.0);
}

static inline void biscuit_dsp_init_params(struct biscuit_dsp *d, int volume,
					   const struct biscuit_dyn_params *p)
{
	int i;
	memset(d, 0, sizeof(*d));
	for (i = 0; i < BISCUIT_EQ_SECTIONS; ++i)
		biscuit_design(&d->eq[i], biscuit_eq[i].shape, biscuit_eq[i].fc,
			       biscuit_eq[i].q, biscuit_eq[i].gain_db);
	for (i = 0; i < BISCUIT_BANDS; ++i)
		d->makeup[i] = 1.0f;
	d->volume = volume;
	d->eq_active = volume >= 0;
	d->eq_gain = d->next_eq_gain = biscuit_volume_gain(volume);
	for (i = 0; i < 3; ++i)
		biscuit_lr4_design(&d->split[i], p->fc[i]);
	biscuit_lr4_design(&d->ap_low1, p->fc[1]);
	biscuit_lr4_design(&d->ap_low2, p->fc[2]);
	biscuit_lr4_design(&d->ap_mid2, p->fc[2]);
	for (i = 0; i < BISCUIT_BANDS; ++i) {
		struct biscuit_band *b = &d->band[i];
		b->p_fast = b->p_env = 1e-12;
		b->comp_gain = 1.0f;
		b->c_fast = biscuit_group_coef(p->t_fast[i]);
		b->c_attack = biscuit_group_coef(p->t_attack[i]);
		b->c_release = biscuit_group_coef(p->t_release[i]);
		b->c_gain = biscuit_group_coef(p->t_gain[i]);
		biscuit_limiter_init(&b->lim, p->lim_thresh_db[i], p->lim_release_ms[i]);
		b->thresh_db = p->comp_thresh_db[i] - p->det_offset_db[i];
		b->slope = 1.0f - 1.0f / p->comp_ratio[i];
		b->min_gain = powf(10.0f, p->comp_min_db[i] / 20.0f);
	}
	biscuit_limiter_init(&d->full, p->full_thresh_db, p->full_release_ms);
}

static inline void biscuit_dsp_init(struct biscuit_dsp *d, int volume)
{
	biscuit_dsp_init_params(d, volume, &biscuit_dyn_default);
}

/* Apply the bass makeup after biscuit_dsp_init() (which resets it to the
 * stock 0 dB); clamped to 0..6 dB, non-finite values select stock. */
static inline void biscuit_dsp_configure(struct biscuit_dsp *d, float bass_makeup_db)
{
	if (!(bass_makeup_db >= 0.0f))
		bass_makeup_db = 0.0f;
	if (bass_makeup_db > 6.0f)
		bass_makeup_db = 6.0f;
	d->bass_makeup_db = bass_makeup_db;
	d->makeup[0] = d->makeup[1] = powf(10.0f, bass_makeup_db / 20.0f);
}

/* Volume changes only move a broadband gain; ramp it to avoid zipper noise. */
static inline void biscuit_dsp_set_volume(struct biscuit_dsp *d, int volume)
{
	if (volume == d->volume)
		return;
	/* The cascade is fixed; only an unknown<->known transition changes
	 * whether it runs, and that is rare enough to switch directly. */
	if ((volume >= 0) != d->eq_active) {
		float bass_makeup_db = d->bass_makeup_db;

		biscuit_dsp_init(d, volume);
		biscuit_dsp_configure(d, bass_makeup_db);
		return;
	}
	d->volume = volume;
	d->next_eq_gain = biscuit_volume_gain(volume);
	d->ramp = BISCUIT_VOLUME_RAMP_FRAMES;
}

/* Input and output are PCM-scaled floats on the mono programme bus.  The
 * output is held at the full-band limiter ceiling (-0.1 dBFS) and is
 * delayed by 2 * BISCUIT_LA samples of limiter look-ahead. */
static inline float biscuit_dsp_process(struct biscuit_dsp *d, float sample)
{
	double x = sample, lo0, hi0, lo1, hi1, lo2, hi2, bands[BISCUIT_BANDS];
	float sum = 0.0f;
	int i, group_end;

	if (!isfinite(sample))
		return 0.0f;
	if (d->eq_active)
		for (i = 0; i < BISCUIT_EQ_SECTIONS; ++i)
			x = biscuit_step(&d->eq[i], x);
	if (d->ramp > 0) {
		d->eq_gain += (d->next_eq_gain - d->eq_gain) / d->ramp;
		--d->ramp;
	}
	x *= d->eq_gain;

	biscuit_lr4_step(&d->split[0], x, &lo0, &hi0);
	biscuit_lr4_step(&d->split[1], hi0, &lo1, &hi1);
	biscuit_lr4_step(&d->split[2], hi1, &lo2, &hi2);
	bands[0] = biscuit_allpass(&d->ap_low2, biscuit_allpass(&d->ap_low1, lo0));
	bands[1] = biscuit_allpass(&d->ap_mid2, lo1);
	bands[2] = lo2;
	bands[3] = hi2;

	d->group_pos = (d->group_pos + 1u) % BISCUIT_GROUP;
	group_end = d->group_pos == 0u;
	for (i = 0; i < BISCUIT_BANDS; ++i) {
		struct biscuit_band *b = &d->band[i];
		double xn = bands[i] / BISCUIT_PCM;
		float y;
		b->acc += xn * xn;
		if (group_end) {
			double e = b->acc / BISCUIT_GROUP, ldb;
			float target = 1.0f;
			b->acc = 0.0;
			b->p_fast += b->c_fast * (e - b->p_fast);
			b->p_env += (b->p_fast > b->p_env ? b->c_attack : b->c_release) *
				    (b->p_fast - b->p_env);
			ldb = 10.0 * log10(b->p_env + 1e-12);
			if (ldb > b->thresh_db) {
				target = powf(10.0f, -(float)(ldb - b->thresh_db) * b->slope / 20.0f);
				if (target < b->min_gain)
					target = b->min_gain;
			}
			b->comp_gain += b->c_gain * (target - b->comp_gain);
		}
		y = (float)bands[i] * b->comp_gain * d->makeup[i];
		sum += biscuit_limiter_step(&b->lim, y);
	}
	return biscuit_limiter_step(&d->full, sum);
}

#endif /* LIBREECHO_BISCUIT_SPEAKER_DSP_H */
