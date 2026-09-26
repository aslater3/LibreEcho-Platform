#!/usr/bin/env bash
# Build the pinned ARM32 Dropbear server and host-key utility.
set -euo pipefail

SSH_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PIPELINE_ROOT_INPUT="${LIBREECHO_PIPELINE_ROOT:-}"
[[ -n "$PIPELINE_ROOT_INPUT" ]] || {
  echo "ERROR: set LIBREECHO_PIPELINE_ROOT to the canonical pipeline directory" >&2
  exit 1
}
PIPELINE_ROOT="$(cd -- "$PIPELINE_ROOT_INPUT" && pwd -P)"

SOURCE_ARCHIVE="${DROPBEAR_SOURCE_ARCHIVE:-$PIPELINE_ROOT/inputs/dropbear-2026.93.tar.bz2}"
OUTPUT_ROOT_INPUT="${DROPBEAR_OUTPUT_ROOT:-$PIPELINE_ROOT/work/dropbear-2026.93}"
OUTPUT_ROOT="$(mkdir -p "$OUTPUT_ROOT_INPUT" && cd -- "$OUTPUT_ROOT_INPUT" && pwd -P)"
SOURCE_DIR="$OUTPUT_ROOT/source"
OUTPUT_DIR="$OUTPUT_ROOT/output"
PATCH_DIR="$SSH_DIR/patches"
LIBCRYPT_DEV_PACKAGE="${DROPBEAR_LIBCRYPT_DEV_PACKAGE:-$PIPELINE_ROOT/inputs/libcrypt-dev_4.4.36-4build1_armhf.deb}"
LIBCRYPT_RUNTIME_PACKAGE="${DROPBEAR_LIBCRYPT_RUNTIME_PACKAGE:-$PIPELINE_ROOT/inputs/libcrypt1_4.4.36-4build1_armhf.deb}"
LIBCRYPT_ROOT="$OUTPUT_ROOT/libcrypt-sysroot"
CROSS_PREFIX="${DROPBEAR_CROSS_PREFIX:-/usr/bin/arm-linux-gnueabihf-}"
CC="${DROPBEAR_CC:-${CROSS_PREFIX}gcc}"
STRIP="${DROPBEAR_STRIP:-${CROSS_PREFIX}strip}"
BUILD_CC="${DROPBEAR_BUILD_CC:-cc}"
JOBS="${JOBS:-$(nproc)}"
EXPECTED_SOURCE_SHA256="310a6087952897c182efbe16088fa0c4d07c467e850a22699472137278fabf09"
EXPECTED_LIBCRYPT_DEV_SHA256="58010a8f588477dae4444f3b92636ef4e02aa2d21ab4587d33702299163a5380"
EXPECTED_LIBCRYPT_RUNTIME_SHA256="198990e999add09e21b0ab3522c94333a0e135656c7f00960f534cec15477a74"

[[ -f "$SOURCE_ARCHIVE" && ! -L "$SOURCE_ARCHIVE" ]] || {
  echo "ERROR: missing Dropbear source archive: $SOURCE_ARCHIVE" >&2
  exit 1
}
[[ -f "$LIBCRYPT_DEV_PACKAGE" && ! -L "$LIBCRYPT_DEV_PACKAGE" ]] || {
  echo "ERROR: missing ARMHF libcrypt development package: $LIBCRYPT_DEV_PACKAGE" >&2
  exit 1
}
[[ -f "$LIBCRYPT_RUNTIME_PACKAGE" && ! -L "$LIBCRYPT_RUNTIME_PACKAGE" ]] || {
  echo "ERROR: missing ARMHF libcrypt runtime package: $LIBCRYPT_RUNTIME_PACKAGE" >&2
  exit 1
}
[[ -x "$CC" ]] || { echo "ERROR: ARM32 compiler not found: $CC" >&2; exit 1; }
[[ -x "$STRIP" ]] || { echo "ERROR: ARM32 strip not found: $STRIP" >&2; exit 1; }
[[ -x "$(command -v "$BUILD_CC")" ]] || { echo "ERROR: build compiler not found: $BUILD_CC" >&2; exit 1; }
actual_source_sha256="$(sha256sum "$SOURCE_ARCHIVE" | awk '{print $1}')"
[[ "$actual_source_sha256" == "$EXPECTED_SOURCE_SHA256" ]] || {
  echo "ERROR: Dropbear source hash mismatch: $actual_source_sha256" >&2
  exit 1
}
actual_libcrypt_dev_sha256="$(sha256sum "$LIBCRYPT_DEV_PACKAGE" | awk '{print $1}')"
[[ "$actual_libcrypt_dev_sha256" == "$EXPECTED_LIBCRYPT_DEV_SHA256" ]] || {
  echo "ERROR: ARMHF libcrypt development package hash mismatch: $actual_libcrypt_dev_sha256" >&2
  exit 1
}
actual_libcrypt_runtime_sha256="$(sha256sum "$LIBCRYPT_RUNTIME_PACKAGE" | awk '{print $1}')"
[[ "$actual_libcrypt_runtime_sha256" == "$EXPECTED_LIBCRYPT_RUNTIME_SHA256" ]] || {
  echo "ERROR: ARMHF libcrypt runtime package hash mismatch: $actual_libcrypt_runtime_sha256" >&2
  exit 1
}

