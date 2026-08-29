#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_wpa_supplicant.sh --archive FILE --libnl-archive FILE --output DIR --cc COMPILER --sysroot DIR --kernel-headers DIR'
}

ARCHIVE=
LIBNL_ARCHIVE=
OUTPUT=
CC=
SYSROOT=
KERNEL_HEADERS=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --libnl-archive) shift; (($#)) || { usage >&2; exit 2; }; LIBNL_ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
    --kernel-headers) shift; (($#)) || { usage >&2; exit 2; }; KERNEL_HEADERS=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$LIBNL_ARCHIVE" && -n "$OUTPUT" && -n "$CC" &&
   -n "$SYSROOT" && -n "$KERNEL_HEADERS" ]] || { usage >&2; exit 2; }
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
[[ -f "$LIBNL_ARCHIVE" && ! -L "$LIBNL_ARCHIVE" ]] || { printf 'ERROR: unsafe libnl archive\n' >&2; exit 1; }
[[ -x "$CC" ]] || { printf 'ERROR: cross compiler is unavailable\n' >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
CONFIG="$SCRIPT_DIR/wpa_supplicant-2.10.config"
LOCK="$SCRIPT_DIR/SOURCE.lock"
readarray -t expected_hashes < <(python3 - "$LOCK" <<'PY'
import json, sys
record = json.load(open(sys.argv[1]))
print(record["source_sha256"])
print(record["libnl_source_sha256"])
PY
)
wpa_actual=$(sha256sum "$ARCHIVE" | awk '{print $1}')
libnl_actual=$(sha256sum "$LIBNL_ARCHIVE" | awk '{print $1}')
[[ "$wpa_actual" == "${expected_hashes[0]}" ]] || {
  printf 'ERROR: wpa_supplicant source hash mismatch: expected=%s actual=%s\n' "${expected_hashes[0]}" "$wpa_actual" >&2
  exit 1
}
[[ "$libnl_actual" == "${expected_hashes[1]}" ]] || {
  printf 'ERROR: libnl source hash mismatch: expected=%s actual=%s\n' "${expected_hashes[1]}" "$libnl_actual" >&2
  exit 1
}
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] || { printf 'ERROR: wpa_supplicant config is unavailable\n' >&2; exit 1; }

OUTPUT=$(mkdir -p "$OUTPUT" && cd -- "$OUTPUT" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-wpa-build.XXXXXX")
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
wpa_src="$work/wpa_supplicant-2.10"
libnl_src="$work/libnl-3.11.0"
mkdir -p "$wpa_src" "$libnl_src"
tar -xf "$ARCHIVE" -C "$wpa_src" --strip-components=1
tar -xf "$LIBNL_ARCHIVE" -C "$libnl_src" --strip-components=1
cp "$CONFIG" "$wpa_src/wpa_supplicant/.config"

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
cross_prefix=${CC%gcc*}
AR="${cross_prefix}ar"
RANLIB="${cross_prefix}ranlib"
STRIP="${cross_prefix}strip"
for tool in "$AR" "$RANLIB" "$STRIP"; do
  [[ -x "$tool" ]] || { printf 'ERROR: cross tool is unavailable: %s\n' "$tool" >&2; exit 1; }
done

export SOURCE_DATE_EPOCH=0
canonical=/usr/src/wpa_supplicant-2.10
common_cflags="-Os -march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -ffile-prefix-map=$work=$canonical -fdebug-prefix-map=$work=$canonical -fmacro-prefix-map=$work=$canonical"
(
  cd "$libnl_src"
  CC="$cc_wrapper" AR="$AR" RANLIB="$RANLIB" STRIP="$STRIP" \
    CFLAGS="$common_cflags -idirafter $KERNEL_HEADERS" \
    ./configure --host=arm-linux-gnueabihf \
      --disable-shared --enable-static --disable-pthreads --disable-debug >/dev/null
  make -j"${LIBREECHO_BUILD_JOBS:-2}" lib/libnl-3.la lib/libnl-genl-3.la >/dev/null
)
libnl_libdir="$libnl_src/lib/.libs"
[[ -f "$libnl_libdir/libnl-3.a" && -f "$libnl_libdir/libnl-genl-3.a" ]] || {
  printf 'ERROR: static libnl output is incomplete\n' >&2
  exit 1
}

make -C "$wpa_src/wpa_supplicant" -j"${LIBREECHO_BUILD_JOBS:-2}" \
  CC="$cc_wrapper" LIBNL_INC="$libnl_src/include" \
  EXTRA_CFLAGS="$common_cflags -I$libnl_src/include -idirafter $KERNEL_HEADERS" \
  LDFLAGS="-static -no-pie -s -Wl,--build-id=none -L$libnl_libdir" \
  wpa_supplicant >/dev/null

binary="$OUTPUT/wpa_supplicant"
install -m 0755 "$wpa_src/wpa_supplicant/wpa_supplicant" "$binary"
readelf_output="$(readelf -h -l -d "$binary")"
grep -Eq 'Type:[[:space:]]+EXEC' <<< "$readelf_output" || {
  printf 'ERROR: wpa_supplicant is not a non-PIE executable\n' >&2
  exit 1
}
if grep -q 'Requesting program interpreter' <<< "$readelf_output"; then
  printf 'ERROR: wpa_supplicant is not static\n' >&2
  exit 1
fi
if strings "$binary" | grep -Eq '/home/|libreecho-wpa-build'; then
  printf 'ERROR: wpa_supplicant contains a forbidden build path\n' >&2
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
driver_output=$(qemu-arm-static "$binary" -h 2>&1)
[[ "$driver_output" == *'nl80211 = Linux nl80211/cfg80211'* && "$driver_output" == *'wext = Linux wireless extensions'* ]] || {
  printf 'ERROR: wpa_supplicant dual-driver contract is unavailable\n' >&2
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
python3 - "$OUTPUT/wpa-supplicant-source.json" "$binary_sha" "$binary_size" "$config_sha" "$compiler" "$kernel_uapi_sha" "$libnl_actual" <<'PY'
import json, pathlib, sys
out, binary_sha, binary_size, config_sha, compiler, kernel_uapi_sha, libnl_sha = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({
    "binary_sha256": binary_sha,
    "binary_size": int(binary_size),
    "build_epoch": 0,
    "compiler": compiler,
    "config_path": "tools/mt8163-arm32/wpa-supplicant/wpa_supplicant-2.10.config",
    "config_sha256": config_sha,
    "crypto": "internal",
    "drivers": ["nl80211", "wext"],
    "kernel_uapi_sha256": kernel_uapi_sha,
    "libnl_license": "LGPL-2.1-only",
    "libnl_source_sha256": libnl_sha,
    "libnl_source_url": "https://github.com/thom311/libnl/releases/download/libnl3_11_0/libnl-3.11.0.tar.gz",
    "libnl_version": "3.11.0",
    "license": "BSD-3-Clause",
    "source_sha256": "20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f",
    "source_url": "https://w1.fi/releases/wpa_supplicant-2.10.tar.gz",
    "static": True,
    "version": "2.10",
}, sort_keys=True, indent=2) + "\n")
PY
printf 'wpa_supplicant_sha256=%s\nwpa_supplicant_config_sha256=%s\nkernel_uapi_sha256=%s\nlibnl_source_sha256=%s\n' "$binary_sha" "$config_sha" "$kernel_uapi_sha" "$libnl_actual"
