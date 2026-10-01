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
# The build receipt --build writes and --emit-metadata requires.  It binds a
# standalone metadata emission to the SOURCE.lock in force and to the exact
# artefacts the verified build produced (plus the toolchain that compiled them).
RECEIPT_NAME=recovery-ap-build-receipt.json
RECEIPT_SCHEMA=libreecho-recovery-ap-build-receipt/v1

usage() {
    printf '%s\n' \
        'usage: build_recovery_ap.sh --verify [--archive NAME=FILE]... [--cache DIR]' \
        '                               [--libnl-archive FILE] [--libnl-cache DIR]' \
        '       build_recovery_ap.sh --build  [--archive NAME=FILE]... [--cache DIR]' \
        '                                 --libnl-archive FILE' \
        '                                 --output DIR --cc FILE --ar FILE --ranlib FILE' \
        '                                 [--sysroot DIR] [--strip FILE]' \
        '       build_recovery_ap.sh --emit-metadata --output DIR'
        ''
        'emit-metadata refuses to run unless DIR holds the build receipt the'
        'verified --build path wrote there for the same SOURCE.lock and binaries.'
}

MODE=
OUTPUT=
CC=
AR=
RANLIB=
SYSROOT=
STRIP=
CACHE=
LIBNL_ARCHIVE=
LIBNL_CACHE=
declare -A ARCHIVE_ARG