rm -rf -- "$SOURCE_DIR" "$OUTPUT_DIR" "$LIBCRYPT_ROOT"
mkdir -p "$SOURCE_DIR" "$OUTPUT_DIR" "$LIBCRYPT_ROOT"
tar --extract --bzip2 --file "$SOURCE_ARCHIVE" --strip-components=1 --directory "$SOURCE_DIR"
dpkg-deb --extract "$LIBCRYPT_DEV_PACKAGE" "$LIBCRYPT_ROOT"
dpkg-deb --extract "$LIBCRYPT_RUNTIME_PACKAGE" "$LIBCRYPT_ROOT"
cp -- "$SSH_DIR/localoptions.h" "$SOURCE_DIR/localoptions.h"
patch -d "$SOURCE_DIR" -p1 --forward < "$PATCH_DIR/0001-linux-pty-controlling-tty.patch"
patch -d "$SOURCE_DIR" -p1 --forward < "$PATCH_DIR/0002-webui-users-password-auth.patch"
patch -d "$SOURCE_DIR" -p1 --forward < "$PATCH_DIR/0003-webui-users-password-branch.patch"
cp -- "$SSH_DIR/libreecho-auth.c" "$SOURCE_DIR/src/libreecho-auth.c"

cd "$SOURCE_DIR"
build_triplet="$($BUILD_CC -dumpmachine)"
host_triplet="$($CC -dumpmachine)"
export CC
export CFLAGS="${DROPBEAR_CFLAGS:--Os -ffunction-sections -fdata-sections -fno-ident}"
export LDFLAGS="${DROPBEAR_LDFLAGS:--static -Wl,--gc-sections,--build-id=none}"
# Cross-configure cannot inspect the target's /dev/ptmx.  The LibreEcho
# initramfs mounts devpts and materializes /dev/ptmx, so select Dropbear's
# Linux PTY backend explicitly instead of silently shipping a shell-less SSH
# server.
export CPPFLAGS="${DROPBEAR_CPPFLAGS:-} -DUSE_DEV_PTMX -I$LIBCRYPT_ROOT/usr/include"
export LIBS="${DROPBEAR_LIBS:--L$LIBCRYPT_ROOT/usr/lib/arm-linux-gnueabihf -lcrypt}"
./configure \
  --build="$build_triplet" \
  --host="$host_triplet" \
  --enable-static \
  --disable-zlib \
  --disable-openpty \
  --disable-syslog \
  --disable-lastlog \
  --disable-utmp \
  --disable-utmpx \
  --disable-wtmp \
  --disable-wtmpx \
  --disable-loginfunc \
  --disable-largefile
make -j"$JOBS" PROGRAMS="dropbear dropbearkey scp"

"$STRIP" --strip-unneeded dropbear dropbearkey scp
cp -- dropbear dropbearkey scp "$OUTPUT_DIR/"

for binary in dropbear dropbearkey scp; do
  path="$OUTPUT_DIR/$binary"
  file -L "$path" | grep -Eq 'ELF 32-bit.*ARM' || {
    echo "ERROR: unexpected ELF identity: $path" >&2
    exit 1
  }
  ! readelf -l "$path" | grep -q 'INTERP' || {
    echo "ERROR: dynamic interpreter present: $path" >&2
    exit 1
  }
  ! readelf -d "$path" 2>&1 | grep -q 'NEEDED' || {
    echo "ERROR: shared-library dependency present: $path" >&2
    exit 1
  }
done
! strings "$OUTPUT_DIR/dropbear" | grep -q 'authorized_keys' || {
  echo "ERROR: public-key authorization code appears in dropbear" >&2
  exit 1
}

dropbear_sha256="$(sha256sum "$OUTPUT_DIR/dropbear" | awk '{print $1}')"
dropbearkey_sha256="$(sha256sum "$OUTPUT_DIR/dropbearkey" | awk '{print $1}')"
scp_sha256="$(sha256sum "$OUTPUT_DIR/scp" | awk '{print $1}')"
cat > "$OUTPUT_DIR/provenance.txt" <<EOF
schema=1
source_archive=$SOURCE_ARCHIVE
source_sha256=$actual_source_sha256
source_version=2026.93
cross_compiler=$CC
build_compiler=$BUILD_CC
cross_triplet=$build_triplet
host_triplet=$host_triplet
libcrypt_dev_package=$LIBCRYPT_DEV_PACKAGE
libcrypt_dev_sha256=$actual_libcrypt_dev_sha256
libcrypt_runtime_package=$LIBCRYPT_RUNTIME_PACKAGE
libcrypt_runtime_sha256=$actual_libcrypt_runtime_sha256
dropbear_sha256=$dropbear_sha256
dropbearkey_sha256=$dropbearkey_sha256
scp_sha256=$scp_sha256
server_auth=webui-users-sha256
server_public_key_auth=disabled
pty_backend=linux-unix98-existing-slave
static=1
EOF
printf 'dropbear=%s\ndropbearkey=%s\nscp=%s\nprovenance=%s\n' \
  "$OUTPUT_DIR/dropbear" "$OUTPUT_DIR/dropbearkey" "$OUTPUT_DIR/scp" "$OUTPUT_DIR/provenance.txt"
printf 'dropbear_sha256=%s\ndropbearkey_sha256=%s\nscp_sha256=%s\n' \
  "$dropbear_sha256" "$dropbearkey_sha256" "$scp_sha256"
