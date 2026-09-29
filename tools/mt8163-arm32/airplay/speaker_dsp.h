#ifndef LIBREECHO_SPEAKER_DSP_H
#define LIBREECHO_SPEAKER_DSP_H

/*
 * Radar-Puffin speaker processing on the mixed mono programme bus: a
 * volume-indexed loudness approximation, parametric EQ and an independently
 * designed four-band compressor/limiter, before the existing +3 dB trim and
 * final PCM safety guard in puffin_downmix.h.
 *
 * The stock chain orders FIR EQ, parametric EQ, MBCL and OutputTrim. Its
 * filter-bank and compressor detector internals are not specified by the
 * stored configuration, so speaker_mbcl.h matches the documented parameters
 * without claiming sample-exact stock processing.
 *
 * Provenance: the stock device drove these from vendor tuning files
 * (audio-algorithms/EQ_<volume>.cfg and ParametricEQ.cfg).  Those files are not
 * redistributed with LibreEcho and no vendor coefficient table is embedded
 * here.  What this module contains is:
 *
 *   - an EXPERIMENTAL authored replacement for the loudness curve: 26 biquad
 *     sections and an absolute scalar at each first-upper volume boundary.
 *     Fc/Q/gain differ between anchors. This frozen compact design is not
 *     production-promoted and does not establish full-chain stock parity;
 *   - the parametric EQ as its two *active* filter specifications (the stock
 *     file defines eight biquads, six of which are BYPASS), turned into
 *     coefficients here by our own RBJ design code.
 *
 * All coefficients are computed at init on the device.  No vendor-authored
 * coefficient data ships in the artifact.
 */

#include <math.h>
#include <stdint.h>
#include "speaker_mbcl.h"

/* M_PI is a POSIX extension and is not exposed under strict -std=c99. */
#define SPEAKER_DSP_TWO_PI 6.28318530717958647692

/* 26 authored loudness sections, then the unchanged two parametric sections */
#define SPEAKER_DSP_SECTIONS 28
#define SPEAKER_DSP_LOUDNESS_SECTIONS 26
#define SPEAKER_DSP_VOLUME_RAMP_FRAMES 4800

/* Volume steps the loudness ladder is anchored on (stock "Volume Boundary"). */
#define SPEAKER_DSP_STEP_COUNT 5

enum speaker_dsp_shape {
	SPEAKER_SHAPE_LOWSHELF = 0,
	SPEAKER_SHAPE_PEAK = 1,
	SPEAKER_SHAPE_HIGHSHELF = 2
};

struct speaker_dsp_section {
	int shape;
	float fc_hz;
	float q;
	float gain_db;
};

struct speaker_dsp_biquad {
	float b0, b1, b2, a1, a2;
	float z1, z2; /* transposed direct form II state */
};

struct speaker_dsp {
	struct speaker_dsp_biquad sections[SPEAKER_DSP_SECTIONS];
	struct speaker_dsp_biquad next_sections[SPEAKER_DSP_SECTIONS];
	struct speaker_mbcl mbcl;
	float scalar_gain;
	float next_scalar_gain;
	int active;
	int next_active;
	int volume_percent;
	int queued_volume_percent;
	int volume_ramp_remaining;
};

/* Experimental authored design: each boundary has its own Fc/Q/gain
 * topology and absolute scalar. Frozen inputs: speaker_eq26_design.json.
 * These are design parameters, not stock coefficients or sampled spectra. */
