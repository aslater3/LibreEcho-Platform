/*
 * Host integration test for the pinned static Opus decode stack built by
 * build_opus.sh (libopusfile.a + libopus.a + libogg.a).
 *
 * The test exercises the real decoder, not just the link:
 *   1. encodes a known 48 kHz stereo sine to five Opus packets with libopus;
 *   2. muxes them into one Ogg Opus logical stream with libogg;
 *   3. decodes it back through libopusfile (op_open_file / op_read_float);
 *   4. requires the decoded frame count to equal the encoded frames minus the
 *      codec look-ahead (the samples opusfile trims as pre-skip), a nonzero
 *      finite signal, and the real channel count;
 *   5. requires op_open_file to reject a non-Opus Ogg stream honestly.
 *
 * Compile with the prefix include layout, for example:
 *   cc -O2 test_decode_host.c -I<PREFIX>/include/opus -I<PREFIX>/include \
 *      <PREFIX>/lib/libopusfile.a <PREFIX>/lib/libopus.a <PREFIX>/lib/libogg.a -lm
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <ogg/ogg.h>
#include <opus.h>
#include <opusfile.h>

#define RATE 48000
#define CHANNELS 2
#define FRAME_SAMPLES 960 /* 20 ms at 48 kHz */
#define PACKETS 5

static int write_page(const ogg_page *page, FILE *out)
{
    if (fwrite(page->header, 1, (size_t)page->header_len, out) !=
        (size_t)page->header_len)
        return -1;
    if (fwrite(page->body, 1, (size_t)page->body_len, out) !=
        (size_t)page->body_len)
        return -1;
    return 0;
}

static int flush_pages(ogg_stream_state *os, FILE *out)
{
    ogg_page page;
    while (ogg_stream_flush(os, &page)) {
        if (write_page(&page, out) != 0)
            return -1;
    }
    return 0;
}

static int write_header_packets(ogg_stream_state *os, int lookahead, FILE *out)
{
    ogg_packet pkt;
    unsigned char head[19];
    unsigned char tags[8 + 4 + 9 + 4];

    memset(head, 0, sizeof(head));
    memcpy(head, "OpusHead", 8);
    head[8] = 1; /* version */
    head[9] = CHANNELS;
    head[10] = (unsigned char)(lookahead & 0xff);
    head[11] = (unsigned char)((lookahead >> 8) & 0xff);
    head[12] = (unsigned char)(RATE & 0xff);
    head[13] = (unsigned char)((RATE >> 8) & 0xff);
    head[14] = (unsigned char)((RATE >> 16) & 0xff);
    head[15] = (unsigned char)((RATE >> 24) & 0xff);
    /* head[16..17] output gain = 0, head[18] mapping family = 0 */
    memset(&pkt, 0, sizeof(pkt));
    pkt.packet = head;
    pkt.bytes = (long)sizeof(head);
    pkt.b_o_s = 1;
    pkt.granulepos = 0;
    pkt.packetno = 0;
    if (ogg_stream_packetin(os, &pkt) != 0)
        return -1;
    if (flush_pages(os, out) != 0)
        return -1;

    memset(tags, 0, sizeof(tags));
    memcpy(tags, "OpusTags", 8);
    tags[8] = 9; /* vendor string length, little-endian */
    memcpy(tags + 12, "libreecho", 9);
    /* tags[21] user comment count = 0 */
    memset(&pkt, 0, sizeof(pkt));
    pkt.packet = tags;
    pkt.bytes = (long)sizeof(tags);
    pkt.packetno = 1;
    if (ogg_stream_packetin(os, &pkt) != 0)
        return -1;
    if (flush_pages(os, out) != 0)
        return -1;
    return 0;
}

