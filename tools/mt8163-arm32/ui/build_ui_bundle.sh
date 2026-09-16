#!/usr/bin/env bash
# Build and stage the externally-owned LibreEcho-UI runtime bundle.
#
# The image repository owns this packaging contract.  LibreEcho-UI remains a
# separate source repository and is supplied as an explicit checkout.
#
# HTTPS is a shipped production capability, so the production bundle links the
# pinned ARM32 mbedTLS dependency built by tools/mt8163-arm32/mbedtls.  Without
# the prefix the UI Makefile silently selects src/tls_stub.c, the Web UI keeps
# advertising the HTTPS toggle, and port 8443 can never listen (issue #250).
# A stub TLS bundle is therefore not a production artifact and this builder
# fails closed instead of producing one.
set -euo pipefail

UI_SOURCE=${1:-${LIBREECHO_UI_SRC:-}}
OUTPUT=${2:-}
MAKE_BIN=${MAKE:-make}
CROSS_COMPILE=${LIBREECHO_UI_CROSS_COMPILE:-/usr/bin/arm-linux-gnueabihf-}
CC_BIN=${LIBREECHO_UI_CC:-gcc}
STRIP_BIN=${LIBREECHO_UI_STRIP:-${CROSS_COMPILE}strip}
MBEDTLS_ROOT=${LIBREECHO_UI_MBEDTLS_ROOT:-}
TLS_LIBS=${LIBREECHO_UI_TLS_LIBS:--lmbedtls -lmbedx509 -lmbedcrypto}
GC_LDFLAGS=${LIBREECHO_UI_GC_LDFLAGS:--static -Wl,--gc-sections}
USERS_SOURCE=${LIBREECHO_WEB_USERS_FILE:-}
MUSL_NATIVE_ROOT=${LIBREECHO_UI_MUSL_NATIVE_ROOT:-/path/to/musl-native-root}
MUSL_SYSROOT=${LIBREECHO_UI_MUSL_SYSROOT:-/path/to/musl-arm32-sysroot}
MUSL_CC=${LIBREECHO_UI_MUSL_CC:-$MUSL_NATIVE_ROOT/usr/bin/armv7-alpine-linux-musleabihf-gcc}
MUSL_NATIVE_LIB=${LIBREECHO_UI_MUSL_NATIVE_LIB:-$MUSL_NATIVE_ROOT/usr/lib}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
VERIFY_TLS=$SCRIPT_DIR/verify_ui_tls.sh
TLS_BINARIES=(libreecho-web libreecho-radiod)

