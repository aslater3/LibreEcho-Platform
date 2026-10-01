#ifndef LIBREECHO_MUSIC_FEATURES_H
#define LIBREECHO_MUSIC_FEATURES_H

/*
 * Portable musical feature producer for the Radar Puffin LED pipeline.
 *
 * This unit turns the twelve normalized band levels already produced by
 * audio_visualizer.c into the frozen version-2 visualizer feature frame that
 * LibreEcho-UI consumes (UI #64/#65, Platform #44/#45).  It is deliberately
 * header-only and dependency-free, matching the existing speaker_dsp.h /
 * speaker_mbcl.h / audio_period_buffer.h units, so it needs no build-script
 * change to be adopted.
 *
 * Everything here is integer fixed point.  There is no floating point, no
 * allocation, no wall-clock access and no randomness, so a synthetic PCM
 * stream always yields the identical feature sequence on every host and on
 * the target.  The analysis is not a claim of exact instrument recognition:
 * it publishes bounded perceptual evidence (loudness, balance, density,
 * differentiated low/mid/high onset energy, a tempo hypothesis with decaying
 * confidence, and confidence-gated structural events).
 *
 * Units and reset semantics are documented in music-features.md.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#define MUSIC_FEATURES_VERSION 2U
#define MUSIC_FEATURES_BANDS 12U
#define MUSIC_FEATURES_BAND_GROUPS 3U

/* One analysis update per PCM period.  The engine uses 2048-frame periods of
 * 48 kHz stereo; the constants are parameters of music_features_update() so
 * the unit can be exercised at other rates in tests. */
#define MUSIC_FEATURES_DEFAULT_FRAMES 2048U
#define MUSIC_FEATURES_DEFAULT_RATE 48000U

/* Onset envelope history and the autocorrelation lag search.  At 2048 frames
 * per update the update rate is 23.4375 Hz, so lags 6..32 span roughly
 * 44..234 BPM.  The history is a fixed ring: bounded memory, no growth. */
#define MUSIC_FEATURES_ONSET_HISTORY 128U
#define MUSIC_FEATURES_MIN_LAG 6U
#define MUSIC_FEATURES_MAX_LAG 32U
/* Slow-axis delay used to measure the multi-second loudness trend. */
#define MUSIC_FEATURES_SLOW_HISTORY 64U

/* Frozen event flag bits (UI #65). */
#define MUSIC_EVENT_KICK 0x0001U
#define MUSIC_EVENT_SNARE 0x0002U
#define MUSIC_EVENT_HIGH 0x0004U
#define MUSIC_EVENT_FILL 0x0008U
#define MUSIC_EVENT_BUILD 0x0010U
#define MUSIC_EVENT_REENTRY 0x0020U
#define MUSIC_EVENT_BREAKDOWN 0x0040U
#define MUSIC_EVENT_SECTION 0x0080U
#define MUSIC_EVENT_DROP 0x0100U
#define MUSIC_EVENT_STRUCTURAL (MUSIC_EVENT_BUILD | MUSIC_EVENT_REENTRY | \
				MUSIC_EVENT_BREAKDOWN | MUSIC_EVENT_SECTION | \
				MUSIC_EVENT_DROP)

/* Bounded serialized frame.  The largest v2 frame is well under this and
 * comfortably below the AF_UNIX stream buffer the LED daemon reads. */
#define MUSIC_FEATURES_FRAME_MAX 768U

/*
 * Published fields.  Every field is an explicit contract value; the engine
 * never relies on a provider default.  All 0..255 fields are perceptual
 * strength, not calibrated acoustic units.  brightness_axis is spectral
 * brightness and is distinct from the LED master brightness carried by the
 * legacy "brightness" argument.
 */
struct music_features {
	uint8_t energy;
	uint8_t warmth;
	uint8_t brightness_axis;
	uint8_t density;
	uint8_t transientness;
	uint8_t groove;
	uint8_t build;
	uint8_t spaciousness;
	uint8_t loudness_fast;
	uint8_t loudness_slow;
	uint8_t onset_low;
	uint8_t onset_mid;
	uint8_t onset_high;
	uint8_t beat_strength;
	uint8_t beat_confidence;
	uint8_t novelty;
	uint8_t event_strength;
	uint16_t beat_phase;	/* 0..65535, phase within the current beat */
	uint16_t bpm_x100;	/* 0..30000, 0 when no confident tempo */
	uint16_t events;	/* MUSIC_EVENT_* bitmask */
};

