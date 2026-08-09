#!/usr/bin/env bash
set -euo pipefail

SOURCE_ARCHIVE="${1:?usage: build_curl.sh <curl-source.tar.xz> <armhf-sysroot> <output> <license-output>}"
SYSROOT="${2:?usage: build_curl.sh <curl-source.tar.xz> <armhf-sysroot> <output> <license-output>}"
OUTPUT="${3:?usage: build_curl.sh <curl-source.tar.xz> <armhf-sysroot> <output> <license-output>}"
LICENSE_OUTPUT="${4:?usage: build_curl.sh <curl-source.tar.xz> <armhf-sysroot> <output> <license-output>}"
CROSS="${LIBREECHO_ASSISTANT_CROSS:-/usr/bin/arm-linux-gnueabihf-}"
JOBS="${JOBS:-$(nproc)}"
SOURCE_SHA256=aa1b66a70eace83dc624508745646c08ae561de512ab403adffb93ac87fc72e6
RELINK_OUTPUT="${LIBREECHO_ASSISTANT_RELINK_OUTPUT:-}"

[[ -f "$SOURCE_ARCHIVE" && ! -L "$SOURCE_ARCHIVE" ]] || {
  echo "ERROR: curl source archive is missing or is a symlink" >&2
  exit 1
}
[[ -d "$SYSROOT" && ! -L "$SYSROOT" ]] || {
  echo "ERROR: ARMHF dependency sysroot is missing or is a symlink" >&2
  exit 1
}
[[ ! -e "$OUTPUT" && ! -e "$LICENSE_OUTPUT" ]] || {
  echo "ERROR: refusing to overwrite curl output or license" >&2
  exit 1
}
[[ -x "${CROSS}gcc" && -x "${CROSS}strip" ]] || {
  echo "ERROR: ARMHF cross compiler is missing: $CROSS" >&2
  exit 1
}
[[ "$(sha256sum "$SOURCE_ARCHIVE" | awk '{print $1}')" == "$SOURCE_SHA256" ]] || {
  echo "ERROR: curl source hash is not the reviewed 8.21.0 release" >&2
  exit 1
}
SOURCE_ARCHIVE="$(readlink -f "$SOURCE_ARCHIVE")"
SYSROOT="$(cd -- "$SYSROOT" && pwd -P)"
for required in \
  "$SYSROOT/usr/include/openssl/ssl.h" \
  "$SYSROOT/usr/include/arm-linux-gnueabihf/openssl/opensslconf.h" \
  "$SYSROOT/usr/lib/arm-linux-gnueabihf/libssl.a" \
  "$SYSROOT/usr/lib/arm-linux-gnueabihf/libcrypto.a"; do
  [[ -f "$required" ]] || {
    echo "ERROR: static curl dependency is missing: $required" >&2
    exit 1
  }
done

work_parent="$(dirname -- "$OUTPUT")"
license_parent="$(dirname -- "$LICENSE_OUTPUT")"
mkdir -p "$work_parent" "$license_parent"
work_parent="$(cd -- "$work_parent" && pwd -P)"
license_parent="$(cd -- "$license_parent" && pwd -P)"
OUTPUT="$work_parent/$(basename -- "$OUTPUT")"
LICENSE_OUTPUT="$license_parent/$(basename -- "$LICENSE_OUTPUT")"
work="$(mktemp -d "$work_parent/.curl-build.XXXXXX")"
trap 'rm -rf "$work"' EXIT
source_root="$work/source"
build_root="$work/build"
mkdir -p "$source_root" "$build_root"
tar -xJf "$SOURCE_ARCHIVE" --strip-components=1 -C "$source_root"

include_root="$SYSROOT/usr/include"
library_root="$SYSROOT/usr/lib/arm-linux-gnueabihf"
pkgconfig_root="$library_root/pkgconfig"

