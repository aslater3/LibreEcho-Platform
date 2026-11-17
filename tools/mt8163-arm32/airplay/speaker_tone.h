#ifndef LIBREECHO_SPEAKER_TONE_H
#define LIBREECHO_SPEAKER_TONE_H

/*
 * Owner equaliser settings for the speaker chains.  The settings file is
 * published into the audio runtime root (tone.conf), read at playback start
 * and re-read while playing when its stat changes (see audio_engine.c).  Flat
 * (every band 0 dB) is the stock sound: a missing file, or any file that does
 * not parse exactly, selects it.
 *
 * Schema 2, the four-band equaliser:
 *
 *   schema=2
 *   bass_db=<-6.00..6.00>        low shelf, 120 Hz
 *   low_mid_db=<-6.00..6.00>     peak, 400 Hz, Q 0.9
 *   high_mid_db=<-6.00..6.00>    peak, 2.5 kHz, Q 0.9
 *   treble_db=<-6.00..6.00>      high shelf, 6 kHz
 *
 * Schema 1, the earlier bass/treble file, stays accepted unchanged (the two
 * mid bands are then flat):
 *
 *   schema=1
 *   bass_db=<-6.00..6.00>
 *   treble_db=<-6.00..6.00>
 *
 * Each key of the file's schema appears exactly once, in any order, with no
 * other keys (a mid-band key in a schema 1 file is an unknown key).  A file
 * with any error is rejected whole, so a partial write never applies some of
 * the values.
 */

#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define SPEAKER_TONE_LIMIT_DB 6.0f
#define SPEAKER_TONE_BANDS 4

struct speaker_tone {
	float bass_db;
	float low_mid_db;
	float high_mid_db;
	float treble_db;
};

static inline void speaker_tone_flat(struct speaker_tone *t)
{
	t->bass_db = 0.0f;
	t->low_mid_db = 0.0f;
	t->high_mid_db = 0.0f;
	t->treble_db = 0.0f;
}

static inline int speaker_tone_is_flat(const struct speaker_tone *t)
{
	return t->bass_db == 0.0f && t->low_mid_db == 0.0f &&
		t->high_mid_db == 0.0f && t->treble_db == 0.0f;
}

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
	enum { K_SCHEMA, K_BASS, K_LOW_MID, K_HIGH_MID, K_TREBLE, K_N };
	static const char *const keys[K_N] = {
		"schema", "bass_db", "low_mid_db", "high_mid_db", "treble_db"
	};
	int seen[K_N] = { 0 }, k, schema = 0;
	char *line, *save = NULL;
	struct speaker_tone t;

	speaker_tone_flat(&t);
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
			if (!strcmp(value, "1"))
				schema = 1;
			else if (!strcmp(value, "2"))
				schema = 2;
			else
				goto reject;
			break;
		case K_BASS:
			if (!speaker_tone_parse_db(value, &t.bass_db))
				goto reject;
			break;
		case K_LOW_MID:
			if (!speaker_tone_parse_db(value, &t.low_mid_db))
				goto reject;
			break;
		case K_HIGH_MID:
			if (!speaker_tone_parse_db(value, &t.high_mid_db))
				goto reject;
			break;
		case K_TREBLE:
			if (!speaker_tone_parse_db(value, &t.treble_db))
				goto reject;
			break;
		}
	}
	/* The key set is exactly the schema's: schema 1 has no mid bands. */
	if (!seen[K_SCHEMA] || !seen[K_BASS] || !seen[K_TREBLE])
		goto reject;
	if (seen[K_LOW_MID] != (schema == 2) || seen[K_HIGH_MID] != (schema == 2))
		goto reject;
	*out = t;
	return 1;

reject:
	speaker_tone_flat(out);
	return 0;
}

#endif /* LIBREECHO_SPEAKER_TONE_H */
