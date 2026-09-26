/* Host-only deterministic checks of the original Radar MBCL approximation. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include "speaker_mbcl.h"

#define RATE 48000
#define PI 3.14159265358979323846
#define PCM 32768.0f
#define CHECK(expr) do { if (!(expr)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #expr); \
    exit(1); \
} } while (0)

static float tone(int n, double hz, float amplitude)
{
    return amplitude * (float)sin(2.0 * PI * hz * n / RATE);
}

static double measure_tone(double hz, float amplitude, int lead, int frames)
{
    struct speaker_mbcl m;
    double sin_part = 0.0, cos_part = 0.0;
    int n;
    speaker_mbcl_init(&m);
    for (n = 0; n < lead + frames; ++n) {
        float y = speaker_mbcl_process(&m, tone(n, hz, amplitude));
        CHECK(isfinite(y));
        if (n >= lead) {
            sin_part += (double)y * sin(2.0 * PI * hz * n / RATE);
            cos_part += (double)y * cos(2.0 * PI * hz * n / RATE);
        }
    }
    return 2.0 * hypot(sin_part, cos_part) / frames;
}

static void test_filterbank_delay_and_allpass_sum(void)
{
    struct speaker_mbcl m;
    double energy = 0.0;
    int n, k;
    speaker_mbcl_init(&m);
    for (n = 0; n < 4096; ++n) {
        float bands[4], sum = 0.0f;
        speaker_mbcl_split(&m, n == 0 ? 1.0f : 0.0f, bands);
        for (k = 0; k < 4; ++k) {
            if (n < 3) CHECK(bands[k] == 0.0f);
            sum += bands[k];
        }
        energy += (double)sum * sum;
    }
    /* LR4 band summation is allpass, not sample-wise input identity. */
    CHECK(fabs(energy - 1.0) < 0.001);
    CHECK(fabsf(speaker_mbcl_process(&m, 0.0f)) < 0.001f);
}

static void test_lr4_crossover_transfer(void)
{
    const double frequency = 125.0;
    struct speaker_mbcl m;
    double sine[4] = {0}, cosine[4] = {0};
    int n, k;
    speaker_mbcl_init(&m);
    for (n = 0; n < RATE; ++n) {
        float bands[4];
        speaker_mbcl_split(&m, tone(n, frequency, 1.0f), bands);
        if (n < RATE / 2) continue;
        for (k = 0; k < 4; ++k) {
            sine[k] += bands[k] * sin(2.0 * PI * frequency * n / RATE);
            cosine[k] += bands[k] * cos(2.0 * PI * frequency * n / RATE);
        }
    }
    /* Analytic 70/200/3250-Hz LR4 target, rather than one-pole residual. */
    {
        double low = 2.0 * hypot(sine[0], cosine[0]) / (RATE / 2);
        double mid = 2.0 * hypot(sine[1], cosine[1]) / (RATE / 2);
        CHECK(low > 0.08 && low < 0.10); /* approximately -21 dB */
        CHECK(mid > 0.77 && mid < 0.81); /* approximately -2 dB */
    }
}

static void test_band_isolation(void)
{
    const double frequencies[4] = {30.0, 120.0, 1000.0, 9000.0};
    int i, n;
    for (i = 0; i < 4; ++i) {
        struct speaker_mbcl m;
        double energy[4] = {0};
        int k;
        speaker_mbcl_init(&m);
        for (n = 0; n < RATE; ++n) {
            float bands[4];
            speaker_mbcl_split(&m, tone(n, frequencies[i], 100.0f), bands);
            if (n >= RATE / 2)
                for (k = 0; k < 4; ++k) energy[k] += bands[k] * bands[k];
        }
        for (k = 0; k < 4; ++k)
            if (k != i) CHECK(energy[i] > energy[k]);
        printf("band %d isolation at %.0f Hz: target energy %.1f\n", i + 1,
               frequencies[i], energy[i]);
    }
}

static void test_low_level_gain_and_distinct_dynamics(void)
{
    const double frequencies[4] = {30.0, 120.0, 1000.0, 9000.0};
    double quiet[4], loud[4];
    int i;
    for (i = 0; i < 4; ++i) {
        quiet[i] = measure_tone(frequencies[i], 0.2f, RATE, RATE);
        loud[i] = measure_tone(frequencies[i], PCM * 0.8f, RATE, RATE);
        CHECK(quiet[i] > 0.1);
        CHECK(loud[i] / (PCM * 0.8) < quiet[i] / 0.2);
    }
    /* The low bands' stronger compression must differ from the upper bands. */
    CHECK(loud[0] / quiet[0] < loud[3] / quiet[3]);
    printf("band dynamics loud/quiet amplitude ratios: %.2f %.2f %.2f %.2f\n",
           loud[0]/quiet[0], loud[1]/quiet[1], loud[2]/quiet[2], loud[3]/quiet[3]);
}

