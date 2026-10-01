/*
 * Portable feature-packet trace emitter.
 *
 * Drives the real producer path -- audio_visualizer_process_features() then
 * music_features_format_frame() through music_feature_transport -- over a
 * scripted synthetic timeline, and writes one version-2 visualizer packet per
 * emitted frame as JSONL.  The output is what a consumer (LibreEcho-UI #64/#65)
 * will replay for the cross-repository roundtrip.
 *
 * The session id is a fixed nonzero constant and the timestamp is derived from
 * the deterministic update clock, so two runs produce byte-identical traces.
 *
 * Usage: test-music-feature-trace [output.jsonl]
 */
#define _DEFAULT_SOURCE

#include "audio_visualizer.h"
#include "music_features.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TRACE_FRAMES 2048U
#define TRACE_RATE 48000.0
#define TRACE_PERIODS 420
#define TRACE_FRAME_PERIODS 2U
#define TRACE_SESSION 0x2a2a0001U

static const double dense_centres[AUDIO_VISUALIZER_BANDS] = {
	63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6500, 11000
};

static void buffer_add(int16_t *buffer, unsigned int frame, double value)
{
	double l = (double)buffer[frame * 2U] + value;
	double r = (double)buffer[frame * 2U + 1U] + value;

	if (l > 32767.0)
		l = 32767.0;
	if (l < -32768.0)
		l = -32768.0;
	if (r > 32767.0)
		r = 32767.0;
	if (r < -32768.0)
		r = -32768.0;
	buffer[frame * 2U] = (int16_t)lround(l);
	buffer[frame * 2U + 1U] = (int16_t)lround(r);
}

static void add_tone(int16_t *buffer, int period, double frequency,
		     double amplitude)
{
	double base = (double)period * TRACE_FRAMES;
	unsigned int frame;

	for (frame = 0; frame < TRACE_FRAMES; ++frame) {
		double phase = 2.0 * 3.14159265358979323846 * frequency *
			(base + frame) / TRACE_RATE;

		buffer_add(buffer, frame, sin(phase) * amplitude);
	}
}

static void add_click(int16_t *buffer, int period, double frequency,
		      double amplitude, double decay)
{
	double base = (double)period * TRACE_FRAMES;
	unsigned int frame;

	for (frame = 0; frame < TRACE_FRAMES; ++frame) {
		double seconds = (double)frame / TRACE_RATE;
		double phase = 2.0 * 3.14159265358979323846 * frequency *
			(base + frame) / TRACE_RATE;

		buffer_add(buffer, frame,
			   sin(phase) * amplitude * exp(-seconds / decay));
	}
}

static void add_dense(int16_t *buffer, int period, double amplitude)
{
	unsigned int band;

	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
		add_tone(buffer, period, dense_centres[band],
			 amplitude / (double)AUDIO_VISUALIZER_BANDS * 2.0);
}

/*
 * Scripted timeline: tempo click train -> build -> brief dip -> impact ->
 * loud passage -> quiet gap -> re-entry.  The exact shape is not a musical
 * claim; it exercises every event gate and the slow axes.
 */
static void generate(int period, int16_t *buffer)
{
	if (period < 120) {
		if ((period % 12) == 0)
			add_click(buffer, period, 90.0, 24000.0, 0.05);
	} else if (period < 220) {
		double t = (double)(period - 120) / 100.0;

		add_dense(buffer, period, 8000.0 + 22000.0 * t);
	} else if (period < 228) {
		add_dense(buffer, period, 4000.0);
	} else if (period == 228) {
		add_dense(buffer, period, 30000.0);
		add_click(buffer, period, 70.0, 28000.0, 0.12);
		add_click(buffer, period, 5000.0, 16000.0, 0.03);
	} else if (period < 300) {
		add_dense(buffer, period, 26000.0);
	} else if (period < 340) {
		/* quiet gap */
	} else {
		add_tone(buffer, period, 220.0, 18000.0);
		add_tone(buffer, period, 1000.0, 12000.0);
	}
}

int main(int argc, char **argv)
{
	const char *path = argc > 1 ? argv[1] : "-";
	struct audio_visualizer visualizer;
	struct music_feature_transport transport;
	struct music_features features;
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	int16_t buffer[TRACE_FRAMES * 2U];
	char frame[MUSIC_FEATURES_FRAME_MAX];
	FILE *out = stdout;
	unsigned long written = 0;
	int period;
	int max_length = 0;

	if (strcmp(path, "-") != 0) {
		out = fopen(path, "wb");
		if (!out) {
			fprintf(stderr, "cannot open %s\n", path);
			return 1;
		}
	}

	audio_visualizer_init(&visualizer);
	memset(&features, 0, sizeof(features));
	music_feature_transport_init(&transport, TRACE_SESSION, TRACE_FRAMES,
				     (uint32_t)TRACE_RATE);

	for (period = 0; period < TRACE_PERIODS; ++period) {
		memset(buffer, 0, sizeof(buffer));
		generate(period, buffer);
		music_feature_transport_tick(&transport);
		audio_visualizer_process_features(&visualizer, buffer,
						  TRACE_FRAMES, 2, levels,
						  &features);
		if ((period % TRACE_FRAME_PERIODS) != 0)
			continue;
		{
			uint32_t seq =
				music_feature_transport_next_seq(&transport);
			uint32_t timestamp =
				music_feature_transport_timestamp_ms(&transport);
			int length = music_features_format_frame(
				frame, sizeof(frame), &features, levels, 70,
				MUSIC_FEATURES_VERSION, transport.session,
				seq, timestamp);

			if (length <= 0) {
				fprintf(stderr, "frame format failed at %d\n",
					period);
				if (out != stdout)
					fclose(out);
				return 1;
			}
			if (length > max_length)
				max_length = length;
			fputs(frame, out);
			++written;
		}
	}

	if (out != stdout)
		fclose(out);
	fprintf(stderr,
		"feature trace: %lu frames, session=0x%08x, max_packet=%d bytes\n",
		written, TRACE_SESSION, max_length);
	if (max_length >= (int)MUSIC_FEATURES_FRAME_MAX) {
		fprintf(stderr, "packet exceeded frame bound\n");
		return 1;
	}
	return 0;
}
