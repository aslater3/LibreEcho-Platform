#!/usr/bin/env bash
# Fail-closed verification of the LibreEcho UI HTTPS/TLS packaging contract.
#
# Two checks are supported and both are executed by the production bundle
# builder:
#   --prefix DIR   the pinned ARM32 mbedTLS prefix that is linked into the UI
#   --binary FILE  a produced or packaged libreecho-web / libreecho-radiod
#
# The verifier never downloads anything and never accepts a dynamic or
# non-ARM32 artifact: a build that silently falls back to src/tls_stub.c (or
# that reports LE_TLS_AVAILABLE=0) must fail here instead of shipping a Web UI
# whose HTTPS toggle can never listen on 8443.
set -euo pipefail

usage() {
  printf '%s\n' 'usage: verify_ui_tls.sh --prefix DIR | --binary FILE [--objects DIR] [--label NAME]'
}

PREFIX=
BINARY=
OBJECTS=
LABEL=
while (($#)); do
  case "$1" in
    --prefix) shift; (($#)) || { usage >&2; exit 2; }; PREFIX=$1 ;;
    --binary) shift; (($#)) || { usage >&2; exit 2; }; BINARY=$1 ;;
    --objects) shift; (($#)) || { usage >&2; exit 2; }; OBJECTS=$1 ;;
    --label) shift; (($#)) || { usage >&2; exit 2; }; LABEL=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$PREFIX" || -n "$BINARY" ]] || { usage >&2; exit 2; }
[[ -z "$PREFIX" || -z "$BINARY" ]] || {
  printf 'ERROR: --prefix and --binary are separate checks\n' >&2; exit 2
}

# src/tls.c only string literals.  src/tls_stub.c has none of them, so their
# presence in a packaged binary proves the real implementation was compiled in
# and LE_TLS_AVAILABLE resolved to 1.
TLS_SOURCE_MARKERS=(
  'libreecho-tls'
  'CN=%s,O=LibreEcho'
  '20200101000000'
)
# Read-only data that only the linked mbedTLS libraries can provide once the
# artifact has been stripped.
MBEDTLS_DATA_MARKERS=(
  '-----BEGIN CERTIFICATE-----'
  'ecdsa_secp256r1_sha256'
  'secp256r1'
)
# Symbols defined by the linked mbedTLS static archives in an unstripped build.
MBEDTLS_SYMBOL_PREFIX=mbedtls_
MBEDTLS_SYMBOL_MIN=50

require_static_arm32() {
  local binary=$1 description
  [[ -f "$binary" && ! -L "$binary" && -s "$binary" ]] || {
    printf 'ERROR: UI TLS artifact is missing or unsafe: %s\n' "$binary" >&2; exit 1
  }
  description=$(file -b "$binary")
  case "$description" in
    *"ELF 32-bit"*"ARM"*"statically linked"*) ;;
    *) printf 'ERROR: UI TLS artifact is not static ARM32: %s: %s\n' \
         "$binary" "$description" >&2; exit 1 ;;
  esac
  if readelf -l "$binary" | grep -q 'Requesting program interpreter'; then
    printf 'ERROR: UI TLS artifact has a dynamic interpreter: %s\n' "$binary" >&2
    exit 1
  fi
  if readelf -d "$binary" 2>/dev/null | grep -q 'NEEDED'; then
    printf 'ERROR: UI TLS artifact has dynamic dependencies: %s\n' "$binary" >&2
    exit 1
  fi
}

count_markers() {
  local text=$1 pattern
  shift
  local count=0
  for pattern in "$@"; do
    if grep -qF -- "$pattern" <<<"$text"; then
      count=$((count + 1))
    fi
  done
  printf '%s' "$count"
}

mbedtls_symbol_count() {
  local binary=$1
  nm --defined-only "$binary" 2>/dev/null |
    awk '{print $NF}' | grep -c "^${MBEDTLS_SYMBOL_PREFIX}" || true
}

verify_prefix() {
  local prefix=$1
  [[ -d "$prefix" && ! -L "$prefix" ]] || {
    printf 'ERROR: UI ARM32 mbedTLS prefix is unavailable: %s\n' "$prefix" >&2
    exit 1
  }
  local header
  for header in mbedtls/ssl.h mbedtls/x509_crt.h mbedtls/pk.h mbedtls/entropy.h \
      mbedtls/build_info.h; do
    [[ -f "$prefix/include/$header" && ! -L "$prefix/include/$header" ]] || {
      printf 'ERROR: UI ARM32 mbedTLS prefix is missing %s: %s\n' "$header" "$prefix" >&2
      exit 1
    }
  done
  local version
  version=$(sed -n 's/^#define MBEDTLS_VERSION_STRING  *"\(.*\)"$/\1/p' \
    "$prefix/include/mbedtls/build_info.h" | head -n 1)
  [[ -n "$version" ]] || {
    printf 'ERROR: UI ARM32 mbedTLS prefix has no version string: %s\n' "$prefix" >&2
    exit 1
  }
  local archive members=0
  for archive in libmbedcrypto.a libmbedx509.a libmbedtls.a; do
    [[ -f "$prefix/lib/$archive" && ! -L "$prefix/lib/$archive" ]] || {
      printf 'ERROR: UI ARM32 mbedTLS prefix is missing lib/%s: %s\n' \
        "$archive" "$prefix" >&2
      exit 1
    }
    ar t "$prefix/lib/$archive" 2>/dev/null | grep -q . || {
      printf 'ERROR: UI ARM32 mbedTLS archive is empty: %s/%s\n' "$prefix/lib" "$archive" >&2
      exit 1
    }
    if file -b "$prefix/lib/$archive" | grep -q 'shared object'; then
      printf 'ERROR: UI ARM32 mbedTLS archive is a shared object: %s\n' "$archive" >&2
      exit 1
    fi
    members=$((members + $(ar t "$prefix/lib/$archive" | wc -l)))
  done
  if find "$prefix/lib" -maxdepth 1 -name '*.so*' -print -quit | grep -q .; then
    printf 'ERROR: UI ARM32 mbedTLS prefix contains dynamic libraries: %s\n' "$prefix" >&2
    exit 1
  fi
  printf 'ui_tls_prefix=ok mbedtls_version=%s mbedtls_archive_members=%s\n' \
    "$version" "$members"
}

verify_binary() {
  local binary=$1
  local label=${2:-$binary}
  require_static_arm32 "$binary"
  local text source_markers data_markers symbols
  text=$(strings -a "$binary")
  source_markers=$(count_markers "$text" "${TLS_SOURCE_MARKERS[@]}")
  data_markers=$(count_markers "$text" "${MBEDTLS_DATA_MARKERS[@]}")
  symbols=$(mbedtls_symbol_count "$binary")
  ((source_markers >= 2)) || {
    printf 'ERROR: %s carries no real TLS implementation (src/tls.c markers: %s/%s): %s\n' \
      "$label" "$source_markers" "${#TLS_SOURCE_MARKERS[@]}" "$binary" >&2
    exit 1
  }
  if ((symbols < MBEDTLS_SYMBOL_MIN)) && ((data_markers < 1)); then
    printf 'ERROR: %s has no linked mbedTLS evidence (symbols=%s markers=%s/%s): %s\n' \
      "$label" "$symbols" "$data_markers" "${#MBEDTLS_DATA_MARKERS[@]}" "$binary" >&2
    exit 1
  fi
  if [[ -n "$OBJECTS" ]]; then
    [[ -f "$OBJECTS/tls.o" && ! -L "$OBJECTS/tls.o" ]] || {
      printf 'ERROR: %s did not compile src/tls.c (missing tls.o): %s\n' \
        "$label" "$OBJECTS" >&2
      exit 1
    }
    if [[ -e "$OBJECTS/tls_stub.o" ]]; then
      printf 'ERROR: %s compiled src/tls_stub.c (LE_TLS_AVAILABLE=0): %s\n' \
        "$label" "$OBJECTS" >&2
      exit 1
    fi
  fi
  printf 'ui_tls=%s label=%s static=1 tls_source_markers=%s mbedtls_symbols=%s mbedtls_data_markers=%s\n' \
    'real' "$label" "$source_markers" "$symbols" "$data_markers"
}

[[ -z "$PREFIX" ]] || verify_prefix "$PREFIX"
[[ -z "$BINARY" ]] || verify_binary "$BINARY" "$LABEL"