static const struct speaker_dsp_section speaker_loudness_sections[SPEAKER_DSP_STEP_COUNT][SPEAKER_DSP_LOUDNESS_SECTIONS] = {
	{ /* volume 50 */
		{ SPEAKER_SHAPE_LOWSHELF, 164.82f, 1.5000f, 12.270f },
		{ SPEAKER_SHAPE_HIGHSHELF, 1480.02f, 1.5000f, 7.770f },
		{ SPEAKER_SHAPE_PEAK, 8596.52f, 7.5561f, -12.135f },
		{ SPEAKER_SHAPE_PEAK, 2264.50f, 2.2506f, -15.962f },
		{ SPEAKER_SHAPE_PEAK, 13054.63f, 3.0883f, -9.853f },
		{ SPEAKER_SHAPE_PEAK, 5949.79f, 1.4313f, -15.902f },
		{ SPEAKER_SHAPE_PEAK, 3927.23f, 15.8319f, -7.634f },
		{ SPEAKER_SHAPE_PEAK, 590.88f, 4.8539f, -6.955f },
		{ SPEAKER_SHAPE_PEAK, 20131.19f, 2.6450f, -24.000f },
		{ SPEAKER_SHAPE_PEAK, 724.43f, 11.1484f, -4.666f },
		{ SPEAKER_SHAPE_PEAK, 171.42f, 0.7074f, -18.006f },
		{ SPEAKER_SHAPE_PEAK, 420.85f, 7.1323f, -3.621f },
		{ SPEAKER_SHAPE_PEAK, 1386.07f, 4.3791f, 8.104f },
		{ SPEAKER_SHAPE_PEAK, 9582.68f, 6.3663f, -7.993f },
		{ SPEAKER_SHAPE_PEAK, 14546.27f, 5.5444f, -7.882f },
		{ SPEAKER_SHAPE_PEAK, 11251.19f, 8.6245f, -4.798f },
		{ SPEAKER_SHAPE_PEAK, 2834.16f, 8.6900f, 2.758f },
		{ SPEAKER_SHAPE_PEAK, 17.51f, 0.4283f, -15.968f },
		{ SPEAKER_SHAPE_PEAK, 266.10f, 4.8876f, 4.262f },
		{ SPEAKER_SHAPE_PEAK, 22000.00f, 0.2106f, -13.453f },
		{ SPEAKER_SHAPE_PEAK, 1138.94f, 19.8546f, 3.121f },
		{ SPEAKER_SHAPE_PEAK, 1951.91f, 15.9563f, -4.496f },
		{ SPEAKER_SHAPE_PEAK, 889.55f, 17.2960f, 1.753f },
		{ SPEAKER_SHAPE_PEAK, 1493.18f, 1.3044f, -19.161f },
		{ SPEAKER_SHAPE_PEAK, 1764.18f, 25.0909f, -2.887f },
		{ SPEAKER_SHAPE_PEAK, 20768.31f, 1.6407f, 11.768f },
	},
	{ /* volume 60 */
		{ SPEAKER_SHAPE_LOWSHELF, 171.27f, 1.5000f, 11.855f },
		{ SPEAKER_SHAPE_HIGHSHELF, 2717.83f, 1.5000f, 5.258f },
		{ SPEAKER_SHAPE_PEAK, 8596.24f, 7.8207f, -11.887f },
		{ SPEAKER_SHAPE_PEAK, 1986.27f, 1.5950f, -23.591f },
		{ SPEAKER_SHAPE_PEAK, 13067.31f, 3.1983f, -9.133f },
		{ SPEAKER_SHAPE_PEAK, 5936.41f, 1.5583f, -14.303f },
		{ SPEAKER_SHAPE_PEAK, 3927.36f, 15.4573f, -7.732f },
		{ SPEAKER_SHAPE_PEAK, 591.53f, 4.0814f, -8.333f },
		{ SPEAKER_SHAPE_PEAK, 20000.48f, 3.9839f, -15.278f },
		{ SPEAKER_SHAPE_PEAK, 727.45f, 8.7704f, -5.998f },
		{ SPEAKER_SHAPE_PEAK, 174.18f, 0.6753f, -18.498f },
		{ SPEAKER_SHAPE_PEAK, 421.33f, 6.6343f, -3.887f },
		{ SPEAKER_SHAPE_PEAK, 1357.66f, 6.6417f, 3.382f },
		{ SPEAKER_SHAPE_PEAK, 9572.86f, 6.5430f, -7.695f },
		{ SPEAKER_SHAPE_PEAK, 14546.93f, 5.6949f, -7.610f },
		{ SPEAKER_SHAPE_PEAK, 11253.21f, 9.1844f, -4.475f },
		{ SPEAKER_SHAPE_PEAK, 2809.91f, 5.6601f, 4.453f },
		{ SPEAKER_SHAPE_PEAK, 1119.86f, 2.5112f, -8.168f },
		{ SPEAKER_SHAPE_PEAK, 266.01f, 4.6378f, 4.529f },
		{ SPEAKER_SHAPE_PEAK, 1854.61f, 11.9521f, 6.654f },
		{ SPEAKER_SHAPE_PEAK, 15.00f, 0.2712f, -17.989f },
		{ SPEAKER_SHAPE_PEAK, 21603.74f, 0.2000f, -10.607f },
		{ SPEAKER_SHAPE_PEAK, 1136.37f, 11.3424f, 4.937f },
		{ SPEAKER_SHAPE_PEAK, 1688.61f, 27.3988f, 3.343f },
		{ SPEAKER_SHAPE_PEAK, 2050.81f, 7.8418f, 8.414f },
		{ SPEAKER_SHAPE_PEAK, 833.42f, 27.2547f, -2.243f },
	},
	{ /* volume 70 */
		{ SPEAKER_SHAPE_LOWSHELF, 143.06f, 1.5000f, 5.458f },
		{ SPEAKER_SHAPE_HIGHSHELF, 41.25f, 0.3112f, 24.000f },
		{ SPEAKER_SHAPE_PEAK, 8593.37f, 7.8870f, -11.637f },
		{ SPEAKER_SHAPE_PEAK, 2269.02f, 3.4548f, -7.981f },
		{ SPEAKER_SHAPE_PEAK, 590.47f, 5.5470f, -6.359f },
		{ SPEAKER_SHAPE_PEAK, 13079.09f, 3.5796f, -7.643f },
		{ SPEAKER_SHAPE_PEAK, 6001.22f, 2.0582f, -10.678f },
		{ SPEAKER_SHAPE_PEAK, 3922.79f, 17.7792f, -5.686f },
		{ SPEAKER_SHAPE_PEAK, 20050.33f, 5.5163f, -14.163f },
		{ SPEAKER_SHAPE_PEAK, 726.12f, 9.5852f, -4.387f },
		{ SPEAKER_SHAPE_PEAK, 227.73f, 0.6923f, -15.918f },
		{ SPEAKER_SHAPE_PEAK, 420.62f, 7.8129f, -3.375f },
		{ SPEAKER_SHAPE_PEAK, 9564.00f, 6.5866f, -7.129f },
		{ SPEAKER_SHAPE_PEAK, 1358.93f, 4.3418f, 7.780f },
		{ SPEAKER_SHAPE_PEAK, 14521.14f, 6.1834f, -6.539f },
		{ SPEAKER_SHAPE_PEAK, 2763.58f, 6.2425f, 3.059f },
		{ SPEAKER_SHAPE_PEAK, 11254.11f, 9.6143f, -3.920f },
		{ SPEAKER_SHAPE_PEAK, 889.96f, 15.7534f, 1.927f },
		{ SPEAKER_SHAPE_PEAK, 1139.77f, 17.0450f, 3.388f },
		{ SPEAKER_SHAPE_PEAK, 1953.44f, 19.9948f, -3.749f },
		{ SPEAKER_SHAPE_PEAK, 264.17f, 4.1391f, 4.822f },
		{ SPEAKER_SHAPE_PEAK, 790.21f, 32.6319f, 1.476f },
		{ SPEAKER_SHAPE_PEAK, 21380.11f, 0.2000f, -11.068f },
		{ SPEAKER_SHAPE_PEAK, 1357.65f, 0.7656f, -22.313f },
		{ SPEAKER_SHAPE_PEAK, 1759.92f, 27.4060f, -2.488f },
		{ SPEAKER_SHAPE_PEAK, 1545.87f, 38.2118f, -2.268f },
	},
	{ /* volume 80 */
		{ SPEAKER_SHAPE_LOWSHELF, 141.50f, 1.3328f, 6.213f },
		{ SPEAKER_SHAPE_HIGHSHELF, 1432.41f, 1.5000f, 11.213f },
		{ SPEAKER_SHAPE_PEAK, 8577.84f, 10.7872f, -9.340f },
		{ SPEAKER_SHAPE_PEAK, 2284.34f, 1.2884f, -22.847f },
		{ SPEAKER_SHAPE_PEAK, 589.26f, 7.6559f, -5.081f },
		{ SPEAKER_SHAPE_PEAK, 725.42f, 14.2489f, -2.995f },
		{ SPEAKER_SHAPE_PEAK, 221.09f, 0.7542f, -24.000f },
		{ SPEAKER_SHAPE_PEAK, 12702.27f, 2.6285f, -6.594f },
		{ SPEAKER_SHAPE_PEAK, 6028.19f, 2.7068f, -6.210f },
		{ SPEAKER_SHAPE_PEAK, 3921.26f, 20.4843f, -2.922f },
		{ SPEAKER_SHAPE_PEAK, 20084.98f, 8.7576f, -14.196f },
		{ SPEAKER_SHAPE_PEAK, 414.35f, 5.1992f, -4.418f },
		{ SPEAKER_SHAPE_PEAK, 1358.78f, 9.7835f, 2.268f },
		{ SPEAKER_SHAPE_PEAK, 9431.78f, 6.1624f, -5.166f },
		{ SPEAKER_SHAPE_PEAK, 902.61f, 4.4473f, 4.287f },
		{ SPEAKER_SHAPE_PEAK, 1136.08f, 17.2917f, 3.259f },
		{ SPEAKER_SHAPE_PEAK, 2646.96f, 2.8478f, 6.860f },
		{ SPEAKER_SHAPE_PEAK, 1949.22f, 17.2043f, -4.244f },
		{ SPEAKER_SHAPE_PEAK, 315.19f, 4.4680f, -4.405f },
		{ SPEAKER_SHAPE_PEAK, 791.10f, 26.3449f, 1.976f },
		{ SPEAKER_SHAPE_PEAK, 21426.96f, 0.2000f, -7.366f },
		{ SPEAKER_SHAPE_PEAK, 14502.90f, 8.0022f, -3.827f },
		{ SPEAKER_SHAPE_PEAK, 1569.80f, 7.2333f, -3.795f },
		{ SPEAKER_SHAPE_PEAK, 1760.99f, 19.1273f, -3.633f },
		{ SPEAKER_SHAPE_PEAK, 264.73f, 0.5302f, 24.000f },
		{ SPEAKER_SHAPE_PEAK, 12247.19f, 8.5089f, 3.545f },
	},
	{ /* volume 100 */
		{ SPEAKER_SHAPE_LOWSHELF, 162.55f, 1.0810f, 5.402f },
		{ SPEAKER_SHAPE_HIGHSHELF, 1559.91f, 1.5000f, 6.459f },
		{ SPEAKER_SHAPE_PEAK, 8574.89f, 11.4880f, -8.794f },
		{ SPEAKER_SHAPE_PEAK, 2241.14f, 2.5073f, -11.940f },
		{ SPEAKER_SHAPE_PEAK, 591.08f, 8.0721f, -5.070f },
		{ SPEAKER_SHAPE_PEAK, 222.45f, 0.7945f, -22.812f },
		{ SPEAKER_SHAPE_PEAK, 725.27f, 12.2023f, -3.381f },
		{ SPEAKER_SHAPE_PEAK, 12394.31f, 2.5470f, -11.582f },
		{ SPEAKER_SHAPE_PEAK, 6033.12f, 2.9449f, -5.613f },
		{ SPEAKER_SHAPE_PEAK, 3919.72f, 18.3761f, -3.038f },
		{ SPEAKER_SHAPE_PEAK, 20083.69f, 7.7380f, -14.110f },
		{ SPEAKER_SHAPE_PEAK, 414.81f, 6.7688f, -3.604f },
		{ SPEAKER_SHAPE_PEAK, 1360.34f, 9.5987f, 2.463f },
		{ SPEAKER_SHAPE_PEAK, 9508.26f, 2.6052f, -18.172f },
		{ SPEAKER_SHAPE_PEAK, 891.68f, 9.6548f, 2.658f },
		{ SPEAKER_SHAPE_PEAK, 1137.99f, 22.2817f, 2.858f },
		{ SPEAKER_SHAPE_PEAK, 2687.41f, 5.5463f, 3.397f },
		{ SPEAKER_SHAPE_PEAK, 1947.84f, 15.5628f, -4.765f },
		{ SPEAKER_SHAPE_PEAK, 313.06f, 4.5589f, -3.896f },
		{ SPEAKER_SHAPE_PEAK, 791.65f, 25.4900f, 1.954f },
		{ SPEAKER_SHAPE_PEAK, 14498.56f, 8.9146f, -3.527f },
		{ SPEAKER_SHAPE_PEAK, 1575.84f, 6.7208f, -4.114f },
		{ SPEAKER_SHAPE_PEAK, 1762.61f, 17.3139f, -4.065f },
		{ SPEAKER_SHAPE_PEAK, 265.81f, 0.6307f, 24.000f },
		{ SPEAKER_SHAPE_PEAK, 9726.59f, 1.4253f, 17.843f },
		{ SPEAKER_SHAPE_PEAK, 12226.14f, 5.8990f, 7.473f },
	},
};

