#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_musl.sh --archive FILE --output DIR --cc COMPILER'
}
ARCHIVE=
OUTPUT=
CC=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" && -n "$CC" ]] || { usage >&2; exit 2; }
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || { printf 'ERROR: unsafe musl archive\n' >&2; exit 1; }
[[ -x "$CC" ]] || { printf 'ERROR: cross compiler is unavailable\n' >&2; exit 1; }
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
SOURCE_LOCK="$SCRIPT_DIR/SOURCE.lock"
expected_archive_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$SOURCE_LOCK")
actual_archive_sha=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ "$actual_archive_sha" == "$expected_archive_sha" ]] || {
  printf 'ERROR: musl source archive hash mismatch\n' >&2; exit 1;
}
mkdir -p "$OUTPUT"
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-musl-build.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar -xzf "$ARCHIVE" -C "$work"
src="$work/musl-1.2.5"
[[ -f "$src/COPYRIGHT" && -x "$src/configure" ]] || { printf 'ERROR: malformed musl source archive\n' >&2; exit 1; }
cc_dir=$(cd -- "$(dirname -- "$CC")" && pwd -P)
host_usr=$(cd -- "$cc_dir/.." && pwd -P)
host_root=$(cd -- "$host_usr/.." && pwd -P)
host_library_path="$host_usr/lib:$host_root/lib"
wrapper_dir="$work/cross"
mkdir -p "$wrapper_dir"
for tool in gcc ar ranlib ld; do
  real="$(dirname "$CC")/armv7-alpine-linux-musleabihf-$tool"
  [[ -x "$real" ]] || { printf 'ERROR: cross tool is unavailable: %s\n' "$real" >&2; exit 1; }
  wrapper="$wrapper_dir/armv7-alpine-linux-musleabihf-$tool"
  python3 - "$wrapper" "$real" "$host_library_path" <<'PY'
import pathlib, shlex, sys
out, real, library_path = sys.argv[1:]
pathlib.Path(out).write_text(
    "#!/bin/sh\nexport LD_LIBRARY_PATH=" + shlex.quote(library_path) +
    "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\nexec " + shlex.quote(real) + " \"$@\"\n"
)
pathlib.Path(out).chmod(0o755)
PY
done
cc_wrapper="$wrapper_dir/armv7-alpine-linux-musleabihf-gcc"
export SOURCE_DATE_EPOCH=0
(
  cd "$src"
  export PATH="$wrapper_dir:$PATH"
  CC="$cc_wrapper" CFLAGS='-Os -ffile-prefix-map='$work'=/usr/src/musl-1.2.5 -fdebug-prefix-map='$work'=/usr/src/musl-1.2.5 -fmacro-prefix-map='$work'=/usr/src/musl-1.2.5' \
    LDFLAGS='-Wl,--build-id=none -s' \
    ./configure --target=armv7-alpine-linux-musleabihf --prefix=/usr --syslibdir=/lib >/dev/null
  make -j"${LIBREECHO_BUILD_JOBS:-2}" >/dev/null
)
loader="$OUTPUT/ld-musl-armhf.so.1"
install -m 0755 "$src/lib/libc.so" "$loader"
if readelf -l "$loader" | grep -q 'Requesting program interpreter'; then
  printf 'ERROR: generated musl loader has an unexpected interpreter\n' >&2; exit 1
fi
if strings "$loader" | grep -Eq '/home/|libreecho-musl-build'; then
  printf 'ERROR: generated musl loader contains a private build path\n' >&2; exit 1
fi
loader_sha=$(sha256sum "$loader" | awk '{print $1}')
compiler=$("$cc_wrapper" --version | python3 -c 'import sys; print(sys.stdin.readline().strip())')
python3 - "$OUTPUT/musl-source.json" "$loader_sha" "$compiler" <<'PY'
import json, pathlib, sys
out, loader_sha, compiler = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({
    "loader_sha256": loader_sha,
    "source_archive_sha256": "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4",
    "source_url": "https://musl.libc.org/releases/musl-1.2.5.tar.gz",
    "version": "1.2.5",
    "license": "MIT",
    "compiler": compiler,
}, indent=2, sort_keys=True) + "\n")
PY
printf 'musl_loader_sha256=%s\n' "$loader_sha"
