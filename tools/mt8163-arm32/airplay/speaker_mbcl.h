#ifndef LIBREECHO_SPEAKER_MBCL_H
#define LIBREECHO_SPEAKER_MBCL_H

/*
 * Original, mono, 48 kHz approximation of Radar's stock MBCL configuration.
 * No proprietary coefficients or vendor filter/detector implementation are used.
 * The independently designed LR4/phase-aligned filterbank is measured against
 * the stock four-band output for the configured 48-kHz path. Band summation
 * has flat magnitude but is not a sample-wise identity. The compressor and
 * limiter remain approximations and are not claimed to match stock dynamics.
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

/* Two independently designed Butterworth biquads form each LR4 branch. */
struct speaker_mbcl_biquad {
    double b0, b1, b2, a1, a2;
    double x1, x2, y1, y2;
};
struct speaker_mbcl_pair {
    struct speaker_mbcl_biquad low[2], high[2];
};
struct speaker_mbcl {
    struct speaker_mbcl_pair split[3], phase_low1, phase_low2, phase_mid2;
    float band_delay[SPEAKER_MBCL_BANDS][3];
    unsigned int delay_cursor;
    struct speaker_mbcl_band band[SPEAKER_MBCL_BANDS];
    float full_gain;
    float full_release;
};

static inline void speaker_mbcl_design_biquad(struct speaker_mbcl_biquad *b,
                                               double hz, int high)
{
    const double w = 6.2831853071795864769 * hz / SPEAKER_MBCL_RATE;
    const double alpha = sin(w) / 1.4142135623730950488;
    const double c = cos(w), a0 = 1.0 + alpha;
    b->b0 = (high ? 1.0 + c : 1.0 - c) * 0.5 / a0;
    b->b1 = (high ? -(1.0 + c) : 1.0 - c) / a0;
    b->b2 = b->b0;
    b->a1 = -2.0 * c / a0;
    b->a2 = (1.0 - alpha) / a0;
}

static inline void speaker_mbcl_design_pair(struct speaker_mbcl_pair *p, double hz)
{
    int i;
    for (i = 0; i < 2; ++i) {
        speaker_mbcl_design_biquad(&p->low[i], hz, 0);
        speaker_mbcl_design_biquad(&p->high[i], hz, 1);
    }
}

static inline double speaker_mbcl_biquad_step(struct speaker_mbcl_biquad *b, double x)
{
    double y = b->b0*x + b->b1*b->x1 + b->b2*b->x2 - b->a1*b->y1 - b->a2*b->y2;
    b->x2 = b->x1; b->x1 = x;
    b->y2 = b->y1; b->y1 = y;
    return y;
}

static inline void speaker_mbcl_pair_step(struct speaker_mbcl_pair *p, double x,
                                           double *low, double *high)
{
    int i;
    *low = *high = x;
    for (i = 0; i < 2; ++i) {
        *low = speaker_mbcl_biquad_step(&p->low[i], *low);
        *high = speaker_mbcl_biquad_step(&p->high[i], *high);
    }
}

static inline double speaker_mbcl_phase_step(struct speaker_mbcl_pair *p, double x)
{
    double low, high;
    speaker_mbcl_pair_step(p, x, &low, &high);
    return low + high;
}

/* Exponential one-pole: 1/e decay in the configured time interval. */
static inline float speaker_mbcl_release(float milliseconds)
{
    return expf(-1000.0f / (SPEAKER_MBCL_RATE * milliseconds));
}

static inline void speaker_mbcl_init(struct speaker_mbcl *m)
{
    static const double fc[3] = {70.0, 200.0, 3250.0};
    static const float release[4] = {200.0f, 80.0f, 20.0f, 20.0f};
    int i;
    memset(m, 0, sizeof(*m));
    for (i = 0; i < 3; ++i)
        speaker_mbcl_design_pair(&m->split[i], fc[i]);
    speaker_mbcl_design_pair(&m->phase_low1, fc[1]);
    speaker_mbcl_design_pair(&m->phase_low2, fc[2]);
    speaker_mbcl_design_pair(&m->phase_mid2, fc[2]);
    for (i = 0; i < SPEAKER_MBCL_BANDS; ++i) {
        m->band[i].lim_gain = 1.0f;
        m->band[i].lim_release = speaker_mbcl_release(release[i]);
        m->band[i].detector_release = m->band[i].lim_release;
    }
    m->full_gain = 1.0f;
    m->full_release = speaker_mbcl_release(20.0f);
}

/* Four LR4 bands, later-crossover allpasses and measured three-frame delay. */
static inline void speaker_mbcl_split(struct speaker_mbcl *m, float x, float out[4])
{
    double low0, high0, low1, high1, low2, high2;
    double raw[4];
    int i;
    speaker_mbcl_pair_step(&m->split[0], x, &low0, &high0);
    speaker_mbcl_pair_step(&m->split[1], high0, &low1, &high1);
    speaker_mbcl_pair_step(&m->split[2], high1, &low2, &high2);
    raw[0] = speaker_mbcl_phase_step(&m->phase_low2,
             speaker_mbcl_phase_step(&m->phase_low1, low0));
    raw[1] = speaker_mbcl_phase_step(&m->phase_mid2, low1);
    raw[2] = low2;
    raw[3] = high2;
    for (i = 0; i < SPEAKER_MBCL_BANDS; ++i) {
        out[i] = m->band_delay[i][m->delay_cursor];
        m->band_delay[i][m->delay_cursor] = (float)raw[i];
    }
    m->delay_cursor = (m->delay_cursor + 1u) % 3u;
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
