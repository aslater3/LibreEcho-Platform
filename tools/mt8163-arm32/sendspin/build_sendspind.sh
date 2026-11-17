#!/usr/bin/env bash
# Release build of the Sendspin player (libreecho-sendspind) for the airplay2
# feature payload.
#
#   build_sendspind.sh <ui-source> <public-deps-root> <output-dir>
#
# <public-deps-root> holds the pinned Sendspin sources exactly as
# build/ci/fetch-public-deps.py stages them (archive hash verified, declared
# patches applied): sendspin-sdk, sendspin-arduinojson, sendspin-ixwebsocket,
# sendspin-micro-flac (with lib/micro-ogg-demuxer) and sendspin-noise-c.
# CROSS_PREFIX names the image's ARMHF glibc cross toolchain (the one the UI
# daemons use). The player is linked fully static, like the core daemons: it
# runs from the mounted payload outside the AirPlay sandbox, so it may not
# depend on the payload's or the core image's shared libraries.
#
# Output: <output-dir>/libreecho-sendspind and
# <output-dir>/licenses/libreecho-sendspin/ (each source's license text, the
# static glibc and GCC runtime notices, and COMPONENTS.tsv from SOURCE.lock).
set -euo pipefail
UI_SOURCE=${1:?usage: build_sendspind.sh <ui-source> <public-deps-root> <output-dir>}
DEPS=${2:?usage: build_sendspind.sh <ui-source> <public-deps-root> <output-dir>}
OUT=${3:?usage: build_sendspind.sh <ui-source> <public-deps-root> <output-dir>}
CROSS_PREFIX=${CROSS_PREFIX:?ERROR: set CROSS_PREFIX to the ARMHF glibc cross toolchain prefix}
JOBS=${JOBS:-$(nproc)}
HERE=$(cd -- "$(dirname -- "$0")" && pwd -P)
NOTICES=$(cd -- "$HERE/../third-party-licenses" && pwd -P)

fail() { echo "ERROR: $*" >&2; exit 1; }
for tool in gcc g++ strip readelf; do
  [[ -x "${CROSS_PREFIX}$tool" ]] || fail "missing cross tool ${CROSS_PREFIX}$tool"
done
command -v cmake >/dev/null || fail "cmake is required"
[[ -f "$UI_SOURCE/src/adapter/sendspin/CMakeLists.txt" ]] || fail "UI source lacks the Sendspin companion: $UI_SOURCE"
declare -A TREE=(
  [sdk]="$DEPS/sendspin-sdk" [arduinojson]="$DEPS/sendspin-arduinojson"
  [ixwebsocket]="$DEPS/sendspin-ixwebsocket" [micro-flac]="$DEPS/sendspin-micro-flac"
  [micro-ogg-demuxer]="$DEPS/sendspin-micro-flac/lib/micro-ogg-demuxer"
  [noise-c]="$DEPS/sendspin-noise-c"
)
for name in "${!TREE[@]}"; do
  [[ -d "${TREE[$name]}" && ! -L "${TREE[$name]}" ]] || fail "missing pinned Sendspin source $name: ${TREE[$name]}"
done
# The player relies on the downstream SDK lifecycle patches (typed stream end).
grep -q 'STREAM_END_NATURAL' "${TREE[sdk]}/src/player_role_impl.h" ||
  fail "Sendspin SDK is not patched (0001-stream-end-reason.patch missing)"

work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-sendspind.XXXXXX")
trap 'rm -rf "$work"' EXIT
# Quoted CMake values; refuse characters CMake cannot carry in one.
for path in "$CROSS_PREFIX" "${TREE[@]}"; do
  [[ "$path" != *[\;\"\\\$]* && "$path" != *$'\n'* ]] || fail "unsupported character in path: $path"
done
cat > "$work/armhf.cmake" <<CMAKE
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(CMAKE_C_COMPILER "${CROSS_PREFIX}gcc")
set(CMAKE_CXX_COMPILER "${CROSS_PREFIX}g++")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_EXE_LINKER_FLAGS_INIT "-static")
CMAKE
# Out of the UI checkout: its build/ tree is snapshotted as relink input.
cmake -S "$UI_SOURCE/src/adapter/sendspin" -B "$work/build" \
  -DCMAKE_TOOLCHAIN_FILE="$work/armhf.cmake" -DCMAKE_BUILD_TYPE=Release \
  -DSENDSPIN_SDK_DIR="${TREE[sdk]}" -DSENDSPIN_ARDUINOJSON_DIR="${TREE[arduinojson]}" \
  -DSENDSPIN_MICRO_FLAC_DIR="${TREE[micro-flac]}" -DSENDSPIN_IXWEBSOCKET_DIR="${TREE[ixwebsocket]}" \
  -DSENDSPIN_NOISE_C_DIR="${TREE[noise-c]}"
cmake --build "$work/build" --target sendspind -j "$JOBS"

mkdir -p "$OUT"
"${CROSS_PREFIX}strip" -o "$OUT/libreecho-sendspind" "$work/build/sendspind"
chmod 0755 "$OUT/libreecho-sendspind"
header=$("${CROSS_PREFIX}readelf" -h "$OUT/libreecho-sendspind")
programs=$("${CROSS_PREFIX}readelf" -lW "$OUT/libreecho-sendspind")
dynamic=$("${CROSS_PREFIX}readelf" -dW "$OUT/libreecho-sendspind")
grep -q 'Machine:[[:space:]]*ARM' <<<"$header" || fail "sendspind is not an ARM executable"
grep -q 'Type:[[:space:]]*EXEC' <<<"$header" || fail "sendspind is not a static executable"
! grep -q 'INTERP' <<<"$programs" || fail "sendspind requests a dynamic loader"
! grep -q 'NEEDED' <<<"$dynamic" || fail "sendspind needs shared libraries"

licenses="$OUT/licenses/libreecho-sendspin"
rm -rf "$licenses"
mkdir -p "$licenses"
for name in "${!TREE[@]}"; do
  mkdir -p "$licenses/$name"
  found=0
  for file in "${TREE[$name]}"/LICENSE* "${TREE[$name]}"/COPYING* "${TREE[$name]}"/NOTICE*; do
    [[ -f "$file" ]] || continue
    install -m 0644 "$file" "$licenses/$name/"
    found=1
  done
  [[ "$found" == 1 ]] || fail "no license text in Sendspin source $name"
done
install -m 0644 "$NOTICES/glibc-copyright" "$licenses/glibc-copyright"
install -m 0644 "$NOTICES/gcc-runtime-copyright" "$licenses/gcc-runtime-copyright"
python3 -B - "$HERE/SOURCE.lock" "$licenses/COMPONENTS.tsv" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))
rows = [("sdk", lock["identity"]["sdk"]["repository"], lock["identity"]["sdk"]["commit"],
         lock["identity"]["sdk"]["license"])]
for dep in lock["dependencies"]:
    rows.append((dep["name"], dep["repository"], dep["commit"], dep["license"]))
    for sub in dep.get("submodules", []):
        rows.append((sub["path"].rsplit("/", 1)[-1], sub["repository"], sub["commit"], sub["license"]))
with open(sys.argv[2], "w") as out:
    out.write("component\trepository\tcommit\tlicense\n")
    for row in rows:
        out.write("\t".join(row) + "\n")
PY
sha256sum "$OUT/libreecho-sendspind"
