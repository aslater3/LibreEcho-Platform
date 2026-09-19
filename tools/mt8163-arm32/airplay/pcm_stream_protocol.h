/* LibreEcho PCM stream protocol v1. Keep identical in UI and Platform.
 * A SOCK_SEQPACKET connection is one immutable playback generation. Ordered
 * FINISH preserves every accepted frame; an un-finished disconnect cancels.
 * All fields and PCM samples are little endian. No native structs on the wire.
 */
#ifndef LIBREECHO_PCM_STREAM_PROTOCOL_H
#define LIBREECHO_PCM_STREAM_PROTOCOL_H
#include <stdint.h>
#include <stddef.h>
#define LE_PCM_MAGIC 0x3150434cU
#define LE_PCM_VERSION 1U
#define LE_PCM_HEADER 32U
#define LE_PCM_PACKET_FRAMES 1024U
#define LE_PCM_FRAME_BYTES 4U
#define LE_PCM_PACKET_BYTES (LE_PCM_HEADER + LE_PCM_PACKET_FRAMES * LE_PCM_FRAME_BYTES)
#define LE_PCM_FOCUS 0x100U
#define LE_PCM_SOCKET "streams.sock"
enum le_pcm_message { LE_PCM_OPEN = 1, LE_PCM_DATA, LE_PCM_FINISH,
                      LE_PCM_CANCEL, LE_PCM_QUERY, LE_PCM_STATE };
enum le_pcm_state { LE_PCM_ACCEPTING = 1, LE_PCM_FINISHING, LE_PCM_DRAINED,
                    LE_PCM_CANCELLED, LE_PCM_FAILED };
static inline void le_pcm_put32(unsigned char *p, uint32_t v)
{
    unsigned int i;
    for (i = 0; i < 4; ++i) p[i] = (unsigned char)(v >> (i * 8U));
}
static inline uint32_t le_pcm_get32(const unsigned char *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static inline void le_pcm_put64(unsigned char *p, uint64_t v)
{
    le_pcm_put32(p, (uint32_t)v); le_pcm_put32(p + 4, (uint32_t)(v >> 32));
}
static inline uint64_t le_pcm_get64(const unsigned char *p)
{
    return le_pcm_get32(p) | ((uint64_t)le_pcm_get32(p + 4) << 32);
}
static inline void le_pcm_header(unsigned char *p, unsigned int kind,
                                  unsigned int count, unsigned int role)
{
    le_pcm_put32(p, LE_PCM_MAGIC); le_pcm_put32(p + 4, kind);
    le_pcm_put32(p + 8, count); le_pcm_put32(p + 12, role);
    le_pcm_put64(p + 16, 0); le_pcm_put64(p + 24, 0);
}
#endif
