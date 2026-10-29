/*
 * music-trace: turn a recording into the production music feature trace (#46).
 *
 * Runs the same producer path the audio engine uses --
 * audio_visualizer_process_features() then music_features_format_frame()
 * through music_feature_transport -- over a WAV or raw PCM file, and writes one
 * version-2 visualizer packet per emitted frame as JSONL. The session id and the
 * clock are deterministic, so the same input gives a byte-identical trace.
 *
 *   music-trace [--raw --channels N] [--gain-db DB] [--session HEX] input output.jsonl
 *
 * Input: RIFF/WAVE PCM 16-bit, mono or stereo, 48000 Hz (convert with
 * `ffmpeg -i in -ar 48000 -ac 2 -c:a pcm_s16le out.wav`), or raw s16le with
 * --raw. --gain-db scales the samples before analysis, to compare how a level
 * change moves the features.
 */
#define _DEFAULT_SOURCE

#include "audio_visualizer.h"
#include "music_features.h"

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TRACE_FRAMES 2048U
#define TRACE_FRAME_PERIODS 2U
#define TRACE_SESSION 0x2a2a0001U

static uint32_t le32(const unsigned char *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static uint16_t le16(const unsigned char *p) { return (uint16_t)(p[0] | p[1] << 8); }

/* Position the file at the PCM data of a WAV; returns channels or -1 with a reason. */
static int open_wav(FILE *f, long *data_bytes, const char **why)
{
	unsigned char head[12], chunk[8], fmt[16];
	int channels = 0;

	if (fread(head, 1, 12, f) != 12 || memcmp(head, "RIFF", 4) || memcmp(head + 8, "WAVE", 4)) {
		*why = "not a RIFF/WAVE file";
		return -1;
	}
	while (fread(chunk, 1, 8, f) == 8) {
		uint32_t size = le32(chunk + 4);

		if (!memcmp(chunk, "fmt ", 4)) {
			if (size < 16 || fread(fmt, 1, 16, f) != 16) { *why = "short fmt chunk"; return -1; }
			if (le16(fmt) != 1) { *why = "not PCM (format tag must be 1)"; return -1; }
			channels = le16(fmt + 2);
			if (channels < 1 || channels > 2) { *why = "only mono or stereo is supported"; return -1; }
			if (le32(fmt + 4) != AUDIO_VISUALIZER_RATE) { *why = "sample rate must be 48000 Hz"; return -1; }
			if (le16(fmt + 14) != 16) { *why = "samples must be 16-bit"; return -1; }
			if (size > 16 && fseek(f, (long)(size - 16 + (size & 1U)), SEEK_CUR)) { *why = "truncated file"; return -1; }
		} else if (!memcmp(chunk, "data", 4)) {
			if (!channels) { *why = "data before fmt"; return -1; }
			*data_bytes = (long)size;
			return channels;
		} else if (fseek(f, (long)(size + (size & 1U)), SEEK_CUR)) {
			*why = "truncated file";
			return -1;
		}
	}
	*why = "no data chunk";
	return -1;
}

int main(int argc, char **argv)
{
	const char *in_path = NULL, *out_path = NULL, *why = "";
	int raw = 0, channels = 2, i, length;
	double gain = 1.0;
	uint32_t session = TRACE_SESSION;
	long data_bytes = -1;
	FILE *in, *out;
	struct audio_visualizer visualizer;
	struct music_feature_transport transport;
	struct music_features features;
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	int16_t input[TRACE_FRAMES * 2U], stereo[TRACE_FRAMES * 2U];
	char frame[MUSIC_FEATURES_FRAME_MAX];
	unsigned long period = 0, written = 0;

	for (i = 1; i < argc; ++i) {
		if (!strcmp(argv[i], "--raw")) raw = 1;
		else if (!strcmp(argv[i], "--channels") && i + 1 < argc) channels = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--gain-db") && i + 1 < argc) gain = pow(10.0, atof(argv[++i]) / 20.0);
		else if (!strcmp(argv[i], "--session") && i + 1 < argc) session = (uint32_t)strtoul(argv[++i], NULL, 16);
		else if (!in_path) in_path = argv[i];
		else if (!out_path) out_path = argv[i];
		else { fprintf(stderr, "music-trace: unexpected argument %s\n", argv[i]); return 2; }
	}
	if (!in_path || !out_path || channels < 1 || channels > 2 || !session) {
		fprintf(stderr, "usage: music-trace [--raw --channels 1|2] [--gain-db DB] [--session HEX] input output.jsonl\n");
		return 2;
	}
	in = fopen(in_path, "rb");
	if (!in) { fprintf(stderr, "music-trace: %s: %s\n", in_path, strerror(errno)); return 1; }
	if (!raw && (channels = open_wav(in, &data_bytes, &why)) < 0) {
		fprintf(stderr, "music-trace: %s: %s\n", in_path, why);
		fclose(in);
		return 1;
	}
	out = strcmp(out_path, "-") ? fopen(out_path, "wb") : stdout;
	if (!out) { fprintf(stderr, "music-trace: %s: %s\n", out_path, strerror(errno)); fclose(in); return 1; }

	audio_visualizer_init(&visualizer);
	memset(&features, 0, sizeof(features));
	music_feature_transport_init(&transport, session, TRACE_FRAMES, AUDIO_VISUALIZER_RATE);
	for (;;) {
		size_t want = TRACE_FRAMES * (size_t)channels, got, f;

		if (data_bytes >= 0 && (long)(want * 2U) > data_bytes)
			want = (size_t)data_bytes / 2U;
		got = want ? fread(input, 2, want, in) : 0;
		if (data_bytes >= 0)
			data_bytes -= (long)(got * 2U);
		if (got < (size_t)channels)
			break;
		/* Whole periods only, as the engine analyses; the tail is zero-padded. */
		memset(stereo, 0, sizeof(stereo));
		for (f = 0; f < got / (size_t)channels; ++f) {
			double l = input[f * (size_t)channels] * gain;
			double r = input[f * (size_t)channels + (size_t)(channels - 1)] * gain;
			stereo[f * 2U] = (int16_t)(l > 32767.0 ? 32767 : l < -32768.0 ? -32768 : lround(l));
			stereo[f * 2U + 1U] = (int16_t)(r > 32767.0 ? 32767 : r < -32768.0 ? -32768 : lround(r));
		}
		music_feature_transport_tick(&transport);
		audio_visualizer_process_features(&visualizer, stereo, TRACE_FRAMES, 2, levels, &features);
		if ((period++ % TRACE_FRAME_PERIODS) == 0) {
			uint32_t seq = music_feature_transport_next_seq(&transport);
			uint32_t timestamp = music_feature_transport_timestamp_ms(&transport);

			length = music_features_format_frame(frame, sizeof(frame), &features, levels, 70,
							     MUSIC_FEATURES_VERSION, transport.session, seq, timestamp);
			if (length <= 0) {
				fprintf(stderr, "music-trace: frame formatting failed at period %lu\n", period);
				return 1;
			}
			fputs(frame, out);
			++written;
		}
		if (got < want)
			break;
	}
	fclose(in);
	if (out != stdout && fclose(out)) { fprintf(stderr, "music-trace: write failed\n"); return 1; }
	fprintf(stderr, "music-trace: %lu periods, %lu frames\n", period, written);
	return 0;
}