[[ -n "$UI_SOURCE" && -d "$UI_SOURCE" ]] || {
    echo "ERROR: LibreEcho-UI source checkout is required" >&2
    exit 1
}
[[ -n "$OUTPUT" ]] || {
    echo "ERROR: UI bundle output directory is required" >&2
    exit 1
}
[[ ! -e "$OUTPUT" ]] || {
    echo "ERROR: refusing to overwrite UI bundle output: $OUTPUT" >&2
    exit 1
}
command -v "$MAKE_BIN" >/dev/null 2>&1 || {
    echo "ERROR: make not found: $MAKE_BIN" >&2
    exit 1
}
[[ -x "${CROSS_COMPILE}gcc" ]] || {
    echo "ERROR: UI ARM32 compiler not found: ${CROSS_COMPILE}gcc" >&2
    exit 1
}
[[ -x "$STRIP_BIN" ]] || {
    echo "ERROR: UI ARM32 strip tool not found: $STRIP_BIN" >&2
    exit 1
}
[[ -x "$MUSL_CC" && -f "$MUSL_SYSROOT/usr/include/errno.h" ]] || {
    echo "ERROR: UI ARM32 musl compiler/sysroot is unavailable" >&2
    exit 1
}
[[ -n "$MBEDTLS_ROOT" ]] || {
    echo "ERROR: LIBREECHO_UI_MBEDTLS_ROOT must name the pinned ARM32 mbedTLS prefix (tools/mt8163-arm32/mbedtls)" >&2
    exit 1
}
[[ -x "$VERIFY_TLS" ]] || {
    echo "ERROR: UI TLS contract verifier is missing: $VERIFY_TLS" >&2
    exit 1
}
"$VERIFY_TLS" --prefix "$MBEDTLS_ROOT"
if [[ -n "$USERS_SOURCE" ]]; then
    [[ -f "$USERS_SOURCE" && ! -L "$USERS_SOURCE" ]] || {
        echo "ERROR: LibreEcho web users file must be a regular file: $USERS_SOURCE" >&2
        exit 1
    }
    users_mode=$(stat -c %a "$USERS_SOURCE")
    (( 8#$users_mode & 077 )) && {
        echo "ERROR: LibreEcho web users file is group/world accessible: $USERS_SOURCE" >&2
        exit 1
    }
fi

UI_SOURCE=$(cd -- "$UI_SOURCE" && pwd -P)
ui_commit=$(git -C "$UI_SOURCE" rev-parse HEAD)
source_state_sha256() {
    local repository=$1

    {
        git -C "$repository" diff --binary HEAD
        while IFS= read -r -d '' relative; do
            printf '\0untracked:%s\0' "$relative"
            sha256sum "$repository/$relative" | awk '{print $1}'
        done < <(
            git -C "$repository" ls-files --others --exclude-standard -z |
                LC_ALL=C sort -z
        )
    } | sha256sum | awk '{print $1}'
}
ui_diff_sha256=$(source_state_sha256 "$UI_SOURCE")

# The UI Makefile appends its own definitions to CPPFLAGS, so the mbedTLS
# include path is exported (a command-line CPPFLAGS would suppress them).  The
# library search path must travel in GC_LDFLAGS because `release` replaces
# LDFLAGS with GC_LDFLAGS for its recursive build.
export CPPFLAGS="-I$MBEDTLS_ROOT/include${CPPFLAGS:+ $CPPFLAGS}"
"$MAKE_BIN" -C "$UI_SOURCE" clean
"$MAKE_BIN" -C "$UI_SOURCE" \
    CROSS_COMPILE="$CROSS_COMPILE" CC="$CC_BIN" \
    GC_LDFLAGS="$GC_LDFLAGS -L$MBEDTLS_ROOT/lib" \
    WEB_TLS_LIBS="$TLS_LIBS" RADIOD_TLS_LIBS="$TLS_LIBS" \
    release

# A build that selected src/tls_stub.c (LE_TLS_AVAILABLE=0) or that failed to
# link mbedTLS must fail here, before anything is staged for the image.
for binary in "${TLS_BINARIES[@]}"; do
    "$VERIFY_TLS" --binary "$UI_SOURCE/build/$binary" \
        --objects "$UI_SOURCE/build" --label "$binary"
done

# These clients need only libc and the ramdisk already carries the pinned musl
# loader. Avoid embedding a separate static glibc copy in each constrained
# 16 MiB boot image.
env LD_LIBRARY_PATH="$MUSL_NATIVE_LIB" \
    "$MAKE_BIN" -B -C "$UI_SOURCE" \
    CROSS_COMPILE= CC="$MUSL_CC --sysroot=$MUSL_SYSROOT" \
    CFLAGS="-Os -ffunction-sections -fdata-sections" \
    LDFLAGS="-Wl,--gc-sections" \
    build/libreecho-sttd-wyoming build/libreecho-ttsd-wyoming

for binary in \
    libreecho-web libreecho-logd libreecho-networkd libreecho-timed \
    libreecho-timerd \
    libreecho-audiod libreecho-micd libreecho-ledd libreecho-buttond \
    libreecho-radiod libreecho-btd \
    libreecho-airplayd libreecho-wyomingd
do
    path="$UI_SOURCE/build/$binary"
    [[ -f "$path" && ! -L "$path" ]] || {
        echo "ERROR: missing UI binary: $path" >&2
        exit 1
    }
    description=$(file -b "$path")
    case "$description" in
        *"ELF 32-bit"*"ARM"*"statically linked"*) ;;
        *) echo "ERROR: UI binary is not static ARM32: $path: $description" >&2; exit 1 ;;
    esac
    if readelf -l "$path" | grep -q 'Requesting program interpreter'; then
        echo "ERROR: UI binary has a dynamic interpreter: $path" >&2
        exit 1
    fi
done

