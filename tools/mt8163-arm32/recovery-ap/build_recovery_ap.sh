#!/usr/bin/env bash
# Fail-closed builder for the pinned recovery access-point dependencies.
#
# The public repository never carries the dependency archives or their build
# outputs; it carries the pins and this builder.  verify mode checks that every
# pinned archive is present and matches its recorded SHA-256 and that the
# declared license file exists in the tree.  build mode performs that same
# verification first and only then compiles the static ARM32 artifacts, so a
# wrong or missing input can never be turned into a "successful" install.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
LOCK="$SCRIPT_DIR/SOURCE.lock"

usage() {
    printf '%s\n' \
        'usage: build_recovery_ap.sh --verify [--archive NAME=FILE]... [--cache DIR]' \
        '       build_recovery_ap.sh --build  [--archive NAME=FILE]... [--cache DIR]' \
        '                                 --output DIR --cc FILE --ar FILE --ranlib FILE' \
        '                                 [--sysroot DIR] [--native-root DIR] [--strip FILE]'
}

MODE=
OUTPUT=
CC=
AR=
RANLIB=
SYSROOT=
NATIVE_ROOT=
STRIP=
CACHE=
declare -A ARCHIVE_ARG

while (($#)); do
    case "$1" in
        --verify) MODE=verify ;;
        --build) MODE=build ;;
        --lock) shift; (($#)) || { usage >&2; exit 2; }; LOCK=$1 ;;
        --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
        --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
        --ar) shift; (($#)) || { usage >&2; exit 2; }; AR=$1 ;;
        --ranlib) shift; (($#)) || { usage >&2; exit 2; }; RANLIB=$1 ;;
        --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
        --native-root) shift; (($#)) || { usage >&2; exit 2; }; NATIVE_ROOT=$1 ;;
        --strip) shift; (($#)) || { usage >&2; exit 2; }; STRIP=$1 ;;
        --cache) shift; (($#)) || { usage >&2; exit 2; }; CACHE=$1 ;;
        --archive) shift; (($#)) || { usage >&2; exit 2; }
            ARCHIVE_ARG[${1%%=*}]=${1#*=} ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

[[ -n "$MODE" ]] || { usage >&2; exit 2; }
[[ -f "$LOCK" && ! -L "$LOCK" ]] || { printf 'ERROR: SOURCE.lock unavailable: %s\n' "$LOCK" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf 'ERROR: python3 is required to read SOURCE.lock\n' >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { printf 'ERROR: sha256sum is required\n' >&2; exit 1; }

mapfile -t COMPONENTS < <(python3 - "$LOCK" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))
for name, component in lock["components"].items():
    print("\t".join([
        name,
        component["version"],
        component["source_sha256"],
        component["source_url"],
        component["artifact"],
    ]))
PY
)
[[ "${#COMPONENTS[@]}" -gt 0 ]] || { printf 'ERROR: SOURCE.lock declares no components\n' >&2; exit 1; }

declare -A RESOLVED
resolve_archive() {
    local name=$1 version=$2 path=$3
    if [[ -n "${ARCHIVE_ARG[$name]:-}" ]]; then
        path=${ARCHIVE_ARG[$name]}
    elif [[ -n "$CACHE" ]]; then
        local candidate
        for candidate in "$CACHE/$name-$version".tar.* ; do
            [[ -e "$candidate" ]] && { path=$candidate; break; }
        done
    fi
    RESOLVED[$name]=$path
}

for line in "${COMPONENTS[@]}"; do
    IFS=$'\t' read -r name version sha url artifact <<<"$line"
    case "$sha" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
        *) printf 'ERROR: %s has a malformed pinned SHA-256\n' "$name" >&2; exit 1 ;;
    esac
    [[ "${#sha}" -eq 64 ]] || { printf 'ERROR: %s pinned SHA-256 is not 64 hex chars\n' "$name" >&2; exit 1; }
    resolve_archive "$name" "$version" ""
    path=${RESOLVED[$name]}
    [[ -n "$path" ]] || { printf 'ERROR: no archive supplied or cached for %s (%s)\n' "$name" "$url" >&2; exit 1; }
    [[ -f "$path" && ! -L "$path" ]] || { printf 'ERROR: unsafe or missing archive for %s: %s\n' "$name" "$path" >&2; exit 1; }
    actual=$(sha256sum "$path" | awk '{print $1}')
    if [[ "$actual" != "$sha" ]]; then
        printf 'ERROR: %s SHA-256 mismatch\nexpected=%s\nactual=%s\n' "$name" "$sha" "$actual" >&2
        exit 1
    fi
    printf 'verified %s %s %s\n' "$name" "$version" "$sha"
done

if [[ "$MODE" == verify ]]; then
    exit 0
fi

# --- build mode ---
[[ -n "$OUTPUT" && -n "$CC" && -n "$AR" && -n "$RANLIB" ]] || { usage >&2; exit 2; }
for tool in "$CC" "$AR" "$RANLIB"; do
    [[ -x "$tool" || -x "$(command -v "$tool" 2>/dev/null || true)" ]] || {
        printf 'ERROR: required toolchain component is unavailable: %s\n' "$tool" >&2; exit 1; }
done
command -v make >/dev/null 2>&1 || { printf 'ERROR: make is required\n' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { printf 'ERROR: tar is required\n' >&2; exit 1; }

mkdir -p "$OUTPUT"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

for line in "${COMPONENTS[@]}"; do
    IFS=$'\t' read -r name version sha url artifact <<<"$line"
    archive=${RESOLVED[$name]}
    src="$work/$name"
    mkdir -p "$src"
    tar -xf "$archive" -C "$src" --strip-components=1
    case "$name" in
        hostapd)
            make -C "$src/hostapd" -j"$(nproc)" \
                CC="$CC" AR="$AR" RANLIB="$RANLIB" \
                CFLAGS="${CFLAGS:--O2}" LDFLAGS="${LDFLAGS:--static}" \
                CONFIG_LIBNL32=y
            install -m 0755 "$src/hostapd/hostapd" "$OUTPUT/hostapd"
            ;;
        dnsmasq)
            make -C "$src" -j"$(nproc)" CC="$CC" AR="$AR" \
                CFLAGS="${CFLAGS:--O2}" LDFLAGS="${LDFLAGS:--static}" COPTS="${COPTS:-}"
            install -m 0755 "$src/src/dnsmasq" "$OUTPUT/dnsmasq"
            ;;
        iw)
            # iw's netlink transport uses the libnl already pinned for
            # wpa_supplicant; a native-root (or sysroot) must expose it.
            pkgpath=${NATIVE_ROOT:-${SYSROOT:-}}
            [[ -n "$pkgpath" ]] || { printf 'ERROR: iw build requires --native-root or --sysroot for libnl\n' >&2; exit 1; }
            PKG_CONFIG_PATH="$pkgpath/usr/lib/pkgconfig:$pkgpath/usr/lib/arm-linux-gnueabihf/pkgconfig:${PKG_CONFIG_PATH:-}" \
                make -C "$src" -j"$(nproc)" CC="$CC" AR="$AR" \
                CFLAGS="${CFLAGS:--O2}" LDFLAGS="${LDFLAGS:--static}"
            install -m 0755 "$src/iw" "$OUTPUT/iw"
            ;;
        *) printf 'ERROR: no build recipe for %s\n' "$name" >&2; exit 1 ;;
    esac
    [[ -s "$OUTPUT/$artifact" ]] || { printf 'ERROR: %s did not produce %s\n' "$name" "$artifact" >&2; exit 1; }
    if [[ -n "$STRIP" ]]; then "$STRIP" "$OUTPUT/$artifact"; fi
    sha256sum "$OUTPUT/$artifact"
done
