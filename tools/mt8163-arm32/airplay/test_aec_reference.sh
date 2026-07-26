#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d /tmp/libreecho-aec-reference-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

cc -D_POSIX_C_SOURCE=200809L -std=c99 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/aec_reference.c" "$SCRIPT_DIR/test_aec_reference.c" \
  -o "$WORK/test-aec-reference"
"$WORK/test-aec-reference"
