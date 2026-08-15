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
FLATBUFFERS_PYTHON="${LIBREECHO_WAKE_FLATBUFFERS_PYTHON:?ERROR: set LIBREECHO_WAKE_FLATBUFFERS_PYTHON to the pinned FlatBuffers Python source}"
RE2_ARCHIVE="${LIBREECHO_WAKE_RE2_ARCHIVE:?ERROR: set LIBREECHO_WAKE_RE2_ARCHIVE to the pinned ARM32 RE2 archive}"
RELINK_OUTPUT="${LIBREECHO_WAKE_RELINK_OUTPUT:?ERROR: set LIBREECHO_WAKE_RELINK_OUTPUT to an immutable run-local directory}"
CROSS="${LIBREECHO_WAKE_CROSS:-/usr/bin/arm-linux-gnueabihf-}"
JOBS="${JOBS:-$(nproc)}"
ORT_COMMIT=8f0278c77bf44b0cc83c098c6c722b92a36ac4b5
SPEEX_SHA=d17ca363654556a4ff1d02cc13d9eb1fc5a8642c90b40bd54ce266c3807b91a7

for tool in cmake make python3 autoconf automake libtoolize flock; do
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
[[ -f "$FLATBUFFERS_PYTHON/flatbuffers/__init__.py" ]] || {
  echo "ERROR: pinned FlatBuffers Python source is unavailable: $FLATBUFFERS_PYTHON" >&2
  exit 1
}
[[ -f "$RE2_ARCHIVE" && ! -L "$RE2_ARCHIVE" ]] || {
  echo "ERROR: pinned ARM32 RE2 archive is unavailable: $RE2_ARCHIVE" >&2
  exit 1
}
[[ ! -e "$OUTPUT" ]] || {
  echo "ERROR: refusing to overwrite wakeword runtime: $OUTPUT" >&2
  exit 1
}
[[ ! -e "$RELINK_OUTPUT" && ! -L "$RELINK_OUTPUT" ]] || {
  echo "ERROR: refusing to overwrite wakeword relink snapshot: $RELINK_OUTPUT" >&2
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
  # The install prefix is a per-run path, but the cached wake-runtime payload
  # must stay byte-identical for identical inputs.  Only the static archive,
  # headers, and docs are consumed downstream; rewrite the libtool/pkg-config
  # metadata to a canonical prefix so it cannot leak the run path.
  sed -i \
    -e "s|^prefix=.*|prefix=/opt/libreecho/speexdsp|" \
    "$SPEEX_PREFIX/lib/pkgconfig/speexdsp.pc"
  sed -i \
    -e "s|^libdir=.*|libdir='/opt/libreecho/speexdsp/lib'|" \
    "$SPEEX_PREFIX/lib/libspeexdsp.la"
fi

required_ort_archives=(
  libonnxruntime_session.a libonnxruntime_optimizer.a
  libonnxruntime_providers.a libonnxruntime_graph.a
  libonnxruntime_framework.a libonnxruntime_common.a
  libonnxruntime_mlas.a libonnxruntime_util.a
  libonnxruntime_flatbuffers.a libonnxruntime_lora.a
)
mkdir -p "$(dirname -- "$ORT_BUILD")"
ort_lock_path="${ORT_BUILD}.lock"
exec {ort_lock_fd}>"$ort_lock_path"
flock -x "$ort_lock_fd"
mkdir -p "$ORT_BUILD"
# ONNX Runtime filters paths matching */ml/* when excluding its own ML
# provider sources.  Keep the checkout's logical CMake path outside that
# pattern; the real source path may legitimately contain an `ml` directory.
ort_source_for_cmake="$ORT_BUILD/.onnxruntime-source"
if [[ -e "$ort_source_for_cmake" || -L "$ort_source_for_cmake" ]]; then
  [[ -L "$ort_source_for_cmake" ]] || {
    echo "ERROR: refusing to replace non-symlink $ort_source_for_cmake" >&2
    exit 1
  }
  [[ "$(readlink -f "$ort_source_for_cmake")" == "$(readlink -f "$ORT_SOURCE")" ]] || {
    echo "ERROR: ONNX Runtime source symlink points at an unexpected checkout" >&2
    exit 1
  }
else
  ln -s -- "$ORT_SOURCE" "$ort_source_for_cmake"
fi
if [[ -x /usr/bin/python3 ]]; then
  ort_python=/usr/bin/python3
else
  ort_python="$(command -v python3)"
fi
[[ -n "$ort_python" && -x "$ort_python" ]] || {
  echo "ERROR: no usable host python3 interpreter" >&2
  exit 1
}
ort_ready=1
for archive in "${required_ort_archives[@]}"; do
  [[ -f "$ORT_BUILD/$archive" ]] || ort_ready=0
done
provider_archive="$ORT_BUILD/libonnxruntime_providers.a"
if [[ "$ort_ready" == 1 ]]; then
  provider_symbols="$ORT_BUILD/provider-symbols.txt"
  "${CROSS}nm" -C "$provider_archive" > "$provider_symbols"
  grep -Fq 'CPUExecutionProvider::CPUExecutionProvider' "$provider_symbols" || ort_ready=0
  grep -Fq 'OrtApis::CreateCpuMemoryInfo' "$provider_symbols" || ort_ready=0
fi
if [[ "$ort_ready" != 1 ]]; then
  [[ "$ORT_BUILD" == */onnxruntime-wake-reduced ]] || {
    echo "ERROR: refusing to clean an unexpected ONNX Runtime build directory" >&2
    exit 1
  }
  rm -rf -- "$ORT_BUILD"
  mkdir -p "$ORT_BUILD"
  ln -s -- "$ORT_SOURCE" "$ort_source_for_cmake"
  PYTHONPATH="$FLATBUFFERS_PYTHON:$ORT_SOURCE/tools/ci_build${PYTHONPATH:+:$PYTHONPATH}" \
    "$ort_python" - "$OPS_CONFIG" "$ORT_BUILD" <<'PY'
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
  # ORT logging macros expand __FILE__ into compiled objects, so a build that
  # records its own per-run directory would produce different bytes for the
  # same inputs on every run.  Rewrite both the build directory and the source
  # checkout to canonical prefixes so the reduced archives are reproducible
  # and the wake-ort component cache can store them without collisions.
  ort_repro_flags="-ffile-prefix-map=$ORT_BUILD=ort-build -ffile-prefix-map=$ORT_SOURCE=ort-src"
  cmake -S "$ort_source_for_cmake/cmake" -B "$ORT_BUILD" \
    -DPython_EXECUTABLE="$ort_python" \
    -DCMAKE_BUILD_TYPE=MinSizeRel \
    -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=arm \
    -DCMAKE_C_COMPILER="${CROSS}gcc" \
    -DCMAKE_CXX_COMPILER="${CROSS}g++" \
    -DCMAKE_AR="${CROSS}ar" -DCMAKE_RANLIB="${CROSS}ranlib" \
    -DCMAKE_STRIP="${CROSS}strip" \
    -DCMAKE_C_FLAGS="-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -ffunction-sections -fdata-sections $ort_repro_flags" \
    -DCMAKE_CXX_FLAGS="-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -ffunction-sections -fdata-sections $ort_repro_flags" \
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
  RE2_ARCHIVE="$RE2_ARCHIVE" \
  WAKE_ORT_SOURCE="$ORT_SOURCE" \
  WAKE_ORT_BUILD="$ORT_BUILD" \
  build/libreecho-waked-onnx-arm32
relink_objects=(
  waked.wake.arm.o voice_aec.wake.arm.o voice_reference.wake.arm.o
  voice_dsp.wake.arm.o voice_stream.wake.arm.o wake_worker.wake.arm.o
  wake_led.wake.arm.o adapter_client.wake.arm.o adapter_server.wake.arm.o
  log.wake.arm.o wake_engine_onnx.arm.o
)
mkdir -p "$RELINK_OUTPUT"
for object_name in "${relink_objects[@]}"; do
  object="$UI_SOURCE/build/$object_name"
  [[ -f "$object" && ! -L "$object" ]] || {
    echo "ERROR: wakeword relink object is missing or unsafe: $object" >&2
    exit 1
  }
  install -m 0644 "$object" "$RELINK_OUTPUT/$object_name"
done
printf 'wakeword_relink_object_count=%s\n' "${#relink_objects[@]}"
install -m 0755 "$UI_SOURCE/build/libreecho-waked-onnx-arm32" "$OUTPUT"
file -b "$OUTPUT" | grep -Eq 'ELF 32-bit.*ARM.*statically linked' || {
  echo "ERROR: wakeword runtime output is not static ARM32" >&2
  exit 1
}
echo "wakeword_runtime=$OUTPUT"
sha256sum "$OUTPUT"
