/* Music AVL contract: stock music curve, time constants and caps. */
#include <math.h>
#include <stdio.h>
#include "music_avl.h"

static int failures;
static void check(int ok, const char *what, double got, double want)
{
	printf("  %s %-58s %+8.2f (want %+.2f)\n", ok ? "ok  " : "FAIL", what, got, want);
	failures += !ok;
}

int main(void)
{
	struct music_avl avl;
	double t;
	float g;

	/* Static curve: 2:1 upward below -10 dBFS, capped, gated below -55. */
	check(fabsf(music_avl_static_gain_db(-5.0f, 15.0f)) < 1e-6, "loud material (-5 dBFS) gets no boost", music_avl_static_gain_db(-5.0f, 15.0f), 0);
	check(fabsf(music_avl_static_gain_db(-10.0f, 15.0f)) < 1e-6, "no boost at the -10 dBFS threshold", music_avl_static_gain_db(-10.0f, 15.0f), 0);
	check(fabsf(music_avl_static_gain_db(-20.0f, 15.0f) - 5.0f) < 1e-4, "-20 dBFS gets half the gap (+5 dB)", music_avl_static_gain_db(-20.0f, 15.0f), 5);
	check(fabsf(music_avl_static_gain_db(-34.0f, 15.0f) - 12.0f) < 1e-4, "-34 dBFS gets +12 dB", music_avl_static_gain_db(-34.0f, 15.0f), 12);
	check(fabsf(music_avl_static_gain_db(-45.0f, 15.0f) - 15.0f) < 1e-4, "boost is capped at maxGain (15 dB)", music_avl_static_gain_db(-45.0f, 15.0f), 15);
	check(music_avl_static_gain_db(-60.0f, 15.0f) < 15.0f, "below the -55 dBFS gate the boost fades", music_avl_static_gain_db(-60.0f, 15.0f), 10.9);
	check(fabsf(music_avl_static_gain_db(-120.0f, 15.0f)) < 1e-6, "silence is never lifted", music_avl_static_gain_db(-120.0f, 15.0f), 0);
	check(fabsf(music_avl_max_gain_db(50) - 15.0f) < 1e-6, "maxGain 15 dB at mid volume", music_avl_max_gain_db(50), 15);
	check(fabsf(music_avl_max_gain_db(80) - 13.0f) < 1e-6, "maxGain 13 dB at volume level 8+", music_avl_max_gain_db(80), 13);
	check(fabsf(music_avl_max_gain_db(0)) < 1e-6, "no boost when muted", music_avl_max_gain_db(0), 0);

	/* Release (10 s): from 0 dB toward +12 dB on steady -34 dBFS. */
	music_avl_init(&avl);
	for (t = 0; t < 10.0; t += 0.04267)
		g = music_avl_update(&avl, -34.0f, 15.0f, 0.04267f);
	check(fabsf(avl.gain_db - 12.0f * (1.0f - expf(-1.0f))) < 0.3f, "release reaches 63% of +12 dB after 10 s", avl.gain_db, 7.59);
	/* Attack (0.5 s): a loud passage pulls the boost down quickly. */
	for (t = 0; t < 0.5; t += 0.04267)
		g = music_avl_update(&avl, -5.0f, 15.0f, 0.04267f);
	check(avl.gain_db < 7.59f * 0.40f, "attack removes ~63% of the boost within 0.5 s", avl.gain_db, 2.8);
	check(g > 0.99f, "linear gain stays >= unity", g, 1.0);
	/* A lower cap after a volume change applies immediately. */
	avl.gain_db = 15.0f; g = music_avl_update(&avl, -60.0f, 13.0f, 0.04267f);
	check(avl.gain_db <= 13.0f + 1e-4f, "a lower cap takes effect immediately", avl.gain_db, 13);
	puts(failures ? "music_avl: FAIL" : "music_avl: PASS");
	return failures != 0;
}
