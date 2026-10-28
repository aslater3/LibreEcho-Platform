#!/usr/bin/env bash
#
# Host test: the music analysis tap (#44) in the real Platform PCM engine.
#
#   sh test_music_analysis_tap.sh
#
# Compiles the production audio_engine.c (via test_music_analysis_tap.c) with
# tinyalsa stubbed in a throwaway work directory: no live ALSA card, mixer or
# sysfs path is touched.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-analysis-tap.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

STUB="$WORK/stub/include/tinyalsa"
mkdir -p "$STUB"

cat > "$STUB/pcm.h" <<'EOF'
/* Host stub for <tinyalsa/pcm.h>.  Records nothing and owns no card. */
#ifndef LE_TEST_TINYALSA_PCM_H
#define LE_TEST_TINYALSA_PCM_H
#include <stddef.h>
#include <stdint.h>
#define PCM_OUT 0U
#define PCM_FORMAT_S16_LE 0
#define PCM_FORMAT_S32_LE 1
struct pcm;
struct pcm_config {
    unsigned int channels;
    unsigned int rate;
    unsigned int period_size;
    unsigned int period_count;
    int format;
    unsigned int start_threshold;
    unsigned int stop_threshold;
    unsigned int silence_threshold;
    unsigned int silence_size;
    unsigned int avail_min;
};
static inline struct pcm *pcm_open(unsigned int card, unsigned int device,
                                   unsigned int flags,
                                   const struct pcm_config *config)
{ (void)card; (void)device; (void)flags; (void)config; return (struct pcm *)1; }
static inline int pcm_is_ready(const struct pcm *pcm)
{ (void)pcm; return 1; }
static inline const char *pcm_get_error(const struct pcm *pcm)
{ (void)pcm; return ""; }
static inline int pcm_prepare(struct pcm *pcm)
{ (void)pcm; return 0; }
static inline int pcm_writei(struct pcm *pcm, const void *data,
                             unsigned int frames)
{ (void)pcm; (void)data; return (int)frames; }
static inline int pcm_close(struct pcm *pcm)
{ (void)pcm; return 0; }
#endif
EOF

cat > "$STUB/mixer.h" <<'EOF'
/* Host stub for <tinyalsa/mixer.h>.  Records nothing and owns no card. */
#ifndef LE_TEST_TINYALSA_MIXER_H
#define LE_TEST_TINYALSA_MIXER_H
struct mixer;
struct mixer_ctl;
static inline struct mixer *mixer_open(unsigned int card)
{ (void)card; return (struct mixer *)1; }
static inline void mixer_close(struct mixer *mixer)
{ (void)mixer; }
static inline struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *mixer,
                                                      const char *name)
{ (void)mixer; (void)name; return (struct mixer_ctl *)1; }
static inline int mixer_ctl_set_enum_by_string(struct mixer_ctl *ctl,
                                               const char *value)
{ (void)ctl; (void)value; return 0; }
static inline unsigned int mixer_ctl_get_num_values(struct mixer_ctl *ctl)
{ (void)ctl; return 2U; }
static inline int mixer_ctl_set_value(struct mixer_ctl *ctl, unsigned int id,
                                      int value)
{ (void)ctl; (void)id; (void)value; return 0; }
static inline int mixer_ctl_get_value(struct mixer_ctl *ctl, unsigned int id)
{ (void)ctl; (void)id; return 127; }
#endif
EOF

CC=${CC:-cc}
CFLAGS="-std=c99 -Wall -Wextra -Wpedantic -Werror \
        -Wno-unused-function -Wno-unused-parameter"

"$CC" $CFLAGS -I"$WORK/stub/include" \
    "$SCRIPT_DIR/test_music_analysis_tap.c" \
    "$SCRIPT_DIR/aec_reference.c" \
    "$SCRIPT_DIR/audio_visualizer.c" \
    "$SCRIPT_DIR/playback_status.c" \
    -lm -o "$WORK/test-music-analysis-tap"

"$WORK/test-music-analysis-tap"
