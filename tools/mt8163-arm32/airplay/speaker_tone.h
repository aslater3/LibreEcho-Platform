#ifndef LIBREECHO_SPEAKER_TONE_H
#define LIBREECHO_SPEAKER_TONE_H

/*
 * Owner bass/treble settings for the speaker chains.  The settings file is
 * published into the audio runtime root (tone.conf) and read once per
 * playback start, like the audio profile.  Flat (0 dB, 0 dB) is the stock
 * sound: a missing file, or any file that does not parse exactly, selects it.
 *
 *   schema=1
 *   bass_db=<-6.00..6.00>
 *   treble_db=<-6.00..6.00>
 *
 * Each key appears exactly once, in any order, with no other keys.  A file
 * with any error is rejected whole, so a partial write never applies one of
 * the two values.
 */

#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define SPEAKER_TONE_LIMIT_DB 6.0f

struct speaker_tone {
	float bass_db;
	float treble_db;
};

static inline int speaker_tone_parse_db(const char *text, float *out)
{
	char *end;
	double value;

	errno = 0;
	value = strtod(text, &end);
	if (errno || end == text || *end || !isfinite(value))
		return 0;
	if (value < -(double)SPEAKER_TONE_LIMIT_DB || value > (double)SPEAKER_TONE_LIMIT_DB)
		return 0;
	*out = (float)value;
	return 1;
}

/* Parses a complete tone.conf body in place (text is modified by strtok_r).
 * Returns 1 when valid and stores the result in *out, 0 when rejected.  On
 * rejection *out is left flat, so the caller can use it without checking. */
static inline int speaker_tone_parse(char *text, struct speaker_tone *out)
{
	enum { K_SCHEMA, K_BASS, K_TREBLE, K_N };
	static const char *const keys[K_N] = { "schema", "bass_db", "treble_db" };
	int seen[K_N] = { 0 }, k;
	char *line, *save = NULL;
	struct speaker_tone t = { 0.0f, 0.0f };

	for (line = strtok_r(text, "\n", &save); line;
	     line = strtok_r(NULL, "\n", &save)) {
		char *value = strchr(line, '=');

		if (!value)
			goto reject;
		*value++ = '\0';
		for (k = 0; k < K_N && strcmp(line, keys[k]); ++k)
			;
		if (k == K_N || seen[k]++)
			goto reject;
		switch (k) {
		case K_SCHEMA:
			if (strcmp(value, "1"))
				goto reject;
			break;
		case K_BASS:
			if (!speaker_tone_parse_db(value, &t.bass_db))
				goto reject;
			break;
		case K_TREBLE:
			if (!speaker_tone_parse_db(value, &t.treble_db))
				goto reject;
			break;
		}
	}
	for (k = 0; k < K_N; ++k)
		if (!seen[k])
			goto reject;
	*out = t;
	return 1;

reject:
	out->bass_db = 0.0f;
	out->treble_db = 0.0f;
	return 0;
}

#endif /* LIBREECHO_SPEAKER_TONE_H */