struct music_features_state {
	/* Slow visual axes, Q8 of a 0..255 value. */
	uint16_t loudness_fast_q8;
	uint16_t loudness_slow_q8;
	uint16_t build_q8;
	uint16_t density_q8;
	uint16_t spacious_q8;
	uint16_t brightness_q8;
	uint16_t warmth_q8;
	uint16_t transient_q8;
	uint16_t novelty_q8;
	uint16_t event_strength_q8;
	uint16_t onset_mean_q8;
	uint16_t group_mean_q8[MUSIC_FEATURES_BAND_GROUPS];
	/* Per-band slow spectrum for structural novelty. */
	uint16_t slow_raw_q8[MUSIC_FEATURES_BANDS];
	uint8_t prev_raw[MUSIC_FEATURES_BANDS];
	uint8_t onset_out[MUSIC_FEATURES_BAND_GROUPS];
	uint8_t onset_hist[MUSIC_FEATURES_ONSET_HISTORY];
	uint16_t hist_index;
	uint16_t hist_filled;
	/* Delayed slow axis used to measure the multi-second trend. */
	uint8_t slow_hist[MUSIC_FEATURES_SLOW_HISTORY];
	uint8_t slow_index;
	uint8_t slow_filled;
	/* Build/drop state: peak after a build, dip depth, recovery. */
	uint8_t build_peak;
	uint8_t dip_active;
	uint8_t dip_low;
	/* Tempo hypothesis. */
	uint16_t period_frames;
	uint16_t lag_candidate;
	uint8_t lag_votes;
	uint8_t confidence;
	uint16_t phase;
	uint8_t beat_strength;
	uint8_t beat_refractory;
	uint8_t group_refractory[MUSIC_FEATURES_BAND_GROUPS];
	/* Multi-frame structural gates with cooldowns (in updates). */
	uint8_t had_activity;
	uint8_t reentry_armed;
	uint8_t build_armed;
	uint8_t quiet_frames;
	uint8_t loud_frames;
	uint8_t build_frames;
	uint8_t section_frames;
	uint16_t drop_window;
	uint16_t cooldown[6];
	/* Latch so a one-period event survives a multi-period frame cadence. */
	uint16_t event_latch;
	uint8_t event_hold;
	uint32_t period_index;
};

enum music_features_cooldown {
	MUSIC_COOLDOWN_BUILD = 0,
	MUSIC_COOLDOWN_REENTRY,
	MUSIC_COOLDOWN_BREAKDOWN,
	MUSIC_COOLDOWN_SECTION,
	MUSIC_COOLDOWN_DROP,
	MUSIC_COOLDOWN_FILL,
	MUSIC_COOLDOWN_COUNT
};

/*
 * Deterministic transport counters.  The session id is nonzero and stable for
 * one producer lifetime; it changes only on producer start or reset.  seq is
 * monotonic within a session and timestamp_ms is monotonic milliseconds
 * derived from the update clock, so a consumer can reject stale or reordered
 * frames without a wall clock.
 */
struct music_feature_transport {
	uint32_t session;
	uint32_t seq;
	uint32_t update_count;
	uint32_t frames_per_update;
	uint32_t rate;
};

static inline uint8_t music_features_u8(uint16_t q8)
{
	uint32_t value = ((uint32_t)q8 + 128U) >> 8;

	return value > 255U ? 255U : (uint8_t)value;
}

static inline uint16_t music_features_ema_q8(uint16_t current, uint32_t target_q8,
					     unsigned int shift)
{
	int32_t value = (int32_t)current +
		(((int32_t)target_q8 - (int32_t)current) >> shift);

	if (value < 0)
		value = 0;
	if (value > 0xffff)
		value = 0xffff;
	return (uint16_t)value;
}

static inline uint8_t music_features_clamp8(int value)
{
	if (value < 0)
		return 0;
	if (value > 255)
		return 255;
	return (uint8_t)value;
}

static inline uint8_t music_features_abs_diff_u8(int a, int b)
{
	int d = a - b;

	return (uint8_t)(d < 0 ? -d : d);
}

static inline void music_features_state_init(struct music_features_state *state)
{
	unsigned int i;

	for (i = 0; i < sizeof(*state); ++i)
		((uint8_t *)state)[i] = 0;
}

/* Tunables, expressed in update (PCM period) counts. */
#define MF_ATTACK_SHIFT 2U
#define MF_SLOW_SHIFT 6U
#define MF_DENSITY_SHIFT 3U
#define MF_SPACIOUS_SHIFT 5U
#define MF_BRIGHT_SHIFT 5U
#define MF_WARMTH_SHIFT 4U
#define MF_TRANSIENT_RISE 1U
#define MF_TRANSIENT_FALL 3U
#define MF_ONSET_MEAN_SHIFT 4U
#define MF_GROUP_MEAN_SHIFT 5U
#define MF_NOVELTY_SHIFT 5U
#define MF_NOVELTY_DIV 12U
#define MF_BUILD_RISE 3U
#define MF_BUILD_FALL 6U
#define MF_SPECTRUM_SHIFT 6U
#define MF_BEAT_ATTACK 1U
#define MF_BEAT_DECAY 5U
#define MF_BEAT_MIN_SCORE 48U
#define MF_BEAT_CONFIDENCE_LEVEL 32U
#define MF_BEAT_PHASE_LEVEL 64U
#define MF_BEAT_COHERENT_LEVEL 48U
#define MF_LAG_VOTES 3U
#define MF_ONSET_DECAY 20U
#define MF_GROUP_FLOOR 24
#define MF_GROUP_REFRACTORY 3U

