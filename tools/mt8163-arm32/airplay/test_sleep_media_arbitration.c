/*
 * Host test: nursery/sleep audio through the real Platform PCM engine.
 *
 * Scope (core-audio review items F2/F3): prove by EXECUTING the production
 * engine translation unit -- not by grepping it -- that
 *
 *   1. a generated sleep signal written to the media FIFO is read by
 *      read_sources() and carried by mix_sources_frame();
 *   2. an announcement period on the announcement bus ducks media by the
 *      engine's MEDIA_DUCK_Q15 factor and is summed into the same mono mix;
 *   3. when the announcement producer finishes, the engine's own
 *      read/consume cycle restores media to its unducked gain (resume);
 *   4. the AEC reference published by write_period() is exactly the rendered
 *      mono programme (the actual mix) with the bus activity mask.
 *
 * The engine is compiled in, not mocked: audio_engine.c is #included (its
 * production main is renamed) so its static mix/render/publish functions run
 * unchanged.  Only tinyalsa is stubbed -- never a live card, ALSA or sysfs --
 * and the media waveform comes from the shipped sleep_generator.h.
 */
#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define main engine_program_main
#include "audio_engine.c"
#undef main

#include "sleep_generator.h"

static int failures;

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
        ++failures; \
    } \
} while (0)

#define PERIOD_BYTES (PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t))
#define SAMPLE_BYTES (INPUT_CHANNELS * sizeof(int16_t))

static int bind_receiver(const char *root, struct sockaddr_un *address)
{
    char path[256];
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);

    if (fd < 0)
        return -1;
    snprintf(path, sizeof(path), "%s/%s", root, LE_AEC_REFERENCE_SOCKET);
    if (strlen(path) >= sizeof(address->sun_path)) {
        close(fd);
        return -1;
    }
    memset(address, 0, sizeof(*address));
    address->sun_family = AF_UNIX;
    memcpy(address->sun_path, path, strlen(path) + 1);
    if (bind(fd, (struct sockaddr *)address,
             (socklen_t)(offsetof(struct sockaddr_un, sun_path) +
                         strlen(address->sun_path) + 1)) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static void fill_constant(int16_t *pcm, size_t frames, int16_t value)
{
    size_t i;

    for (i = 0; i < frames * INPUT_CHANNELS; ++i)
        pcm[i] = value;
}

static int write_bytes(int fd, const void *buffer, size_t bytes)
{
    const unsigned char *cursor = buffer;
    size_t sent = 0;
    int spins = 0;

    while (sent < bytes) {
        ssize_t n = write(fd, cursor + sent, bytes - sent);

        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (++spins > 1000)
                return -1;
            continue;
        }
        if (n <= 0)
            return -1;
        sent += (size_t)n;
    }
    return 0;
}

static int32_t mono_at(const struct source_bus *source, size_t frame)
{
    return ((int32_t)source->samples[frame * INPUT_CHANNELS] +
            (int32_t)source->samples[frame * INPUT_CHANNELS + 1]) / 2;
}

static int peak16(const int16_t *pcm, size_t samples)
{
    int peak = 0;
    size_t i;

    for (i = 0; i < samples; ++i) {
        int v = pcm[i] < 0 ? -pcm[i] : pcm[i];

        if (v > peak)
            peak = v;
    }
    return peak;
}

static int write_master_volume(const char *root)
{
    char path[256];
    int fd;

    if (snprintf(path, sizeof(path), "%s/master.volume", root) >=
        (int)sizeof(path))
        return -1;
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        return -1;
    if (write(fd, "80\n", 3) != 3) {
        close(fd);
        return -1;
    }
    close(fd);
    return 0;
}

