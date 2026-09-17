#!/usr/bin/env bash
# Build the pinned ARM32 mbedTLS dependency used by the production UI bundle.
#
# The image repository owns this packaging contract.  Source acquisition stays
# outside this repository: the caller supplies the exact archive recorded in
# SOURCE.lock and this script fails closed on any hash, toolchain, or output
# mismatch.
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_mbedtls.sh --archive FILE --output DIR --cc COMPILER [--python PYTHON] [--jobs N]'
}

ARCHIVE=
OUTPUT=
CC=
PYTHON=
JOBS=${LIBREECHO_BUILD_JOBS:-2}
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    --python) shift; (($#)) || { usage >&2; exit 2; }; PYTHON=$1 ;;
    --jobs) shift; (($#)) || { usage >&2; exit 2; }; JOBS=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" && -n "$CC" ]] || { usage >&2; exit 2; }
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || {
  printf 'ERROR: unsafe mbedTLS source archive: %s\n' "$ARCHIVE" >&2; exit 1
}
[[ -x "$CC" ]] || { printf 'ERROR: cross compiler is unavailable: %s\n' "$CC" >&2; exit 1; }
[[ "$JOBS" =~ ^[0-9]+$ && "$JOBS" -ge 1 ]] || {
  printf 'ERROR: invalid job count: %s\n' "$JOBS" >&2; exit 1
}

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
SOURCE_LOCK="$SCRIPT_DIR/SOURCE.lock"
[[ -f "$SOURCE_LOCK" && ! -L "$SOURCE_LOCK" ]] || {
  printf 'ERROR: missing mbedTLS source lock: %s\n' "$SOURCE_LOCK" >&2; exit 1
}
command -v sha256sum >/dev/null 2>&1 || {
  printf 'ERROR: sha256sum is required to validate the mbedTLS source archive\n' >&2; exit 1
}
# One interpreter runs every helper below: the lock parse, the pinned build
# requirement check, the mbedTLS library Makefile, and the metadata record.
[[ -n "$PYTHON" ]] || PYTHON=python3
[[ -x "$PYTHON" ]] || command -v "$PYTHON" >/dev/null 2>&1 || {
  printf 'ERROR: python interpreter is unavailable: %s\n' "$PYTHON" >&2; exit 1
}
AR_BIN="${CC%gcc}ar"
[[ "$AR_BIN" != "$CC" && -x "$AR_BIN" ]] || {
  printf 'ERROR: matching archiver is unavailable: %s\n' "$AR_BIN" >&2; exit 1
}

read_lock_field() {
  "$PYTHON" - "$SOURCE_LOCK" "$1" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
value = lock.get(sys.argv[2])
if not isinstance(value, str) or not value:
    sys.exit(f"ERROR: SOURCE.lock is missing {sys.argv[2]}")
print(value)
PY
}

mbedtls_version=$(read_lock_field version)
expected_archive_sha=$(read_lock_field source_sha256)
actual_archive_sha=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ "$actual_archive_sha" == "$expected_archive_sha" ]] || {
  printf 'ERROR: mbedTLS source archive hash mismatch: %s\n' "$actual_archive_sha" >&2
  exit 1
}

# The pinned Python packages are build requirements: the release tarball ships
# generated PSA driver wrappers that the library Makefile regenerates with the
# bundled Jinja templates.
"$PYTHON" - "$SOURCE_LOCK" <<'PY' || exit 1
import importlib.metadata as metadata
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
requirements = lock.get("build_requirements") or {}
for module, field in (("jinja2", "jinja2"), ("jsonschema", "jsonschema")):
    expected = requirements.get(field)
    if not expected:
        sys.exit(f"ERROR: SOURCE.lock does not pin the {field} build requirement")
    try:
        actual = metadata.version(module)
    except metadata.PackageNotFoundError:
        sys.exit(
            f"ERROR: pinned build requirement is missing: {field}=={expected} "
            f"(install it for the interpreter running the build)"
        )
    if actual != expected:
        sys.exit(
            f"ERROR: pinned build requirement mismatch: {field}: "
            f"expected {expected}, found {actual}"
        )
PY

work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-mbedtls-build.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar -xjf "$ARCHIVE" -C "$work"
src="$work/mbedtls-$mbedtls_version"
[[ -f "$src/LICENSE" && -f "$src/library/Makefile" && -f "$src/include/mbedtls/ssl.h" ]] || {
  printf 'ERROR: malformed mbedTLS source archive\n' >&2; exit 1
}

export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
export LC_ALL=C
export TZ=UTC
umask 022

build_cflags="-Os -fno-asynchronous-unwind-tables -fno-unwind-tables"
build_cflags+=" -ffile-prefix-map=$work=/usr/src/mbedtls-$mbedtls_version"
build_cflags+=" -fdebug-prefix-map=$work=/usr/src/mbedtls-$mbedtls_version"
build_cflags+=" -fmacro-prefix-map=$work=/usr/src/mbedtls-$mbedtls_version"

rm -rf "$OUTPUT"
mkdir -p "$OUTPUT/include" "$OUTPUT/lib"
if ! make -C "$src/library" -j"$JOBS" static \
    CC="$CC" AR="$AR_BIN" PYTHON="$PYTHON" CFLAGS="$build_cflags" \
    >"$work/mbedtls-build.log" 2>&1; then
  printf 'ERROR: mbedTLS ARM32 static build failed\n' >&2
  tail -n 30 "$work/mbedtls-build.log" >&2
  exit 1
fi

for archive in libmbedcrypto.a libmbedx509.a libmbedtls.a; do
  [[ -f "$src/library/$archive" && ! -L "$src/library/$archive" ]] || {
    printf 'ERROR: mbedTLS build did not produce %s\n' "$archive" >&2; exit 1
  }
done
if find "$src/library" -maxdepth 1 -name '*.so*' -print -quit | grep -q .; then
  printf 'ERROR: mbedTLS build produced dynamic libraries\n' >&2; exit 1
fi

cp -R "$src/include/." "$OUTPUT/include/"
cp "$src/LICENSE" "$OUTPUT/LICENSE"
for archive in libmbedcrypto.a libmbedx509.a libmbedtls.a; do
  install -m 0644 "$src/library/$archive" "$OUTPUT/lib/$archive"
  members=$(ar t "$OUTPUT/lib/$archive" | wc -l)
  ((members > 0)) || {
    printf 'ERROR: empty mbedTLS archive: %s\n' "$archive" >&2; exit 1
  }
  if ar t "$OUTPUT/lib/$archive" | grep -qE '(^|/)\.\.'; then
    printf 'ERROR: unsafe member name in mbedTLS archive: %s\n' "$archive" >&2; exit 1
  fi
  member_dir="$work/members-$archive"
  mkdir -p "$member_dir"
  (cd "$member_dir" && ar x "$OUTPUT/lib/$archive")
  while IFS= read -r -d '' member; do
    file -b "$member" | grep -Eq '^ELF 32-bit LSB relocatable, ARM' || {
      printf 'ERROR: non-ARM32 object in %s: %s\n' "$archive" "$(basename "$member")" >&2
      exit 1
    }
  done < <(find "$member_dir" -type f -print0)
  rm -rf "$member_dir"
done

for header in include/mbedtls/ssl.h include/mbedtls/x509_crt.h include/mbedtls/pk.h \
    include/mbedtls/entropy.h include/mbedtls/build_info.h; do
  [[ -f "$OUTPUT/$header" && ! -L "$OUTPUT/$header" ]] || {
    printf 'ERROR: missing mbedTLS header: %s\n' "$header" >&2; exit 1
  }
done
packed_version=$(sed -n 's/^#define MBEDTLS_VERSION_STRING  *"\(.*\)"$/\1/p' \
  "$OUTPUT/include/mbedtls/build_info.h" | head -n 1)
[[ "$packed_version" == "$mbedtls_version" ]] || {
  printf 'ERROR: mbedTLS version mismatch: expected %s, found %s\n' \
    "$mbedtls_version" "${packed_version:-unknown}" >&2
  exit 1
}
# The archives must not record the private build directory.  This cannot be a
# `strings ... | grep -q` pipeline: `grep -q` leaves on the first match, which
# leaves `strings` writing into a closed pipe, and `set -o pipefail` then reports
# its SIGPIPE status (141) instead of the match, so the rejection below would be
# skipped for exactly the archives that do leak a build path.  Writing the full
# scan to a file consumes the stream and lets `strings` fail normally instead.
leak_scan="$work/mbedtls-build-paths.txt"
strings -a "$OUTPUT/lib/libmbedtls.a" "$OUTPUT/lib/libmbedx509.a" \
  "$OUTPUT/lib/libmbedcrypto.a" > "$leak_scan"
if grep -qE "$work|/home/" "$leak_scan"; then
  printf 'ERROR: mbedTLS archives contain a private build path\n' >&2; exit 1
fi

compiler_version=$("$CC" --version | sed -n '1p')
python_version=$("$PYTHON" -c 'import platform;print(platform.python_version())')
# Python 3.8 is the floor this lock permits, so the record uses only
# 3.8-compatible syntax (str.removesuffix is 3.9+).
"$PYTHON" - "$OUTPUT" "$SOURCE_LOCK" "$compiler_version" "$python_version" <<'PY'
import hashlib
import json
import pathlib
import sys

output = pathlib.Path(sys.argv[1])
lock = json.load(open(sys.argv[2], encoding="utf-8"))
compiler, python_version = sys.argv[3:5]


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    value.update(path.read_bytes())
    return value.hexdigest()


record = {
    "name": lock["name"],
    "version": lock["version"],
    "license": lock["license"],
    "source_url": lock["source_url"],
    "source_archive_sha256": lock["source_sha256"],
    "target": lock["target"],
    "build_requirements": lock["build_requirements"],
    "python": python_version,
    "compiler": compiler,
    "archives": {
        name: digest(output / "lib" / name)
        for name in ("libmbedcrypto.a", "libmbedx509.a", "libmbedtls.a")
    },
    "include_sha256": digest(output / "include" / "mbedtls" / "build_info.h"),
}
(output / "mbedtls-source.json").write_text(
    json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print("mbedtls_version=" + record["version"])
for name, value in sorted(record["archives"].items()):
    print("mbedtls_" + name[:-2] + "_sha256=" + value)
print("mbedtls_archives=3")
PY
