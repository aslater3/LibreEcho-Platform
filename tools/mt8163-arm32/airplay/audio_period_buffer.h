#ifndef LIBREECHO_AUDIO_PERIOD_BUFFER_H
#define LIBREECHO_AUDIO_PERIOD_BUFFER_H

#include <stddef.h>
#include <string.h>

/* Keep two complete output periods available without allowing an unbounded
 * producer burst to grow the engine's per-source storage. */
#define LE_AUDIO_PERIOD_BUFFER_PERIODS 2U

static inline size_t le_audio_period_buffer_append(
    unsigned char *buffer, size_t *used, size_t capacity,
    const void *input, size_t input_bytes)
{
    size_t available;

    if (!buffer || !used || *used > capacity ||
        (input_bytes != 0 && !input))
        return 0;
    available = capacity - *used;
    if (input_bytes > available)
        input_bytes = available;
    if (input_bytes != 0)
        memcpy(buffer + *used, input, input_bytes);
    *used += input_bytes;
    return input_bytes;
}

static inline int le_audio_period_buffer_ready(size_t used,
                                               size_t period_bytes)
{
    return period_bytes != 0 && used >= period_bytes;
}

static inline void le_audio_period_buffer_consume(unsigned char *buffer,
                                                  size_t *used,
                                                  size_t period_bytes)
{
    if (!buffer || !used || period_bytes == 0 || *used < period_bytes)
        return;
    *used -= period_bytes;
    if (*used != 0)
        memmove(buffer, buffer + period_bytes, *used);
}

#endif