#define MF_QUIET_LEVEL 20
#define MF_QUIET_MIN 10
#define MF_RESUME_FRAMES 4
#define MF_ACTIVE_LEVEL 64
#define MF_BREAKDOWN_MIN 16
#define MF_BUILD_LEVEL 96
#define MF_BUILD_MIN 12
#define MF_SECTION_LEVEL 80
#define MF_SECTION_MIN 6
#define MF_REENTRY_LEVEL 48
#define MF_REENTRY_MIN 4
#define MF_DROP_ONSET 100
#define MF_DROP_LEVEL 96
#define MF_DROP_DIP_DELTA 24
#define MF_DROP_RECOVER 32
#define MF_DROP_WINDOW 200U
#define MF_FILL_LEVEL 96

#define MF_CD_BUILD 48U
#define MF_CD_REENTRY 96U
#define MF_CD_BREAKDOWN 96U
#define MF_CD_SECTION 144U
#define MF_CD_DROP 168U
#define MF_CD_FILL 72U
/* Event flags are held for this many updates so an event that lands between
 * two emitted frames is still delivered by the next one. */
#define MF_EVENT_HOLD 4U

static inline int mf_cooldown_ok(const struct music_features_state *state,
				 enum music_features_cooldown which)
{
	return state->cooldown[which] == 0;
}

static inline void mf_cooldown_arm(struct music_features_state *state,
				   enum music_features_cooldown which,
				   uint16_t periods)
{
	state->cooldown[which] = periods;
}

static inline uint16_t mf_lag_to_bpm_x100(uint32_t fps_q8, uint16_t lag)
{
	uint64_t numerator;

	if (lag == 0)
		return 0;
	numerator = (uint64_t)fps_q8 * 100U * 60U;
	numerator = numerator / 256U;
	numerator = (numerator + lag / 2U) / lag;
	if (numerator > 30000U)
		numerator = 30000U;
	return (uint16_t)numerator;
}

static inline void music_features_update(struct music_features_state *state,
					 const uint8_t raw[MUSIC_FEATURES_BANDS],
					 uint32_t frames_per_update,
					 uint32_t rate,
					 struct music_features *out)
{
	uint32_t fps_q8;
	uint32_t sum = 0;
	uint32_t low = 0, mid = 0, high = 0;
	uint32_t cen_num = 0, cen_den = 0;
	uint32_t dense = 0, empty = 0;
	int flux_low = 0, flux_mid = 0, flux_high = 0;
	uint32_t spectral_move = 0;
	uint32_t den = 0;
	unsigned int i, g;
	unsigned int best_lag = 0;
	int32_t best_score = -1;
	uint8_t onset_env;
	uint8_t fast255, slow255;
	uint16_t events = 0;
	uint16_t beat_bpm = 0;

	if (!state || !raw || !out || frames_per_update == 0 || rate == 0)
		return;

	fps_q8 = (rate * 256U) / frames_per_update;

	for (i = 0; i < MUSIC_FEATURES_BANDS; ++i) {
		uint32_t v = raw[i];
		int delta = (int)v - (int)state->prev_raw[i];

		sum += v;
		if (i < 4)
			low += v;
		else if (i < 8)
			mid += v;
		else
			high += v;
		cen_num += (uint32_t)i * v;
		cen_den += v;
		if (v >= 48U)
			++dense;
		if (v < 32U)
			++empty;
		if (delta > 0) {
			if (i < 4)
				flux_low += delta;
			else if (i < 8)
				flux_mid += delta;
			else
				flux_high += delta;
		}
		{
			uint16_t previous = state->slow_raw_q8[i];
			uint16_t updated;

			/*
			 * Prime the slow spectrum from the first programme
			 * period so the settling of an already-steady input is
			 * not mistaken for structural novelty.
			 */
			if (state->period_index == 0)
				updated = (uint16_t)(v << 8);
			else
				updated = music_features_ema_q8(
					previous, v << 8, MF_SPECTRUM_SHIFT);
			if (state->period_index != 0)
				spectral_move += (updated >= previous)
					? (uint32_t)(updated - previous)
					: (uint32_t)(previous - updated);
			state->slow_raw_q8[i] = updated;
		}
		state->prev_raw[i] = (uint8_t)v;
	}

	/* Loudness axes: fast reacts inside a note, slow spans seconds. */
	state->loudness_fast_q8 = music_features_ema_q8(
		state->loudness_fast_q8, (sum / MUSIC_FEATURES_BANDS) << 8,
		MF_ATTACK_SHIFT);
	state->loudness_slow_q8 = music_features_ema_q8(
		state->loudness_slow_q8, (sum / MUSIC_FEATURES_BANDS) << 8,
		MF_SLOW_SHIFT);

