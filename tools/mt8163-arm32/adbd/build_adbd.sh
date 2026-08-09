#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_adbd.sh --source DIR --output DIR --cc COMPILER --kernel-headers DIR [--sysroot DIR] [--test-ffs-root DIR]'
}

SOURCE=
OUTPUT=
CC=
sysroot=
kernel_headers=
test_ffs_root=
while (($#)); do
  case "$1" in
    --source) shift; (($#)) || { usage >&2; exit 2; }; SOURCE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; sysroot=$1 ;;
    --kernel-headers) shift; (($#)) || { usage >&2; exit 2; }; kernel_headers=$1 ;;
    --test-ffs-root) shift; (($#)) || { usage >&2; exit 2; }; test_ffs_root=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$SOURCE" && -n "$OUTPUT" && -n "$CC" && -n "$kernel_headers" ]] || { usage >&2; exit 2; }
ADBD_SOURCE=$SOURCE
ADBD_SOURCE_COMMIT=
[[ -d "$SOURCE/.git" ]] || { printf 'ERROR: adbd source is not a Git checkout: %s\n' "$SOURCE" >&2; exit 1; }
[[ -x "$CC" ]] || { printf 'ERROR: adbd compiler is unavailable: %s\n' "$CC" >&2; exit 1; }
[[ -z "$sysroot" || -d "$sysroot" ]] || { printf 'ERROR: adbd sysroot is unavailable: %s\n' "$sysroot" >&2; exit 1; }
[[ -d "$kernel_headers" ]] || { printf 'ERROR: adbd kernel UAPI headers are unavailable: %s\n' "$kernel_headers" >&2; exit 1; }
[[ -f "$kernel_headers/linux/capability.h" && -f "$kernel_headers/linux/usb/functionfs.h" ]] || {
  printf 'ERROR: adbd kernel UAPI header contract is incomplete: %s\n' "$kernel_headers" >&2
  exit 1
}

source_commit=$(git -C "$SOURCE" rev-parse HEAD)
ADBD_SOURCE_COMMIT=$source_commit
expected_commit=$(awk -F= '$1 == "source_commit" {print $2}' "$(dirname "$0")/SOURCE.lock")
[[ "$source_commit" == "$expected_commit" ]] || {
  printf 'ERROR: adbd source commit mismatch: expected=%s actual=%s\n' "$expected_commit" "$source_commit" >&2
  exit 1
}
[[ -z "$(git -C "$SOURCE" status --porcelain -- adb include libcutils libmincrypt)" ]] || {
  printf 'ERROR: adbd source checkout is dirty\n' >&2
  exit 1
}

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
mkdir -p "$OUTPUT"
OUTPUT=$(cd -- "$OUTPUT" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-adbd-build.XXXXXX")
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

cp -a "$SOURCE/adb" "$work/adb"
cp -a "$SOURCE/include" "$work/include"
cp -a "$SOURCE/libcutils" "$work/libcutils"
cp -a "$SOURCE/libmincrypt" "$work/libmincrypt"
mkdir -p "$work/kernel-headers"
for header_tree in linux asm asm-generic; do
  [[ -d "$kernel_headers/$header_tree" ]] || {
    printf 'ERROR: adbd kernel UAPI subtree is missing: %s/%s\n' "$kernel_headers" "$header_tree" >&2
    exit 1
  }
  cp -a "$kernel_headers/$header_tree" "$work/kernel-headers/$header_tree"
done
mkdir -p "$work/compat/hardware"
cp "$SCRIPT_DIR/compat/property_compat.c" "$work/compat/property_compat.c"
cp "$SCRIPT_DIR/compat/android_reboot_compat.c" "$work/compat/android_reboot_compat.c"
cp "$SCRIPT_DIR/compat/base64_compat.c" "$work/compat/base64_compat.c"
cp "$SCRIPT_DIR/compat/libreecho-adbd-compat.h" "$work/compat/libreecho-adbd-compat.h"
cp "$SCRIPT_DIR/compat/hardware/qemu_pipe.h" "$work/compat/hardware/qemu_pipe.h"
patch --batch --forward --fuzz=0 -p1 -d "$work" < "$SCRIPT_DIR/libreecho-adbd.patch" >/dev/null

CFLAGS=(
  -march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -O2
  -ffunction-sections -fdata-sections -fno-omit-frame-pointer
  "-ffile-prefix-map=$work=/usr/src/libreecho-adbd"
  "-fdebug-prefix-map=$work=/usr/src/libreecho-adbd"
  "-fmacro-prefix-map=$work=/usr/src/libreecho-adbd"
  -DADB_HOST=0 -DHAVE_FORKEXEC=1 -D_XOPEN_SOURCE -D_GNU_SOURCE
  -DALLOW_ADBD_ROOT=1 -DHAVE_SYMLINKS -DBOARD_ALWAYS_INSECURE -DHAVE_TERMIO_H
  "-I$work/include" "-I$work/include/cutils" "-I$work/adb"
  "-I$work/libmincrypt" "-I$work/libcutils" "-I$work/include/private"
  "-I$work/include/system" "-I$work/compat" "-I$work/kernel-headers"
  "-include" "$work/compat/libreecho-adbd-compat.h"
)
if [[ -n "$test_ffs_root" ]]; then
  [[ "$test_ffs_root" == /* && "$test_ffs_root" == */ ]] || {
    printf 'ERROR: test FunctionFS root must be an absolute directory path ending in /: %s\n' "$test_ffs_root" >&2
    exit 1
  }
  CFLAGS+=("-DLIBREECHO_TEST_FFS_ROOT=\"$test_ffs_root\"")
fi
if [[ -n "$sysroot" ]]; then
  CFLAGS+=("--sysroot=$sysroot")
fi

sources=(
  "$work/adb/adb.c" "$work/adb/backup_service.c" "$work/adb/fdevent.c"
  "$work/adb/transport.c" "$work/adb/transport_local.c" "$work/adb/transport_usb.c"
  "$work/adb/adb_auth_client.c" "$work/adb/sockets.c" "$work/adb/services.c"
  "$work/adb/file_sync_service.c" "$work/adb/jdwp_service.c" "$work/adb/framebuffer_service.c"
  "$work/adb/remount_service.c" "$work/adb/usb_linux_client.c" "$work/adb/log_service.c"
  "$work/adb/utils.c" "$work/libcutils/socket_inaddr_any_server.c"
  "$work/libcutils/socket_local_client.c" "$work/libcutils/socket_local_server.c"
  "$work/libcutils/socket_loopback_client.c" "$work/libcutils/socket_loopback_server.c"
  "$work/libcutils/socket_network_client.c" "$work/libcutils/list.c" "$work/libcutils/load_file.c"
  "$work/libmincrypt/rsa.c" "$work/libmincrypt/rsa_e_3.c" "$work/libmincrypt/rsa_e_f4.c"
  "$work/libmincrypt/sha.c" "$work/compat/property_compat.c"
  "$work/compat/android_reboot_compat.c" "$work/compat/base64_compat.c"
)
objects=()
for source in "${sources[@]}"; do
  object="$work/$(basename "${source%.*}").o"
  "$CC" "${CFLAGS[@]}" -c "$source" -o "$object"
  objects+=("$object")
done

"$CC" "${CFLAGS[@]}" -static -Wl,--gc-sections -Wl,--build-id=none -Wl,-z,now \
  -o "$OUTPUT/adbd" "${objects[@]}" -lpthread
chmod 0750 "$OUTPUT/adbd"

binary_sha=$(sha256sum "$OUTPUT/adbd" | awk '{print $1}')
binary_size=$(stat -c %s "$OUTPUT/adbd")
patch_sha=$(sha256sum "$SCRIPT_DIR/libreecho-adbd.patch" | awk '{print $1}')
compiler_version=$("$CC" --version | python3 -c 'import sys; print(sys.stdin.readline().strip())')
python3 -c 'import json, pathlib, sys
out, commit, patch_sha, compiler, kernel_headers, size, binary_sha = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({
  "source": "AOSP platform/system/core",
  "source_url": "https://android.googlesource.com/platform/system/core",
  "source_commit": commit,
  "source_license": "Apache-2.0",
  "patch_sha256": patch_sha,
  "compiler": compiler,
  "kernel_headers": "exported-linux-uapi",
  "binary_sha256": binary_sha,
  "binary_size": int(size),
  "transport": "usb-functionfs-only",
  "tcp_listener": False,
}, sort_keys=True, indent=2) + "\n")' \
  "$OUTPUT/adbd-source.json" "$source_commit" "$patch_sha" "$compiler_version" "$kernel_headers" "$binary_size" "$binary_sha"
printf 'adbd_source_commit=%s\nadbd_patch_sha256=%s\nadbd_sha256=%s\nadbd_size=%s\n' \
  "$source_commit" "$patch_sha" "$binary_sha" "$binary_size"
