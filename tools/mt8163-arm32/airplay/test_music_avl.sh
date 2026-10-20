#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d /tmp/libreecho-music-avl-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
cc -D_POSIX_C_SOURCE=200809L -std=c99 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/test_music_avl.c" -o "$WORK/test-music-avl" -lm
"$WORK/test-music-avl"