	state->density_q8 = music_features_ema_q8(
		state->density_q8, (dense * 255U / MUSIC_FEATURES_BANDS) << 8,
		MF_DENSITY_SHIFT);
	state->spacious_q8 = music_features_ema_q8(
		state->spacious_q8, (empty * 255U / MUSIC_FEATURES_BANDS) << 8,
		MF_SPACIOUS_SHIFT);

	if (cen_den != 0) {
		uint32_t target = cen_num * 255U /
			(cen_den * (MUSIC_FEATURES_BANDS - 1U));

		state->brightness_q8 = music_features_ema_q8(
			state->brightness_q8, target << 8, MF_BRIGHT_SHIFT);
	}
	state->warmth_q8 = music_features_ema_q8(
		state->warmth_q8,
		(low * 255U) / (low + mid + high + 1U) << 8, MF_WARMTH_SHIFT);

	/*
	 * Differentiated onsets: positive spectral flux per band group, held
	 * with a short peak decay so a single period of attack is visible in
	 * the published frame.  Steady material converges to zero flux.
	 */
	{
		int raw_group[3];
		uint8_t out_group[3];

		raw_group[0] = flux_low >> 2;	/* 4 bands, 255 each */
		raw_group[1] = flux_mid >> 2;
		raw_group[2] = flux_high >> 2;
		for (g = 0; g < MUSIC_FEATURES_BAND_GROUPS; ++g) {
			int value = raw_group[g];

			if (value > 255)
				value = 255;
			if (value < (int)state->onset_out[g] &&
			    state->onset_out[g] > MF_ONSET_DECAY)
				value = state->onset_out[g] - MF_ONSET_DECAY;
			else if (value < state->onset_out[g])
				value = 0;
			out_group[g] = (uint8_t)value;
			state->onset_out[g] = out_group[g];
			state->group_mean_q8[g] = music_features_ema_q8(
				state->group_mean_q8[g],
				(uint32_t)out_group[g] << 8, MF_GROUP_MEAN_SHIFT);
		}
	}
	onset_env = music_features_u8(
		(uint16_t)(((uint32_t)(flux_low + flux_mid + flux_high) >> 4)
			   << 8));

	/* Transientness: fast rise, slower fall. */
	{
		uint16_t target_q8 = (uint16_t)((uint8_t)onset_env << 8);

		state->transient_q8 = music_features_ema_q8(
			state->transient_q8, target_q8,
			(target_q8 > state->transient_q8)
				? MF_TRANSIENT_RISE : MF_TRANSIENT_FALL);
	}
	state->onset_mean_q8 = music_features_ema_q8(
		state->onset_mean_q8, (uint32_t)onset_env << 8,
		MF_ONSET_MEAN_SHIFT);

	/*
	 * Structural novelty: how fast the slow spectrum is moving.  A steady
	 * programme stops moving, so novelty falls to zero; a section change,
	 * loudness drop or re-entry moves the slow spectrum for a while and
	 * raises it.
	 */
	{
		uint32_t nov = spectral_move / MF_NOVELTY_DIV;

		if (nov > 255U)
			nov = 255U;
		state->novelty_q8 = music_features_ema_q8(state->novelty_q8,
							  nov << 8,
							  MF_NOVELTY_SHIFT);
	}

	/*
	 * Build: a sustained multi-second rising trend of the slow loudness
	 * axis.  A steady programme has no trend even when it is loud, and a
	 * single transient never appears in the slow axis, so neither opens
	 * the gate.
	 */
	{
		uint8_t slow_now = music_features_u8(state->loudness_slow_q8);
		uint8_t slow_ref =
			(state->slow_filled >= MUSIC_FEATURES_SLOW_HISTORY)
			? state->slow_hist[state->slow_index] : slow_now;
		int slope;
		uint16_t target_q8;

		state->slow_hist[state->slow_index] = slow_now;
		state->slow_index = (uint8_t)((state->slow_index + 1U) %
					      MUSIC_FEATURES_SLOW_HISTORY);
		if (state->slow_filled < MUSIC_FEATURES_SLOW_HISTORY)
			++state->slow_filled;

		slope = (int)slow_now - (int)slow_ref;
		if (slope < 0)
			slope = 0;
		if (slope > 85)
			slope = 85;
		target_q8 = (uint16_t)((uint32_t)(slope * 3) << 8);
		state->build_q8 = music_features_ema_q8(
			state->build_q8, target_q8,
			(target_q8 > state->build_q8)
				? MF_BUILD_RISE : MF_BUILD_FALL);
	}

	/* Tempo hypothesis from the onset envelope autocorrelation. */
	state->onset_hist[state->hist_index] = onset_env;
	state->hist_index = (uint16_t)((state->hist_index + 1U) %
				       MUSIC_FEATURES_ONSET_HISTORY);
	if (state->hist_filled < MUSIC_FEATURES_ONSET_HISTORY)
		++state->hist_filled;

