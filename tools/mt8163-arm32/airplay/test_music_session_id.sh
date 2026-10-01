#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d /tmp/libreecho-music-session-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

${CC:-cc} -O2 -std=c99 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/test_music_session_id.c" \
  -o "$WORK/test-music-session-id"
"$WORK/test-music-session-id"
