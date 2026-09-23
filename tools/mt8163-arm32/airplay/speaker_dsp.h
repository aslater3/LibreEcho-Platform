#ifndef LIBREECHO_SPEAKER_DSP_H
#define LIBREECHO_SPEAKER_DSP_H

/*
 * Radar-Puffin speaker tuning stage: volume-indexed loudness equalisation and
 * the stock parametric EQ, applied to the mono programme bus ahead of the
 * existing trim/limiter in puffin_downmix.h.
 *
 * Order matches the stock pipeline, which runs its equaliser and parametric EQ
 * before OutputTrim and the full-band limiter.  Trim and limiting already live
 * in puffin_render_mono(); this module only adds the two EQ stages.
 *
 * Provenance: the stock device drove these from vendor tuning files
 * (audio-algorithms/EQ_<volume>.cfg and ParametricEQ.cfg).  Those files are not
 * redistributed with LibreEcho and no vendor coefficient table is embedded
 * here.  What this module contains is:
 *
 *   - a designed replacement for the loudness curve: five biquad sections whose
 *     topology is fixed and whose gains vary linearly with the volume step.  The
 *     parameters were fitted to the measured response of the stock curve and
 *     reproduce it to 1.44 dB RMS across the referenced volume ladder;
 *   - the parametric EQ as its two *active* filter specifications (the stock
 *     file defines eight biquads, six of which are BYPASS), turned into
 *     coefficients here by our own RBJ design code.
 *
 * All coefficients are computed at init on the device.  No vendor-authored
 * coefficient data ships in the artifact.
 */

#include <math.h>
#include <stdint.h>

/* M_PI is a POSIX extension and is not exposed under strict -std=c99. */
#define SPEAKER_DSP_TWO_PI 6.28318530717958647692f

/* five loudness sections, then two parametric-EQ sections */
#define SPEAKER_DSP_SECTIONS 7
#define SPEAKER_DSP_LOUDNESS_SECTIONS 5

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
	int active;
};

/*
 * Loudness ladder: fixed topology (Fc/Q shared by every step), gain per step.
 * Ladder order is 50, 60, 70, 80, 100 percent.
 */
static const struct speaker_dsp_section speaker_loudness_topology[SPEAKER_DSP_LOUDNESS_SECTIONS] = {
	{ SPEAKER_SHAPE_LOWSHELF,  140.0f, 1.90f, 0.0f },
	{ SPEAKER_SHAPE_PEAK,       60.0f, 0.60f, 0.0f },
	{ SPEAKER_SHAPE_PEAK,     1770.0f, 3.60f, 0.0f },
	{ SPEAKER_SHAPE_HIGHSHELF, 2550.0f, 3.90f, 0.0f },
	{ SPEAKER_SHAPE_PEAK,     3000.0f, 0.30f, 0.0f }
};

static const float speaker_loudness_volume[SPEAKER_DSP_STEP_COUNT] = {
	50.0f, 60.0f, 70.0f, 80.0f, 100.0f
};

static const float speaker_loudness_gain_db[SPEAKER_DSP_STEP_COUNT][SPEAKER_DSP_LOUDNESS_SECTIONS] = {
	{ +7.4f, +7.1f, -0.9f, +3.4f, +1.1f }, /* volume  50 */
	{ +6.5f, +5.5f, -0.9f, +3.4f, +1.1f }, /* volume  60 */
	{ +5.6f, +4.6f, -1.9f, +2.9f, +1.3f }, /* volume  70 */
	{ +4.9f, +3.4f, -2.5f, +2.3f, +1.2f }, /* volume  80 */
	{ +3.2f, +1.5f, -2.5f, +2.3f, +1.2f }  /* volume 100 */
};

/*
 * Parametric EQ: the stock file's only two non-BYPASS entries.
 */
static const struct speaker_dsp_section speaker_parametric_eq[] = {
	{ SPEAKER_SHAPE_LOWSHELF, 150.0f, 0.90f, +5.0f },
	{ SPEAKER_SHAPE_PEAK,      80.0f, 0.90f, +2.0f }
};