static int make_opus_fixture(const char *path, int *decoded_expect)
{
    int err = OPUS_OK, lookahead = 0, i, packet_index;
    OpusEncoder *enc;
    ogg_stream_state os;
    ogg_packet pkt;
    unsigned char packet[4096];
    short pcm[FRAME_SAMPLES * CHANNELS];
    FILE *out;
    long long decoded = 0;

    enc = opus_encoder_create(RATE, CHANNELS, OPUS_APPLICATION_AUDIO, &err);
    if (enc == NULL || err != OPUS_OK)
        return -1;
    if (opus_encoder_ctl(enc, OPUS_SET_BITRATE(96000)) != OPUS_OK)
        return -1;
    if (opus_encoder_ctl(enc, OPUS_GET_LOOKAHEAD(&lookahead)) != OPUS_OK)
        return -1;
    for (i = 0; i < FRAME_SAMPLES; i++) {
        double t = (double)i / RATE;
        short s = (short)(12000.0 *
                          sin(2.0 * 3.14159265358979323846 * 1000.0 * t));
        pcm[2 * i] = s;
        pcm[2 * i + 1] = s;
    }

    out = fopen(path, "wb");
    if (out == NULL)
        return -1;
    if (ogg_stream_init(&os, 0x4c45u) != 0) {
        fclose(out);
        return -1;
    }
    if (write_header_packets(&os, lookahead, out) != 0) {
        ogg_stream_clear(&os);
        fclose(out);
        return -1;
    }

    for (packet_index = 0; packet_index < PACKETS; packet_index++) {
        int packet_len = opus_encode(enc, pcm, FRAME_SAMPLES, packet,
                                     sizeof(packet));
        if (packet_len <= 0) {
            ogg_stream_clear(&os);
            fclose(out);
            return -1;
        }
        decoded += FRAME_SAMPLES;
        memset(&pkt, 0, sizeof(pkt));
        pkt.packet = packet;
        pkt.bytes = packet_len;
        pkt.e_o_s = (packet_index == PACKETS - 1) ? 1 : 0;
        pkt.granulepos = (ogg_int64_t)lookahead + decoded;
        pkt.packetno = 2 + packet_index;
        if (ogg_stream_packetin(&os, &pkt) != 0) {
            ogg_stream_clear(&os);
            fclose(out);
            return -1;
        }
        if (flush_pages(&os, out) != 0) {
            ogg_stream_clear(&os);
            fclose(out);
            return -1;
        }
    }
    ogg_stream_clear(&os);
    opus_encoder_destroy(enc);
    if (fclose(out) != 0)
        return -1;
    /* opusfile trims the codec look-ahead, so the decodable frame count is one
     * look-ahead short of the encoded total. */
    *decoded_expect = (int)(decoded - lookahead);
    return 0;
}

static int decode_and_check(const char *path, int expect)
{
    int total = 0, n, li = 0, err = 0;
    float pcm[FRAME_SAMPLES * CHANNELS];
    double energy = 0.0;
    OggOpusFile *of = op_open_file(path, &err);

    if (of == NULL) {
        fprintf(stderr, "ERROR: op_open_file failed on the fixture: %d\n", err);
        return -1;
    }
    if (op_channel_count(of, -1) != CHANNELS) {
        fprintf(stderr, "ERROR: decoded channel count is not %d\n", CHANNELS);
        op_free(of);
        return -1;
    }
    for (;;) {
        n = op_read_float(of, pcm, FRAME_SAMPLES, &li);
        if (n == 0)
            break;
        if (n < 0) {
            fprintf(stderr, "ERROR: op_read_float failed: %d\n", n);
            op_free(of);
            return -1;
        }
        {
            int i;
            for (i = 0; i < n * CHANNELS; i++) {
                if (!isfinite(pcm[i])) {
                    fprintf(stderr, "ERROR: decoded a non-finite sample\n");
                    op_free(of);
                    return -1;
                }
                energy += (double)pcm[i] * pcm[i];
            }
        }
        total += n;
    }
    op_free(of);
    if (total != expect) {
        fprintf(stderr, "ERROR: decoded %d frames, expected %d\n", total, expect);
        return -1;
    }
    if (!(energy > 1.0)) {
        fprintf(stderr, "ERROR: decoded stream carries no signal (energy=%.6f)\n",
                energy);
        return -1;
    }
    return 0;
}

static int rejects_non_opus(const char *path)
{
    static const unsigned char junk[] = {'O', 'g', 'g', 'S', 0, 2, 0, 0,
                                         0, 0, 0, 0, 0, 0, 0, 0,
                                         1, 2, 3, 4};
    int err = 0;
    OggOpusFile *of;
    FILE *out = fopen(path, "wb");

    if (out == NULL)
        return -1;
    if (fwrite(junk, 1, sizeof(junk), out) != sizeof(junk)) {
        fclose(out);
        return -1;
    }
    fclose(out);
    of = op_open_file(path, &err);
    if (of != NULL) {
        op_free(of);
        fprintf(stderr, "ERROR: op_open_file accepted a non-Opus stream\n");
        return -1;
    }
    if (!(err < 0)) {
        fprintf(stderr, "ERROR: rejecting a non-Opus stream reported no error\n");
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    char good[1024], bad[1024];
    int expect = 0;

    if (argc != 2) {
        fprintf(stderr, "usage: %s WORKDIR\n", argv[0]);
        return 2;
    }
    if (snprintf(good, sizeof(good), "%s/good.opus", argv[1]) >= (int)sizeof(good) ||
        snprintf(bad, sizeof(bad), "%s/bad.opus", argv[1]) >= (int)sizeof(bad)) {
        fprintf(stderr, "ERROR: work directory path is too long\n");
        return 2;
    }

    if (make_opus_fixture(good, &expect) != 0) {
        fprintf(stderr, "ERROR: could not generate the Ogg Opus fixture\n");
        return 1;
    }
    if (decode_and_check(good, expect) != 0)
        return 1;
    if (rejects_non_opus(bad) != 0)
        return 1;

    printf("opus_decode=ok frames=%d channels=%d non_opus=rejected\n",
           expect, CHANNELS);
    return 0;
}