	for (i = 0; i < MUSIC_FEATURES_ONSET_HISTORY; ++i)
		den += (uint32_t)state->onset_hist[i] *
			state->onset_hist[i];

	if (state->hist_filled >= MUSIC_FEATURES_ONSET_HISTORY &&
	    den != 0) {
		unsigned int lag;

		for (lag = MUSIC_FEATURES_MIN_LAG;
		     lag <= MUSIC_FEATURES_MAX_LAG; ++lag) {
			uint32_t num = 0;
			int32_t score;
			unsigned int k;

			for (k = 0; k < MUSIC_FEATURES_ONSET_HISTORY; ++k) {
				unsigned int a = (state->hist_index +
						  MUSIC_FEATURES_ONSET_HISTORY -
						  1U - k) %
					MUSIC_FEATURES_ONSET_HISTORY;
				unsigned int b = (state->hist_index +
						  MUSIC_FEATURES_ONSET_HISTORY -
						  1U - k - lag) %
					MUSIC_FEATURES_ONSET_HISTORY;

				num += (uint32_t)state->onset_hist[a] *
					state->onset_hist[b];
			}
			score = (int32_t)(num * 256U / den);
			if (score > best_score) {
				best_score = score;
				best_lag = lag;
			}
		}
	}

	if (best_score >= (int32_t)MF_BEAT_MIN_SCORE) {
		int32_t target = best_score > 255 ? 255 : best_score;

		if (state->lag_candidate == (uint16_t)best_lag) {
			if (state->lag_votes < 0xffU)
				++state->lag_votes;
		} else {
			state->lag_candidate = (uint16_t)best_lag;
			state->lag_votes = 1;
		}
		if (state->lag_votes >= MF_LAG_VOTES)
			state->period_frames = (uint16_t)best_lag;
		if ((int)state->confidence < target)
			state->confidence = music_features_clamp8(
				state->confidence +
				((target - state->confidence) >>
				 MF_BEAT_ATTACK));
		else
			state->confidence = music_features_clamp8(
				state->confidence -
				((state->confidence - target) >>
				 MF_BEAT_ATTACK));
	} else {
		if (state->confidence >
		    (uint8_t)((1U << MF_BEAT_DECAY) + 1U))
			state->confidence = (uint8_t)(
				state->confidence -
				(state->confidence >> MF_BEAT_DECAY) - 1U);
		else
			state->confidence = 0;
	}

	/* Phase advances at the estimated period; a beat snaps it to zero. */
	if (state->confidence >= MF_BEAT_PHASE_LEVEL &&
	    state->period_frames >= MUSIC_FEATURES_MIN_LAG &&
	    state->period_frames <= MUSIC_FEATURES_MAX_LAG) {
		uint32_t step = 65536U / state->period_frames;

		state->phase = (uint16_t)((state->phase + step) & 0xffffU);
	}

	/* Beat / per-group onset evidence. */
	if (state->beat_refractory > 0)
		--state->beat_refractory;
	for (g = 0; g < MUSIC_FEATURES_BAND_GROUPS; ++g)
		if (state->group_refractory[g] > 0)
			--state->group_refractory[g];

	{
		int threshold = (int)(state->onset_mean_q8 >> 8) * 2;

		if (threshold < MF_GROUP_FLOOR)
			threshold = MF_GROUP_FLOOR;
		if (state->beat_refractory == 0 &&
		    state->confidence >= MF_BEAT_COHERENT_LEVEL &&
		    onset_env >= threshold) {
			state->phase = 0;
			state->beat_refractory = (uint8_t)(
				(state->period_frames * 3U / 4U) < 3U ? 3U :
				 state->period_frames * 3U / 4U);
			state->beat_strength = music_features_clamp8(
				(int)onset_env + state->confidence / 4);
		}
	}

	for (g = 0; g < MUSIC_FEATURES_BAND_GROUPS; ++g) {
		int threshold = (int)(state->group_mean_q8[g] >> 8) * 2;

		if (threshold < MF_GROUP_FLOOR)
			threshold = MF_GROUP_FLOOR;
		if (state->group_refractory[g] == 0 &&
		    state->onset_out[g] >= threshold &&
		    state->onset_out[g] >= MF_GROUP_FLOOR) {
			if (g == 0)
				events |= MUSIC_EVENT_KICK;
			else if (g == 1)
				events |= MUSIC_EVENT_SNARE;
			else
				events |= MUSIC_EVENT_HIGH;
			state->group_refractory[g] = MF_GROUP_REFRACTORY;
		}
	}

	fast255 = music_features_u8(state->loudness_fast_q8);
	slow255 = music_features_u8(state->loudness_slow_q8);

	/* Quiet / re-entry tracking. */
	if (fast255 < MF_QUIET_LEVEL) {
		if (state->quiet_frames < 0xffU)
			++state->quiet_frames;
		state->loud_frames = 0;
	} else {
		if (state->loud_frames < 0xffU)
			++state->loud_frames;
		if (state->loud_frames >= MF_RESUME_FRAMES)
			state->quiet_frames = 0;
	}
	if (state->quiet_frames >= MF_QUIET_MIN)
		state->reentry_armed = 1;
	if (slow255 >= MF_ACTIVE_LEVEL)
		state->had_activity = 1;

