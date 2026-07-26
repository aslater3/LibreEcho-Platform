#!/usr/bin/env bash
# Build the small ARM32 openWakeWord runtime from pinned source identities.
set -euo pipefail

UI_SOURCE="${1:?usage: build_runtime.sh <ui-source> <ort-source> <ort-build> <speex-archive> <speex-prefix> <output>}"
ORT_SOURCE="${2:?usage: build_runtime.sh <ui-source> <ort-source> <ort-build> <speex-archive> <speex-prefix> <output>}"
ORT_BUILD="${3:?usage: build_runtime.sh <ui-source> <ort-source> <ort-build> <speex-archive> <speex-prefix> <output>}"
SPEEX_ARCHIVE="${4:?usage: build_runtime.sh <ui-source> <ort-source> <ort-build> <speex-archive> <speex-prefix> <output>}"
SPEEX_PREFIX="${5:?usage: build_runtime.sh <ui-source> <ort-source> <ort-build> <speex-archive> <speex-prefix> <output>}"
OUTPUT="${6:?usage: build_runtime.sh <ui-source> <ort-source> <ort-build> <speex-archive> <speex-prefix> <output>}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
OPS_CONFIG="$SCRIPT_DIR/required_operators.config"
CROSS="${LIBREECHO_WAKE_CROSS:-/usr/bin/arm-linux-gnueabihf-}"
JOBS="${JOBS:-$(nproc)}"
ORT_COMMIT=8f0278c77bf44b0cc83c098c6c722b92a36ac4b5
SPEEX_SHA=d17ca363654556a4ff1d02cc13d9eb1fc5a8642c90b40bd54ce266c3807b91a7

for tool in cmake make python3 autoconf automake libtoolize; do
  command -v "$tool" >/dev/null || {
    echo "ERROR: required build tool is missing: $tool" >&2
    exit 1
  }
done
for compiler in gcc g++ ar ranlib strip; do
  [[ -x "${CROSS}${compiler}" ]] || {
    echo "ERROR: ARMHF toolchain member is missing: ${CROSS}${compiler}" >&2
    exit 1
  }
done
[[ -d "$UI_SOURCE" && -d "$ORT_SOURCE/.git" ]] || {
  echo "ERROR: reviewed UI and ONNX Runtime source checkouts are required" >&2
  exit 1
}
[[ "$(git -C "$ORT_SOURCE" rev-parse HEAD)" == "$ORT_COMMIT" ]] || {
  echo "ERROR: ONNX Runtime source is not pinned v1.27.0 ($ORT_COMMIT)" >&2
  exit 1
}
[[ -f "$SPEEX_ARCHIVE" && ! -L "$SPEEX_ARCHIVE" &&
   "$(sha256sum "$SPEEX_ARCHIVE" | awk '{print $1}')" == "$SPEEX_SHA" ]] || {
  echo "ERROR: SpeexDSP source identity changed" >&2
  exit 1
}
[[ -f "$OPS_CONFIG" ]] || {
  echo "ERROR: reduced operator configuration is missing" >&2
  exit 1
}
[[ ! -e "$OUTPUT" ]] || {
  echo "ERROR: refusing to overwrite wakeword runtime: $OUTPUT" >&2
  exit 1
}

if [[ ! -f "$SPEEX_PREFIX/lib/libspeexdsp.a" ]]; then
  speex_work="$(mktemp -d /tmp/libreecho-speex-arm32.XXXXXX)"
  cleanup_speex_work() {
    if [[ -n "${speex_work:-}" && -d "$speex_work" &&
          "$speex_work" == /tmp/libreecho-speex-arm32.* ]]; then
      rm -rf -- "$speex_work"
    fi
  }
  trap cleanup_speex_work EXIT
  tar -xf "$SPEEX_ARCHIVE" -C "$speex_work"
  speex_source="$(find "$speex_work" -mindepth 1 -maxdepth 1 -type d | head -1)"
  (
    cd "$speex_source"
    ./autogen.sh
    CFLAGS="-Os -march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard" \
      ./configure --host=arm-linux-gnueabihf --prefix="$SPEEX_PREFIX" \
        --disable-shared --enable-static --enable-fixed-point \
        --disable-examples
    make -j"$JOBS"
    make install
  )
