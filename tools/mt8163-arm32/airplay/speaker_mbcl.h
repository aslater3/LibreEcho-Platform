#ifndef LIBREECHO_SPEAKER_MBCL_H
#define LIBREECHO_SPEAKER_MBCL_H

/*
 * Original, mono, 48 kHz approximation of Radar's stock MBCL configuration.
 * No proprietary coefficients or vendor filter/detector implementation are used.
 * Three sequential one-pole low-pass/residual splits form complementary bands:
 * their sample-wise sum is the input, even at crossover transients.  The
 * independent peak detectors use instantaneous attack and exponential release;
 * these are deliberately not claimed to reproduce the vendor's internals.
 *
 * Input and output are wide PCM sample units (32768 units = 0 dBFS), never
 * S16-clamped here.  The -3 dBFS bus limiter precedes the caller's +3 dB trim.
 */
#include <math.h>
#include <string.h>

#define SPEAKER_MBCL_RATE 48000.0f
#define SPEAKER_MBCL_PCM 32768.0f
#define SPEAKER_MBCL_BANDS 4
#define SPEAKER_MBCL_CEILING (SPEAKER_MBCL_PCM * 0.7079457844f)

struct speaker_mbcl_band {
    float detector;
    float detector_release;
    float lim_gain;
    float lim_release;
};

struct speaker_mbcl {
    float lp[3];
    float lp_alpha[3];
    struct speaker_mbcl_band band[SPEAKER_MBCL_BANDS];
    float full_gain;
    float full_release;
};

/* Exponential one-pole: 1/e decay in the configured time interval. */
static inline float speaker_mbcl_release(float milliseconds)
{
    return expf(-1000.0f / (SPEAKER_MBCL_RATE * milliseconds));
}

static inline void speaker_mbcl_init(struct speaker_mbcl *m)
{
    static const float fc[3] = {70.0f, 200.0f, 3250.0f};
    static const float release[4] = {200.0f, 80.0f, 20.0f, 20.0f};
    int i;
    memset(m, 0, sizeof(*m));
    for (i = 0; i < 3; ++i)
        m->lp_alpha[i] = 1.0f - expf(-6.283185307179586f * fc[i] / SPEAKER_MBCL_RATE);
    for (i = 0; i < SPEAKER_MBCL_BANDS; ++i) {
        m->band[i].lim_gain = 1.0f;
        m->band[i].lim_release = speaker_mbcl_release(release[i]);
        m->band[i].detector_release = m->band[i].lim_release;
    }
    m->full_gain = 1.0f;
    m->full_release = speaker_mbcl_release(20.0f);
}

/* This independent crossover entry point also permits direct unity testing. */
static inline void speaker_mbcl_split(struct speaker_mbcl *m, float x, float out[4])
{
    int i;
    for (i = 0; i < 3; ++i) {
        m->lp[i] += m->lp_alpha[i] * (x - m->lp[i]);
        out[i] = m->lp[i];
        x -= out[i];
    }
    out[3] = x;
}

/* Instantaneous attack, 1/e recovery of gain toward unity. */
static inline float speaker_mbcl_limit(float x, float ceiling,
                                       float *gain, float release)
{
    float target = 1.0f;
    float magnitude = fabsf(x);
    if (magnitude > ceiling)
        target = ceiling / magnitude;
    if (target < *gain)
        *gain = target;
    else {
        *gain = 1.0f - (1.0f - *gain) * release;
        if (*gain > target) *gain = target;
    }
    /* Roundoff in x * (ceiling / fabs(x)) must not exceed the ceiling. */
    x *= *gain;
    return fmaxf(-ceiling, fminf(ceiling, x));
}

static inline float speaker_mbcl_process(struct speaker_mbcl *m, float sample)
{
    /* Settings transcribed as PARAMETERS, not as vendor-designed coefficients. */
    static const float comp_in[4] = {1.0f, 1.0f, 1.412537545f, 1.412537545f};
    static const float ratio[4] = {20.0f, 10.0f, 3.0f, 2.0f};
    static const float comp_threshold[4] = {
        SPEAKER_MBCL_PCM * 0.0562341325f, /* -25 dB */
        SPEAKER_MBCL_PCM * 0.125892541f,  /* -18 dB */
        SPEAKER_MBCL_PCM * 0.177827941f,  /* -15 dB */
        SPEAKER_MBCL_PCM * 0.316227766f   /* -10 dB */
    };
    static const float lim_in[4] = {1.0f, 1.0f, 1.412537545f, 1.0f};
    static const float lim_threshold[4] = {
        SPEAKER_MBCL_PCM * 0.251188643f, /* -12 dB */
        SPEAKER_MBCL_PCM * 0.251188643f, /* -12 dB */
        SPEAKER_MBCL_PCM * 0.630957344f, /* -4 dB */
        SPEAKER_MBCL_CEILING          /* -3 dB */
    };
    float bands[4], sum = 0.0f;
    int i;

    if (!isfinite(sample))
        return 0.0f;
    /* A wide pathological-input guard; never truncate EQ overshoot to S16. */
    if (sample > SPEAKER_MBCL_PCM * 32.0f)
        sample = SPEAKER_MBCL_PCM * 32.0f;
    else if (sample < -SPEAKER_MBCL_PCM * 32.0f)
        sample = -SPEAKER_MBCL_PCM * 32.0f;

    speaker_mbcl_split(m, sample * 1.584893192f, bands); /* +4 dB input */
    for (i = 0; i < SPEAKER_MBCL_BANDS; ++i) {
        struct speaker_mbcl_band *b = &m->band[i];
        float x = bands[i] * comp_in[i];
        float level = fabsf(x);
        float gain = 1.0f;
        if (level > b->detector)
            b->detector = level;
        else
            b->detector *= b->detector_release;
        if (b->detector > comp_threshold[i]) {
            gain = powf(comp_threshold[i] / b->detector,
                        1.0f - 1.0f / ratio[i]);
            if (gain < 0.01f) gain = 0.01f; /* -40 dB max reduction */
        }
        x *= gain * lim_in[i];
        sum += speaker_mbcl_limit(x, lim_threshold[i],
                                  &b->lim_gain, b->lim_release);
    }
    return speaker_mbcl_limit(sum, SPEAKER_MBCL_CEILING,
                              &m->full_gain, m->full_release);
}

#endif /* LIBREECHO_SPEAKER_MBCL_H */
