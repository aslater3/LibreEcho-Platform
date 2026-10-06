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
GC_LDFLAGS=${LIBREECHO_UI_GC_LDFLAGS:--static -Wl,--gc-sections}
USERS_SOURCE=${LIBREECHO_WEB_USERS_FILE:-}
MUSL_NATIVE_ROOT=${LIBREECHO_UI_MUSL_NATIVE_ROOT:-/path/to/musl-native-root}
MUSL_SYSROOT=${LIBREECHO_UI_MUSL_SYSROOT:-/path/to/musl-arm32-sysroot}
MUSL_CC=${LIBREECHO_UI_MUSL_CC:-$MUSL_NATIVE_ROOT/usr/bin/armv7-alpine-linux-musleabihf-gcc}
MUSL_NATIVE_LIB=${LIBREECHO_UI_MUSL_NATIVE_LIB:-$MUSL_NATIVE_ROOT/usr/lib}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
VERIFY_TLS=$SCRIPT_DIR/verify_ui_tls.sh
TLS_BINARIES=(libreecho-web libreecho-radiod)
OPUS_LOCK=$SCRIPT_DIR/opus/SOURCE.lock
# The decoder entry points libreecho-radiod actually calls
# (src/adapter/radio_opus.c): op_open_callbacks / op_read_stereo / op_free.
# op_open_file is deliberately *not* required -- the linked daemon never calls
# it -- and none of the HTTP/URL entry points may be present.
OPUS_DECODE_SYMBOLS=(op_open_callbacks op_read_stereo op_free op_channel_count)
OPUS_FORBIDDEN_SYMBOLS='op_open_url|op_vopen_url|op_test_url|op_vtest_url|op_http_open'
# A caller-supplied prebuilt Opus prefix self-certifies: its metadata and the
# archive hashes it is checked against travel together, so a substituted prefix
# can describe itself.  LIBREECHO_UI_OPUS_ROOT is therefore only trusted when
# the caller explicitly declares that Opus was built in this same run (CI does;
# see build_opus.sh).  Without that declaration the bundle builds the pinned
# Opus prefix itself from the locked source archives.
OPUS_PREFIX_TRUSTED=${LIBREECHO_UI_OPUS_PREFIX_TRUSTED:-0}
OPUS_ARCHIVE_DIR=${LIBREECHO_OPUS_ARCHIVES_DIR:-}
OPUS_BUILD_ROOT=
OPUS_TMP=

opus_fail() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup_opus_tmp() {
    [[ -n "$OPUS_TMP" ]] && rm -rf -- "$OPUS_TMP"
    [[ -n "$OPUS_BUILD_ROOT" ]] && rm -rf -- "$OPUS_BUILD_ROOT"
    [[ -z "${UI_HEALTH_TMP:-}" ]] || rm -rf -- "$UI_HEALTH_TMP"
    return 0
}
trap cleanup_opus_tmp EXIT

# Strip-safe Opus capability proof for a linked UI daemon.  It runs on the
# unstripped build output: `strip --strip-unneeded` removes the symbol table
# that proves the decoder was compiled in, so the check must happen before the
# staged copy is stripped.  A build that left LE_RADIOD_ENABLE_OPUS unset keeps
# the honest stub (no op_* symbols) and is refused here instead of shipping a
# daemon that reports the Opus stream unsupported.
verify_radiod_opus_capability() {
    local binary=$1
    [[ -f "$binary" && ! -L "$binary" && -s "$binary" ]] ||
        opus_fail "missing UI Opus daemon: $binary"
    command -v nm >/dev/null 2>&1 || opus_fail "nm is required to verify the Opus decoder link"
    local symbols required
    symbols=$(nm --defined-only "$binary" 2>/dev/null | awk '{print $NF}')
    for required in "${OPUS_DECODE_SYMBOLS[@]}"; do
        grep -qw -- "$required" <<<"$symbols" ||
            opus_fail "libreecho-radiod has no real Opus decoder ($required absent); the stub build (LE_RADIOD_ENABLE_OPUS unset) must not be packaged: $binary"
    done
    if grep -Eqw "$OPUS_FORBIDDEN_SYMBOLS" <<<"$symbols"; then
        opus_fail "libreecho-radiod links an HTTP/URL Opus entry point (must be HTTP/TLS-free): $binary"
    fi
    printf 'ui_opus=real label=radiod opus_symbols=present http_symbols=absent\n'
}

