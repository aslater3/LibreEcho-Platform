#!/usr/bin/env bash
# Development build of libreecho-audio-engine for hot-staging on a device.
# Static ARMv7 hard-float, so it runs inside or outside the airplay2 feature
# chroot. Not a release artifact: release builds go through
# tools/mt8163-arm32/airplay/build_airplay.sh against the pinned sysroot.
#
#   build_engine.sh TINYALSA_ARCHIVE OUT_DIR
#
# CROSS_PREFIX defaults to the rootless Ubuntu GCC 15 toolchain used by
# le-ui-arm-build. Extra compiler flags (for example -DLE_ENGINE_TRACE) go in
# ENGINE_CFLAGS.
set -euo pipefail
TINYALSA_ARCHIVE=${1:?TinyALSA source archive (pinned e43025bb)}
OUT=${2:?output directory}
CROSS_PREFIX=${CROSS_PREFIX:-$HOME/.cache/libreecho/toolchains/armhf-gcc15/bin/arm-linux-gnueabihf-}
CC=${CROSS_PREFIX}gcc
AIRPLAY=$(cd -- "$(dirname -- "$0")/../../airplay" && pwd -P)
command -v "$CC" >/dev/null || { echo "missing cross compiler: $CC" >&2; exit 2; }
mkdir -p "$OUT"
work=$(mktemp -d "${TMPDIR:-$HOME/.cache}/le-engine-build.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar -xf "$TINYALSA_ARCHIVE" -C "$work"
tiny=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'tinyalsa-*' -print -quit)
make -s -C "$tiny/src" libtinyalsa.a CROSS_COMPILE="$CROSS_PREFIX" CFLAGS="-O2 -Wno-cpp -I$tiny/include"
flags=(-O2 -std=c99 -Wall -Wextra -Wpedantic -I"$tiny/include" ${ENGINE_CFLAGS:-})
objs=()
for src in audio_engine audio_sink audio_timing audio_visualizer playback_status aec_reference; do
    "$CC" "${flags[@]}" -c "$AIRPLAY/$src.c" -o "$work/$src.o"
    objs+=("$work/$src.o")
done
"$CC" "${flags[@]}" -static "${objs[@]}" "$tiny/src/libtinyalsa.a" -lm \
    -o "$OUT/libreecho-audio-engine"
"${CROSS_PREFIX}strip" "$OUT/libreecho-audio-engine"
sha256sum "$OUT/libreecho-audio-engine"
# Latency tooling: the trace engine and the mic capture tool.
if [[ ${LATENCY_TOOLS:-0} == 1 ]]; then
    "$CC" -O2 -std=c99 -Wall -Wextra -I"$tiny/include" -static \
        "$(dirname -- "$0")/le_latency_cap.c" "$tiny/src/libtinyalsa.a" -lm -o "$OUT/le-latency-cap"
    "${CROSS_PREFIX}strip" "$OUT/le-latency-cap"
    sha256sum "$OUT/le-latency-cap"
fi