	/* Breakdown: a material quiet stretch after activity. */
	if (state->had_activity && state->quiet_frames >= MF_BREAKDOWN_MIN) {
		if (mf_cooldown_ok(state, MUSIC_COOLDOWN_BREAKDOWN)) {
			events |= MUSIC_EVENT_BREAKDOWN;
			mf_cooldown_arm(state, MUSIC_COOLDOWN_BREAKDOWN,
					MF_CD_BREAKDOWN);
			state->event_strength_q8 = (uint16_t)(
				(255U - fast255) << 8);
		}
		state->had_activity = 0;
	}

	/* Build gate: build must stay high across many periods. */
	if (music_features_u8(state->build_q8) >= MF_BUILD_LEVEL)
		state->build_frames = (state->build_frames < 0xffU)
			? (uint8_t)(state->build_frames + 1U) : 0xffU;
	else
		state->build_frames = 0;
	if (state->build_frames >= MF_BUILD_MIN &&
	    mf_cooldown_ok(state, MUSIC_COOLDOWN_BUILD)) {
		events |= MUSIC_EVENT_BUILD;
		mf_cooldown_arm(state, MUSIC_COOLDOWN_BUILD, MF_CD_BUILD);
		state->build_armed = 1;
		state->drop_window = MF_DROP_WINDOW;
		state->build_peak = fast255;
		state->dip_active = 0;
		state->dip_low = fast255;
		state->event_strength_q8 = (uint16_t)(
			music_features_u8(state->build_q8) << 8);
		state->build_frames = 0;
	}

	/* Section gate: sustained structural novelty. */
	if (music_features_u8(state->novelty_q8) >= MF_SECTION_LEVEL)
		state->section_frames = (state->section_frames < 0xffU)
			? (uint8_t)(state->section_frames + 1U) : 0xffU;
	else
		state->section_frames = 0;
	if (state->section_frames >= MF_SECTION_MIN &&
	    mf_cooldown_ok(state, MUSIC_COOLDOWN_SECTION)) {
		events |= MUSIC_EVENT_SECTION;
		mf_cooldown_arm(state, MUSIC_COOLDOWN_SECTION, MF_CD_SECTION);
		state->event_strength_q8 = (uint16_t)(
			music_features_u8(state->novelty_q8) << 8);
		state->section_frames = 0;
	}

	/*
	 * Drop: an armed build that dips and slams back, or resolves straight
	 * into a high-impact downbeat.  Either way it needs the build evidence
	 * plus an impact, so a lone loud hit never opens it.
	 */
	if (state->drop_window > 0) {
		--state->drop_window;
		if (fast255 > state->build_peak)
			state->build_peak = fast255;
		if ((int)fast255 + MF_DROP_DIP_DELTA <= (int)state->build_peak) {
			if (!state->dip_active) {
				state->dip_active = 1;
				state->dip_low = fast255;
			} else if (fast255 < state->dip_low) {
				state->dip_low = fast255;
			}
		}
		if (fast255 >= MF_DROP_LEVEL &&
		    ((state->dip_active &&
		      (int)fast255 >= (int)state->dip_low + MF_DROP_RECOVER) ||
		     onset_env >= MF_DROP_ONSET) &&
		    mf_cooldown_ok(state, MUSIC_COOLDOWN_DROP)) {
			events |= MUSIC_EVENT_DROP;
			mf_cooldown_arm(state, MUSIC_COOLDOWN_DROP, MF_CD_DROP);
			state->build_armed = 0;
			state->dip_active = 0;
			state->drop_window = 0;
			state->event_strength_q8 = (uint16_t)(
				(uint32_t)fast255 << 8);
		}
	} else {
		state->build_armed = 0;
		state->dip_active = 0;
	}

	/* Re-entry: sustained loudness after a material quiet stretch. */
	if (state->reentry_armed && state->loud_frames >= MF_REENTRY_MIN) {
		if (state->build_armed &&
		    mf_cooldown_ok(state, MUSIC_COOLDOWN_DROP)) {
			events |= MUSIC_EVENT_DROP;
			mf_cooldown_arm(state, MUSIC_COOLDOWN_DROP, MF_CD_DROP);
			state->build_armed = 0;
		} else if (mf_cooldown_ok(state, MUSIC_COOLDOWN_REENTRY)) {
			events |= MUSIC_EVENT_REENTRY;
			mf_cooldown_arm(state, MUSIC_COOLDOWN_REENTRY,
					MF_CD_REENTRY);
		}
		state->event_strength_q8 = (uint16_t)(
			(uint32_t)fast255 << 8);
		state->reentry_armed = 0;
		state->quiet_frames = 0;
	}