static const float speaker_loudness_volume[SPEAKER_DSP_STEP_COUNT] = {
	50.0f, 60.0f, 70.0f, 80.0f, 100.0f
};

static const float speaker_loudness_scalar_db[SPEAKER_DSP_STEP_COUNT] = {
	7.680f, 8.508f, -11.068f, -2.734f, -4.987f
};

/*
 * Parametric EQ: the stock file's only two non-BYPASS entries are a 150 Hz
 * Q 0.9 +5 dB low shelf and an 80 Hz Q 0.9 +2 dB peak.  The stock designer's
 * shelf convention differs from the RBJ cookbook, so a literal RBJ reading of
 * those numbers places the shelf turnover too low (up to 1.2 dB short around
 * 200 Hz).  These are the effective RBJ parameters fitted to an offline
 * measurement of the stock stage; they are our own design inputs, not
 * coefficients.
 */
static const struct speaker_dsp_section speaker_parametric_eq[] = {
	{ SPEAKER_SHAPE_LOWSHELF, 177.0f, 0.72f, +4.8f },
	{ SPEAKER_SHAPE_PEAK,      81.0f, 0.85f, +2.0f }
};

static inline void speaker_biquad_design(struct speaker_dsp_biquad *bq,
					 const struct speaker_dsp_section *s)
{
	/* Design in double to avoid cancellation at low Fc/high Q; only the
	 * normalized coefficients and per-sample state use float. */
	double a = pow(10.0, s->gain_db / 40.0);
	double w = SPEAKER_DSP_TWO_PI * s->fc_hz / 48000.0;
	double cw = cos(w);
	double sw = sin(w);
	double alpha = sw / (2.0 * s->q);
	double b0, b1, b2, a0, a1, a2;

	if (s->shape == SPEAKER_SHAPE_LOWSHELF) {
		double sq = 2.0 * sqrt(a) * alpha;
		b0 = a * ((a + 1.0) - (a - 1.0) * cw + sq);
		b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cw);
		b2 = a * ((a + 1.0) - (a - 1.0) * cw - sq);
		a0 = (a + 1.0) + (a - 1.0) * cw + sq;
		a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cw);
		a2 = (a + 1.0) + (a - 1.0) * cw - sq;
	} else if (s->shape == SPEAKER_SHAPE_HIGHSHELF) {
		double sq = 2.0 * sqrt(a) * alpha;
		b0 = a * ((a + 1.0) + (a - 1.0) * cw + sq);
		b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cw);
		b2 = a * ((a + 1.0) + (a - 1.0) * cw - sq);
		a0 = (a + 1.0) - (a - 1.0) * cw + sq;
		a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cw);
		a2 = (a + 1.0) - (a - 1.0) * cw - sq;
	} else {
		b0 = 1.0 + alpha * a;
		b1 = -2.0 * cw;
		b2 = 1.0 - alpha * a;
		a0 = 1.0 + alpha / a;
		a1 = -2.0 * cw;
		a2 = 1.0 - alpha / a;
	}
	bq->b0 = b0 / a0;
	bq->b1 = b1 / a0;
	bq->b2 = b2 / a0;
	bq->a1 = a1 / a0;
	bq->a2 = a2 / a0;
	bq->z1 = 0.0f;
	bq->z2 = 0.0f;
}

