#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
WORK=$(mktemp -d /tmp/libreecho-music-trace-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

${CC:-cc} -O2 -std=c99 -Wall -Wextra -Wpedantic -Werror \
  "$SCRIPT_DIR/audio_visualizer.c" \
  "$SCRIPT_DIR/test_music_feature_trace.c" \
  -lm -o "$WORK/test-music-feature-trace"

"$WORK/test-music-feature-trace" "$WORK/trace-a.jsonl"
"$WORK/test-music-feature-trace" "$WORK/trace-b.jsonl"

if ! cmp -s "$WORK/trace-a.jsonl" "$WORK/trace-b.jsonl"; then
  echo "feature trace is not deterministic" >&2
  exit 1
fi

if [[ $# -ge 1 ]]; then
  cp "$WORK/trace-a.jsonl" "$1"
fi

echo "feature trace determinism: ok ($(wc -l < "$WORK/trace-a.jsonl") frames)"
