#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-speaker-dsp-test.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

cc -D_POSIX_C_SOURCE=200809L -std=c99 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/test_speaker_dsp.c" -o "$WORK/test-speaker-dsp" -lm
"$WORK/test-speaker-dsp"
python3 "$SCRIPT_DIR/test_speaker_eq26.py"