	/*
	 * Fill: a strong broadband accent while the groove is confident.  A
	 * single accent plus the cooldown keeps it from chattering.
	 */
	if (onset_env >= MF_FILL_LEVEL &&
	    state->confidence >= (MF_BEAT_CONFIDENCE_LEVEL + 8U) &&
	    mf_cooldown_ok(state, MUSIC_COOLDOWN_FILL)) {
		events |= MUSIC_EVENT_FILL;
		mf_cooldown_arm(state, MUSIC_COOLDOWN_FILL, MF_CD_FILL);
		state->event_strength_q8 = (uint16_t)(
			(uint32_t)onset_env << 8);
	}

	/* Cooldowns and event strength decay. */
	for (g = 0; g < MUSIC_COOLDOWN_COUNT; ++g)
		if (state->cooldown[g] > 0)
			--state->cooldown[g];
	if (state->event_strength_q8 >
	    (uint16_t)((1U << 5) + 1U))
		state->event_strength_q8 = (uint16_t)(
			state->event_strength_q8 -
			(state->event_strength_q8 >> 5) - 1U);
	else
		state->event_strength_q8 = 0;
	if (state->beat_strength > 0)
		state->beat_strength = (state->beat_strength > MF_ONSET_DECAY)
			? (uint8_t)(state->beat_strength - MF_ONSET_DECAY) : 0;

	if (state->confidence >= MF_BEAT_CONFIDENCE_LEVEL &&
	    state->period_frames >= MUSIC_FEATURES_MIN_LAG &&
	    state->period_frames <= MUSIC_FEATURES_MAX_LAG)
		beat_bpm = mf_lag_to_bpm_x100(fps_q8, state->period_frames);

	++state->period_index;

	/*
	 * Latch event flags for a short hold.  The engine emits a frame every
	 * second period, so a one-period kick/build/drop must survive until
	 * the next frame instead of being silently lost.
	 */
	if (events != 0) {
		state->event_latch |= events;
		state->event_hold = MF_EVENT_HOLD;
	} else if (state->event_hold > 0) {
		--state->event_hold;
	}
	if (state->event_hold == 0)
		state->event_latch = 0;

	out->energy = music_features_u8(state->loudness_fast_q8);
	out->warmth = music_features_u8(state->warmth_q8);
	out->brightness_axis = music_features_u8(state->brightness_q8);
	out->density = music_features_u8(state->density_q8);
	out->transientness = music_features_u8(state->transient_q8);
	out->groove = state->confidence;
	out->build = music_features_u8(state->build_q8);
	out->spaciousness = music_features_u8(state->spacious_q8);
	out->loudness_fast = music_features_u8(state->loudness_fast_q8);
	out->loudness_slow = music_features_u8(state->loudness_slow_q8);
	out->onset_low = state->onset_out[0];
	out->onset_mid = state->onset_out[1];
	out->onset_high = state->onset_out[2];
	out->beat_strength = state->beat_strength;
	out->beat_confidence = state->confidence;
	out->novelty = music_features_u8(state->novelty_q8);
	out->event_strength = music_features_u8(state->event_strength_q8);
	out->beat_phase = state->phase;
	out->bpm_x100 = beat_bpm;
	out->events = state->event_latch;
}

static inline void music_features_frame_levels_hex(
	const uint8_t levels[MUSIC_FEATURES_BANDS], char *out)
{
	static const char hexadecimal[] = "0123456789abcdef";
	unsigned int i;

	for (i = 0; i < MUSIC_FEATURES_BANDS; ++i) {
		out[i * 2U] = hexadecimal[levels[i] >> 4];
		out[i * 2U + 1U] = hexadecimal[levels[i] & 0x0fU];
	}
	out[MUSIC_FEATURES_BANDS * 2U] = '\0';
}

/*
 * Serialize one frame.  feature_version 1 emits the legacy twelve-level frame
 * (compatibility mode); version 2 adds the frozen perceptual fields.  Returns
 * the byte length without the trailing NUL, or -1 on overflow.  The envelope,
 * command name, owner and the legacy brightness meaning are unchanged.
 */