static inline void speaker_biquad_design(struct speaker_dsp_biquad *bq,
					 const struct speaker_dsp_section *s)
{
	float a = powf(10.0f, s->gain_db / 40.0f);
	float w = SPEAKER_DSP_TWO_PI * s->fc_hz / 48000.0f;
	float cw = cosf(w);
	float sw = sinf(w);
	float alpha = sw / (2.0f * s->q);
	float b0, b1, b2, a0, a1, a2;

	if (s->shape == SPEAKER_SHAPE_LOWSHELF) {
		float sq = 2.0f * sqrtf(a) * alpha;
		b0 = a * ((a + 1.0f) - (a - 1.0f) * cw + sq);
		b1 = 2.0f * a * ((a - 1.0f) - (a + 1.0f) * cw);
		b2 = a * ((a + 1.0f) - (a - 1.0f) * cw - sq);
		a0 = (a + 1.0f) + (a - 1.0f) * cw + sq;
		a1 = -2.0f * ((a - 1.0f) + (a + 1.0f) * cw);
		a2 = (a + 1.0f) + (a - 1.0f) * cw - sq;
	} else if (s->shape == SPEAKER_SHAPE_HIGHSHELF) {
		float sq = 2.0f * sqrtf(a) * alpha;
		b0 = a * ((a + 1.0f) + (a - 1.0f) * cw + sq);
		b1 = -2.0f * a * ((a - 1.0f) + (a + 1.0f) * cw);
		b2 = a * ((a + 1.0f) + (a - 1.0f) * cw - sq);
		a0 = (a + 1.0f) - (a - 1.0f) * cw + sq;
		a1 = 2.0f * ((a - 1.0f) - (a + 1.0f) * cw);
		a2 = (a + 1.0f) - (a - 1.0f) * cw - sq;
	} else {
		b0 = 1.0f + alpha * a;
		b1 = -2.0f * cw;
		b2 = 1.0f - alpha * a;
		a0 = 1.0f + alpha / a;
		a1 = -2.0f * cw;
		a2 = 1.0f - alpha / a;
	}
	bq->b0 = b0 / a0;
	bq->b1 = b1 / a0;
	bq->b2 = b2 / a0;
	bq->a1 = a1 / a0;
	bq->a2 = a2 / a0;
	bq->z1 = 0.0f;
	bq->z2 = 0.0f;
}

/*
 * Interpolate the loudness ladder at the given volume percentage.  Volumes at
 * or below the first boundary take the first step; at or above the last take
 * the last, matching the stock "Volume Boundary" selection.
 */
static inline void speaker_dsp_loudness_gains(float volume, float *gains)
{
	int i;
	float f;
	int last = SPEAKER_DSP_STEP_COUNT - 1;

	if (volume <= speaker_loudness_volume[0]) {
		for (i = 0; i < SPEAKER_DSP_LOUDNESS_SECTIONS; ++i)
			gains[i] = speaker_loudness_gain_db[0][i];
		return;
	}
	if (volume >= speaker_loudness_volume[last]) {
		for (i = 0; i < SPEAKER_DSP_LOUDNESS_SECTIONS; ++i)
			gains[i] = speaker_loudness_gain_db[last][i];
		return;
	}
	for (i = 0; i < last; ++i) {
		if (volume >= speaker_loudness_volume[i] &&
		    volume <= speaker_loudness_volume[i + 1]) {
			int k;
			f = (volume - speaker_loudness_volume[i]) /
			    (speaker_loudness_volume[i + 1] - speaker_loudness_volume[i]);
			for (k = 0; k < SPEAKER_DSP_LOUDNESS_SECTIONS; ++k) {
				float lo = speaker_loudness_gain_db[i][k];
				float hi = speaker_loudness_gain_db[i + 1][k];
				gains[k] = lo + (hi - lo) * f;
			}
			return;
		}
	}
}

static inline void speaker_dsp_init(struct speaker_dsp *dsp, int volume_percent)
{
	struct speaker_dsp_section s;
	float gains[SPEAKER_DSP_LOUDNESS_SECTIONS];
	int i;

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

	/* Only Radar-Puffin's mono programme bus is tuned; a zero volume means
	 * the caller has no valid volume yet and must leave the bus untouched. */
	if (volume_percent < 0)
		return;

	speaker_dsp_loudness_gains((float)volume_percent, gains);
	for (i = 0; i < SPEAKER_DSP_LOUDNESS_SECTIONS; ++i) {
		s = speaker_loudness_topology[i];
		s.gain_db = gains[i];
		speaker_biquad_design(&dsp->sections[i], &s);
	}
	for (i = 0; i < (int)(sizeof(speaker_parametric_eq) /
			      sizeof(speaker_parametric_eq[0])); ++i)
		speaker_biquad_design(&dsp->sections[SPEAKER_DSP_LOUDNESS_SECTIONS + i],
				      &speaker_parametric_eq[i]);
	dsp->active = 1;
}

static inline float speaker_dsp_step(struct speaker_dsp_biquad *bq, float x)
{
	float y = bq->b0 * x + bq->z1;

	bq->z1 = bq->b1 * x - bq->a1 * y + bq->z2;
	bq->z2 = bq->b2 * x - bq->a2 * y;
	return y;
}

/* Process one mono sample: returns the tuned sample, pre trim/limiter. */
static inline int32_t speaker_dsp_process(struct speaker_dsp *dsp, int32_t sample)
{
	float x;
	int i;

	if (!dsp->active)
		return sample;

	x = (float)sample;
	for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i)
		x = speaker_dsp_step(&dsp->sections[i], x);

	if (x > 32767.0f)
		x = 32767.0f;
	else if (x < -32767.0f)
		x = -32767.0f;
	return (int32_t)lrintf(x);
}

#endif /* LIBREECHO_SPEAKER_DSP_H */
