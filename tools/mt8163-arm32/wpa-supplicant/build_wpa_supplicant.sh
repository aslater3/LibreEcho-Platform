#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_wpa_supplicant.sh --archive FILE --output DIR --cc COMPILER --sysroot DIR --kernel-headers DIR'
}

ARCHIVE=
OUTPUT=
CC=
SYSROOT=
KERNEL_HEADERS=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
    --kernel-headers) shift; (($#)) || { usage >&2; exit 2; }; KERNEL_HEADERS=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" && -n "$CC" && -n "$SYSROOT" &&
   -n "$KERNEL_HEADERS" ]] || { usage >&2; exit 2; }
# Host archive, checksum, and build tools must not load target-chroot libraries.
# The generated compiler wrapper restores the pmbootstrap runtime privately.
unset LD_LIBRARY_PATH
[[ -d "$SYSROOT/usr/include" ]] || { printf 'ERROR: target sysroot is unavailable\n' >&2; exit 1; }
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
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || { printf 'ERROR: unsafe wpa_supplicant archive\n' >&2; exit 1; }
[[ -x "$CC" ]] || { printf 'ERROR: cross compiler is unavailable\n' >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
CONFIG="$SCRIPT_DIR/wpa_supplicant-2.10.config"
LOCK="$SCRIPT_DIR/SOURCE.lock"
expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$LOCK")
actual=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ "$actual" == "$expected" ]] || {
  printf 'ERROR: wpa_supplicant source hash mismatch: expected=%s actual=%s\n' "$expected" "$actual" >&2
  exit 1
}
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] || { printf 'ERROR: wpa_supplicant config is unavailable\n' >&2; exit 1; }

OUTPUT=$(mkdir -p "$OUTPUT" && cd -- "$OUTPUT" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-wpa-build.XXXXXX")
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
src="$work/wpa_supplicant-2.10"
mkdir -p "$src"
tar -xf "$ARCHIVE" -C "$src" --strip-components=1
cp "$CONFIG" "$src/wpa_supplicant/.config"

cc_dir=$(cd -- "$(dirname -- "$CC")" && pwd -P)
host_usr=$(cd -- "$cc_dir/.." && pwd -P)
host_root=$(cd -- "$host_usr/.." && pwd -P)
host_library_path="$host_usr/lib:$host_root/lib"
cc_wrapper="$work/target-cc"
python3 - "$cc_wrapper" "$CC" "$host_library_path" "$SYSROOT" <<'PY'
import pathlib, shlex, sys
out, real, library_path, sysroot = sys.argv[1:]
pathlib.Path(out).write_text(
    "#!/bin/sh\nexport LD_LIBRARY_PATH=" + shlex.quote(library_path) +
    "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\nexec " + shlex.quote(real) +
    " --sysroot=" + shlex.quote(sysroot) + " \"$@\"\n"
)
pathlib.Path(out).chmod(0o755)
PY
export SOURCE_DATE_EPOCH=0
canonical=/usr/src/wpa_supplicant-2.10
cflags=(
  -Os -march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard
  "-idirafter" "$KERNEL_HEADERS"
  "-ffile-prefix-map=$work=$canonical"
  "-fdebug-prefix-map=$work=$canonical"
  "-fmacro-prefix-map=$work=$canonical"
)
make -C "$src/wpa_supplicant" -j"${LIBREECHO_BUILD_JOBS:-2}" \
  CC="$cc_wrapper" EXTRA_CFLAGS="${cflags[*]}" LDFLAGS='-static -s -Wl,--build-id=none' \
  wpa_supplicant >/dev/null

binary="$OUTPUT/wpa_supplicant"
install -m 0755 "$src/wpa_supplicant/wpa_supplicant" "$binary"
if readelf -l "$binary" | grep -q 'Requesting program interpreter'; then
  printf 'ERROR: wpa_supplicant is not static\n' >&2
  exit 1
fi
if strings "$binary" | grep -Eq '/home/|libreecho-wpa-build|libnl-'; then
  printf 'ERROR: wpa_supplicant contains a forbidden path or libnl marker\n' >&2
  exit 1
fi
version=$(qemu-arm-static "$binary" -v 2>&1 | python3 -c 'import sys; print(sys.stdin.readline().strip())')
[[ "$version" == 'wpa_supplicant v2.10' ]] || {
  printf 'ERROR: wpa_supplicant version contract changed: %s\n' "$version" >&2
  exit 1
}
license_output=$(qemu-arm-static "$binary" -L)
[[ "$license_output" == *'BSD license'* ]] || {
  printf 'ERROR: wpa_supplicant BSD notice is unavailable\n' >&2
  exit 1
}

binary_sha=$(sha256sum "$binary" | awk '{print $1}')
binary_size=$(stat -c %s "$binary")
config_sha=$(sha256sum "$CONFIG" | awk '{print $1}')
compiler=$("$cc_wrapper" --version | python3 -c 'import sys; print(sys.stdin.readline().strip())')
kernel_uapi_sha=$(python3 - "$KERNEL_HEADERS" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"symlink in exported UAPI tree: {path}")
    if path.is_file():
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
print(digest.hexdigest())
PY
)
python3 - "$OUTPUT/wpa-supplicant-source.json" "$binary_sha" "$binary_size" "$config_sha" "$compiler" "$kernel_uapi_sha" <<'PY'
import json, pathlib, sys
out, binary_sha, binary_size, config_sha, compiler, kernel_uapi_sha = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({
    "binary_sha256": binary_sha,
    "binary_size": int(binary_size),
    "build_epoch": 0,
    "compiler": compiler,
    "config_path": "tools/mt8163-arm32/wpa-supplicant/wpa_supplicant-2.10.config",
    "config_sha256": config_sha,
    "crypto": "internal",
    "drivers": ["wext"],
    "kernel_uapi_sha256": kernel_uapi_sha,
    "license": "BSD-3-Clause",
    "source_sha256": "20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f",
    "source_url": "https://w1.fi/releases/wpa_supplicant-2.10.tar.gz",
    "static": True,
    "version": "2.10",
}, sort_keys=True, indent=2) + "\n")
PY
printf 'wpa_supplicant_sha256=%s\nwpa_supplicant_config_sha256=%s\nkernel_uapi_sha256=%s\n' "$binary_sha" "$config_sha" "$kernel_uapi_sha"
