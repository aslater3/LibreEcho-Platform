#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_connectivity_helpers.sh --output DIR --cc COMPILER --sysroot DIR'
}

OUTPUT=
CC=
SYSROOT=
while (($#)); do
  case "$1" in
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cc) shift; (($#)) || { usage >&2; exit 2; }; CC=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$OUTPUT" && -n "$CC" && -n "$SYSROOT" ]] || { usage >&2; exit 2; }
[[ -x "$CC" ]] || { echo "ERROR: compiler is not executable: $CC" >&2; exit 1; }
[[ -f "$SYSROOT/usr/include/errno.h" ]] || { echo "ERROR: target sysroot is unavailable: $SYSROOT" >&2; exit 1; }

script_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
cc_dir="$(cd -- "$(dirname -- "$CC")" && pwd -P)"
host_usr="$(cd -- "$cc_dir/.." && pwd -P)"
host_root="$(cd -- "$host_usr/.." && pwd -P)"
host_library_path="$host_usr/lib:$host_root/lib"
source_epoch="${SOURCE_DATE_EPOCH:-1704067200}"
export SOURCE_DATE_EPOCH="$source_epoch"
export LC_ALL=C
export TZ=UTC
umask 022

for source in wmt_configure.c wmt_responder.c wmt_bt_on.c wmt_stock_compat.c wmt_launcher.c wmt_ioctl.h SOURCE.lock; do
  [[ -f "$script_dir/$source" && ! -L "$script_dir/$source" ]] || {
    echo "ERROR: missing connectivity source: $source" >&2
    exit 1
  }
done

rm -rf "$OUTPUT"
mkdir -p "$OUTPUT"
common=(
  -static -no-pie -Os -std=c11 -Wall -Wextra -Werror
  "--sysroot=$SYSROOT"
  -Wl,--build-id=none
  "-ffile-prefix-map=$script_dir=/usr/src/libreecho-connectivity"
  "-fdebug-prefix-map=$script_dir=/usr/src/libreecho-connectivity"
  "-fmacro-prefix-map=$script_dir=/usr/src/libreecho-connectivity"
)
for name in wmt_configure wmt_responder wmt_bt_on wmt_stock_compat wmt_launcher; do
  env LD_LIBRARY_PATH="$host_library_path" \
    "$CC" "${common[@]}" -I"$script_dir" -o "$OUTPUT/$name" "$script_dir/$name.c"
  chmod 0755 "$OUTPUT/$name"
  file "$OUTPUT/$name" | grep -Eq 'ELF 32-bit.*ARM.*statically linked' || {
    echo "ERROR: $name is not a static ARM32 ELF" >&2
    exit 1
  }
  if strings "$OUTPUT/$name" | grep -Eq '/home/|libreecho-connectivity-build'; then
    echo "ERROR: $name contains a host/build path" >&2
    exit 1
  fi
done

compiler_version="$(env LD_LIBRARY_PATH="$host_library_path" "$CC" --version | sed -n '1p')"
python3 - "$script_dir" "$OUTPUT" "$compiler_version" <<'PY'
import hashlib
import json
import pathlib
import sys

source_dir = pathlib.Path(sys.argv[1])
output_dir = pathlib.Path(sys.argv[2])
compiler = sys.argv[3]
source_names = [
    "wmt_configure.c", "wmt_responder.c", "wmt_bt_on.c",
    "wmt_stock_compat.c", "wmt_launcher.c", "wmt_ioctl.h", "SOURCE.lock",
]
output_names = [
    "wmt_configure", "wmt_responder", "wmt_bt_on",
    "wmt_stock_compat", "wmt_launcher",
]
sha256 = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
metadata = {
    "name": "LibreEcho MT8163 connectivity helpers",
    "license": "GPL-2.0-only",
    "compiler": compiler,
    "sources": {name: sha256(source_dir / name) for name in source_names},
    "outputs": {
        name: {
            "sha256": sha256(output_dir / name),
            "size": (output_dir / name).stat().st_size,
        }
        for name in output_names
    },
}
(output_dir / "connectivity-source.json").write_text(
    json.dumps(metadata, indent=2, sort_keys=True) + "\n"
)
PY

printf 'connectivity helpers rebuilt from checked-in GPL source\n'
