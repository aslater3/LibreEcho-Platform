/*
 * Development tool: capture the mic array with CLOCK_MONOTONIC frame stamps
 * and report click onsets per channel.  Pairs with an engine built with
 * -DLE_ENGINE_LATENCY_TRACE to measure physical output latency.  Needs the
 * capture PCM free (stop micd first).  Not shipped.
 *
 *   le-latency-cap SECONDS [card device channels rate]
 *   (defaults: 0 24 9 16000, S24_3LE in 640x4 periods like micd's tinycap)
 */
#define _POSIX_C_SOURCE 200809L
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <tinyalsa/asoundlib.h>

#define MAX_CH 16
#define PERIOD 640

static long long now_us(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}

int main(int argc, char **argv)
{
	double seconds = argc > 1 ? atof(argv[1]) : 4.0;
	unsigned int card = argc > 2 ? (unsigned)atoi(argv[2]) : 0;
	unsigned int device = argc > 3 ? (unsigned)atoi(argv[3]) : 24;
	unsigned int ch = argc > 4 ? (unsigned)atoi(argv[4]) : 9;
	unsigned int rate = argc > 5 ? (unsigned)atoi(argv[5]) : 16000;
	struct pcm_config cfg;
	struct pcm *pcm;
	static uint8_t buf[PERIOD * MAX_CH * 3];
	double noise[MAX_CH] = { 0 };
	long long quiet_until[MAX_CH] = { 0 };
	unsigned long long total = 0, limit;
	unsigned int c, i, baseline_frames;
	long long t_end;

	if (ch == 0 || ch > MAX_CH || seconds <= 0 || seconds > 60)
		return 2;
	memset(&cfg, 0, sizeof(cfg));
	cfg.channels = ch;
	cfg.rate = rate;
	cfg.period_size = PERIOD;
	cfg.period_count = 4;
	cfg.format = PCM_FORMAT_S24_3LE;
	pcm = pcm_open(card, device, PCM_IN | PCM_MONOTONIC, &cfg);
	if (!pcm || !pcm_is_ready(pcm)) {
		fprintf(stderr, "capture open failed: %s\n", pcm ? pcm_get_error(pcm) : "");
		return 1;
	}
	limit = (unsigned long long)(seconds * rate);
	baseline_frames = rate / 4;
	t_end = now_us() + (long long)(seconds * 1e6) + 2000000LL;
	printf("capture card=%u device=%u channels=%u rate=%u\n", card, device, ch, rate);
	while (total < limit && now_us() < t_end) {
		struct timespec ts;
		unsigned int avail = 0;
		long long stamp_us;

		if (pcm_readi(pcm, buf, PERIOD) != (int)PERIOD) {
			fprintf(stderr, "read failed: %s\n", pcm_get_error(pcm));
			break;
		}
		/* hw pointer frame (total + PERIOD + avail) was captured at ts. */
		if (pcm_get_htimestamp(pcm, &avail, &ts) != 0)
			continue;
		stamp_us = (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
		for (i = 0; i < PERIOD; ++i) {
			unsigned long long frame = total + i;
			long long frame_us = stamp_us -
				(long long)((total + PERIOD + avail - frame) * 1000000ULL / rate);

			for (c = 0; c < ch; ++c) {
				const uint8_t *b = &buf[(i * ch + c) * 3];
				int32_t raw = (int32_t)(((uint32_t)b[0] << 8) | ((uint32_t)b[1] << 16) |
							((uint32_t)b[2] << 24)) >> 8;
				double v = fabs((double)raw);

				if (frame < baseline_frames) {
					if (v > noise[c])
						noise[c] = v;
					continue;
				}
				if (v > 6.0 * noise[c] + 2000.0 && frame_us > quiet_until[c]) {
					printf("onset ch=%u mono_us=%lld frame=%llu level=%.0f noise=%.0f\n",
					       c, frame_us, frame, v, noise[c]);
					quiet_until[c] = frame_us + 150000;
				}
				if (v > 3.0 * noise[c] + 1000.0 && quiet_until[c] > frame_us)
					quiet_until[c] = frame_us + 150000;
			}
		}
		total += PERIOD;
	}
	pcm_close(pcm);
	fflush(stdout);
	return 0;
}