while (($#)); do
    case "$1" in
        --verify) MODE=verify ;;
        --build) MODE=build ;;
        --emit-metadata) MODE=emit-metadata ;;
        --lock) shift; (($#)) || { usage >&2; exit 2; }; LOCK=$1 ;;
        --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
        --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
        --ar) shift; (($#)) || { usage >&2; exit 2; }; AR=$1 ;;
        --ranlib) shift; (($#)) || { usage >&2; exit 2; }; RANLIB=$1 ;;
        --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
        --strip) shift; (($#)) || { usage >&2; exit 2; }; STRIP=$1 ;;
        --cache) shift; (($#)) || { usage >&2; exit 2; }; CACHE=$1 ;;
        --libnl-archive) shift; (($#)) || { usage >&2; exit 2; }; LIBNL_ARCHIVE=$1 ;;
        --libnl-cache) shift; (($#)) || { usage >&2; exit 2; }; LIBNL_CACHE=$1 ;;
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
        component["license"],
        component["source_license"],
        component.get("source_license_secondary", ""),
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

# The pinned source archives are only needed to verify or build.  Emitting the
# image metadata from already-built artifacts reads only SOURCE.lock and the
# output directory, so it never requires the archives.
if [[ "$MODE" != emit-metadata ]]; then
for line in "${COMPONENTS[@]}"; do
    IFS=$'\t' read -r name version sha url artifact license source_license source_license_secondary <<<"$line"
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

# libnl is a verified build input (never staged into the image): hostapd's
# netlink driver (CONFIG_LIBNL32) and iw both need it.  It is resolved from
# --libnl-archive or --libnl-cache and hash-checked against the pin.
LIBNL_RECORD=$(python3 - "$LOCK" <<'PY'
import json, sys
record = json.load(open(sys.argv[1])).get("build_dependencies", {}).get("libnl")
print("\t".join([record["version"], record["source_sha256"], record["source_url"]]) if record else "")
PY
)
[[ -n "$LIBNL_RECORD" ]] || { printf 'ERROR: SOURCE.lock declares no libnl build dependency\n' >&2; exit 1; }
IFS=$'\t' read -r LIBNL_VERSION LIBNL_SHA LIBNL_URL <<<"$LIBNL_RECORD"
case "$LIBNL_SHA" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
    *) printf 'ERROR: libnl has a malformed pinned SHA-256\n' >&2; exit 1 ;;
esac
[[ "${#LIBNL_SHA}" -eq 64 ]] || { printf 'ERROR: libnl pinned SHA-256 is not 64 hex chars\n' >&2; exit 1; }
if [[ -z "$LIBNL_ARCHIVE" && -n "$LIBNL_CACHE" ]]; then
    for candidate in "$LIBNL_CACHE/libnl-$LIBNL_VERSION".tar.*; do
        [[ -e "$candidate" ]] && { LIBNL_ARCHIVE=$candidate; break; }
    done
fi
if [[ -n "$LIBNL_ARCHIVE" ]]; then
    [[ -f "$LIBNL_ARCHIVE" && ! -L "$LIBNL_ARCHIVE" ]] || {
        printf 'ERROR: unsafe or missing libnl archive: %s\n' "$LIBNL_ARCHIVE" >&2; exit 1; }
    actual=$(sha256sum "$LIBNL_ARCHIVE" | awk '{print $1}')
    if [[ "$actual" != "$LIBNL_SHA" ]]; then
        printf 'ERROR: libnl SHA-256 mismatch\nexpected=%s\nactual=%s\n' "$LIBNL_SHA" "$actual" >&2
        exit 1
    fi
    printf 'verified libnl %s %s\n' "$LIBNL_VERSION" "$LIBNL_SHA"
fi
fi

# --- GPL corresponding-source offer ---------------------------------------
# The recovery image redistributes compiled binaries, so every component under
# a GPL family licence must be covered by an explicit corresponding-source
# offer in SOURCE.lock.  The pinned upstream archive is that source; this check
# refuses to proceed when a GPL component is pinned without one, so a GPL
# binary can never ship without its source obligation recorded.
SOURCE_OFFER_COMPONENTS=$(python3 - "$LOCK" <<'PY'
import json, sys
offer = json.load(open(sys.argv[1])).get("source_offer") or {}
print("\t".join(sorted(offer.get("components", []))))
PY
)
declare -A OFFERED_COMPONENT=()
if [[ -n "$SOURCE_OFFER_COMPONENTS" ]]; then
    while IFS= read -r offered; do
        [[ -n "$offered" ]] && OFFERED_COMPONENT[$offered]=1
    done < <(printf '%s\n' "$SOURCE_OFFER_COMPONENTS" | tr '\t' '\n')
fi
for line in "${COMPONENTS[@]}"; do
    IFS=$'\t' read -r name version sha url artifact license source_license source_license_secondary <<<"$line"
    case "$license" in
        *GPL*)
            [[ -n "${OFFERED_COMPONENT[$name]:-}" ]] || {
                printf 'ERROR: GPL component %s declares no corresponding-source offer\n' "$name" >&2
                exit 1; }
            printf 'source-offer %s %s %s\n' "$name" "$license" "$url"
            ;;
    esac
done

# --- image metadata emission ----------------------------------------------
# build_recovery_image.py --recovery-ap-metadata consumes exactly this document:
# the per-component artifact SHA-256/size the image stages, plus the licence and
# corresponding-source provenance that ships on-image with them.  It is emitted
# from the real built artifacts (never hand-written), so the image input is bound
# to the bytes this builder produced.
emit_metadata() {
    local output=$1
    [[ -d "$output" ]] || {
        printf 'ERROR: metadata output directory is unavailable: %s\n' "$output" >&2; exit 1; }
    python3 - "$LOCK" "$output" "$RECEIPT_NAME" "$RECEIPT_SCHEMA" <<'PY'
import hashlib
import json
import os
import sys

lock_path = sys.argv[1]
output = sys.argv[2]
receipt_name = sys.argv[3]
receipt_schema = sys.argv[4]
lock_data = open(lock_path, "rb").read()
lock = json.loads(lock_data.decode("utf-8"))
image_paths = {
    "hostapd": "usr/local/sbin/hostapd",
    "dnsmasq": "usr/local/sbin/dnsmasq",
    "iw": "usr/local/sbin/iw",
    "libreecho-recovery-button": "usr/local/sbin/libreecho-recovery-button",
}


def digest(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return hashlib.sha256(data).hexdigest(), len(data)


# A standalone emission is only trustworthy when it re-publishes an output the
# verified --build path produced: the build receipt binds the artefacts to this
# SOURCE.lock and to the toolchain that compiled them.  Without it, an arbitrary
# directory of ELF files could be labelled as the pinned set.
receipt_path = os.path.join(output, receipt_name)
if not os.path.isfile(receipt_path) or os.path.islink(receipt_path):
    raise SystemExit(
        "ERROR: --emit-metadata requires the build receipt %s written by --build"
        % receipt_name)
with open(receipt_path, encoding="utf-8") as handle:
    receipt = json.load(handle)
if not isinstance(receipt, dict) or receipt.get("schema") != receipt_schema:
    raise SystemExit("ERROR: build receipt schema is not the pinned contract")
if receipt.get("lock_sha256") != hashlib.sha256(lock_data).hexdigest():
    raise SystemExit("ERROR: build receipt is not bound to this SOURCE.lock")
toolchain = receipt.get("toolchain")
if (not isinstance(toolchain, dict) or
        not all(isinstance(toolchain.get(key), str) and toolchain[key].strip()
                for key in ("cc", "ar", "ranlib"))):
    raise SystemExit("ERROR: build receipt carries no toolchain identity")
receipt_binaries = receipt.get("binaries")
if not isinstance(receipt_binaries, dict) or not receipt_binaries:
    raise SystemExit("ERROR: build receipt carries no binary identities")


components = {}
binaries = {}
for name, record in lock["components"].items():
    artifact = record["artifact"]
    path = os.path.join(output, artifact)
    if not os.path.isfile(path) or os.path.islink(path):
        raise SystemExit("ERROR: built artifact is missing or not a regular file: %s" % path)
    sha256, size = digest(path)
    provenance = {
        "version": record["version"],
        "license": record["license"],
        "source_url": record["source_url"],
        "source_sha256": record["source_sha256"],
        "source_license": record["source_license"],
        "artifact": artifact,
        "image_path": image_paths[name],
    }
    if record.get("source_license_secondary"):
        provenance["source_license_secondary"] = record["source_license_secondary"]
    image_license = lock.get("on_image", {}).get("licenses", {}).get(name)
    if image_license:
        provenance["image_license"] = image_license
    components[name] = provenance
    binaries[name] = {"sha256": sha256, "size": size}
for name, record in lock.get("first_party", {}).items():
    artifact = record["artifact"]
    path = os.path.join(output, artifact)
    if not os.path.isfile(path) or os.path.islink(path):
        raise SystemExit("ERROR: built artifact is missing or not a regular file: %s" % path)
    sha256, size = digest(path)
    components[name] = {
        "license": record["license"],
        "source_url": record["source_url"],
        "source_path": record["source_path"],
        "artifact": artifact,
        "image_path": image_paths[name],
    }
    binaries[name] = {"sha256": sha256, "size": size}
missing = sorted(set(image_paths) - set(binaries))
if missing:
    raise SystemExit(
        "ERROR: SOURCE.lock does not describe the pinned set: missing %s" % ", ".join(missing))
# Refuse when any artefact no longer matches the receipt the verified build wrote:
# a binary swapped in after the build must not be re-labelled as the pinned set.
if set(receipt_binaries) != set(binaries):
    raise SystemExit("ERROR: build receipt binary set does not match the pinned set")
for name, record in binaries.items():
    receipt_record = receipt_binaries.get(name)
    if (not isinstance(receipt_record, dict) or
            receipt_record.get("sha256") != record["sha256"] or
            receipt_record.get("size") != record["size"]):
        raise SystemExit(
            "ERROR: artefact %s no longer matches the build receipt" % name)
document = {
    "schema": "libreecho-recovery-ap-binaries/v1",
    "builder": "tools/mt8163-arm32/recovery-ap/build_recovery_ap.sh",
    "components": components,
    "binaries": binaries,
    "source_offer": lock.get("source_offer", {}),
}
target = os.path.join(output, "recovery-ap-binaries.json")
with open(target, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
print("metadata=%s" % target)
PY
}

# Record what the verified --build path actually produced, so a later
# --emit-metadata can prove it is re-publishing this run's artefacts rather than
# labelling an arbitrary directory.  The receipt is never shipped in an image; it
# only binds the metadata emission to SOURCE.lock and the toolchain that compiled
# the binaries.  Authenticity of a locally supplied build still rests on whoever
# ran the trusted builder/CI, not on this receipt.
write_build_receipt() {
    local output=$1
    python3 - "$LOCK" "$output" "$RECEIPT_NAME" "$RECEIPT_SCHEMA" \
        "$CC" "$AR" "$RANLIB" "${STRIP:-}" <<'PY'
import hashlib
import json
import os
import subprocess
import sys

lock_path, output, receipt_name, schema = sys.argv[1:5]
cc, ar, ranlib, strip = sys.argv[5:9]
lock_data = open(lock_path, "rb").read()
lock = json.loads(lock_data.decode("utf-8"))


def digest(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return hashlib.sha256(data).hexdigest(), len(data)


def identity(tool):
    try:
        result = subprocess.run([tool, "--version"], capture_output=True, text=True)
    except OSError:
        return tool
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.splitlines()[0].strip()
    return tool


binaries = {}
for name, record in lock["components"].items():
    sha256, size = digest(os.path.join(output, record["artifact"]))
    binaries[name] = {"sha256": sha256, "size": size}
for name, record in lock.get("first_party", {}).items():
    sha256, size = digest(os.path.join(output, record["artifact"]))
    binaries[name] = {"sha256": sha256, "size": size}
receipt = {
    "schema": schema,
    "lock_path": os.path.abspath(lock_path),
    "lock_sha256": hashlib.sha256(lock_data).hexdigest(),
    "toolchain": {
        "cc": cc,
        "ar": ar,
        "ranlib": ranlib,
        "strip": strip,
        "cc_identity": identity(cc),
    },
    "binaries": binaries,
}
target = os.path.join(output, receipt_name)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(receipt, handle, indent=2, sort_keys=True)
    handle.write("\n")
print("build_receipt=%s" % target)
PY
}

if [[ "$MODE" == emit-metadata ]]; then
    [[ -n "$OUTPUT" ]] || { usage >&2; exit 2; }
    emit_metadata "$OUTPUT"
    exit 0
fi

if [[ "$MODE" == verify ]]; then
    exit 0
fi

# --- build mode ---
[[ -n "$OUTPUT" && -n "$CC" && -n "$AR" && -n "$RANLIB" ]] || { usage >&2; exit 2; }
[[ -n "$LIBNL_ARCHIVE" ]] || { printf 'ERROR: --libnl-archive is required to build hostapd and iw\n' >&2; exit 2; }
for tool in "$CC" "$AR" "$RANLIB"; do
    [[ -x "$tool" || -x "$(command -v "$tool" 2>/dev/null || true)" ]] || {
        printf 'ERROR: required toolchain component is unavailable: %s\n' "$tool" >&2; exit 1; }
done
# libnl 3.11's release tarball regenerates its yacc/lex parsers, so bison and
# flex are build-host tools the caller must provide (with a working m4 reachable
# through the M4 environment variable when the toolchain lives outside its root).
for tool in make tar pkg-config bison flex; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'ERROR: %s is required\n' "$tool" >&2; exit 1; }
done

mkdir -p "$OUTPUT"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# --- pinned libnl (static prefix shared by hostapd and iw) ---
libnl_src="$work/libnl-src"
libnl_prefix="$work/libnl-prefix"
mkdir -p "$libnl_src"
tar -xf "$LIBNL_ARCHIVE" -C "$libnl_src" --strip-components=1
(
    cd "$libnl_src"
    CC="$CC" AR="$AR" RANLIB="$RANLIB" \
        ./configure --host="$("$CC" -dumpmachine)" --prefix="$libnl_prefix" \
            --enable-static --disable-shared --disable-cli >/dev/null
    make -j"$(nproc)" >/dev/null
    make install >/dev/null
)
[[ -f "$libnl_prefix/lib/libnl-3.a" && -f "$libnl_prefix/lib/libnl-genl-3.a" ]] || {
    printf 'ERROR: pinned libnl did not produce the static libraries\n' >&2; exit 1; }
LIBNL_INCLUDE="$libnl_prefix/include"
LIBNL_LIB="$libnl_prefix/lib"
export PKG_CONFIG_PATH="$libnl_prefix/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

for line in "${COMPONENTS[@]}"; do
    IFS=$'\t' read -r name version sha url artifact license source_license source_license_secondary <<<"$line"
    archive=${RESOLVED[$name]}
    src="$work/$name"
    mkdir -p "$src"
    tar -xf "$archive" -C "$src" --strip-components=1
    # Licence provenance: every declared licence text must actually be present
    # in the verified upstream source tree, so a component can never be pinned
    # against a tree that hides or omits its licence.
    for licence in "$source_license" "${source_license_secondary:-}"; do
        [[ -n "$licence" ]] || continue
        [[ -f "$src/$licence" ]] || {
            printf 'ERROR: %s: declared licence %s is missing from the source tree\n' \
                "$name" "$licence" >&2
            exit 1; }
    done
    case "$name" in
        hostapd)
            # WPA-PSK AP with internal crypto/TLS: no OpenSSL (or any external
            # crypto library) is required, so the only external dependency is
            # the pinned static libnl wired in through CFLAGS/LDFLAGS.
            {
                # Drop the EC/TLS-heavy provisioning features (DPP/SAE/OWE/FILS)
                # that the internal crypto backend does not implement; a WPA2-PSK
                # captive AP does not need them and they would fail to link.
                grep -vE '^CONFIG_(DPP|SAE|OWE|FILS|PASN)' "$src/hostapd/defconfig"
                printf '%s\n' \
                    'CONFIG_DRIVER_NL80211=y' \
                    'CONFIG_LIBNL32=y' \
                    'CONFIG_IEEE80211N=y' \
                    'CONFIG_CRYPTO=internal' \
                    'CONFIG_TLS=internal' \
                    'CONFIG_INTERNAL_LIBTOMMATH=y'
            } > "$src/hostapd/.config"
            # Defensive: the internal crypto/TLS backend has no EC point API,
            # so any surviving DPP/SAE/OWE/FILS/PASN symbol would leave hostapd
            # with unresolved crypto_ec_* references at link time.  Fail closed
            # here rather than emitting a broken binary.
            if grep -qE '^CONFIG_(DPP|SAE|OWE|FILS|PASN)' "$src/hostapd/.config"; then
                printf 'ERROR: hostapd config still enables an unsupported provisioning feature\n' >&2
                exit 1
            fi
            make -C "$src/hostapd" -j"$(nproc)" \
                CC="$CC" AR="$AR" RANLIB="$RANLIB" \
                EXTRA_CFLAGS="-I$LIBNL_INCLUDE" \
                LDFLAGS="${LDFLAGS:--static} -L$LIBNL_LIB"
            install -m 0755 "$src/hostapd/hostapd" "$OUTPUT/hostapd"
            ;;
        dnsmasq)
            make -C "$src" -j"$(nproc)" CC="$CC" AR="$AR" \
                LDFLAGS="${LDFLAGS:--static}" COPTS="${COPTS:-}"
            install -m 0755 "$src/src/dnsmasq" "$OUTPUT/dnsmasq"
            ;;
        iw)
            # iw appends the libnl pkg-config cflags/libs with ``override``, so a
            # command-line CFLAGS cannot clobber its netlink wiring; PKG_CONFIG_PATH
            # (set above to the pinned libnl prefix) supplies them.
            make -C "$src" -j"$(nproc)" CC="$CC" AR="$AR" \
                CFLAGS="-I$LIBNL_INCLUDE" \
                LDFLAGS="${LDFLAGS:--static} -L$LIBNL_LIB"
            install -m 0755 "$src/iw" "$OUTPUT/iw"
            ;;
        *) printf 'ERROR: no build recipe for %s\n' "$name" >&2; exit 1 ;;
    esac
    [[ -s "$OUTPUT/$artifact" ]] || { printf 'ERROR: %s did not produce %s\n' "$name" "$artifact" >&2; exit 1; }
    if [[ -n "$STRIP" ]]; then "$STRIP" "$OUTPUT/$artifact"; fi
    sha256sum "$OUTPUT/$artifact"
done

# --- first-party physical boot detector (compiled, not a shell overlay file) ---
detector="$SCRIPT_DIR/libreecho-recovery-button.c"
[[ -f "$detector" && ! -L "$detector" ]] || {
    printf 'ERROR: detector source unavailable: %s\n' "$detector" >&2; exit 1; }
"$CC" -static -O2 -Wall -Wextra -o "$OUTPUT/libreecho-recovery-button" "$detector"
[[ -s "$OUTPUT/libreecho-recovery-button" ]] || {
    printf 'ERROR: detector build produced no output\n' >&2; exit 1; }
if [[ -n "$STRIP" ]]; then "$STRIP" "$OUTPUT/libreecho-recovery-button"; fi
sha256sum "$OUTPUT/libreecho-recovery-button"

# Emit the verified image input alongside the artifacts, so the build output and
# the metadata build_recovery_image.py consumes can never drift apart.  The build
# receipt is written first: --emit-metadata refuses to run without it.
write_build_receipt "$OUTPUT"
emit_metadata "$OUTPUT"