/* Select the first configured upper boundary, without interpolating presets. */
static inline int speaker_dsp_loudness_index(float volume)
{
	int i;

	for (i = 0; i < SPEAKER_DSP_STEP_COUNT - 1; ++i)
		if (volume <= speaker_loudness_volume[i])
			break;
	return i;
}

static inline void speaker_dsp_init(struct speaker_dsp *dsp, int volume_percent)
{
	int i, anchor;

	for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i) {
		dsp->sections[i].b0 = 1.0f;
		dsp->sections[i].b1 = 0.0f;
		dsp->sections[i].b2 = 0.0f;
		dsp->sections[i].a1 = 0.0f;
		dsp->sections[i].a2 = 0.0f;
		dsp->sections[i].z1 = 0.0f;
		dsp->sections[i].z2 = 0.0f;
	}
	dsp->active = 0;
	dsp->next_active = 0;
	dsp->scalar_gain = 1.0f;
	dsp->next_scalar_gain = 1.0f;
	dsp->volume_percent = volume_percent;
	dsp->queued_volume_percent = -2; /* -1 is a valid unknown level. */
	dsp->volume_ramp_remaining = 0;
	speaker_mbcl_init(&dsp->mbcl);

	/* Unknown volume bypasses the loudness EQ, but never the common output
	 * protection: all sources still pass through the same MBCL. */
	if (volume_percent < 0)
		return;

	anchor = speaker_dsp_loudness_index((float)volume_percent);
	dsp->scalar_gain = powf(10.0f, speaker_loudness_scalar_db[anchor] / 20.0f);
	for (i = 0; i < SPEAKER_DSP_LOUDNESS_SECTIONS; ++i)
		speaker_biquad_design(&dsp->sections[i], &speaker_loudness_sections[anchor][i]);
	for (i = 0; i < (int)(sizeof(speaker_parametric_eq) /
			      sizeof(speaker_parametric_eq[0])); ++i)
		speaker_biquad_design(&dsp->sections[SPEAKER_DSP_LOUDNESS_SECTIONS + i],
				      &speaker_parametric_eq[i]);
	dsp->active = 1;
}

