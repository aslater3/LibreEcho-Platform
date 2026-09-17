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
# The staging directory is a sibling of OUTPUT, so OUTPUT must name a path rather
# than a directory with a trailing separator: `--output /prefix/` or `/prefix/.`
# would make `/prefix/.stage.$$` a child of the output, creating the stage would
# create OUTPUT itself, and the no-replace publication below would then refuse an
# output that only the stage had created - after the whole build had already run.
# Normalise the separator and reject a path that still cannot name a sibling.
output_argument=$OUTPUT
# Strip trailing separators and trailing '/.' components until neither rule can
# make further progress.  A single ordered pass leaves '/prefix//.' as
# '/prefix/', which would place STAGE inside OUTPUT and create OUTPUT early.
while :; do
  previous_output=$OUTPUT
  while [[ "$OUTPUT" == */ ]]; do OUTPUT=${OUTPUT%/}; done
  while [[ "$OUTPUT" == */. ]]; do OUTPUT=${OUTPUT%/.}; done
  [[ "$OUTPUT" == "$previous_output" ]] && break
done
case "$OUTPUT" in
  ""|.|..|*/..)
    printf 'ERROR: unsafe mbedTLS prefix output path: %s\n' "$output_argument" >&2
    exit 1
    ;;
esac
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

# The lock's interpreter floor is enforced here, before any work.  The pinned
# build requirements carry their own Requires-Python metadata (jsonschema 4.25.1
# requires 3.9), so an interpreter below the floor cannot install them at all and
# would otherwise fail much later with an unresolvable pinned package instead of
# a clear refusal.
python_floor=$("$PYTHON" - "$SOURCE_LOCK" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
floor = (lock.get("build_requirements") or {}).get("python3")
if not isinstance(floor, str) or not floor:
    sys.exit("ERROR: SOURCE.lock does not pin the python3 build requirement floor")
print(floor)
PY
) || exit 1
python_version=$("$PYTHON" -c 'import platform;print(platform.python_version())')
[[ "$python_floor" =~ ^\>=([0-9]+)\.([0-9]+)$ ]] || {
  printf 'ERROR: unsupported python3 floor in SOURCE.lock: %s\n' "$python_floor" >&2
  exit 1
}
floor_major=${BASH_REMATCH[1]}
floor_minor=${BASH_REMATCH[2]}
[[ "$python_version" =~ ^([0-9]+)\.([0-9]+) ]] || {
  printf 'ERROR: cannot read the interpreter version: %s\n' "$PYTHON" >&2
  exit 1
}
python_major=${BASH_REMATCH[1]}
python_minor=${BASH_REMATCH[2]}
if ((10#$python_major < 10#$floor_major)) \
  || { ((10#$python_major == 10#$floor_major)) \
    && ((10#$python_minor < 10#$floor_minor)); }; then
  printf 'ERROR: python %s is older than the pinned floor %s: %s\n' \
    "$python_version" "$python_floor" "$PYTHON" >&2
  exit 1
fi

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
# The prefix is staged beside the target and moved into place only after every
# check below has passed.  The output path itself is never erased: an accidental
# shared directory would otherwise lose unrelated artifacts before the build
# even started, and a failed build would then leave neither the old contents nor
# a usable prefix.
STAGE="${OUTPUT}.stage.$$"
# Installed before the refusal guards, which exit early: a rejected retry must
# not leave its private work directory behind in TMPDIR.
trap 'rm -rf "$work" "$STAGE"' EXIT
[[ ! -e "$OUTPUT" && ! -L "$OUTPUT" ]] || {
  printf 'ERROR: refusing to overwrite an existing mbedTLS prefix: %s\n' \
    "$OUTPUT" >&2
  exit 1
}
[[ ! -e "$STAGE" && ! -L "$STAGE" ]] || {
  printf 'ERROR: stale mbedTLS prefix staging path: %s\n' "$STAGE" >&2
  exit 1
}
# Publication below is a no-replace rename, and its two options are what make a
# concurrent build safe: `-T` never treats an existing directory as a container
# for the stage, and `-n` never replaces what is already there.  Probe the live
# `mv` once, before any work, because an `mv` that does not provide those
# semantics would turn a concurrent publication into a contaminated prefix plus
# a success status - the exact failure this guard exists to prevent.
probe_mv_no_replace() {
  local probe="$work/mv-probe" status=0
  mkdir -p "$probe/source" "$probe/target"
  printf 'incumbent\n' > "$probe/target/incumbent"
  mv -T -n -- "$probe/source" "$probe/target" 2>"$work/mv-probe.log" || status=$?
  # `mv` reports a refused no-replace rename with 0 or 1; 2 or more is its own
  # usage error, which is what an unsupported -T/-n looks like.
  (( status <= 1 )) || return 1
  # The refused rename must leave both sides exactly as they were: the existing
  # directory is neither replaced nor treated as a container for the stage.
  [[ -d "$probe/source" && -f "$probe/target/incumbent" \
    && ! -e "$probe/target/source" ]] || return 1
  # The same invocation must still publish a staged directory when the target is
  # absent, so a probe cannot pass on an `mv` that silently ignores both options.
  status=0
  mv -T -n -- "$probe/source" "$probe/fresh" 2>>"$work/mv-probe.log" || status=$?
  [[ $status -eq 0 && -d "$probe/fresh" && ! -e "$probe/source" ]] || return 1
  rm -rf "$probe"
  return 0
}
probe_mv_no_replace || {
  printf 'ERROR: mv does not provide atomic no-replace publication (-T -n): %s\n' \
    "$(command -v mv)" >&2
  cat "$work/mv-probe.log" >&2
  exit 1
}

# The lock names a specific float ABI and mbedtls-source.json publishes that
# value verbatim, but the compiler is a caller argument: a soft-float
# `arm-linux-gnueabi-gcc` still emits `ELF 32-bit LSB relocatable, ARM` objects,
# so the archive check below would accept it and the prefix would ship with a
# provenance record that misstates its own ABI - and the production hard-float
# UI link may then reject the cached prefix.  Probe what the compiler actually
# emits instead of trusting its filename or the recorded target text.
cc_target=$(read_lock_field target)
case "$cc_target" in
  *eabihf*) cc_float_abi=hard ;;
  *eabi)    cc_float_abi=soft ;;
  *)
    printf 'ERROR: unsupported mbedTLS target ABI in SOURCE.lock: %s\n' "$cc_target" >&2
    exit 1
    ;;
esac
# Prefer the compiler's own cross readelf, so the attributes are read by the
# toolchain that produced them.
READELF_BIN="${CC%gcc}readelf"
[[ -x "$READELF_BIN" ]] || READELF_BIN=readelf
command -v "$READELF_BIN" >/dev/null 2>&1 || {
  printf 'ERROR: readelf is required to validate the mbedTLS compiler ABI\n' >&2
  exit 1
}
abi_dir="$work/abi-probe"
mkdir -p "$abi_dir"
printf 'int libreecho_mbedtls_abi_probe;\n' > "$abi_dir/probe.c"
"$CC" -c "$abi_dir/probe.c" -o "$abi_dir/probe.o" >"$work/abi-probe.log" 2>&1 || {
  printf 'ERROR: the mbedTLS cross compiler could not compile a probe object: %s\n' \
    "$CC" >&2
  cat "$work/abi-probe.log" >&2
  exit 1
}
[[ -f "$abi_dir/probe.o" && ! -L "$abi_dir/probe.o" ]] || {
  printf 'ERROR: the mbedTLS cross compiler produced no probe object: %s\n' \
    "$CC" >&2
  exit 1
}
# Read the attributes into a file rather than piping into `grep -q`: the short
# circuit would leave the reader writing into a closed pipe, and `pipefail`
# reports that instead of the match.
abi_attrs="$work/abi-probe.attrs"
"$READELF_BIN" -A "$abi_dir/probe.o" > "$abi_attrs" 2>"$work/abi-probe-readelf.log" || {
  printf 'ERROR: cannot read the mbedTLS probe object attributes: %s\n' \
    "$abi_dir/probe.o" >&2
  cat "$work/abi-probe-readelf.log" >&2
  exit 1
}
if [[ "$cc_float_abi" == hard ]]; then
  grep -q 'Tag_ABI_VFP_args: VFP registers' "$abi_attrs" || {
    printf 'ERROR: compiler does not target the pinned hard-float ABI (%s): %s\n' \
      "$cc_target" "$CC" >&2
    exit 1
  }
else
  if grep -q 'Tag_ABI_VFP_args' "$abi_attrs"; then
    printf 'ERROR: compiler does not target the pinned soft-float ABI (%s): %s\n' \
      "$cc_target" "$CC" >&2
    exit 1
  fi
fi
rm -rf "$abi_dir"

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

mkdir -p "$STAGE/include" "$STAGE/lib"
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

cp -R "$src/include/." "$STAGE/include/"
cp "$src/LICENSE" "$STAGE/LICENSE"
for archive in libmbedcrypto.a libmbedx509.a libmbedtls.a; do
  install -m 0644 "$src/library/$archive" "$STAGE/lib/$archive"
  members=$(ar t "$STAGE/lib/$archive" | wc -l)
  ((members > 0)) || {
    printf 'ERROR: empty mbedTLS archive: %s\n' "$archive" >&2; exit 1
  }
  if ar t "$STAGE/lib/$archive" | grep -qE '(^|/)\.\.'; then
    printf 'ERROR: unsafe member name in mbedTLS archive: %s\n' "$archive" >&2; exit 1
  fi
  member_dir="$work/members-$archive"
  mkdir -p "$member_dir"
  (cd "$member_dir" && ar x "$STAGE/lib/$archive")
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
  [[ -f "$STAGE/$header" && ! -L "$STAGE/$header" ]] || {
    printf 'ERROR: missing mbedTLS header: %s\n' "$header" >&2; exit 1
  }
done
packed_version=$(sed -n 's/^#define MBEDTLS_VERSION_STRING  *"\(.*\)"$/\1/p' \
  "$STAGE/include/mbedtls/build_info.h" | head -n 1)
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
strings -a "$STAGE/lib/libmbedtls.a" "$STAGE/lib/libmbedx509.a" \
  "$STAGE/lib/libmbedcrypto.a" > "$leak_scan"
# Both patterns are literal paths, not patterns: `$work` is interpolated into
# this match, and an ERE metacharacter in TMPDIR (a `+`, `[`, or `*`) would
# otherwise change the pattern or make it invalid, so the private build path
# would not match itself and an archive carrying it would be published.  The
# status is inspected explicitly for the same reason: only 1 means "no match",
# and a scan that could not be read must not be reported as a clean one.
leak_status=0
grep -qF -e "$work" -e '/home/' -- "$leak_scan" || leak_status=$?
if ((leak_status == 0)); then
  printf 'ERROR: mbedTLS archives contain a private build path\n' >&2; exit 1
fi
if ((leak_status != 1)); then
  printf 'ERROR: could not scan the mbedTLS archives for private build paths\n' >&2
  exit 1
fi

compiler_version=$("$CC" --version | sed -n '1p')
# python_version was read and checked against the lock's interpreter floor above.
# The record uses only syntax that floor permits.
"$PYTHON" - "$STAGE" "$SOURCE_LOCK" "$compiler_version" "$python_version" <<'PY'
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


def include_tree_digest(root: pathlib.Path) -> str:
    """Digest every header the UI compiles against.

    The verifier reproduces this walk over the prefix it is about to use, so a
    stale or hand-edited header cannot be consumed while the archives still
    match their recorded digests.
    """
    value = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        value.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        value.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
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
    "include_tree_sha256": include_tree_digest(output / "include"),
}
(output / "mbedtls-source.json").write_text(
    json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print("mbedtls_version=" + record["version"])
for name, value in sorted(record["archives"].items()):
    print("mbedtls_" + name[:-2] + "_sha256=" + value)
print("mbedtls_archives=3")
PY

# Only a fully checked prefix is published, by a rename that must not replace
# anything: the staging directory sits beside the requested output path, so the
# rename is atomic on one filesystem.  The existence check above is a fast
# refusal, not the race guard - two builders can both see an absent OUTPUT and
# both finish - so the rename itself carries the contract.  Under `-T` an OUTPUT
# that appeared meanwhile is a path rather than a container, and under `-n` it is
# never replaced: the second builder fails closed instead of nesting its stage
# inside the published prefix.  The stage is asserted to be gone afterwards so
# that the failure is observed rather than inferred from mv's exit status, and
# the EXIT trap removes the stage, so an incumbent prefix is never replaced,
# nested into, or partially overwritten.
publish_status=0
mv -T -n -- "$STAGE" "$OUTPUT" 2>"$work/mv-publish.log" || publish_status=$?
if [[ -e "$STAGE" || -L "$STAGE" ]]; then
  if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
    printf 'ERROR: refusing to publish the mbedTLS prefix: %s appeared during the build\n' \
      "$OUTPUT" >&2
  else
    printf 'ERROR: could not publish the mbedTLS prefix: %s\n' "$OUTPUT" >&2
  fi
  cat "$work/mv-publish.log" >&2
  exit 1
fi
if ((publish_status != 0)) || [[ ! -f "$OUTPUT/mbedtls-source.json" ]]; then
  printf 'ERROR: failed to publish the mbedTLS prefix: %s\n' "$OUTPUT" >&2
  cat "$work/mv-publish.log" >&2
  exit 1
fi
