#ifndef LIBREECHO_AUDIO_VISUALIZER_H
#define LIBREECHO_AUDIO_VISUALIZER_H

#include <stddef.h>
#include <stdint.h>

#define AUDIO_VISUALIZER_BANDS 12U
#define AUDIO_VISUALIZER_RATE 48000U

struct audio_visualizer_band {
	int32_t output_1;
	int32_t output_2;
	uint32_t noise_floor;
	uint8_t previous_level;
	uint8_t level;
	uint8_t display_floor;
	uint8_t display_peak;
};

struct audio_visualizer {
	struct audio_visualizer_band bands[AUDIO_VISUALIZER_BANDS];
	int32_t input_1;
	int32_t input_2;
};

void audio_visualizer_init(struct audio_visualizer *visualizer);
void audio_visualizer_reset(struct audio_visualizer *visualizer);
void audio_visualizer_process(struct audio_visualizer *visualizer,
			      const int16_t *samples, size_t frames,
			      size_t stride,
			      uint8_t levels[AUDIO_VISUALIZER_BANDS]);

#endif