for binary in libreecho-sttd-wyoming libreecho-ttsd-wyoming
do
    path="$UI_SOURCE/build/$binary"
    [[ -f "$path" && ! -L "$path" ]] || {
        echo "ERROR: missing UI binary: $path" >&2
        exit 1
    }
    description=$(file -b "$path")
    case "$description" in
        *"ELF 32-bit"*"ARM"*"dynamically linked"*) ;;
        *) echo "ERROR: Wyoming client is not dynamic ARM32: $path: $description" >&2; exit 1 ;;
    esac
    readelf -l "$path" |
        grep -q 'Requesting program interpreter: /lib/ld-musl-armhf.so.1' || {
            echo "ERROR: Wyoming client musl interpreter changed: $path" >&2
            exit 1
        }
    needed=$(readelf -d "$path" |
        sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p')
    [[ "$needed" == "libc.musl-armv7.so.1" ]] || {
        echo "ERROR: Wyoming client dependencies changed: $path: $needed" >&2
        exit 1
    }
done

mkdir -p "$OUTPUT/sbin" "$OUTPUT/share/libreecho/web" \
    "$OUTPUT/share/libreecho/sounds" "$OUTPUT/etc/init.d" \
    "$OUTPUT/etc/libreecho/avahi-services"

for binary in \
    libreecho-web libreecho-logd libreecho-networkd libreecho-timed \
    libreecho-timerd \
    libreecho-audiod libreecho-micd libreecho-ledd libreecho-buttond \
    libreecho-radiod libreecho-btd \
    libreecho-airplayd libreecho-wyomingd \
    libreecho-sttd-wyoming libreecho-ttsd-wyoming
do
    install -m 0755 "$UI_SOURCE/build/$binary" "$OUTPUT/sbin/$binary"
    "$STRIP_BIN" --strip-unneeded "$OUTPUT/sbin/$binary"
done

# The stripped, staged artifact is what the image ships, so the TLS contract is
# verified again after stripping.
for binary in "${TLS_BINARIES[@]}"; do
    "$VERIFY_TLS" --binary "$OUTPUT/sbin/$binary" --label "sbin/$binary"
done

for script in \
    libreecho-web.init libreecho-logd.init libreecho-networkd.init libreecho-timed.init \
    libreecho-timerd.init \
    libreecho-audiod.init libreecho-micd.init libreecho-ledd.init \
    libreecho-buttond.init libreecho-radiod.init libreecho-btd.init \
    libreecho-airplayd.init libreecho-ttsd.init \
    libreecho-waked.init libreecho-sttd.init libreecho-agentd.init \
    libreecho-wyomingd.init
do
    install -m 0755 "$UI_SOURCE/init/$script" "$OUTPUT/etc/init.d/$script"
done

cp -R "$UI_SOURCE/web/." "$OUTPUT/share/libreecho/web/"
install -m 0600 "$UI_SOURCE/config/defaults.json" \
    "$OUTPUT/etc/libreecho/web-config.json"
install -m 0644 "$UI_SOURCE/config/airplay2.conf" \
    "$OUTPUT/etc/libreecho/airplay2.conf"
install -m 0644 "$UI_SOURCE/config/ntp.conf" \
    "$OUTPUT/etc/libreecho/ntp.conf"
wyoming_service="$UI_SOURCE/config/wyoming.service"
[[ -f "$wyoming_service" && ! -L "$wyoming_service" && -s "$wyoming_service" ]] || {
    echo "ERROR: missing or empty Wyoming service definition: $wyoming_service" >&2
    exit 1
}
grep -Fq '<type>_wyoming._tcp</type>' "$wyoming_service" || {
    echo "ERROR: Wyoming service definition has no _wyoming._tcp entry" >&2
    exit 1
}
grep -Fq '<port>10700</port>' "$wyoming_service" || {
    echo "ERROR: Wyoming service definition has no port 10700" >&2
    exit 1
}
install -m 0644 "$wyoming_service" \
    "$OUTPUT/etc/libreecho/avahi-services/wyoming.service"
for sound in action-1.raw action-2.raw action-3.raw; do
    path="$UI_SOURCE/sounds/$sound"
    [[ -f "$path" && ! -L "$path" && -s "$path" ]] || {
        echo "ERROR: missing or empty UI action sound: $path" >&2
        exit 1
    }
    install -m 0644 "$path" "$OUTPUT/share/libreecho/sounds/$sound"
done
if [[ -n "$USERS_SOURCE" ]]; then
    install -m 0600 "$USERS_SOURCE" "$OUTPUT/etc/libreecho/users"
fi

{
    printf 'schema=1\n'
    printf 'source_commit=%s\n' "$ui_commit"
    printf 'source_diff_sha256=%s\n' "$ui_diff_sha256"
    while IFS= read -r relative; do
        hash=$(sha256sum "$OUTPUT/$relative" | awk '{print $1}')
        printf 'file=%s sha256=%s\n' "$relative" "$hash"
    done < <(find "$OUTPUT" -type f ! -name ui-manifest.txt -printf '%P\n' | LC_ALL=C sort)
} > "$OUTPUT/share/libreecho/ui-manifest.txt"

ui_manifest_sha256=$(sha256sum "$OUTPUT/share/libreecho/ui-manifest.txt" | awk '{print $1}')
printf 'ui_source=%s\nui_commit=%s\nui_diff_sha256=%s\nui_manifest_sha256=%s\nui_tls=real\nui_tls_libs=%s\nui_mbedtls_root=%s\n' \
    "$UI_SOURCE" "$ui_commit" "$ui_diff_sha256" "$ui_manifest_sha256" \
    "$TLS_LIBS" "$MBEDTLS_ROOT"