(
  cd "$build_root"
  env \
    CC="${CROSS}gcc --sysroot=$SYSROOT" AR="${CROSS}ar" RANLIB="${CROSS}ranlib" \
    STRIP="${CROSS}strip" CURL_LDFLAGS_BIN=-all-static \
    PKG_CONFIG_SYSROOT_DIR="$SYSROOT" \
    PKG_CONFIG_LIBDIR="$pkgconfig_root" \
    CPPFLAGS="-I$include_root/arm-linux-gnueabihf -I$include_root" \
    CFLAGS="--sysroot=$SYSROOT -Os -ffunction-sections -fdata-sections" \
    LDFLAGS="--sysroot=$SYSROOT -static -Wl,--gc-sections -L$library_root" \
    "$source_root/configure" \
      --host=arm-linux-gnueabihf --build=x86_64-pc-linux-gnu \
      --prefix=/usr/local --disable-shared --enable-static \
      --enable-http --disable-ftp --disable-file --disable-ipfs \
      --disable-ldap --disable-ldaps --disable-rtsp --disable-proxy \
      --disable-dict --disable-telnet --disable-tftp --disable-pop3 \
      --disable-imap --disable-smb --disable-smtp --disable-gopher \
      --disable-mqtt --disable-manual --disable-docs \
      --disable-libcurl-option --disable-ipv6 \
      --disable-threaded-resolver --disable-verbose \
      --disable-basic-auth --disable-bearer-auth \
      --disable-digest-auth --disable-kerberos-auth \
      --disable-negotiate-auth --disable-aws --disable-ntlm \
      --disable-tls-srp --disable-unix-sockets --disable-cookies \
      --disable-socketpair --disable-http-auth --disable-doh \
      --disable-mime --disable-bindlocal --disable-form-api \
      --disable-dateparse --disable-netrc --disable-progress-meter \
      --disable-sha512-256 --disable-dnsshuffle \
      --disable-get-easy-options --disable-alt-svc \
      --disable-headers-api --disable-hsts --disable-websockets \
      --disable-openssl-auto-load-config \
      --without-zlib --without-brotli --without-zstd \
      --without-libpsl --without-libgsasl --without-libssh2 \
      --without-libssh --without-libidn2 --without-nghttp2 \
      --without-ngtcp2 --without-nghttp3 --without-quiche \
      --without-libuv --without-ca-bundle --without-ca-path \
      --without-ca-fallback --with-openssl
  make -j"$JOBS"
  make -C src clean
  make -C src -j"$JOBS" CURL_LDFLAGS_BIN=-all-static curl
)

preserve_relink_objects() {
  [[ -n "$RELINK_OUTPUT" ]] || return 0
  [[ ! -e "$RELINK_OUTPUT" && ! -L "$RELINK_OUTPUT" ]] || {
    echo "ERROR: refusing to overwrite curl relink output: $RELINK_OUTPUT" >&2
    exit 1
  }
  mkdir -p "$RELINK_OUTPUT"
  local object relative destination count=0
  while IFS= read -r -d '' object; do
    relative=${object#"$build_root"/}
    destination="$RELINK_OUTPUT/$relative"
    mkdir -p "$(dirname -- "$destination")"
    install -m 0644 "$object" "$destination"
    count=$((count + 1))
  done < <(find "$build_root" -type f -name '*.o' -print0 | sort -z)
  [[ "$count" -gt 0 ]] || {
    echo "ERROR: curl build produced no relinkable objects" >&2
    exit 1
  }
  printf 'curl_relink_object_count=%s\n' "$count"
}

preserve_relink_objects

"${CROSS}strip" -o "$OUTPUT" "$build_root/src/curl"
install -m 0644 "$source_root/COPYING" "$LICENSE_OUTPUT"
description="$(file -b "$OUTPUT")"
case "$description" in
  *"ELF 32-bit"*"ARM"*"statically linked"*) ;;
  *)
    echo "ERROR: curl output is not static ARM32: $description" >&2
    exit 1
    ;;
esac
if readelf -l "$OUTPUT" | grep -q 'Requesting program interpreter'; then
  echo "ERROR: curl output has a dynamic interpreter" >&2
  exit 1
fi
if readelf -d "$OUTPUT" 2>/dev/null | grep -q '(NEEDED)'; then
  echo "ERROR: curl output has dynamic dependencies" >&2
  exit 1
fi
printf 'curl_source_sha256=%s\n' "$SOURCE_SHA256"
printf 'curl_binary_sha256=%s\n' "$(sha256sum "$OUTPUT" | awk '{print $1}')"
stat -c 'curl_binary_size=%s' "$OUTPUT"
