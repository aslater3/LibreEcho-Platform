#ifndef LIBREECHO_MUSIC_AVL_H
#define LIBREECHO_MUSIC_AVL_H

/*
 * Music automatic volume leveling (AVL) for the media buses.
 *
 * The stock Radar playback chain runs an "Automatic Volume Leveling" stage
 * ahead of its equaliser (AFE.cfg / StandAloneAAModules.cfg).  This is an
 * independent implementation of its documented *music* behaviour; no vendor
 * code or coefficient data is used:
 *
 *   downward compression  threshold 0 dBFS, ratio 1   (none)
 *   upward compression    below -10 dBFS, ratio 2      (gain = half the gap)
 *   noise gate            below -55 dBFS, ratio 0.55   (the boost fades out)
 *   maximum gain          15 dB at volume levels 2-7, 13 dB at level 8
 *   attack / release      0.5 s / 10 s
 *
 * Levels are the RMS of the mono media programme *after* the master volume,
 * in dBFS: the stock AVL owns the volume table and lists its maximum gain per
 * volume level, so at low and middle volume it lifts the programme toward
 * -10 dBFS (2:1, up to 15 dB) -- stock "smart volume" -- and at the top levels
 * it does nothing.  The result is a smoothed linear gain applied to the media
 * buses only; the multiband protection and output limiter still follow.
 * LibreEcho additionally never lets the boost exceed the headroom up to its
 * own volume-100 level, so nothing plays louder than it would at full volume.
 */

#include <math.h>

#define MUSIC_AVL_UP_THRESHOLD_DB   (-10.0f)
#define MUSIC_AVL_UP_RATIO          2.0f
#define MUSIC_AVL_GATE_THRESHOLD_DB (-55.0f)
#define MUSIC_AVL_GATE_RATIO        0.55f
#define MUSIC_AVL_ATTACK_S          0.5f
#define MUSIC_AVL_RELEASE_S         10.0f
#define MUSIC_AVL_SILENCE_DB        (-120.0f)

struct music_avl {
	float gain_db;   /* smoothed boost applied to the media buses */
};

static inline void music_avl_init(struct music_avl *avl)
{
	avl->gain_db = 0.0f;
}

/* Stock maxGain_music lists volume levels 2..8 of 10 (15 dB, 13 dB at 8);
 * the unlisted levels 0-1 and 9-10 get no boost. */
static inline float music_avl_max_gain_db(int volume_percent)
{
	static const float max_gain[11] = {
		0.0f, 0.0f, 15.0f, 15.0f, 15.0f, 15.0f, 15.0f, 15.0f, 13.0f, 0.0f, 0.0f
	};
	int level = (volume_percent + 5) / 10;

	if (volume_percent <= 0)
		return 0.0f;
	if (level > 10)
		level = 10;
	return max_gain[level];
}

/* Static gain for a programme level, before smoothing. */
static inline float music_avl_static_gain_db(float level_db, float max_gain_db)
{
	float gain;

	if (level_db >= MUSIC_AVL_UP_THRESHOLD_DB)
		return 0.0f;
	gain = (MUSIC_AVL_UP_THRESHOLD_DB - level_db) *
	       (1.0f - 1.0f / MUSIC_AVL_UP_RATIO);
	if (gain > max_gain_db)
		gain = max_gain_db;
	if (level_db < MUSIC_AVL_GATE_THRESHOLD_DB) {
		/* Downward expansion below the gate: the boost fades out so that
		 * silence and noise floors are never lifted. */
		gain += (level_db - MUSIC_AVL_GATE_THRESHOLD_DB) *
			(1.0f / MUSIC_AVL_GATE_RATIO - 1.0f);
		if (gain < 0.0f)
			gain = 0.0f;
	}
	return gain;
}

/* Advance by one block of block_s seconds whose programme RMS is level_db.
 * Falling gain (louder material) follows the attack time; rising gain follows
 * the release time.  Returns the new linear gain. */
static inline float music_avl_update(struct music_avl *avl, float level_db,
				     float max_gain_db, float block_s)
{
	float target = music_avl_static_gain_db(level_db, max_gain_db);
	float tau = target < avl->gain_db ? MUSIC_AVL_ATTACK_S :
		    MUSIC_AVL_RELEASE_S;
	float k = 1.0f - expf(-block_s / tau);

	avl->gain_db += (target - avl->gain_db) * k;
	if (avl->gain_db > max_gain_db)
		avl->gain_db = max_gain_db; /* a volume change lowers the cap */
	return powf(10.0f, avl->gain_db / 20.0f);
}

static inline float music_avl_level_db(double sum_squares, unsigned int frames)
{
	double mean;

	if (frames == 0)
		return MUSIC_AVL_SILENCE_DB;
	mean = sum_squares / (double)frames;
	if (mean <= 0.0)
		return MUSIC_AVL_SILENCE_DB;
	return (float)(10.0 * log10(mean / (32768.0 * 32768.0)));
}

#endif /* LIBREECHO_MUSIC_AVL_H */