/* Run both stable cascades during a volume change.  Blending their outputs
 * leaves the filter history intact and avoids resetting the shared dynamics
 * while AirPlay updates its phone volume in a single open PCM stream. */
static inline void speaker_dsp_set_volume(struct speaker_dsp *dsp,
					  int volume_percent)
{
	struct speaker_dsp next;
	int i;

	if (dsp->volume_ramp_remaining > 0) {
		/* Do not discard a half-complete blend: queue the latest callback
		 * and start its transition from the just-completed target. */
		dsp->queued_volume_percent =
			volume_percent == dsp->volume_percent ? -2 : volume_percent;
		return;
	}
	if (dsp->volume_percent == volume_percent)
		return;
	speaker_dsp_init(&next, volume_percent);
	for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i)
		dsp->next_sections[i] = next.sections[i];
	dsp->next_active = next.active;
	dsp->next_scalar_gain = next.scalar_gain;
	dsp->volume_percent = volume_percent;
	dsp->queued_volume_percent = -2;
	dsp->volume_ramp_remaining = SPEAKER_DSP_VOLUME_RAMP_FRAMES;
}

static inline float speaker_dsp_step(struct speaker_dsp_biquad *bq, float x)
{
	float y = bq->b0 * x + bq->z1;

	bq->z1 = bq->b1 * x - bq->a1 * y + bq->z2;
	bq->z2 = bq->b2 * x - bq->a2 * y;
	return y;
}