int main(int argc, char **argv)
{
    char root[256];
    struct source_bus sources[SOURCE_COUNT];
    struct le_sleep_gen sleep;
    struct le_aec_reference_sender reference;
    struct puffin_dynamics dynamics;
    struct speaker_dsp speaker;
    struct sockaddr_un aec_address;
    struct le_aec_reference_packet packet;
    int16_t *sleep_pcm = NULL;
    int16_t *announcement = NULL;
    int16_t *output = NULL;
    int16_t *media_only = NULL;
    const size_t sleep_frames = PERIOD_SIZE * LE_AUDIO_PERIOD_BUFFER_PERIODS;
    const size_t sleep_bytes = sleep_frames * SAMPLE_BYTES;
    ssize_t received;
    int media_wr = -1, announcement_wr = -1;
    int aec = -1, master, gauge;
    unsigned int frame, guard;
    long ready_frames;
    int32_t current_master;
    int result = 1;

    memset(sources, 0, sizeof(sources));
    memset(&reference, 0, sizeof(reference));
    reference.fd = -1;

    if (argc > 1) {
        snprintf(root, sizeof(root), "%s", argv[1]);
    } else {
        snprintf(root, sizeof(root), "/tmp/libreecho-sleep-arb.XXXXXX");
        if (mkdtemp(root) == NULL) {
            perror("mkdtemp");
            return 1;
        }
    }

    sleep_pcm = malloc(sleep_bytes);
    announcement = malloc(PERIOD_BYTES);
    output = malloc(PERIOD_BYTES);
    media_only = malloc(PERIOD_BYTES);
    if (!sleep_pcm || !announcement || !output || !media_only) {
        fprintf(stderr, "sleep media arbitration: out of memory\n");
        goto out;
    }

    /* The media bus carries the real, generated sleep waveform. */
    CHECK(le_sleep_gen_init(&sleep, LE_SLEEP_SOURCE_HEARTBEAT,
                            LE_SLEEP_BED_BROWN, 60, 80, 0, 0, 0x5EEDu) == 0);
    CHECK(le_sleep_gen_fill(&sleep, sleep_pcm, sleep_frames) == sleep_frames);
    CHECK(peak16(sleep_pcm, sleep_frames * INPUT_CHANNELS) > 0);
    fill_constant(announcement, PERIOD_SIZE, 2000);
    CHECK(peak16(announcement, PERIOD_SIZE * INPUT_CHANNELS) > 0);

    CHECK(setup_sources(sources, root) == 0);
    media_wr = open(sources[SOURCE_MEDIA].path, O_WRONLY | O_NONBLOCK);
    announcement_wr = open(sources[SOURCE_ANNOUNCEMENT].path,
                           O_WRONLY | O_NONBLOCK);
    CHECK(media_wr >= 0 && announcement_wr >= 0);
    CHECK(write_bytes(media_wr, sleep_pcm, sleep_bytes) == 0);

    /* --- Media only: the sleep signal reaches the engine untouched -------- */
    CHECK(read_sources(sources, root) >= 0);
    CHECK(source_period_ready(&sources[SOURCE_MEDIA]));
    CHECK(!higher_priority_active(sources));
    CHECK(sources[SOURCE_MEDIA].gain_q15 == 32768);
    {
        int media_energy = 0;

        for (frame = 0; frame < PERIOD_SIZE; ++frame) {
            int32_t mono = mono_at(&sources[SOURCE_MEDIA], frame);

            if (mono != 0)
                ++media_energy;
            CHECK(mix_sources_frame(sources, frame) == mono);
        }
        CHECK(media_energy > 0);                 /* the mix is not silence */
    }

    /* A defined master (file, never sysfs) so the render gain is known. */
    CHECK(write_master_volume(root) == 0);
    master = logical_master_volume(root);
    CHECK(master == 80);
    gauge = logical_master_gain(master);

    aec = bind_receiver(root, &aec_address);
    CHECK(aec >= 0);
    CHECK(le_aec_reference_init(&reference, root) == 0);

    puffin_dynamics_init(&dynamics);
    speaker_dsp_init(&speaker, speaker_volume_percent(sources, master));
    current_master = gauge;
    render_period(sources, output, &dynamics, &speaker, gauge,
                  &current_master);
    CHECK(peak16(output, PERIOD_SIZE * OUTPUT_CHANNELS) > 0);
    memcpy(media_only, output, PERIOD_BYTES);
    CHECK(write_period(NULL, output, &reference,
                       ready_activity_mask(sources)) == 0);
    received = recv(aec, &packet, sizeof(packet), 0);
    CHECK(received ==
          (ssize_t)(sizeof(packet.header) + PERIOD_SIZE * sizeof(int16_t)));
    CHECK(packet.header.activity_mask == PLAYBACK_BUS_MEDIA);
    CHECK(packet.header.frames == PERIOD_SIZE);
    CHECK(packet.header.channels == 1);
    for (frame = 0; frame < PERIOD_SIZE; ++frame)
        CHECK(packet.samples[frame] == output[frame * OUTPUT_CHANNELS]);

    /* --- Announcement: duck media, sum it into the same mix --------------- */
    CHECK(write_bytes(announcement_wr, announcement, PERIOD_BYTES) == 0);
    CHECK(read_sources(sources, root) >= 0);
    CHECK(source_period_ready(&sources[SOURCE_ANNOUNCEMENT]));
    CHECK(higher_priority_active(sources));
    {
        int media_positive = 0;

        for (frame = 0; frame < PERIOD_SIZE; ++frame) {
            int32_t media_mono = mono_at(&sources[SOURCE_MEDIA], frame);
            int32_t ann_mono = mono_at(&sources[SOURCE_ANNOUNCEMENT], frame);
            int32_t ducked =
                (int32_t)(((int64_t)media_mono * MEDIA_DUCK_Q15) >> 15);

            CHECK(mix_sources_frame(sources, frame) == ducked + ann_mono);
            if (media_mono > 0) {
                ++media_positive;
                CHECK(ducked < media_mono);      /* media really was ducked */
            }
        }
        CHECK(media_positive > 0);
    }

    speaker_dsp_init(&speaker, speaker_volume_percent(sources, master));
    current_master = gauge;
    render_period(sources, output, &dynamics, &speaker, gauge,
                  &current_master);
    CHECK(write_period(NULL, output, &reference,
                       ready_activity_mask(sources)) == 0);
    received = recv(aec, &packet, sizeof(packet), 0);
    CHECK(received ==
          (ssize_t)(sizeof(packet.header) + PERIOD_SIZE * sizeof(int16_t)));
    CHECK(packet.header.activity_mask ==
          (PLAYBACK_BUS_MEDIA | PLAYBACK_BUS_ANNOUNCEMENT));
    for (frame = 0; frame < PERIOD_SIZE; ++frame)
        CHECK(packet.samples[frame] == output[frame * OUTPUT_CHANNELS]);
    /* The announcement changed the actual mix the reference carries. */
    CHECK(memcmp(output, media_only, PERIOD_BYTES) != 0);

    /* --- Resume: the announcement producer finished ----------------------- */
    consume_period(sources);
    CHECK(!higher_priority_active(sources));
    for (guard = 0; guard < SOURCE_IDLE_PERIODS + 2; ++guard)
        CHECK(read_sources(sources, root) >= 0);
    CHECK(!higher_priority_active(sources));
    CHECK(source_period_ready(&sources[SOURCE_MEDIA]));
    ready_frames = (long)(sources[SOURCE_MEDIA].received / SAMPLE_BYTES);
    CHECK(ready_frames > 0);
    for (frame = 0; frame < (unsigned int)ready_frames; ++frame)
        CHECK(mix_sources_frame(sources, frame) ==
              mono_at(&sources[SOURCE_MEDIA], frame));

    result = failures ? 1 : 0;

out:
    if (failures == 0 && result == 0)
        puts("sleep media arbitration: sleep->media mix, announcement duck "
             "and restore, AEC reference = actual mixed programme: ok");
    le_aec_reference_close(&reference);
    if (aec >= 0)
        close(aec);
    if (media_wr >= 0)
        close(media_wr);
    if (announcement_wr >= 0)
        close(announcement_wr);
    close_sources(sources);
    free(sleep_pcm);
    free(announcement);
    free(output);
    free(media_only);
    return result;
}