static void test_bass_suppression_before_bus_limiter(void)
{
    struct speaker_mbcl m;
    double bass_peak = 0.0;
    int n;
    speaker_mbcl_init(&m);
    /* +4 dB input and an EQ-boosted bass bus exceed the fullband ceiling.
     * Band processing must suppress bass beyond a fullbus-only -3 dB clamp. */
    for (n = 0; n < RATE * 2; ++n) {
        float y = speaker_mbcl_process(&m, tone(n, 35.0, PCM * 1.8f));
        if (n >= RATE && fabs(y) > bass_peak) bass_peak = fabs(y);
    }
    CHECK(bass_peak < PCM * 0.45);
    printf("EQ-boosted bass peak after bands: %.1f PCM\n", bass_peak);
}

static void test_fullband_pretrim_ceiling_and_wide_input(void)
{
    struct speaker_mbcl m;
    const float ceiling = PCM * 0.7079457844f; /* -3 dBFS, before +3 trim */
    int n;
    float peak = 0;
    speaker_mbcl_init(&m);
    for (n = 0; n < RATE; ++n) {
        float x = tone(n, 41.0, PCM * 4.0f) + tone(n, 7100.0, PCM * 2.0f);
        float y = speaker_mbcl_process(&m, x);
        CHECK(isfinite(y));
        CHECK(fabsf(y) <= ceiling + 0.1f);
        if (fabsf(y) > peak) peak = fabsf(y);
    }
    CHECK(peak > PCM * 0.25f);
    printf("fullband pretrim peak: %.1f PCM (ceiling %.1f)\n", peak, ceiling);
}

static void test_finite_and_buffer_continuity(void)
{
    struct speaker_mbcl continuous, chunked;
    const int total = RATE * 2;
    const int chunk_sizes[] = {1, 127, 128, 17, 251};
    float *reference = malloc((size_t)total * sizeof(*reference));
    int n, start, batch = 0;
    double max_error = 0.0;
    CHECK(reference != NULL);
    speaker_mbcl_init(&continuous);
    speaker_mbcl_init(&chunked);
    for (n = 0; n < total; ++n) {
        float x = tone(n, 65.0, PCM * 0.9f) + tone(n, 2400.0, PCM * 0.3f);
        reference[n] = speaker_mbcl_process(&continuous, x);
        CHECK(isfinite(reference[n]));
    }
    for (start = 0; start < total; ) {
        int end = start + chunk_sizes[batch++ % 5];
        if (end > total) end = total;
        for (n = start; n < end; ++n) {
            float x = tone(n, 65.0, PCM * 0.9f) + tone(n, 2400.0, PCM * 0.3f);
            float b = speaker_mbcl_process(&chunked, x);
            double error = fabs((double)reference[n] - b);
            CHECK(isfinite(b));
            if (error > max_error) max_error = error;
        }
        start = end; /* Preserve state across nonuniform buffer boundaries. */
    }
    CHECK(max_error == 0.0);
    CHECK(speaker_mbcl_process(&chunked, NAN) == 0.0f);
    CHECK(isfinite(speaker_mbcl_process(&chunked, 1234.0f)));
    free(reference);
    printf("chunk/state continuity max error: %.8f PCM\n", max_error);
}

static void test_release_times(void)
{
    const float release_ms[4] = {200.0f, 80.0f, 20.0f, 20.0f};
    int i, n;
    for (i = 0; i < 4; ++i) {
        struct speaker_mbcl m;
        float initial, at_tau;
        speaker_mbcl_init(&m);
        /* Directly seed gain state to isolate the limiter's 1/e release from
         * crossover attack and compressor detection. */
        m.band[i].lim_gain = 0.1f;
        initial = m.band[i].lim_gain;
        for (n = 0; n < (int)(release_ms[i] * RATE / 1000.0f); ++n)
            speaker_mbcl_process(&m, 0.0f);
        at_tau = m.band[i].lim_gain;
        CHECK(at_tau > 0.64f && at_tau < 0.71f);
        printf("band %d release at %.0f ms: %.4f (from %.2f)\n",
               i + 1, release_ms[i], at_tau, initial);
    }
    {
        struct speaker_mbcl m;
        speaker_mbcl_init(&m);
        m.full_gain = 0.1f;
        for (n = 0; n < 960; ++n) speaker_mbcl_process(&m, 0.0f);
        CHECK(m.full_gain > 0.64f && m.full_gain < 0.71f);
        printf("fullband release at 20 ms: %.4f\n", m.full_gain);
    }
}

int main(void)
{
    test_filterbank_delay_and_allpass_sum();
    test_lr4_crossover_transfer();
    test_band_isolation();
    test_low_level_gain_and_distinct_dynamics();
    test_bass_suppression_before_bus_limiter();
    test_fullband_pretrim_ceiling_and_wide_input();
    test_finite_and_buffer_continuity();
    test_release_times();
    puts("speaker MBCL host tests: PASS");
    return 0;
}
