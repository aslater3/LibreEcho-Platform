#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_busybox.sh --archive FILE --output DIR --cross-prefix PREFIX --sysroot DIR'
}

ARCHIVE=
OUTPUT=
CROSS_PREFIX=
SYSROOT=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cross-prefix) shift; (($#)) || { usage >&2; exit 2; }; CROSS_PREFIX=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" && -n "$CROSS_PREFIX" && -n "$SYSROOT" ]] || { usage >&2; exit 2; }
[[ -d "$SYSROOT/usr/include" ]] || { printf 'ERROR: target sysroot is unavailable\n' >&2; exit 1; }
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || { printf 'ERROR: unsafe BusyBox archive\n' >&2; exit 1; }
[[ -x "${CROSS_PREFIX}gcc" ]] || { printf 'ERROR: cross compiler is unavailable\n' >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
CONFIG="$SCRIPT_DIR/busybox-1.37.0.config"
LOCK="$SCRIPT_DIR/SOURCE.lock"
expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$LOCK")
actual=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ "$actual" == "$expected" ]] || {
  printf 'ERROR: BusyBox source hash mismatch: expected=%s actual=%s\n' "$expected" "$actual" >&2
  exit 1
}
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] || { printf 'ERROR: BusyBox config is unavailable\n' >&2; exit 1; }

OUTPUT=$(mkdir -p "$OUTPUT" && cd -- "$OUTPUT" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-busybox-build.XXXXXX")
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
src="$work/busybox-1.37.0"
mkdir -p "$src"
tar -xf "$ARCHIVE" -C "$src" --strip-components=1
cp "$CONFIG" "$src/.config"

cc_dir=$(cd -- "$(dirname -- "${CROSS_PREFIX}gcc")" && pwd -P)
host_usr=$(cd -- "$cc_dir/.." && pwd -P)
host_root=$(cd -- "$host_usr/.." && pwd -P)
host_library_path="$host_usr/lib:$host_root/lib"
wrapper_prefix="$work/cross/armv7-"
mkdir -p "$(dirname "$wrapper_prefix")"
for tool in gcc g++ ar as ld nm objcopy objdump ranlib readelf size strip; do
  real="${CROSS_PREFIX}${tool}"
  [[ -x "$real" ]] || continue
  wrapper="${wrapper_prefix}${tool}"
  python3 - "$wrapper" "$real" "$host_library_path" "$SYSROOT" "$tool" <<'PY'
import pathlib, shlex, sys
out, real, library_path, sysroot, tool = sys.argv[1:]
extra = " --sysroot=" + shlex.quote(sysroot) if tool in {"gcc", "g++"} else ""
pathlib.Path(out).write_text(
    "#!/bin/sh\nexport LD_LIBRARY_PATH=" + shlex.quote(library_path) +
    "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\nexec " + shlex.quote(real) + extra + " \"$@\"\n"
)
pathlib.Path(out).chmod(0o755)
PY
done
# The target wrappers restore the pmbootstrap runtime privately. Do not let a
# caller's target-side library path contaminate BusyBox host tools such as fixdep.
unset LD_LIBRARY_PATH
export SOURCE_DATE_EPOCH=0
export TZ=UTC
export KBUILD_BUILD_USER=libreecho
export KBUILD_BUILD_HOST=release
export KBUILD_BUILD_TIMESTAMP='1970-01-01 00:00:00 UTC'
make -C "$src" -j"${LIBREECHO_BUILD_JOBS:-2}" \
  CROSS_COMPILE="$wrapper_prefix" HOSTCC=/usr/bin/gcc \
  EXTRA_CFLAGS="-ffile-prefix-map=$work=/usr/src/busybox-1.37.0 -fdebug-prefix-map=$work=/usr/src/busybox-1.37.0 -fmacro-prefix-map=$work=/usr/src/busybox-1.37.0" \
  >/dev/null

binary="$OUTPUT/busybox"
install -m 0755 "$src/busybox" "$binary"
forbidden_paths="$(
  strings "$binary" |
    grep -E '/home/|libreecho-busybox-build' |
    grep -Fvx '/home/%s' || true
)"
if [[ -n "$forbidden_paths" ]]; then
  printf 'ERROR: BusyBox contains a private or volatile build path\n' >&2
  exit 1
fi
readelf -l "$binary" | grep -q '/lib/ld-musl-armhf.so.1' || {
  printf 'ERROR: BusyBox musl interpreter contract changed\n' >&2
  exit 1
}
readelf -d "$binary" | grep -q 'libc.musl-armv7.so.1' || {
  printf 'ERROR: BusyBox musl dependency contract changed\n' >&2
  exit 1
}

binary_sha=$(sha256sum "$binary" | awk '{print $1}')
config_sha=$(sha256sum "$CONFIG" | awk '{print $1}')
compiler=$("${wrapper_prefix}gcc" --version | python3 -c 'import sys; print(sys.stdin.readline().strip())')
python3 - "$OUTPUT/busybox-source.json" "$binary_sha" "$config_sha" "$compiler" <<'PY'
import json, pathlib, sys
out, binary_sha, config_sha, compiler = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({
    "binary_sha256": binary_sha,
    "build_epoch": 0,
    "compiler": compiler,
    "config_path": "tools/mt8163-arm32/busybox/busybox-1.37.0.config",
    "config_sha256": config_sha,
    "license": "GPL-2.0-only",
    "source_sha256": "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4",
    "source_url": "https://busybox.net/downloads/busybox-1.37.0.tar.bz2",
    "version": "1.37.0",
}, sort_keys=True, indent=2) + "\n")
PY
printf 'busybox_sha256=%s\nbusybox_config_sha256=%s\n' "$binary_sha" "$config_sha"