static inline int music_features_format_frame(char *buffer, size_t size,
					      const struct music_features *f,
					      const uint8_t levels[12],
					      unsigned int led_brightness,
					      unsigned int feature_version,
					      uint32_t session, uint32_t seq,
					      uint32_t timestamp_ms)
{
	char levels_hex[MUSIC_FEATURES_BANDS * 2U + 1U];
	int length;

	if (!buffer || size == 0 || !levels)
		return -1;
	music_features_frame_levels_hex(levels, levels_hex);

	if (feature_version < MUSIC_FEATURES_VERSION || !f)
		return snprintf(buffer, size,
			"{\"v\":1,\"id\":2,\"cmd\":\"visualizer\",\"args\":"
			"{\"action\":\"frame\",\"levels\":\"%s\","
			"\"brightness\":%u,\"owner\":\"music\"}}\n",
			levels_hex, led_brightness);

	length = snprintf(buffer, size,
		"{\"v\":1,\"id\":2,\"cmd\":\"visualizer\",\"args\":"
		"{\"action\":\"frame\",\"owner\":\"music\","
		"\"feature_version\":%u,"
		"\"session\":%u,\"seq\":%u,\"timestamp_ms\":%u,"
		"\"levels\":\"%s\",\"brightness\":%u,"
		"\"energy\":%u,\"warmth\":%u,\"brightness_axis\":%u,"
		"\"density\":%u,\"transientness\":%u,\"groove\":%u,"
		"\"build\":%u,\"spaciousness\":%u,"
		"\"loudness_fast\":%u,\"loudness_slow\":%u,"
		"\"onset_low\":%u,\"onset_mid\":%u,\"onset_high\":%u,"
		"\"beat_strength\":%u,\"beat_confidence\":%u,"
		"\"beat_phase\":%u,\"bpm_x100\":%u,"
		"\"novelty\":%u,\"event_strength\":%u,\"events\":%u}}\n",
		feature_version, session, seq, timestamp_ms,
		levels_hex, led_brightness,
		f->energy, f->warmth, f->brightness_axis,
		f->density, f->transientness, f->groove,
		f->build, f->spaciousness,
		f->loudness_fast, f->loudness_slow,
		f->onset_low, f->onset_mid, f->onset_high,
		f->beat_strength, f->beat_confidence,
		f->beat_phase, f->bpm_x100,
		f->novelty, f->event_strength, f->events);
	if (length < 0 || (size_t)length >= size ||
	    (size_t)length >= MUSIC_FEATURES_FRAME_MAX)
		return -1;
	return length;
}

static inline void music_feature_transport_init(
	struct music_feature_transport *transport, uint32_t session,
	uint32_t frames_per_update, uint32_t rate)
{
	transport->session = session != 0 ? session : 1U;
	transport->seq = 0;
	transport->update_count = 0;
	transport->frames_per_update = frames_per_update != 0
		? frames_per_update : MUSIC_FEATURES_DEFAULT_FRAMES;
	transport->rate = rate != 0 ? rate : MUSIC_FEATURES_DEFAULT_RATE;
}

/* Reset starts a fresh session; the session id is never reused by the same
 * running producer and never zero. */
static inline void music_feature_transport_reset(
	struct music_feature_transport *transport, uint32_t session)
{
	transport->session = session != 0 ? session : 1U;
	transport->seq = 0;
	transport->update_count = 0;
}

static inline void music_feature_transport_tick(
	struct music_feature_transport *transport)
{
	++transport->update_count;
}

/*
 * The producer clock: cumulative analysed-music milliseconds.  It is computed
 * in 64 bits so the producer can see the exact value; the frozen wire field is
 * uint32, so at 48 kHz the counter crosses the 32-bit boundary after ~49.7 days
 * of continuous playback.  A consumer rejects a backward timestamp *within* a
 * session, so the producer must start a new session before that crossing rather
 * than publish timestamp_ms=0 in the old one.
 */
static inline uint64_t music_feature_transport_elapsed_ms(
	const struct music_feature_transport *transport)
{
	return (uint64_t)transport->update_count *
		transport->frames_per_update * 1000U / transport->rate;
}

static inline uint32_t music_feature_transport_timestamp_ms(
	const struct music_feature_transport *transport)
{
	return (uint32_t)music_feature_transport_elapsed_ms(transport);
}

/*
 * Whether the next update-clock period would carry the analysed-music clock
 * past the frozen 32-bit wire field.  The producer rotates the session (fresh
 * id, seq 0, clock 0) when this is true and never emits a wrapped timestamp in
 * a live session.
 */
static inline int music_feature_transport_next_tick_wraps(
	const struct music_feature_transport *transport)
{
	uint64_t update_count = (uint64_t)transport->update_count + 1U;

	return update_count * transport->frames_per_update * 1000U /
		transport->rate > (uint64_t)UINT32_MAX;
}

/*
 * Advance one update-clock period, rotating first when the next timestamp would
 * wrap the 32-bit wire field.  A rotation takes a fresh nonzero session id from
 * ``next_session`` and restarts seq and the clock at zero, so a consumer never
 * sees a backward timestamp inside one session and the wire encoding is
 * unchanged.  ``next_session`` is only called at the multi-week wrap boundary,
 * so the producer never pays for an id it does not use.  Returns 1 on rotation.
 */
static inline int music_feature_transport_begin_tick(
	struct music_feature_transport *transport, uint32_t (*next_session)(void))
{
	int rotated = 0;

	if (music_feature_transport_next_tick_wraps(transport)) {
		music_feature_transport_reset(transport, next_session());
		rotated = 1;
	}
	music_feature_transport_tick(transport);
	return rotated;
}

static inline uint32_t music_feature_transport_next_seq(
	struct music_feature_transport *transport)
{
	uint32_t seq = transport->seq;

	++transport->seq;
	return seq;
}

#endif
