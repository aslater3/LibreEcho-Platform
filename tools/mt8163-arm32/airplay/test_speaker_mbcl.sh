#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compiler=${CC:-cc}
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT HUP INT TERM
"$compiler" -std=c99 -O2 -Wall -Wextra -Werror \
    "$here/test_speaker_mbcl.c" -lm -o "$tmp/test_speaker_mbcl"
"$tmp/test_speaker_mbcl"
