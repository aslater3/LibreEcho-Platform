#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-audio-visualizer-test.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# The analyzer hot path once scaled each sample with a signed left shift, which
# is undefined for negative samples; the media-only visualizer path in the
# shared engine therefore exhibited UB whenever the programme had negative
# PCM.  Build the focused vectors under ASan/UBSan and halt on the first error
# so a reintroduced negative shift fails the suite instead of passing silently.
${CC:-cc} -O1 -g -std=c99 -Wall -Wextra -Wpedantic -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  "$SCRIPT_DIR/audio_visualizer.c" \
  "$SCRIPT_DIR/test_audio_visualizer.c" \
  -lm -o "$WORK/test-audio-visualizer"

UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 \
  "$WORK/test-audio-visualizer"
