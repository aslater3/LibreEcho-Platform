#!/usr/bin/env bash
set -euo pipefail
usage() { printf '%s\n' 'usage: build_libsodium.sh --archive FILE --output DIR --cc FILE --ar FILE --ranlib FILE --sysroot DIR --native-root DIR'; }
ARCHIVE= OUTPUT= CC= AR= RANLIB= SYSROOT= NATIVE_ROOT=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    --ar) shift; (($#)) || { usage >&2; exit 2; }; AR=$1 ;;
    --ranlib) shift; (($#)) || { usage >&2; exit 2; }; RANLIB=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
    --native-root) shift; (($#)) || { usage >&2; exit 2; }; NATIVE_ROOT=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" && -n "$CC" && -n "$AR" && -n "$RANLIB" && -n "$SYSROOT" && -n "$NATIVE_ROOT" ]] || { usage >&2; exit 2; }
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" && -d "$SYSROOT/usr/include" ]] || { printf 'ERROR: unsafe or missing libsodium input\n' >&2; exit 1; }
[[ -d "$NATIVE_ROOT" && ! -L "$NATIVE_ROOT" && -d "$NATIVE_ROOT/usr/lib" && -d "$NATIVE_ROOT/lib" ]] || { printf 'ERROR: native toolchain root unavailable\n' >&2; exit 1; }
for tool in "$CC" "$AR" "$RANLIB"; do [[ -x "$tool" ]] || { printf 'ERROR: unavailable tool: %s\n' "$tool" >&2; exit 1; }; done
[[ ! -e "$OUTPUT" ]] || { printf 'ERROR: refusing to overwrite libsodium output\n' >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
LOCK="$SCRIPT_DIR/SOURCE.lock"
expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$LOCK")
actual=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ "$actual" == "$expected" ]] || { printf 'ERROR: libsodium source hash mismatch\n' >&2; exit 1; }
mkdir -p "$OUTPUT"; OUTPUT=$(cd -- "$OUTPUT" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-libsodium-build.XXXXXX"); trap 'rm -rf "$work"' EXIT
src="$work/libsodium-1.0.18"; mkdir -p "$src"; tar --no-same-owner --no-same-permissions -xzf "$ARCHIVE" -C "$work"
[[ -x "$src/autogen.sh" && -f "$src/configure.ac" && -f "$src/LICENSE" ]] || { printf 'ERROR: malformed libsodium source\n' >&2; exit 1; }
for host_tool in autoreconf automake libtoolize; do
  command -v "$host_tool" >/dev/null 2>&1 || { printf 'ERROR: missing host build tool: %s\n' "$host_tool" >&2; exit 1; }
done
wrappers="$work/cross"; mkdir -p "$wrappers"
for tool in gcc ar ranlib; do
  case "$tool" in gcc) real=$CC;; ar) real=$AR;; ranlib) real=$RANLIB;; esac
  python3 - "$wrappers/$tool" "$real" "$NATIVE_ROOT/usr/lib:$NATIVE_ROOT/lib" "$SYSROOT" "$tool" <<'PY'
import pathlib, shlex, sys
out, real, libs, sysroot, tool = sys.argv[1:]
extra = " --sysroot=" + shlex.quote(sysroot) if tool == "gcc" else ""
pathlib.Path(out).write_text("#!/bin/sh\nexport LD_LIBRARY_PATH=" + shlex.quote(libs) + "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\nexec " + shlex.quote(real) + extra + " \"$@\"\n")
pathlib.Path(out).chmod(0o755)
PY
done
export SOURCE_DATE_EPOCH=0 LC_ALL=C TZ=UTC
canonical=/usr/src/libsodium-1.0.18
cflags="-Os -ffile-prefix-map=$work=$canonical -fdebug-prefix-map=$work=$canonical -fmacro-prefix-map=$work=$canonical"
cd "$src"
DO_NOT_UPDATE_CONFIG_SCRIPTS=1 ./autogen.sh >/dev/null
CC="$wrappers/gcc" AR="$wrappers/ar" RANLIB="$wrappers/ranlib" CFLAGS="$cflags" \
  ./configure --host=armv7-alpine-linux-musleabihf --build=x86_64-pc-linux-gnu --prefix=/usr --disable-shared --enable-static --disable-pie >/dev/null
make -j"${LIBREECHO_BUILD_JOBS:-2}" >/dev/null
make DESTDIR="$work/stage" install >/dev/null
install -d "$OUTPUT/lib" "$OUTPUT/include"
install -m 0644 "$work/stage/usr/lib/libsodium.a" "$OUTPUT/lib/libsodium.a"
install -m 0644 "$work/stage/usr/include/sodium.h" "$OUTPUT/include/sodium.h"
cp -a "$work/stage/usr/include/sodium" "$OUTPUT/include/"
install -m 0644 "$src/LICENSE" "$OUTPUT/LICENSE"
sha=$(sha256sum "$OUTPUT/lib/libsodium.a" | awk '{print $1}')
size=$(stat -c %s "$OUTPUT/lib/libsodium.a")
license_sha=$(sha256sum "$OUTPUT/LICENSE" | awk '{print $1}')
python3 - "$OUTPUT/libsodium-source.json" "$sha" "$size" "$license_sha" <<'PY'
import json, pathlib, sys
out, sha, size, license_sha = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({
 "binary_sha256": sha, "binary_size": int(size), "license": "ISC",
 "license_file": "LICENSE", "license_sha256": license_sha,
 "source_sha256": "d59323c6b712a1519a5daf710b68f5e7fde57040845ffec53850911f10a5d4f4",
 "source_url": "https://archive.ubuntu.com/ubuntu/pool/main/libs/libsodium/libsodium_1.0.18.orig.tar.gz",
 "static": True, "version": "1.0.18"
}, indent=2, sort_keys=True) + "\n")
PY
printf 'libsodium_sha256=%s\n' "$sha"
