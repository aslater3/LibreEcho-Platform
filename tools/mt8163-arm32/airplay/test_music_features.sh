#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d /tmp/libreecho-music-features-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

${CC:-cc} -O2 -std=c99 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/audio_visualizer.c" \
  "$SCRIPT_DIR/test_music_features.c" \
  -lm -o "$WORK/test-music-features"
"$WORK/test-music-features"

# Beat tracking on sample-accurate kick/kit tracks (fractional tempo, phase
# motion at low confidence); see test_beat_tracking.c.
${CC:-cc} -O2 -std=c99 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/audio_visualizer.c" \
  "$SCRIPT_DIR/test_beat_tracking.c" \
  -lm -o "$WORK/test-beat-tracking"
"$WORK/test-beat-tracking"