/* Equalise on the wide mono bus; no S16 conversion happens before MBCL. */
static inline float speaker_dsp_equalize(struct speaker_dsp *dsp, int32_t sample)
{
	float x = (float)sample;
	int i;

	if (dsp->active) {
		for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i)
			x = speaker_dsp_step(&dsp->sections[i], x);
		x *= dsp->scalar_gain;
	}
	if (dsp->volume_ramp_remaining > 0) {
		float next = (float)sample;
		float fraction = 1.0f - (float)dsp->volume_ramp_remaining /
			(float)SPEAKER_DSP_VOLUME_RAMP_FRAMES;

		if (dsp->next_active) {
			for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i)
				next = speaker_dsp_step(&dsp->next_sections[i], next);
			next *= dsp->next_scalar_gain;
		}
		x += (next - x) * fraction;
		if (--dsp->volume_ramp_remaining == 0) {
			int queued = dsp->queued_volume_percent;
			for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i)
				dsp->sections[i] = dsp->next_sections[i];
			dsp->active = dsp->next_active;
			dsp->scalar_gain = dsp->next_scalar_gain;
			dsp->queued_volume_percent = -2;
			if (queued != -2 && queued != dsp->volume_percent)
				speaker_dsp_set_volume(dsp, queued);
		}
	}
	return x;
}

/* Stock-like multiband protection acts once on the mixed programme, before
 * OutputTrim.  The later linked limiter remains an emergency PCM guard. */
static inline int32_t speaker_dsp_process(struct speaker_dsp *dsp, int32_t sample)
{
	float x = speaker_dsp_equalize(dsp, sample);

	x = speaker_mbcl_process(&dsp->mbcl, x);
	/* This guard is for pathological filter state, never normal EQ overshoot. */
	if (x > (float)(INT32_MAX / 4))
		x = (float)(INT32_MAX / 4);
	else if (x < (float)(-INT32_MAX / 4))
		x = (float)(-INT32_MAX / 4);
	return (int32_t)lrintf(x);
}

#endif /* LIBREECHO_SPEAKER_DSP_H */
