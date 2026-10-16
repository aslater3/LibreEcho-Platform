#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d /tmp/libreecho-biscuit-speaker-dsp-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

cc -D_POSIX_C_SOURCE=200809L -std=c99 -O2 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/test_biscuit_speaker_dsp.c" -o "$WORK/test-biscuit-speaker-dsp" -lm
"$WORK/test-biscuit-speaker-dsp"