fi

required_ort_archives=(
  libonnxruntime_session.a libonnxruntime_optimizer.a
  libonnxruntime_providers.a libonnxruntime_graph.a
  libonnxruntime_framework.a libonnxruntime_common.a
  libonnxruntime_mlas.a libonnxruntime_util.a
  libonnxruntime_flatbuffers.a libonnxruntime_lora.a
)
ort_ready=1
for archive in "${required_ort_archives[@]}"; do
  [[ -f "$ORT_BUILD/$archive" ]] || ort_ready=0
done
if [[ "$ort_ready" != 1 ]]; then
  mkdir -p "$ORT_BUILD"
  PYTHONPATH="$ORT_SOURCE/tools/ci_build" \
    python3 - "$OPS_CONFIG" "$ORT_BUILD" <<'PY'
import sys
from reduce_op_kernels import reduce_ops

reduce_ops(
    config_path=sys.argv[1],
    build_dir=sys.argv[2],
    enable_type_reduction=False,
    use_cuda=False,
    is_extended_minimal_build_or_higher=True,
)
PY
  cmake -S "$ORT_SOURCE/cmake" -B "$ORT_BUILD" \
    -DCMAKE_BUILD_TYPE=MinSizeRel \
    -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=arm \
    -DCMAKE_C_COMPILER="${CROSS}gcc" \
    -DCMAKE_CXX_COMPILER="${CROSS}g++" \
    -DCMAKE_AR="${CROSS}ar" -DCMAKE_RANLIB="${CROSS}ranlib" \
    -DCMAKE_STRIP="${CROSS}strip" \
    -DCMAKE_C_FLAGS="-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -ffunction-sections -fdata-sections" \
    -DCMAKE_CXX_FLAGS="-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -ffunction-sections -fdata-sections" \
    -Donnxruntime_CROSS_COMPILING=ON \
    -Donnxruntime_BUILD_SHARED_LIB=OFF \
    -Donnxruntime_BUILD_UNIT_TESTS=OFF \
    -Donnxruntime_BUILD_BENCHMARKS=OFF \
    -Donnxruntime_ENABLE_PYTHON=OFF \
    -Donnxruntime_ENABLE_TRAINING=OFF \
    -Donnxruntime_DISABLE_CONTRIB_OPS=ON \
    -Donnxruntime_DISABLE_ML_OPS=ON \
    -Donnxruntime_DISABLE_GENERATION_OPS=ON \
    -Donnxruntime_DISABLE_OPTIONAL_TYPE=ON \
    -Donnxruntime_DISABLE_SPARSE_TENSORS=ON \
    -Donnxruntime_DISABLE_RTTI=ON \
    -Donnxruntime_REDUCED_OPS_BUILD=ON \
    -Donnxruntime_USE_FULL_PROTOBUF=OFF
  cmake --build "$ORT_BUILD" --parallel "$JOBS"
fi

mkdir -p "$(dirname -- "$OUTPUT")"
make -C "$UI_SOURCE" \
  CROSS_COMPILE="$CROSS" CC=gcc \
  ARM_SPEEX_PREFIX="$SPEEX_PREFIX" \
  WAKE_ORT_SOURCE="$ORT_SOURCE" \
  WAKE_ORT_BUILD="$ORT_BUILD" \
  build/libreecho-waked-onnx-arm32
install -m 0755 "$UI_SOURCE/build/libreecho-waked-onnx-arm32" "$OUTPUT"
file -b "$OUTPUT" | grep -Eq 'ELF 32-bit.*ARM.*statically linked' || {
  echo "ERROR: wakeword runtime output is not static ARM32" >&2
  exit 1
}
echo "wakeword_runtime=$OUTPUT"
sha256sum "$OUTPUT"
