#ifndef LIBREECHO_SPEAKER_BOOST_H
#define LIBREECHO_SPEAKER_BOOST_H

/*
 * Owner speaker gain boost for the speaker chains.  The setting is published
 * into the audio runtime root (boost.conf) and read once per playback start,
 * like tone.conf.  Off (0 dB) is the stock sound: a missing file, or any file
 * that does not parse exactly, selects it.
 *
 *   schema=1
 *   boost_db=<0|3|6>
 *
 * The boost is a makeup gain on the mono bus ahead of the chain's own
 * protection (MBCL on Radar-Puffin, the compressors and full-band limiter on
 * Echo Dot).  It never bypasses that protection, so the output ceiling is the
 * same with or without the boost.  Only the three listed steps are accepted.
 */

#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define SPEAKER_BOOST_STEP_COUNT 3

static const float speaker_boost_steps_db[SPEAKER_BOOST_STEP_COUNT] = { 0.0f, 3.0f, 6.0f };

/* Parses a complete boost.conf body in place (text is modified by strtok_r).
 * Returns 1 when valid and stores the dB step in *out_db, 0 when rejected.
 * On rejection *out_db is 0 dB, so the caller can use it without checking. */
static inline int speaker_boost_parse(char *text, float *out_db)
{
	int seen_schema = 0, seen_boost = 0, ok = 1;
	char *line, *save = NULL;
	float db = 0.0f;

	*out_db = 0.0f;
	for (line = strtok_r(text, "\n", &save); line && ok;
	     line = strtok_r(NULL, "\n", &save)) {
		char *value = strchr(line, '=');
		char *end;
		double parsed;
		int i, match = 0;

		if (!value) {
			ok = 0;
			break;
		}
		*value++ = '\0';
		if (!strcmp(line, "schema")) {
			if (seen_schema++ || strcmp(value, "1"))
				ok = 0;
		} else if (!strcmp(line, "boost_db")) {
			if (seen_boost++) {
				ok = 0;
				break;
			}
			errno = 0;
			parsed = strtod(value, &end);
			if (errno || end == value || *end || !isfinite(parsed)) {
				ok = 0;
				break;
			}
			for (i = 0; i < SPEAKER_BOOST_STEP_COUNT; ++i)
				if (parsed == (double)speaker_boost_steps_db[i])
					match = 1;
			if (!match) {
				ok = 0;
				break;
			}
			db = (float)parsed;
		} else {
			ok = 0;
		}
	}
	if (!ok || !seen_schema || !seen_boost)
		return 0;
	*out_db = db;
	return 1;
}

/* Linear makeup for a parsed step.  0 dB is exactly 1.0f: the stock chain. */
static inline float speaker_boost_gain(float boost_db)
{
	return boost_db == 0.0f ? 1.0f : powf(10.0f, boost_db / 20.0f);
}

#endif /* LIBREECHO_SPEAKER_BOOST_H */
