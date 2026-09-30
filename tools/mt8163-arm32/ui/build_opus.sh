#!/usr/bin/env bash
# Build the pinned static Opus decode stack (libogg + libopus + libopusfile)
# linked into LibreEcho-UI's radiod Opus decoder (src/adapter/radio_opus.c).
#
# The image repository owns this packaging contract.  Source acquisition stays
# outside this repository: the caller supplies the three upstream archives
# recorded in opus/SOURCE.lock and this script fails closed on any byte,
# toolchain, output, or identity mismatch.  It never downloads and never runs
# a build step that fetches source.
#
# libopusfile is built HTTP/TLS-free by construction: only the four local-file
# sources are compiled, so src/http.c and its libcurl + OpenSSL/TLS
# dependencies are never pulled in.  A verification step fails the build if any
# HTTP/URL entry point leaked into the archive.
set -euo pipefail

usage() {
  printf '%s\n' \
    'usage: build_opus.sh --ogg-archive FILE --opus-archive FILE --opusfile-archive FILE' \
    '                     --output DIR [--cc COMPILER] [--ar ARCHIVER] [--host TRIPLE]' \
    '                     [--jobs N] [--python PYTHON]'
}

OGG_ARCHIVE=
OPUS_ARCHIVE=
OPUSFILE_ARCHIVE=
OUTPUT=
CCI=
ARG=
HOST=
PYTHON=
JOBS=${LIBREECHO_BUILD_JOBS:-2}
while (($#)); do
  case "$1" in
    --ogg-archive) shift; (($#)) || { usage >&2; exit 2; }; OGG_ARCHIVE=$1 ;;
    --opus-archive) shift; (($#)) || { usage >&2; exit 2; }; OPUS_ARCHIVE=$1 ;;
    --opusfile-archive) shift; (($#)) || { usage >&2; exit 2; }; OPUSFILE_ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CCI=$1 ;;
    --ar) shift; (($#)) || { usage >&2; exit 2; }; ARG=$1 ;;
    --host) shift; (($#)) || { usage >&2; exit 2; }; HOST=$1 ;;
    --jobs) shift; (($#)) || { usage >&2; exit 2; }; JOBS=$1 ;;
    --python) shift; (($#)) || { usage >&2; exit 2; }; PYTHON=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -n "$OGG_ARCHIVE" && -n "$OPUS_ARCHIVE" && -n "$OPUSFILE_ARCHIVE" && -n "$OUTPUT" ]] || {
  usage >&2; exit 2
}
# --output is required: there is deliberately no default prefix, so a build can
# never silently publish beside the source tree or into a shared location.
CCI=${CCI:-cc}
command -v "$CCI" >/dev/null 2>&1 || {
  printf 'ERROR: C compiler is unavailable: %s\n' "$CCI" >&2; exit 1
}
if [[ -z "$ARG" ]]; then
  if [[ "$CCI" == *gcc ]]; then
    ARG="${CCI%gcc}ar"
  else
    ARG=$(command -v ar || true)
  fi
fi
[[ -n "$ARG" && -x "$ARG" ]] || {
  printf 'ERROR: archiver is unavailable: %s\n' "${ARG:-<none>}" >&2; exit 1
}
[[ "$JOBS" =~ ^[0-9]+$ && "$JOBS" -ge 1 ]] || {
  printf 'ERROR: invalid job count: %s\n' "$JOBS" >&2; exit 1
}
[[ -n "$PYTHON" ]] || PYTHON=python3
[[ -x "$PYTHON" ]] || command -v "$PYTHON" >/dev/null 2>&1 || {
  printf 'ERROR: python interpreter is unavailable: %s\n' "$PYTHON" >&2; exit 1
}
for tool in tar make sha256sum file nm; do
  command -v "$tool" >/dev/null 2>&1 || {
    printf 'ERROR: required build tool is unavailable: %s\n' "$tool" >&2; exit 1
  }
done

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
SOURCE_LOCK=$SCRIPT_DIR/opus/SOURCE.lock
[[ -f "$SOURCE_LOCK" && ! -L "$SOURCE_LOCK" ]] || {
  printf 'ERROR: Opus source lock is unavailable: %s\n' "$SOURCE_LOCK" >&2; exit 1
}

CONFIG='static,no-http,no-tls,no-examples,no-doc'

read_lock_field() {
  "$PYTHON" - "$SOURCE_LOCK" "$1" "$2" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
component, field = sys.argv[2], sys.argv[3]
try:
    value = lock["components"][component][field]
except (KeyError, TypeError):
    sys.exit("ERROR: SOURCE.lock is missing components.%s.%s" % (component, field))
if not isinstance(value, str) or not value:
    sys.exit("ERROR: SOURCE.lock field components.%s.%s is empty" % (component, field))
print(value)
PY
}

verify_archive() {
  local component=$1 archive=$2 expected actual
  expected=$(read_lock_field "$component" source_sha256)
  actual=$(sha256sum "$archive" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || {
    printf 'ERROR: %s source archive hash mismatch: expected %s, found %s (%s)\n' \
      "$component" "$expected" "$actual" "$archive" >&2
    exit 1
  }
  printf 'opus_source_%s_sha256=%s\n' "$component" "$actual"
}

# The prefix identity is the cache key: the compiler target, the build config,
# and the three pinned source hashes.  It is what a later caller compares
# against, so a prefix produced for a different architecture, build
# configuration, or source set can never be silently reused as this build's
# output.  The identity is resolved from the lock before the caller's archives
# are read, so a matching prefix is a cache hit that builds nothing.
TARGET=$("$CCI" -dumpmachine)
[[ -n "$TARGET" ]] || { printf 'ERROR: cannot read the compiler target\n' >&2; exit 1; }
pinned_ogg=$(read_lock_field libogg source_sha256)
pinned_opus=$(read_lock_field opus source_sha256)
pinned_opusfile=$(read_lock_field opusfile source_sha256)

check_identity_cache() {
  "$PYTHON" - "$1" "$TARGET" "$CONFIG" "$pinned_ogg" "$pinned_opus" "$pinned_opusfile" <<'PY'
import json
import sys

path, target, config, ogg, opus, opusfile = sys.argv[1:7]
try:
    record = json.load(open(path, encoding="utf-8"))
except Exception as error:  # noqa: BLE001 - any failure is a fail-closed refusal
    sys.exit("ERROR: cannot read the Opus prefix identity record %s: %s" % (path, error))
if not isinstance(record, dict):
    sys.exit("ERROR: malformed Opus prefix identity record: %s" % path)
for field, expected in (("target", target), ("config", config)):
    found = record.get(field)
    if found != expected:
        sys.exit(
            "ERROR: Opus prefix identity mismatch: %s: expected %r, found %r"
            % (field, expected, found)
        )
archives = record.get("archives")
if not isinstance(archives, dict):
    sys.exit("ERROR: Opus prefix identity record has no archives map: %s" % path)
for name, expected in (("libogg", ogg), ("opus", opus), ("opusfile", opusfile)):
    found = archives.get(name)
    if found != expected:
        sys.exit(
            "ERROR: Opus prefix identity mismatch: archives.%s: expected %s, found %s"
            % (name, expected, found)
        )
PY
}

if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
  [[ -d "$OUTPUT" && ! -L "$OUTPUT" ]] || {
    printf 'ERROR: refusing to replace a non-directory Opus prefix: %s\n' "$OUTPUT" >&2
    exit 1
  }
  identity=$OUTPUT/opus-identity.json
  [[ -f "$identity" && ! -L "$identity" ]] || {
    printf 'ERROR: refusing an Opus prefix with no identity record (fail closed): %s\n' \
      "$OUTPUT" >&2
    exit 1
  }
  check_identity_cache "$identity" || exit 1
  printf 'opus_identity_cache=hit target=%s config=%s prefix=%s\n' \
    "$TARGET" "$CONFIG" "$OUTPUT"
  exit 0
fi

# Only a build consumes the caller's archives, so they are checked and hashed
# against the lock here, after any identity cache hit has been resolved.
for archive in "$OGG_ARCHIVE" "$OPUS_ARCHIVE" "$OPUSFILE_ARCHIVE"; do
  [[ -f "$archive" && ! -L "$archive" ]] || {
    printf 'ERROR: Opus source archive is missing or unsafe: %s\n' "$archive" >&2
    exit 1
  }
done
{
  verify_archive libogg "$OGG_ARCHIVE"
  verify_archive opus "$OPUS_ARCHIVE"
  verify_archive opusfile "$OPUSFILE_ARCHIVE"
} >/dev/null

work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-opus-build.XXXXXX")
STAGE="${OUTPUT}.stage.$$"
trap 'rm -rf "$work" "$STAGE"' EXIT
mkdir -p "$STAGE/include/ogg" "$STAGE/include/opus" "$STAGE/lib" "$STAGE/licenses"
probe_mv_no_replace() {
  local probe="$work/mv-probe" status=0
  mkdir -p "$probe/source" "$probe/target"
  printf 'incumbent\n' > "$probe/target/incumbent"
  mv -T -n -- "$probe/source" "$probe/target" 2>"$work/mv-probe.log" || status=$?
  (( status <= 1 )) || return 1
  [[ -d "$probe/source" && -f "$probe/target/incumbent" && ! -e "$probe/target/source" ]] || return 1
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

export LC_ALL=C
export TZ=UTC
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
umask 022

ogg=$work/src/libogg
opus=$work/src/opus
opusfile=$work/src/opusfile
mkdir -p "$ogg" "$opus" "$opusfile"
tar -xzf "$OGG_ARCHIVE" -C "$ogg" --strip-components=1
tar -xzf "$OPUS_ARCHIVE" -C "$opus" --strip-components=1
tar -xzf "$OPUSFILE_ARCHIVE" -C "$opusfile" --strip-components=1
[[ -f "$ogg/src/framing.c" && -f "$opus/src/opus.c" && -f "$opusfile/src/opusfile.c" ]] || {
  printf 'ERROR: an Opus source archive does not contain the expected tree\n' >&2; exit 1
}

# The lock pins each upstream license; the archive must ship exactly that file,
# so the license inventory in the image cannot drift from the source set.
verify_license() {
  local component=$1 tree=$2 expected actual
  expected=$(read_lock_field "$component" license_sha256)
  actual=$(sha256sum "$tree/COPYING" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || {
    printf 'ERROR: %s COPYING hash mismatch: expected %s, found %s\n' \
      "$component" "$expected" "$actual" >&2
    exit 1
  }
}
verify_license libogg "$ogg"
verify_license opus "$opus"
verify_license opusfile "$opusfile"

# One abort is enough to identify a compiler mismatch.  The probe's file(1)
# architecture token must match every archive member below.
probe_dir=$work/probe
mkdir -p "$probe_dir"
printf 'int libreecho_opus_abi_probe;\n' > "$probe_dir/probe.c"
"$CCI" -c "$probe_dir/probe.c" -o "$probe_dir/probe.o" >"$work/probe.log" 2>&1 || {
  printf 'ERROR: the Opus compiler could not compile a probe object: %s\n' "$CCI" >&2
  cat "$work/probe.log" >&2
  exit 1
}
[[ -f "$probe_dir/probe.o" ]] || {
  printf 'ERROR: the Opus compiler produced no probe object: %s\n' "$CCI" >&2; exit 1
}
probe_desc=$(file -b "$probe_dir/probe.o")
case "$probe_desc" in
  ELF*relocatable*) ;;
  *) printf 'ERROR: the Opus compiler did not emit an ELF relocatable object: %s\n' \
       "$probe_desc" >&2; exit 1 ;;
esac
expected_arch=$(printf '%s\n' "$probe_desc" | awk -F', ' '{print $2}')
[[ -n "$expected_arch" ]] || {
  printf 'ERROR: cannot read the Opus compiler architecture: %s\n' "$probe_desc" >&2; exit 1
}

common_cflags="-Os -fno-asynchronous-unwind-tables -fno-unwind-tables"
common_cflags+=" -ffile-prefix-map=$work=/usr/src/libreecho-opus"
common_cflags+=" -fdebug-prefix-map=$work=/usr/src/libreecho-opus"
common_cflags+=" -fmacro-prefix-map=$work=/usr/src/libreecho-opus"
configure_args=(--disable-shared --enable-static --disable-dependency-tracking)
[[ -n "$HOST" ]] && configure_args+=(--host="$HOST")

run_configure() {
  local name=$1
  local dir=$2
  local log=$work/$name-conf.log
  shift 2
  if ! ( cd "$dir" && ./configure "${configure_args[@]}" "$@" \
        CC="$CCI" AR="$ARG" CFLAGS="$common_cflags" ) >"$log" 2>&1; then
    printf 'ERROR: %s configure failed\n' "$name" >&2
    tail -n 30 "$log" >&2
    exit 1
  fi
}

run_make() {
  local name=$1
  local dir=$2
  local log=$work/$name-make.log
  shift 2
  if ! ( cd "$dir" && make -j"$JOBS" "$@" ) >"$log" 2>&1; then
    printf 'ERROR: %s build failed\n' "$name" >&2
    tail -n 30 "$log" >&2
    exit 1
  fi
}

run_configure libogg "$ogg"
# libogg only needs the codec core; skip its examples/tests.
run_make libogg "$ogg/src" libogg.la
[[ -f "$ogg/src/.libs/libogg.a" ]] || {
  printf 'ERROR: libogg build did not produce src/.libs/libogg.a\n' >&2; exit 1
}

run_configure libopus "$opus" \
  --disable-doc --disable-extra-programs --disable-hardening --disable-stack-protector
run_make libopus "$opus"
[[ -f "$opus/.libs/libopus.a" ]] || {
  printf 'ERROR: libopus build did not produce .libs/libopus.a\n' >&2; exit 1
}

for header in "$ogg/include/ogg/"*.h; do
  install -m 0644 "$header" "$STAGE/include/ogg/"
done
mkdir -p "$STAGE/include/opus"
for header in "$opus/include/"*.h; do
  install -m 0644 "$header" "$STAGE/include/opus/"
done
cp "$opusfile/include/opusfile.h" "$STAGE/include/opus/opusfile.h"
install -m 0644 "$ogg/src/.libs/libogg.a" "$STAGE/lib/libogg.a"
install -m 0644 "$opus/.libs/libopus.a" "$STAGE/lib/libopus.a"

# libopusfile is compiled directly from its four local-file sources.  src/http.c
# is deliberately excluded, so the archive carries no HTTP/URL API and no
# libcurl/OpenSSL dependency.  No configure or autotools step is required.
objdir=$work/opusfile-obj
mkdir -p "$objdir"
for source in info internal opusfile stream; do
  "$CCI" -c $common_cflags -I"$STAGE/include" -I"$STAGE/include/opus" \
    "$opusfile/src/$source.c" -o "$objdir/$source.o" \
    >"$work/opusfile-$source.log" 2>&1 || {
    printf 'ERROR: compiling libopusfile src/%s.c failed\n' "$source" >&2
    tail -n 30 "$work/opusfile-$source.log" >&2
    exit 1
  }
done
"$ARG" rcs "$STAGE/lib/libopusfile.a" "$objdir/info.o" "$objdir/internal.o" \
  "$objdir/opusfile.o" "$objdir/stream.o"

install -m 0644 "$ogg/COPYING" "$STAGE/licenses/$(read_lock_field libogg license_copy)"
install -m 0644 "$opus/COPYING" "$STAGE/licenses/$(read_lock_field opus license_copy)"
install -m 0644 "$opusfile/COPYING" "$STAGE/licenses/$(read_lock_field opusfile license_copy)"

for archive in libogg.a libopus.a libopusfile.a; do
  path=$STAGE/lib/$archive
  [[ -f "$path" && ! -L "$path" ]] || {
    printf 'ERROR: Opus build did not produce lib/%s\n' "$archive" >&2; exit 1
  }
  members=$(ar t "$path" | wc -l)
  ((members > 0)) || { printf 'ERROR: empty Opus archive: %s\n' "$archive" >&2; exit 1; }
done

headers=(
  include/ogg/ogg.h include/ogg/os_types.h include/ogg/config_types.h
  include/opus/opus.h include/opus/opus_multistream.h include/opus/opus_types.h
  include/opus/opus_defines.h include/opus/opus_projection.h include/opus/opusfile.h
)
for header in "${headers[@]}"; do
  [[ -f "$STAGE/$header" && ! -L "$STAGE/$header" ]] || {
    printf 'ERROR: missing Opus header: %s\n' "$header" >&2; exit 1
  }
done

# The decoder surface the UI links must be present, and the HTTP/URL surface
# must be absent.  nm over the archive objects is authoritative for a static
# build.
opustool_nm() { nm --defined-only "$1" 2>/dev/null || true; }
for symbol in op_open_file op_open_memory op_read_float op_channel_count; do
  opustool_nm "$STAGE/lib/libopusfile.a" | grep -qw "$symbol" || {
    printf 'ERROR: libopusfile.a is missing the decoder symbol %s\n' "$symbol" >&2; exit 1
  }
done
for symbol in opus_decoder_create opus_decode_float opus_encoder_create; do
  opustool_nm "$STAGE/lib/libopus.a" | grep -qw "$symbol" || {
    printf 'ERROR: libopus.a is missing the codec symbol %s\n' "$symbol" >&2; exit 1
  }
done
opustool_nm "$STAGE/lib/libogg.a" | grep -qw ogg_stream_init || {
  printf 'ERROR: libogg.a is missing ogg_stream_init\n' >&2; exit 1
}
if opustool_nm "$STAGE/lib/libopusfile.a" |
    grep -Eqw 'op_open_url|op_vopen_url|op_test_url|op_vtest_url|op_http_open'; then
  printf 'ERROR: libopusfile.a contains HTTP/URL entry points (must be HTTP/TLS-free)\n' >&2
  exit 1
fi
if find "$STAGE/lib" -maxdepth 1 -name '*.so*' -print -quit | grep -q .; then
  printf 'ERROR: the Opus build produced dynamic libraries\n' >&2; exit 1
fi

# Every archive member must be an ELF object of the probed architecture, and no
# member may leak the private build directory.
member_dir=$work/members
mkdir -p "$member_dir"
for archive in libogg.a libopus.a libopusfile.a; do
  rm -rf "$member_dir"/*
  ( cd "$member_dir" && ar x "$STAGE/lib/$archive" )
  while IFS= read -r -d '' member; do
    desc=$(file -b "$member")
    case "$desc" in
      ELF*relocatable*) ;;
      *) printf 'ERROR: non-ELF member in %s: %s: %s\n' "$archive" \
           "$(basename "$member")" "$desc" >&2; exit 1 ;;
    esac
    member_arch=$(printf '%s\n' "$desc" | awk -F', ' '{print $2}')
    [[ "$member_arch" == "$expected_arch" ]] || {
      printf 'ERROR: %s member %s targets %s, expected %s\n' \
        "$archive" "$(basename "$member")" "$member_arch" "$expected_arch" >&2
      exit 1
    }
  done < <(find "$member_dir" -type f -print0)
done

leak_scan=$work/opus-build-paths.txt
strings -a "$STAGE/lib/libogg.a" "$STAGE/lib/libopus.a" "$STAGE/lib/libopusfile.a" > "$leak_scan"
leak_status=0
grep -qF -e "$work" -e '/home/' -- "$leak_scan" || leak_status=$?
if ((leak_status == 0)); then
  printf 'ERROR: Opus archives contain a private build path\n' >&2; exit 1
fi
if ((leak_status != 1)); then
  printf 'ERROR: could not scan the Opus archives for private build paths\n' >&2; exit 1
fi

compiler_version=$("$CCI" --version | sed -n '1p')
"$PYTHON" - "$STAGE" "$SOURCE_LOCK" "$TARGET" "$CONFIG" \
  "$compiler_version" "$TARGET" <<'PY'
import hashlib
import json
import pathlib
import sys

output = pathlib.Path(sys.argv[1])
lock = json.load(open(sys.argv[2], encoding="utf-8"))
target, config, compiler, compiler_target = sys.argv[3:7]


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def include_tree_digest(root: pathlib.Path) -> str:
    value = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        value.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        value.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return value.hexdigest()


built = {}
archive_files = {"libogg": "libogg.a", "opus": "libopus.a", "opusfile": "libopusfile.a"}
for name in ("libogg", "opus", "opusfile"):
    path = output / "lib" / archive_files[name]
    built[name] = {
        "sha256": digest(path),
        "size": path.stat().st_size,
    }

# The prefix identity is the cache key: compiler target, build config, and the
# three pinned source hashes.  It records the request rather than the result, so
# a later caller can compare it before anything is extracted or built.
identity = {
    "schema": 1,
    "name": lock["name"],
    "target": target,
    "config": config,
    "archives": {
        name: lock["components"][name]["source_sha256"]
        for name in ("libogg", "opus", "opusfile")
    },
}
(output / "opus-identity.json").write_text(
    json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)

record = {
    "schema": 1,
    "name": lock["name"],
    "target": target,
    "config": config,
    "compiler": compiler,
    "compiler_target": compiler_target,
    "python": sys.version.split()[0],
    "http_enabled": False,
    "components": {
        name: {
            "version": lock["components"][name]["version"],
            "license": lock["components"][name]["license"],
            "source_url": lock["components"][name]["source_url"],
            "source_archive_sha256": lock["components"][name]["source_sha256"],
            "license_sha256": lock["components"][name]["license_sha256"],
        }
        for name in ("libogg", "opus", "opusfile")
    },
    "artifacts": {name: built[name]["sha256"] for name in built},
    "artifact_sizes": {name: built[name]["size"] for name in built},
    "include_tree_sha256": include_tree_digest(output / "include"),
}
(output / "opus-source.json").write_text(
    json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)

for name, value in sorted(built.items()):
    print("opus_%s_sha256=%s" % (name, value["sha256"]))
print("opus_include_tree_sha256=" + record["include_tree_sha256"])
PY

publish_status=0
mv -T -n -- "$STAGE" "$OUTPUT" 2>"$work/mv-publish.log" || publish_status=$?
if [[ -e "$STAGE" || -L "$STAGE" ]]; then
  if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
    printf 'ERROR: refusing to publish the Opus prefix: %s appeared during the build\n' \
      "$OUTPUT" >&2
  else
    printf 'ERROR: could not publish the Opus prefix: %s\n' "$OUTPUT" >&2
  fi
  cat "$work/mv-publish.log" >&2
  exit 1
fi
if ((publish_status != 0)) || [[ ! -f "$OUTPUT/opus-source.json" ]]; then
  printf 'ERROR: failed to publish the Opus prefix: %s\n' "$OUTPUT" >&2
  cat "$work/mv-publish.log" >&2
  exit 1
fi

printf 'opus_prefix=%s\nopus_config=%s\nopus_target=%s\nopus_http=absent\n' \
  "$OUTPUT" "$CONFIG" "$TARGET"
