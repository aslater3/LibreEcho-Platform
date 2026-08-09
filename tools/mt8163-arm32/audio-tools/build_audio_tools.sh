#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: build_audio_tools.sh --archive FILE --output DIR --cross-prefix PREFIX --sysroot DIR --kernel-headers DIR'
}
ARCHIVE=
OUTPUT=
CROSS_PREFIX=
SYSROOT=
KERNEL_HEADERS=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    --cross-prefix) shift; (($#)) || { usage >&2; exit 2; }; CROSS_PREFIX=$1 ;;
    --sysroot) shift; (($#)) || { usage >&2; exit 2; }; SYSROOT=$1 ;;
    --kernel-headers) shift; (($#)) || { usage >&2; exit 2; }; KERNEL_HEADERS=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" && -n "$CROSS_PREFIX" && -n "$SYSROOT" && -n "$KERNEL_HEADERS" ]] || { usage >&2; exit 2; }
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || { echo "ERROR: unsafe TinyALSA archive" >&2; exit 1; }
[[ -x "${CROSS_PREFIX}gcc" ]] || { echo "ERROR: target compiler is unavailable" >&2; exit 1; }
[[ -f "$SYSROOT/usr/include/errno.h" ]] || { echo "ERROR: target sysroot is unavailable" >&2; exit 1; }
[[ -f "$KERNEL_HEADERS/include/uapi/linux/ioctl.h" && -f "$KERNEL_HEADERS/Makefile" ]] || {
  echo "ERROR: Linux ARM UAPI headers are unavailable" >&2
  exit 1
}

script_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
lock="$script_dir/SOURCE.lock"
patch_file="$script_dir/tinyalsa-mt8163.patch"
expected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$lock")"
actual="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
[[ "$actual" == "$expected" ]] || {
  echo "ERROR: TinyALSA source hash mismatch: expected=$expected actual=$actual" >&2
  exit 1
}
[[ -f "$patch_file" && ! -L "$patch_file" ]] || { echo "ERROR: TinyALSA MT8163 patch is unavailable" >&2; exit 1; }
expected_patch="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["patch_sha256"])' "$lock")"
actual_patch="$(sha256sum "$patch_file" | awk '{print $1}')"
[[ "$actual_patch" == "$expected_patch" ]] || { echo "ERROR: TinyALSA MT8163 patch hash mismatch" >&2; exit 1; }

OUTPUT="$(mkdir -p "$OUTPUT" && cd -- "$OUTPUT" && pwd -P)"
work="$(mktemp -d "${TMPDIR:-/tmp}/libreecho-tinyalsa-build.XXXXXX")"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
src="$work/tinyalsa"
mkdir -p "$src"
tar -xf "$ARCHIVE" -C "$src" --strip-components=1
patch --batch --forward --fuzz=0 -p1 -d "$src" < "$patch_file" >/dev/null
uapi="$work/kernel-uapi"
make -s -C "$KERNEL_HEADERS" ARCH=arm INSTALL_HDR_PATH="$uapi" headers_install
[[ -f "$uapi/include/linux/ioctl.h" && -f "$uapi/include/asm/ioctl.h" ]] || {
  echo "ERROR: Linux ARM UAPI header installation failed" >&2
  exit 1
}

cc_dir="$(cd -- "$(dirname -- "${CROSS_PREFIX}gcc")" && pwd -P)"
host_usr="$(cd -- "$cc_dir/.." && pwd -P)"
host_root="$(cd -- "$host_usr/.." && pwd -P)"
host_library_path="$host_usr/lib:$host_root/lib"
wrapper_prefix="$work/cross/armv7-"
mkdir -p "$(dirname "$wrapper_prefix")"
for tool in gcc ar ranlib; do
  real="${CROSS_PREFIX}${tool}"
  [[ -x "$real" ]] || { echo "ERROR: target tool is unavailable: $real" >&2; exit 1; }
  wrapper="${wrapper_prefix}${tool}"
  python3 - "$wrapper" "$real" "$host_library_path" "$SYSROOT" "$tool" <<'PY'
import pathlib, shlex, sys
out, real, library_path, sysroot, tool = sys.argv[1:]
extra = " --sysroot=" + shlex.quote(sysroot) if tool == "gcc" else ""
pathlib.Path(out).write_text(
    "#!/bin/sh\nexport LD_LIBRARY_PATH=" + shlex.quote(library_path) +
    "\nexec " + shlex.quote(real) + extra + " \"$@\"\n"
)
pathlib.Path(out).chmod(0o755)
PY
done

export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1704067200}"
export LC_ALL=C
export TZ=UTC
common_cflags="-Os -Wno-cpp -Wno-error -I$src/include -I$uapi/include -ffile-prefix-map=$work=/usr/src/tinyalsa -fdebug-prefix-map=$work=/usr/src/tinyalsa -fmacro-prefix-map=$work=/usr/src/tinyalsa"
make -C "$src/src" -j"${LIBREECHO_BUILD_JOBS:-2}" \
  TINYALSA_VERSION=1.1.1 TINYALSA_VERSION_MAJOR=1 \
  CROSS_COMPILE="$wrapper_prefix" CFLAGS="$common_cflags" libtinyalsa.a >/dev/null
make -C "$src/utils" -j"${LIBREECHO_BUILD_JOBS:-2}" \
  TINYALSA_VERSION=1.1.1 TINYALSA_VERSION_MAJOR=1 \
  CROSS_COMPILE="$wrapper_prefix" CFLAGS="$common_cflags" \
  LDFLAGS='-static -no-pie -Wl,--build-id=none' \
  tinyplay tinycap tinymix >/dev/null

for name in tinyplay tinycap tinymix; do
  install -m 0755 "$src/utils/$name" "$OUTPUT/$name"
  readelf_output="$(readelf -h -l -d "$OUTPUT/$name")"
  grep -Eq 'Class:[[:space:]]+ELF32' <<<"$readelf_output"
  grep -Eq 'Machine:[[:space:]]+ARM' <<<"$readelf_output"
  ! grep -q 'Requesting program interpreter' <<<"$readelf_output"
  ! grep -q 'NEEDED' <<<"$readelf_output"
  if strings "$OUTPUT/$name" | grep -Eq '/home/|libreecho-tinyalsa-build'; then
    echo "ERROR: $name contains a private or volatile build path" >&2
    exit 1
  fi
done

compiler="$("${wrapper_prefix}gcc" --version | sed -n '1p')"
python3 - "$OUTPUT/tinyalsa-source.json" "$OUTPUT" "$compiler" "$patch_file" <<'PY'
import hashlib, json, pathlib, sys
out, root, compiler, patch_file = sys.argv[1:]
root = pathlib.Path(root)
sha256 = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
metadata = {
    "compiler": compiler,
    "license": "BSD-3-Clause",
    "outputs": {
        name: {"sha256": sha256(root / name), "size": (root / name).stat().st_size}
        for name in ("tinyplay", "tinycap", "tinymix")
    },
    "patch": "tinyalsa-mt8163.patch",
    "patch_sha256": sha256(pathlib.Path(patch_file)),
    "source_sha256": "dc75977453304fcce0b91cbfd2b27942641c93479f87898d230cdc440a042d4f",
    "source_url": "https://github.com/tinyalsa/tinyalsa/archive/e43025bbf702eb7dd8edd48c1eb50530c60f1de8.tar.gz",
    "version": "e43025bbf702eb7dd8edd48c1eb50530c60f1de8",
}
pathlib.Path(out).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
PY

printf 'tinyalsa tools rebuilt from pinned public source\n'
