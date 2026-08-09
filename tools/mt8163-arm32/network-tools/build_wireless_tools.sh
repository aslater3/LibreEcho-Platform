#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_wireless_tools.sh --archive FILE --output DIR --cc FILE --ar FILE --ranlib FILE --sysroot DIR --native-root DIR --kernel-headers DIR'
}

ARCHIVE=
OUTPUT=
CC=
AR=
RANLIB=
SYSROOT=
NATIVE_ROOT=
KERNEL_HEADERS=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    --ar) shift; (($#)) || { usage >&2; exit 2; }; AR=$1 ;;
    --ranlib) shift; (($#)) || { usage >&2; exit 2; }; RANLIB=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
    --native-root) shift; (($#)) || { usage >&2; exit 2; }; NATIVE_ROOT=$1 ;;
    --kernel-headers) shift; (($#)) || { usage >&2; exit 2; }; KERNEL_HEADERS=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" && -n "$CC" && -n "$AR" && -n "$RANLIB" &&
   -n "$SYSROOT" && -n "$NATIVE_ROOT" && -n "$KERNEL_HEADERS" ]] || { usage >&2; exit 2; }
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || { printf 'ERROR: unsafe wireless-tools archive\n' >&2; exit 1; }
[[ -d "$SYSROOT/usr/include" && ! -L "$SYSROOT/usr/include" ]] || {
  printf 'ERROR: target sysroot is unavailable\n' >&2; exit 1;
}
[[ -d "$NATIVE_ROOT" && ! -L "$NATIVE_ROOT" &&
   -d "$NATIVE_ROOT/usr/lib" && -d "$NATIVE_ROOT/lib" ]] || {
  printf 'ERROR: native cross-toolchain root is unavailable\n' >&2
  exit 1
}
[[ -d "$KERNEL_HEADERS" && ! -L "$KERNEL_HEADERS" ]] || {
  printf 'ERROR: exported Linux UAPI headers are unavailable\n' >&2
  exit 1
}
for header_tree in linux asm asm-generic; do
  [[ -d "$KERNEL_HEADERS/$header_tree" && ! -L "$KERNEL_HEADERS/$header_tree" ]] || {
    printf 'ERROR: exported Linux UAPI subtree is missing: %s/%s\n' "$KERNEL_HEADERS" "$header_tree" >&2
    exit 1
  }
done
for tool in "$CC" "$AR" "$RANLIB"; do
  [[ -x "$tool" ]] || { printf 'ERROR: target tool is unavailable: %s\n' "$tool" >&2; exit 1; }
done
command -v make >/dev/null 2>&1 || { printf 'ERROR: make is required\n' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { printf 'ERROR: tar is required\n' >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
LOCK="$SCRIPT_DIR/SOURCE.lock"
expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$LOCK")
actual=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ "$actual" == "$expected" ]] || {
  printf 'ERROR: wireless-tools source hash mismatch: expected=%s actual=%s\n' "$expected" "$actual" >&2
  exit 1
}

[[ ! -e "$OUTPUT" ]] || { printf 'ERROR: refusing to overwrite wireless-tools output: %s\n' "$OUTPUT" >&2; exit 1; }
mkdir -p "$OUTPUT"
OUTPUT=$(cd -- "$OUTPUT" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-wireless-tools-build.XXXXXX")
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
src="$work/wireless_tools.30"
mkdir -p "$src"
tar --no-same-owner --no-same-permissions -xzf "$ARCHIVE" -C "$src" --strip-components=1
[[ -f "$src/Makefile" && -f "$src/iwconfig.c" && -f "$src/COPYING" ]] || {
  printf 'ERROR: pinned wireless-tools source layout changed\n' >&2
  exit 1
}

export SOURCE_DATE_EPOCH=0
export TZ=UTC
unset LD_LIBRARY_PATH
canonical=/usr/src/wireless-tools-30~pre9
cflags=(
  -Os -ffile-prefix-map="$work=$canonical"
  -fdebug-prefix-map="$work=$canonical"
  -fmacro-prefix-map="$work=$canonical"
  -I"$KERNEL_HEADERS"
  -I"$SYSROOT/usr/include"
)
wrapper_dir="$work/cross"
mkdir -p "$wrapper_dir"
for tool in gcc ar ranlib; do
  real="$(dirname "$CC")/$tool"
  [[ "$tool" == gcc ]] && real="$CC"
  [[ "$tool" == ar ]] && real="$AR"
  [[ "$tool" == ranlib ]] && real="$RANLIB"
  wrapper="$wrapper_dir/$tool"
  python3 - "$wrapper" "$real" "$NATIVE_ROOT/usr/lib:$NATIVE_ROOT/lib" "$SYSROOT" "$tool" <<'PY'
import pathlib, shlex, sys
out, real, library_path, sysroot, tool = sys.argv[1:]
sysroot_arg = " --sysroot=" + shlex.quote(sysroot) if tool == "gcc" else ""
pathlib.Path(out).write_text(
    "#!/bin/sh\nexport LD_LIBRARY_PATH=" + shlex.quote(library_path) +
    "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\nexec " + shlex.quote(real) +
    sysroot_arg + " \"$@\"\n"
)
pathlib.Path(out).chmod(0o755)
PY
done
make -C "$src" -j"${LIBREECHO_BUILD_JOBS:-2}" \
  CC="$wrapper_dir/gcc" AR="$wrapper_dir/ar" RANLIB="$wrapper_dir/ranlib" \
  CFLAGS="${cflags[*]}" LDFLAGS='-static -Wl,--build-id=none' iwconfig >/dev/null
install -m 0755 "$src/iwconfig" "$OUTPUT/iwconfig"
install -m 0644 "$src/COPYING" "$OUTPUT/wireless-tools-COPYING"

readelf_output="$(readelf -h -l -d "$OUTPUT/iwconfig")"
grep -Eq 'Class:[[:space:]]+ELF32' <<< "$readelf_output"
grep -Eq 'Machine:[[:space:]]+ARM' <<< "$readelf_output"
grep -Eq 'Flags:.*0x(05000400|5000400)' <<< "$readelf_output"
! grep -q 'Requesting program interpreter' <<< "$readelf_output"
! grep -q 'NEEDED' <<< "$readelf_output"
! grep -q 'There is a dynamic section' <<< "$readelf_output"
if strings "$OUTPUT/iwconfig" | grep -Eq '/home/|libreecho-wireless-tools-build'; then
  printf 'ERROR: iwconfig contains a private or volatile build path\n' >&2
  exit 1
fi

kernel_uapi_sha=$(python3 - "$KERNEL_HEADERS" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob('*')):
    if path.is_symlink():
        raise SystemExit(f'symlink in exported UAPI tree: {path}')
    if path.is_file():
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
print(digest.hexdigest())
PY
)
binary_sha=$(sha256sum "$OUTPUT/iwconfig" | awk '{print $1}')
binary_size=$(stat -c %s "$OUTPUT/iwconfig")
license_sha=$(sha256sum "$OUTPUT/wireless-tools-COPYING" | awk '{print $1}')
compiler=$("$wrapper_dir/gcc" --version | sed -n '1p')
python3 - "$OUTPUT/wireless-tools-source.json" "$binary_sha" "$binary_size" "$license_sha" "$compiler" "$kernel_uapi_sha" <<'PY'
import json, pathlib, sys
out, binary_sha, binary_size, license_sha, compiler, kernel_uapi_sha = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({
    "binary_sha256": binary_sha,
    "binary_size": int(binary_size),
    "build_epoch": 0,
    "compiler": compiler,
    "kernel_uapi_sha256": kernel_uapi_sha,
    "license": "GPL-2.0-only AND LGPL-2.1-or-later",
    "license_file": "wireless-tools-COPYING",
    "license_sha256": license_sha,
    "source_sha256": "abd9c5c98abf1fdd11892ac2f8a56737544fe101e1be27c6241a564948f34c63",
    "source_url": "https://archive.ubuntu.com/ubuntu/pool/main/w/wireless-tools/wireless-tools_30~pre9.orig.tar.gz",
    "static": True,
    "version": "30~pre9",
}, sort_keys=True, indent=2) + "\n")
PY
printf 'wireless_tools_sha256=%s\nkernel_uapi_sha256=%s\n' "$binary_sha" "$kernel_uapi_sha"