# Fail-closed verification of the pinned static ARM32 Opus prefix.  This is the
# same routine the production build runs, exposed as `--verify-opus-prefix DIR`
# so the packaging contract can be exercised on a prefix alone (no UI checkout
# or toolchain): every path below either proves the exact pinned ARM32 build or
# aborts the bundle.
verify_opus_prefix() {
    local prefix=$1 trusted=${2:-0}
    # A prebuilt prefix proves nothing on its own: `opus-source.json` and the
    # archives it certifies are written by whoever produced the prefix, so a
    # substituted pair passes every hash below.  Only accept one when the caller
    # declares it was built in this run (CI built it with build_opus.sh);
    # otherwise the bundle builds the prefix itself from the locked archives.
    if [[ "$trusted" != 1 ]]; then
        opus_fail "refusing an untrusted prebuilt Opus prefix (its metadata self-certifies): build Opus with ui/build_opus.sh in this run and pass --opus-prefix-trusted, or let this builder build it from the pinned archives"
    fi
    [[ -d "$prefix" && ! -L "$prefix" ]] || opus_fail "Opus prefix is unavailable: $prefix"
    [[ -f "$OPUS_LOCK" && ! -L "$OPUS_LOCK" ]] ||
        opus_fail "Opus source lock is unavailable: $OPUS_LOCK"
    command -v python3 >/dev/null 2>&1 ||
        opus_fail "python3 is required to verify the Opus prefix provenance"
    command -v nm ar file >/dev/null 2>&1 ||
        opus_fail "nm, ar and file are required to verify the Opus prefix"
    local identity=$prefix/opus-identity.json source=$prefix/opus-source.json
    [[ -f "$identity" && ! -L "$identity" ]] ||
        opus_fail "Opus prefix has no identity record (fail closed): $prefix"
    [[ -f "$source" && ! -L "$source" ]] ||
        opus_fail "Opus prefix has no provenance record (fail closed): $prefix"
    local header
    for header in ogg/ogg.h ogg/os_types.h ogg/config_types.h \
        opus/opus.h opus/opus_multistream.h opus/opus_types.h \
        opus/opus_defines.h opus/opusfile.h; do
        [[ -f "$prefix/include/$header" && ! -L "$prefix/include/$header" ]] ||
            opus_fail "Opus prefix is missing include/$header: $prefix"
    done
    local archive members=0
    for archive in libogg.a libopus.a libopusfile.a; do
        [[ -f "$prefix/lib/$archive" && ! -L "$prefix/lib/$archive" ]] ||
            opus_fail "Opus prefix is missing lib/$archive: $prefix"
        if file -b "$prefix/lib/$archive" | grep -qi 'shared object'; then
            opus_fail "Opus archive is a shared object: $prefix/lib/$archive"
        fi
        ar t "$prefix/lib/$archive" 2>/dev/null | grep -q . ||
            opus_fail "Opus archive is empty: $prefix/lib/$archive"
        members=$((members + $(ar t "$prefix/lib/$archive" | wc -l)))
    done
    if find "$prefix/lib" -maxdepth 1 -name '*.so*' -print -quit | grep -q .; then
        opus_fail "Opus prefix contains dynamic libraries: $prefix"
    fi

    # The HTTP/URL surface must be absent and the real decoder surface present.
    # A stub archive carries neither, so requiring the used entry point is what
    # refuses it (build_opus.sh already guarantees this for its own output; this
    # re-checks whatever prefix is handed to the bundle).
    local symbols required
    symbols=$(nm --defined-only "$prefix/lib/libopusfile.a" 2>/dev/null | awk '{print $NF}')
    for required in "${OPUS_DECODE_SYMBOLS[@]}"; do
        grep -qw -- "$required" <<<"$symbols" ||
            opus_fail "libopusfile.a is missing the decoder entry point $required: $prefix"
    done
    if grep -Eqw "$OPUS_FORBIDDEN_SYMBOLS" <<<"$symbols"; then
        opus_fail "libopusfile.a contains HTTP/URL entry points (must be HTTP/TLS-free): $prefix"
    fi

    # Identity/config/source pins (against SOURCE.lock) and the archive hashes
    # (against the prefix's own provenance record, which must match the files
    # that are about to be linked).
    local pins
    pins=$(python3 - "$identity" "$source" "$OPUS_LOCK" "$prefix" <<'PY'
import hashlib
import json
import pathlib
import sys

identity_path, source_path, lock_path, prefix = sys.argv[1:5]
try:
    identity = json.loads(pathlib.Path(identity_path).read_text(encoding="utf-8"))
    source = json.loads(pathlib.Path(source_path).read_text(encoding="utf-8"))
    lock = json.loads(pathlib.Path(lock_path).read_text(encoding="utf-8"))
except Exception as error:  # noqa: BLE001 - any failure is a fail-closed refusal
    sys.exit("ERROR: cannot read Opus prefix metadata: %s" % error)

components = ("libogg", "opus", "opusfile")
archive_files = {"libogg": "libogg.a", "opus": "libopus.a", "opusfile": "libopusfile.a"}

if identity.get("name") != lock.get("name"):
    sys.exit(
        "ERROR: Opus prefix identity name mismatch: expected %r, found %r"
        % (lock.get("name"), identity.get("name"))
    )
if identity.get("config") != lock.get("config"):
    sys.exit(
        "ERROR: Opus prefix config mismatch: expected %r, found %r"
        % (lock.get("config"), identity.get("config"))
    )
target = identity.get("target")
lock_target = lock.get("target") or ""
# The production target is pinned in SOURCE.lock; the prefix must share its
# architecture.  Exact-triple equality is not required because the ARM32
# release toolchain may be musl or glibc (arm-linux-musleabihf vs
# arm-linux-gnueabihf), and a host prefix must never be accepted.
want_arch = lock_target.split("-", 1)[0]
if not target or not want_arch or target.split("-", 1)[0] != want_arch:
    sys.exit(
        "ERROR: Opus prefix target mismatch: expected an %s target (SOURCE.lock target %r), found %r"
        % (want_arch, lock_target, target)
    )
if source.get("target") != target:
    sys.exit(
        "ERROR: Opus prefix provenance target mismatch: identity %r, provenance %r"
        % (target, source.get("target"))
    )
if source.get("http_enabled") is not False:
    sys.exit("ERROR: Opus prefix provenance does not record an HTTP-free build")
identity_archives = identity.get("archives")
source_components = source.get("components")
if not isinstance(identity_archives, dict) or not isinstance(source_components, dict):
    sys.exit("ERROR: Opus prefix metadata is malformed")
for name in components:
    pinned = lock["components"][name]["source_sha256"]
    if identity_archives.get(name) != pinned:
        sys.exit(
            "ERROR: Opus prefix source pin mismatch: archives.%s: expected %s, found %s"
            % (name, pinned, identity_archives.get(name))
        )
    record = source_components.get(name) or {}
    if record.get("source_archive_sha256") != pinned:
        sys.exit("ERROR: Opus provenance source pin mismatch for %s" % name)
    if record.get("license_sha256") != lock["components"][name]["license_sha256"]:
        sys.exit("ERROR: Opus provenance license pin mismatch for %s" % name)
for name in components:
    archive = pathlib.Path(prefix) / "lib" / archive_files[name]
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if (source.get("artifacts") or {}).get(name) != actual:
        sys.exit(
            "ERROR: Opus prefix archive %s does not match its provenance record"
            % archive_files[name]
        )
print("%s %s %s" % (target, identity.get("config"), identity.get("name")))
PY
) || exit 1

    # Every archive member must be a 32-bit ARM ELF relocatable object.
    OPUS_TMP=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-opus-prefix.XXXXXX")
    local member_dir=$OPUS_TMP/members
    mkdir -p "$member_dir"
    for archive in libogg.a libopus.a libopusfile.a; do
        rm -rf -- "$member_dir"/*
        ( cd "$member_dir" && ar x "$prefix/lib/$archive" )
        local member desc
        while IFS= read -r -d '' member; do
            desc=$(file -b "$member")
            case "$desc" in
                ELF*32-bit*ARM*) ;;
                *) opus_fail "non-ARM32 member in $archive: $(basename "$member"): $desc" ;;
            esac
        done < <(find "$member_dir" -type f -print0)
    done

    printf 'ui_opus_prefix=ok target=%s config=%s name=%s archive_members=%s\n' \
        $pins "$members"
}

# Test/CI seam: verify a prefix on its own, without a UI checkout or toolchain.
if [[ "${1:-}" == "--verify-opus-prefix" ]]; then
    shift
    trusted=$OPUS_PREFIX_TRUSTED
    prefix=
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --opus-prefix-trusted) trusted=1; shift ;;
            --no-opus-prefix-trusted) trusted=0; shift ;;
            -*) printf 'ERROR: unknown --verify-opus-prefix option: %s\n' "$1" >&2; exit 2 ;;
            *) [[ -z "$prefix" ]] ||
                   { printf 'ERROR: --verify-opus-prefix takes exactly one directory\n' >&2; exit 2; }
               prefix=$1; shift ;;
        esac
    done
    [[ -n "$prefix" ]] ||
        { printf 'ERROR: --verify-opus-prefix requires a prefix directory\n' >&2; exit 2; }
    verify_opus_prefix "$prefix" "$trusted"
    exit 0
fi

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
"$VERIFY_TLS" --noise-prefix "$MBEDTLS_ROOT"

# Opus is a shipped production capability: libreecho-radiod decodes Ogg Opus for
# the radio feature.  Without the pinned ARM32 Opus prefix the UI Makefile keeps
# the honest stub (LE_RADIOD_ENABLE_OPUS unset), so radiod reports the stream
# unsupported.  A stub bundle is not a production artifact and this builder
# fails closed instead of producing one.
if [[ "$OPUS_PREFIX_TRUSTED" == 1 ]]; then
    OPUS_ROOT=${LIBREECHO_UI_OPUS_ROOT:-}
    [[ -n "$OPUS_ROOT" ]] || {
        echo "ERROR: LIBREECHO_UI_OPUS_ROOT must name the pinned ARM32 Opus prefix when LIBREECHO_UI_OPUS_PREFIX_TRUSTED=1" >&2
        exit 1
    }
    verify_opus_prefix "$OPUS_ROOT" 1
else
    # No trusted prebuilt prefix: build the pinned Opus prefix from the locked
    # source archives here, so the archives the bundle links are produced in
    # this run rather than trusted from a caller-supplied prefix.
    [[ -n "$OPUS_ARCHIVE_DIR" && -d "$OPUS_ARCHIVE_DIR" ]] || {
        echo "ERROR: no trusted Opus prefix: set LIBREECHO_UI_OPUS_PREFIX_TRUSTED=1 with LIBREECHO_UI_OPUS_ROOT, or LIBREECHO_OPUS_ARCHIVES_DIR to the locked source archives so Opus can be built here" >&2
        exit 1
    }
    for archive in libogg-1.3.5.tar.gz opus-1.4.tar.gz opusfile-0.12.tar.gz; do
        [[ -f "$OPUS_ARCHIVE_DIR/$archive" ]] || {
            echo "ERROR: missing locked Opus source archive: $OPUS_ARCHIVE_DIR/$archive" >&2
            exit 1
        }
    done
    OPUS_BUILD_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-opus-build.XXXXXX")
    "$SCRIPT_DIR/build_opus.sh" \
        --ogg-archive "$OPUS_ARCHIVE_DIR/libogg-1.3.5.tar.gz" \
        --opus-archive "$OPUS_ARCHIVE_DIR/opus-1.4.tar.gz" \
        --opusfile-archive "$OPUS_ARCHIVE_DIR/opusfile-0.12.tar.gz" \
        --output "$OPUS_BUILD_ROOT/prefix" \
        --cc "${LIBREECHO_OPUS_CC:-arm-linux-gnueabihf-gcc}" \
        --ar "${LIBREECHO_OPUS_AR:-arm-linux-gnueabihf-ar}" \
        --host "${LIBREECHO_OPUS_HOST:-arm-linux-gnueabihf}"
    OPUS_ROOT=$OPUS_BUILD_ROOT/prefix
fi
OPUS_ROOT=$(cd -- "$OPUS_ROOT" && pwd)
# Absolute archives in dependency order (libopusfile depends on libopus, which
# depends on libogg), derived from the verified prefix rather than a
# caller-supplied library list.
OPUS_LIBS="$OPUS_ROOT/lib/libopusfile.a $OPUS_ROOT/lib/libopus.a $OPUS_ROOT/lib/libogg.a"

# Bind the linkage to the prefix that was just verified.  The archives are named
# by absolute path rather than through a library search path, and the list is not
# caller-supplied, so no other API-compatible mbedTLS can be linked while the
# recorded provenance describes the pinned one.
MBEDTLS_ROOT=$(cd -- "$MBEDTLS_ROOT" && pwd)
TLS_LIBS="$MBEDTLS_ROOT/lib/libmbedtls.a $MBEDTLS_ROOT/lib/libmbedx509.a $MBEDTLS_ROOT/lib/libmbedcrypto.a"
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
# V3 control-plane health belongs to Platform. Build the companion against a
# private snapshot, so the supplied checkout and its source identity stay intact.
ui_input_source=$UI_SOURCE
UI_HEALTH_TMP=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-ui-health.XXXXXX")
python3 "$SCRIPT_DIR/ota_v3_health.py" --source "$UI_SOURCE" --output "$UI_HEALTH_TMP/source"
UI_SOURCE=$UI_HEALTH_TMP/source
ui_ota_adapter_sha256=$(sha256sum "$SCRIPT_DIR/ota_v3_health.py" "$SCRIPT_DIR/ota_v3_health.h" | awk '{print $1}' | sha256sum | awk '{print $1}')

# The UI Makefile appends its own definitions to CPPFLAGS, so the mbedTLS and
# Opus include paths are exported (a command-line CPPFLAGS would suppress them).
# opusfile.h includes <opus_multistream.h> and <ogg/ogg.h>, hence both the
# include/opus and include roots.  The library search path must travel in
# GC_LDFLAGS because `release` replaces LDFLAGS with GC_LDFLAGS for its
# recursive build.
export CPPFLAGS="-I$OPUS_ROOT/include/opus -I$OPUS_ROOT/include -I$MBEDTLS_ROOT/include${CPPFLAGS:+ $CPPFLAGS}"
"$MAKE_BIN" -C "$UI_SOURCE" clean
"$MAKE_BIN" -C "$UI_SOURCE" \
    CROSS_COMPILE="$CROSS_COMPILE" CC="$CC_BIN" \
    GC_LDFLAGS="$GC_LDFLAGS -L$MBEDTLS_ROOT/lib" \
    WEB_TLS_LIBS="$TLS_LIBS" RADIOD_TLS_LIBS="$TLS_LIBS" \
    RADIOD_OPUS_PREFIX="$OPUS_ROOT" RADIOD_OPUS_LIBS="$OPUS_LIBS" \
    ESPHOMED_NOISE=1 ESPHOMED_TLS_LIBS="$TLS_LIBS" \
    release

# A build that selected src/tls_stub.c (LE_TLS_AVAILABLE=0) or that failed to
# link mbedTLS must fail here, before anything is staged for the image.
for binary in "${TLS_BINARIES[@]}"; do
    "$VERIFY_TLS" --binary "$UI_SOURCE/build/$binary" \
        --objects "$UI_SOURCE/build" --label "$binary"
done

# Prohibit the stub production path: the unstripped libreecho-radiod must carry
# the real Opus decoder before it is staged.  This runs here, not after the
# strip loop below, because stripping removes the symbols that prove it.
verify_radiod_opus_capability "$UI_SOURCE/build/libreecho-radiod"

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
    libreecho-timerd libreecho-watchdogd \
    libreecho-audiod libreecho-micd libreecho-ledd libreecho-buttond \
    libreecho-radiod libreecho-btd \
    libreecho-airplayd libreecho-esphomed libreecho-mdnsd
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
    libreecho-timerd libreecho-watchdogd \
    libreecho-audiod libreecho-micd libreecho-ledd libreecho-buttond \
    libreecho-radiod libreecho-btd \
    libreecho-airplayd libreecho-esphomed \
    libreecho-sttd-wyoming libreecho-ttsd-wyoming libreecho-mdnsd
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
    libreecho-timerd.init libreecho-watchdogd.init \
    libreecho-audiod.init libreecho-micd.init libreecho-ledd.init \
    libreecho-buttond.init libreecho-radiod.init libreecho-btd.init \
    libreecho-airplayd.init libreecho-ttsd.init \
    libreecho-waked.init libreecho-sttd.init libreecho-agentd.init \
    libreecho-esphomed.init
do
    install -m 0755 "$UI_SOURCE/init/$script" "$OUTPUT/etc/init.d/$script"
done

# Guard the actual packaged scripts, not only the adapted source snapshot.
python3 "$SCRIPT_DIR/ota_v3_health.py" --verify-init "$OUTPUT/etc/init.d"

cp -R "$UI_SOURCE/web/." "$OUTPUT/share/libreecho/web/"
install -m 0600 "$UI_SOURCE/config/defaults.json" \
    "$OUTPUT/etc/libreecho/web-config.json"
install -m 0644 "$UI_SOURCE/config/airplay2.conf" \
    "$OUTPUT/etc/libreecho/airplay2.conf"
install -m 0644 "$UI_SOURCE/config/ntp.conf" \
    "$OUTPUT/etc/libreecho/ntp.conf"
esphome_service="$UI_SOURCE/config/esphome.service"
[[ -f "$esphome_service" && ! -L "$esphome_service" && -s "$esphome_service" ]] || {
    echo "ERROR: missing or empty ESPHome service definition: $esphome_service" >&2
    exit 1
}
grep -Fq '<type>_esphomelib._tcp</type>' "$esphome_service" || {
    echo "ERROR: ESPHome service definition has no _esphomelib._tcp entry" >&2
    exit 1
}
grep -Fq '<port>6053</port>' "$esphome_service" || {
    echo "ERROR: ESPHome service definition has no port 6053" >&2
    exit 1
}
python3 - "$esphome_service" <<'PY'
import sys
import xml.etree.ElementTree as ET
service = ET.parse(sys.argv[1]).getroot()
records = service.findall("service")
if len(records) != 1 or records[0].findtext("type") != "_esphomelib._tcp" or records[0].findtext("port") != "6053":
    raise SystemExit("ERROR: ESPHome service definition has invalid type/port")
txt = dict(item.text.split("=", 1) for item in records[0].findall("txt-record") if item.text and "=" in item.text)
if not all(txt.get(key) for key in ("version", "board", "platform")):
    raise SystemExit("ERROR: ESPHome service definition has no TXT identity")
# Reference schema only, outside the runtime Avahi services directory. Device
# identity is supplied by esphomed's readiness-bound lease, never this template.
if (txt.get("mac") not in (None, "@MAC@") or
        any(key in txt for key in ("key", "noise_psk", "api_key"))):
    raise SystemExit("ERROR: ESPHome service definition embeds private identity")
PY
install -m 0644 "$esphome_service" \
    "$OUTPUT/etc/libreecho/avahi-services/esphome.service"
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
    printf 'ui_ota_adapter_sha256=%s\n' "$ui_ota_adapter_sha256"
    printf 'source_diff_sha256=%s\n' "$ui_diff_sha256"
    while IFS= read -r relative; do
        hash=$(sha256sum "$OUTPUT/$relative" | awk '{print $1}')
        printf 'file=%s sha256=%s\n' "$relative" "$hash"
    done < <(find "$OUTPUT" -type f ! -name ui-manifest.txt -printf '%P\n' | LC_ALL=C sort)
} > "$OUTPUT/share/libreecho/ui-manifest.txt"

ui_manifest_sha256=$(sha256sum "$OUTPUT/share/libreecho/ui-manifest.txt" | awk '{print $1}')
# Record the Opus identity (name/target/config and the archive list) rather than
# the machine-specific prefix path, so the public manifest carries reproducible
# source metadata and no private absolute path.
opus_meta=$(python3 - "$OPUS_ROOT/opus-identity.json" <<'PY'
import json
import sys

record = json.load(open(sys.argv[1], encoding="utf-8"))
print("%s %s %s" % (record.get("name", ""), record.get("target", ""), record.get("config", "")))
PY
)
# The compile ran in a private snapshot that the EXIT trap deletes. Callers
# (Product build.sh) snapshot relink objects from "$UI_SOURCE/build" of the
# checkout they passed in, so publish the final build tree back there. Only the
# build output is copied; the caller's sources and source identity are untouched.
rm -rf -- "$ui_input_source/build"
cp -a -- "$UI_SOURCE/build" "$ui_input_source/build"
printf 'ui_source=%s\nui_commit=%s\nui_diff_sha256=%s\nui_manifest_sha256=%s\nui_tls=real\nui_tls_libs=%s\nui_mbedtls_root=%s\nui_opus=real\nui_opus_name=%s\nui_opus_target=%s\nui_opus_config=%s\nui_opus_libs=libopusfile.a libopus.a libogg.a\n' \
    "$ui_input_source" "$ui_commit" "$ui_diff_sha256" "$ui_manifest_sha256" \
    "$TLS_LIBS" "$MBEDTLS_ROOT" $opus_meta
