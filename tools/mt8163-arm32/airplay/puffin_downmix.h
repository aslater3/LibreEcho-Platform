#ifndef LIBREECHO_PUFFIN_DOWNMIX_H
#define LIBREECHO_PUFFIN_DOWNMIX_H

#include <stddef.h>
#include <stdint.h>

#define PUFFIN_OUTPUT_TRIM_Q15 46341
#define PUFFIN_OUTPUT_CEILING 32767
#define PUFFIN_LIMITER_RELEASE_SHIFT 10

struct puffin_dynamics {
	int32_t gain_q15;
};

static inline void puffin_dynamics_init(struct puffin_dynamics *dynamics)
{
	dynamics->gain_q15 = PUFFIN_OUTPUT_TRIM_Q15;
}

/* Shared gain law: instantaneous attack, ~21 ms release toward the trim. */
static inline void puffin_dynamics_track(struct puffin_dynamics *dynamics,
					 int64_t magnitude)
{
	int32_t target_gain = PUFFIN_OUTPUT_TRIM_Q15;

	if (magnitude > 0) {
		int32_t limited_gain = (int32_t)(
			((int64_t)PUFFIN_OUTPUT_CEILING << 15) / magnitude);

		if (limited_gain < target_gain)
			target_gain = limited_gain;
	}
	if (target_gain < dynamics->gain_q15) {
		dynamics->gain_q15 = target_gain;
	} else if (dynamics->gain_q15 < target_gain) {
		dynamics->gain_q15 +=
			(target_gain - dynamics->gain_q15 +
			 ((1 << PUFFIN_LIMITER_RELEASE_SHIFT) - 1)) >>
			PUFFIN_LIMITER_RELEASE_SHIFT;
	}
}

/* 24-bit output path: same limiter, result kept in fractional S16 units. */
static inline float puffin_render_mono_f(struct puffin_dynamics *dynamics,
					 float mixed)
{
	float magnitude = mixed < 0.0f ? -mixed : mixed;
	float mono;

	puffin_dynamics_track(dynamics, (int64_t)(magnitude + 0.5f));
	mono = mixed * ((float)dynamics->gain_q15 / 32768.0f);
	if (mono > (float)PUFFIN_OUTPUT_CEILING)
		mono = (float)PUFFIN_OUTPUT_CEILING;
	if (mono < -(float)PUFFIN_OUTPUT_CEILING)
		mono = -(float)PUFFIN_OUTPUT_CEILING;
	return mono;
}

static inline int16_t puffin_render_mono(struct puffin_dynamics *dynamics,
					int32_t mixed)
{
	int64_t magnitude = mixed < 0 ? -(int64_t)mixed : mixed;
	int32_t mono;

	puffin_dynamics_track(dynamics, magnitude);
	mono = (int32_t)(((int64_t)mixed * dynamics->gain_q15) >> 15);
	if (mono > PUFFIN_OUTPUT_CEILING)
		mono = PUFFIN_OUTPUT_CEILING;
	if (mono < -PUFFIN_OUTPUT_CEILING)
		mono = -PUFFIN_OUTPUT_CEILING;
	return (int16_t)mono;
}

/*
 * Puffin's codec profile treats the two PCM channels as physical speaker
 * bands: left/HPL is the tweeter high-pass and right/HPR is the woofer
 * low-pass.  Convert stereo programme material to a mono speaker bus before
 * those channel-specific filters.  Averaging in 32 bits prevents overflow
 * and leaves 6 dB of headroom when only one programme channel is active.
 *
 * The stock Radar pipeline applies +3 dB OutputTrim after its protected
 * speaker processing.  Reproduce that trim here, but constrain it with a
 * linked, instantaneous-attack limiter.  Its roughly 21 ms release matches
 * the stock full-band limiter and prevents positive codec gain or PCM
 * clipping while restoring loudness to material with available headroom.
 */
static inline void puffin_downmix_stereo(struct puffin_dynamics *dynamics,
					 int16_t *samples, size_t frames)
{
	size_t frame;

	for (frame = 0; frame < frames; ++frame) {
		int32_t mixed = (int32_t)samples[frame * 2] +
				(int32_t)samples[frame * 2 + 1];
		int16_t mono;

		mixed /= 2;
		mono = puffin_render_mono(dynamics, mixed);

		samples[frame * 2] = mono;
		samples[frame * 2 + 1] = mono;
	}
}

#endif
