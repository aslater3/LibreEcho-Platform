#!/usr/bin/env bash
#
# LibreEcho Sendspin — Task 2 SDK/ARM32 feasibility build.
#
# Builds the *real* pinned sendspin-cpp RC1 SDK and the LibreEcho adapter fixture
# (UI src/adapter/sendspin/CMakeLists.txt, UI tests/test_sendspin_sdk.cpp) on the
# host, cross-builds the same fixture for armhf against a *compile sysroot* that
# supplies the armhf glibc-2.39 development headers/objects, stages the project's
# reviewed ARMHF *runtime* closure from the pinned mdns packages, enforces
# resolved symbol versions, enforces the locked ELF NEEDED/loader closure, and
# executes the fixture under that exact reviewed loader.
#
# Compile-input provenance and runtime closure are deliberately kept separate.
# The ARMHF *compile sysroot* is the Product-staged, archive-backed cross
# toolchain prefix: its identity comes from the locked .deb bytes, checked by
# the Product materializer's `verify` before any target tool runs. It is a
# compile input — NOT the reviewed runtime closure (SOURCE.lock
# compile_sysroot.pinned is deliberately still false) and NOT a hermetic or
# cross-host reproducibility claim (the host loader/libc/tools stay external).
# The runtime the fixture actually resolves against is the pinned reviewed
# closure staged from the mdns .deb packages.
#
# Source identity is proven at byte level (see verify_sendspin_sources.py): every
# dependency is taken from the pinned upstream archive whose SHA-256 matches
# SOURCE.lock. Directory names and user-supplied revision strings are never
# trusted. No network access; no privileged install.
#
# Usage:
#   build_sendspin.sh <output-dir>
#   build_sendspin.sh --elf-closure <SOURCE.lock> <elf-report.txt> <out.json>
#       (internal subcommand: compare an ARM ELF report's NEEDED/loader against
#        the lock's runtime_requirements and fail closed on divergence; also
#        exercised directly by test_sdk_build.py)
#
# Required environment:
#   SENDSPIN_ARCHIVE_DIR      Product-staged pinned archives (name embeds commit)
#   UI_SENDSPIN_CMAKE_DIR     path to ui/src/adapter/sendspin
#
# Required for the ARM lane (unless SKIP_ARM=1; there is NO /usr or /mnt
# fallback — the ARM path fails closed without a verified staged prefix):
#   SENDSPIN_ARMHF_PREFIX     the Product-staged, archive-backed ARMHF cross
#                             toolchain prefix (becomes SYSROOT)
#   SENDSPIN_ARMHF_ARCHIVE_DIR  the locked .deb archive pool the prefix was
#                             materialized from (identity comes from these bytes)
#   SENDSPIN_PRODUCT_ROOT     the Product repository root providing
#                             build/ci/armhf_toolchain.py and
#                             build/inputs/armhf-cross-toolchain.lock.json; a
#                             required explicit Product/verifier input
#                             (SENDSPIN_ARMHF_TOOLCHAIN_MODULE and
#                             SENDSPIN_ARMHF_TOOLCHAIN_LOCK may be given instead)
#
# The build runs the Product verifier `verify --lock <lock> --archives <pool>
# --prefix <prefix>` and only after an archive-backed PASS derives, WITHOUT
# evaluating any `env` output:
#   SYSROOT        = <prefix>
#   CROSS_PREFIX   = <prefix>/usr/bin/arm-linux-gnueabihf-
#   LD_LIBRARY_PATH= <prefix>/usr/lib/x86_64-linux-gnu   (armhf compiler support libs)
# Caller-supplied SYSROOT/CROSS_PREFIX that conflict with the verified prefix are
# rejected; target-compiler search-path influences (CPATH, C_INCLUDE_PATH,
# CPLUS_INCLUDE_PATH, LIBRARY_PATH, GCC_EXEC_PREFIX, COMPILER_PATH) and an
# inherited loader path are sanitized for the ARM lane only.
#
# Optional environment:
#   SENDSPIN_STAGED_SOURCES_DIR  a Product-staged source tree to consume by path;
#                                its bytes are verified against the pinned archives
#                                first (a declared receipt is never trusted alone)
#   SENDSPIN_SOURCE_LOCK      SOURCE.lock path (default: alongside this script)
#   SENDSPIN_MDNS_LOCK        Product mdns-packages.lock.json (reviewed runtime closure)
#   SENDSPIN_MDNS_ARCHIVES_DIR  directory holding the pinned runtime .deb archives
#   SENDSPIN_ARMHF_TOOLCHAIN_MODULE  explicit Product verifier path (default derived)
#   SENDSPIN_ARMHF_TOOLCHAIN_LOCK    explicit Product lock path (default derived)
#   SENDSPIN_ARMHF_VERIFY_TIMEOUT    bound on the archive-backed verify step (default 900)
#   QEMU_ARM                  user-mode ARM emulator (required to execute the arm fixture)
#   JOBS                      parallel jobs (clamped to SENDSPIN_MAX_JOBS, default 8)
#   SKIP_ARM / SKIP_HOST      set to 1 to skip that half
#   KEEP_WORK                 set to 1 to keep the scratch work tree
#   SENDSPIN_{CONFIGURE,BUILD,TEST,RUN}_TIMEOUT  per-step timeouts in seconds
#
set -euo pipefail

PYTHON=${SENDSPIN_PYTHON:-python3}

run_elf_closure() {
    # args: <SOURCE.lock> <elf-report.txt> <out.json>
    # Enforce the ARM ELF interpreter + NEEDED set against SOURCE.lock's
    # runtime_requirements; write a receipt and exit non-zero on any divergence.
    "$PYTHON" - "$1" "$2" "$3" <<'PY'
import hashlib, json, re, sys
from pathlib import Path

lock_path, report_path, out_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
lock = json.loads(lock_path.read_text(encoding="utf-8"))
req = lock.get("runtime_requirements") or {}
expected_interp = req.get("interpreter")
expected_needed = list(req.get("needed") or [])
if not expected_interp or not expected_needed:
    print("ELF CLOSURE FAILURE: SOURCE.lock runtime_requirements "
          "(interpreter/needed) is missing", file=sys.stderr)
    raise SystemExit(2)

report = report_path.read_text(encoding="utf-8", errors="replace") if report_path.is_file() else ""
observed_needed = sorted({m for m in re.findall(r"Shared library: \[([^\]]+)\]", report)})
interp = re.search(r"Requesting program interpreter: ([^\]\s]+)", report)
observed_interp = interp.group(1) if interp else None

expected_set, observed_set = set(expected_needed), set(observed_needed)
extra = sorted(observed_set - expected_set)
missing = sorted(expected_set - observed_set)
match = (observed_set == expected_set) and (observed_interp == expected_interp)

receipt = {
    "schema": "libreecho-sendspin-elf-closure/1",
    "lock": str(lock_path),
    "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
    "interpreter_expected": expected_interp,
    "interpreter_observed": observed_interp,
    "needed_expected": sorted(expected_set),
    "needed_observed": observed_needed,
    "extra_needed": extra,
    "missing_needed": missing,
    "match": bool(match),
}
out_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

if not match:
    if observed_interp != expected_interp:
        print(f"ELF CLOSURE FAILURE: interpreter {observed_interp!r} != "
              f"locked {expected_interp!r}", file=sys.stderr)
    if missing:
        print(f"ELF CLOSURE FAILURE: missing locked NEEDED {missing}", file=sys.stderr)
    if extra:
        print(f"ELF CLOSURE FAILURE: unexpected NEEDED {extra}", file=sys.stderr)
    raise SystemExit(1)
print(f"ELF CLOSURE OK: needed={sorted(expected_set)} interpreter={expected_interp}")
raise SystemExit(0)
PY
}

if [[ "${1:-}" == "--elf-closure" ]]; then
    [[ $# -eq 4 ]] || {
        echo "usage: $0 --elf-closure <SOURCE.lock> <elf-report.txt> <out.json>" >&2
        exit 2; }
    set +e
    run_elf_closure "$2" "$3" "$4"
    rc=$?
    set -e
    exit "$rc"
fi

armhf_prefix_is_cmake_safe() {
    # The generated arm-toolchain.cmake quotes every path value, so an ordinary
    # space and a literal '$' are supported.  Characters CMake cannot represent
    # in a quoted set() argument are refused here, at the gate and before any
    # target tool runs:
    #   ;   splits a quoted value into a list
    #   "   closes the quoted string
    #   \   is an escape character
    #   newline/CR breaks the statement
    # and these sequences would be *evaluated* rather than taken literally:
    #   ${...}  $<...>  $ENV{...}  $CACHE{...}
    local p=$1 bad c
    if [[ "$p" == *$'\n'* || "$p" == *$'\r'* ]]; then
        echo "ERROR: SENDSPIN_ARMHF_PREFIX must not contain a newline or carriage return." >&2
        return 1
    fi
    for c in ';' '"' '\'; do
        if [[ "$p" == *"$c"* ]]; then
            echo "ERROR: SENDSPIN_ARMHF_PREFIX contains a character CMake cannot" >&2
            echo "       represent in a quoted set() value ('$c'): $p" >&2
            return 1
        fi
    done
    for bad in '${' '$<' '$ENV{' '$CACHE{'; do
        if [[ "$p" == *"$bad"* ]]; then
            echo "ERROR: SENDSPIN_ARMHF_PREFIX contains a CMake expansion sequence" >&2
            echo "       ($bad) that would be evaluated, not taken literally: $p" >&2
            return 1
        fi
    done
    return 0
}

armhf_toolchain_gate() {
    # Consume the Product *reviewed* staged ARMHF toolchain prefix. Identity is
    # proven from the locked archive bytes by the Product materializer's verify
    # BEFORE any target compiler/binutils runs. Nothing here evaluates `env`
    # output or trusts a receipt / version string / path name on its own.
    if [[ -z "$SENDSPIN_ARMHF_PREFIX" ]]; then
        echo "ERROR: SENDSPIN_ARMHF_PREFIX is required (the Product-staged," >&2
        echo "       archive-backed ARMHF cross toolchain prefix). The ARM lane has no" >&2
        echo "       /usr or /mnt fallback and fails closed without a verified prefix." >&2
        return 1
    fi
    if [[ -z "$SENDSPIN_ARMHF_ARCHIVE_DIR" ]]; then
        echo "ERROR: SENDSPIN_ARMHF_ARCHIVE_DIR is required (the locked .deb archive" >&2
        echo "       pool the prefix is materialized from); the verifier needs the bytes." >&2
        return 1
    fi
    local module="$SENDSPIN_ARMHF_TOOLCHAIN_MODULE"
    local lock="$SENDSPIN_ARMHF_TOOLCHAIN_LOCK"
    if [[ -z "$module" || -z "$lock" ]]; then
        if [[ -z "$SENDSPIN_PRODUCT_ROOT" ]]; then
            echo "ERROR: SENDSPIN_PRODUCT_ROOT (Product repository root) is required, or an" >&2
            echo "       explicit SENDSPIN_ARMHF_TOOLCHAIN_MODULE and SENDSPIN_ARMHF_TOOLCHAIN_LOCK." >&2
            return 1
        fi
        module="${module:-$SENDSPIN_PRODUCT_ROOT/build/ci/armhf_toolchain.py}"
        lock="${lock:-$SENDSPIN_PRODUCT_ROOT/build/inputs/armhf-cross-toolchain.lock.json}"
    fi

    [[ "$SENDSPIN_ARMHF_PREFIX" == /* ]] || {
        echo "ERROR: SENDSPIN_ARMHF_PREFIX must be an absolute path: $SENDSPIN_ARMHF_PREFIX" >&2
        return 1; }
    if [[ -L "$SENDSPIN_ARMHF_PREFIX" || ! -d "$SENDSPIN_ARMHF_PREFIX" ]]; then
        echo "ERROR: SENDSPIN_ARMHF_PREFIX is not a real (non-symlink) directory:" >&2
        echo "       $SENDSPIN_ARMHF_PREFIX" >&2
        return 1
    fi
    # Refuse path characters CMake cannot represent in a quoted set() value,
    # before the verifier writes anything or any target tool runs.
    armhf_prefix_is_cmake_safe "$SENDSPIN_ARMHF_PREFIX" || return 1
    [[ -f "$module" ]] || { echo "ERROR: Product toolchain verifier not found: $module" >&2; return 1; }
    [[ -f "$lock" ]] || { echo "ERROR: Product toolchain lock not found: $lock" >&2; return 1; }

    # A caller may not redirect the build to a different sysroot/toolchain.
    if [[ -n "$SYSROOT" && "$SYSROOT" != "$SENDSPIN_ARMHF_PREFIX" ]]; then
        echo "ERROR: caller SYSROOT=$SYSROOT conflicts with the verified ARMHF prefix" >&2
        echo "       SENDSPIN_ARMHF_PREFIX=$SENDSPIN_ARMHF_PREFIX" >&2
        return 1
    fi
    local expected_cross="$SENDSPIN_ARMHF_PREFIX/usr/bin/arm-linux-gnueabihf-"
    if [[ -n "$CROSS_PREFIX" && "$CROSS_PREFIX" != "$expected_cross" ]]; then
        echo "ERROR: caller CROSS_PREFIX=$CROSS_PREFIX conflicts with the verified ARMHF" >&2
        echo "       prefix toolchain $expected_cross" >&2
        return 1
    fi

    echo "== verify Product ARMHF toolchain prefix (archive-backed) =="
    if ! timeout "${SENDSPIN_ARMHF_VERIFY_TIMEOUT:-900}" "$PYTHON" "$module" verify \
            --lock "$lock" --archives "$SENDSPIN_ARMHF_ARCHIVE_DIR" \
            --prefix "$SENDSPIN_ARMHF_PREFIX" > "$OUTPUT/armhf-toolchain-verify.log" 2>&1; then
        cat "$OUTPUT/armhf-toolchain-verify.log" >&2
        echo "ERROR: the Product archive-backed ARMHF toolchain prefix failed verification" >&2
        echo "       (fail closed). No target compiler is run against an unverified prefix." >&2
        return 1
    fi
    if ! grep -q -- '^armhf_toolchain_verify=PASS ' "$OUTPUT/armhf-toolchain-verify.log"; then
        cat "$OUTPUT/armhf-toolchain-verify.log" >&2
        echo "ERROR: the Product verifier did not report a PASS line; refusing the prefix." >&2
        return 1
    fi
    grep -- '^armhf_toolchain_verify=PASS ' "$OUTPUT/armhf-toolchain-verify.log" >&2 || true

    # Derive the consumer contract from the *just-verified* prefix (no eval).
    ARM_SYSROOT="$SENDSPIN_ARMHF_PREFIX"
    ARM_CROSS_PREFIX="$expected_cross"
    ARM_LD_LIBRARY_PATH="$SENDSPIN_ARMHF_PREFIX/usr/lib/x86_64-linux-gnu"

    local tool
    for tool in gcc g++ as ld ar ranlib nm objcopy objdump strip readelf; do
        if [[ ! -x "${ARM_CROSS_PREFIX}${tool}" ]]; then
            echo "ERROR: verified prefix is missing an executable target tool:" >&2
            echo "       ${ARM_CROSS_PREFIX}${tool}" >&2
            return 1
        fi
    done
    return 0
}

OUTPUT=${1:?output directory}
: "${SENDSPIN_ARCHIVE_DIR:?Product-staged pinned archives directory (SOURCE.lock identity)}"
: "${UI_SENDSPIN_CMAKE_DIR:?path to UI src/adapter/sendspin}"

HERE=$(cd -- "$(dirname -- "$0")" && pwd -P)
SENDSPIN_SOURCE_LOCK=${SENDSPIN_SOURCE_LOCK:-$HERE/SOURCE.lock}
# No implicit cross-toolchain default: the ARM lane consumes the verified staged
# prefix below. These two are only read to *reject* a conflicting caller value.
CROSS_PREFIX=${CROSS_PREFIX:-}
SYSROOT=${SYSROOT:-}
CXX=${CXX:-g++}
CC=${CC:-gcc}
QEMU_ARM=${QEMU_ARM:-}
SKIP_ARM=${SKIP_ARM:-0}
SKIP_HOST=${SKIP_HOST:-0}
KEEP_WORK=${KEEP_WORK:-0}
# The ARMHF staged-prefix consumer interface (see the header for the contract).
SENDSPIN_PRODUCT_ROOT=${SENDSPIN_PRODUCT_ROOT:-}
SENDSPIN_ARMHF_TOOLCHAIN_MODULE=${SENDSPIN_ARMHF_TOOLCHAIN_MODULE:-}
SENDSPIN_ARMHF_TOOLCHAIN_LOCK=${SENDSPIN_ARMHF_TOOLCHAIN_LOCK:-}
SENDSPIN_ARMHF_PREFIX=${SENDSPIN_ARMHF_PREFIX:-}
SENDSPIN_ARMHF_ARCHIVE_DIR=${SENDSPIN_ARMHF_ARCHIVE_DIR:-}
# Derived only after an archive-backed verify PASS.
ARM_SYSROOT=
ARM_CROSS_PREFIX=
ARM_LD_LIBRARY_PATH=

# Bound parallel jobs and every external step so a stall cannot wedge the run.
JOBS_MAX=${SENDSPIN_MAX_JOBS:-8}
JOBS=${JOBS:-$(nproc)}
(( JOBS > JOBS_MAX )) && JOBS=$JOBS_MAX
CONFIGURE_TIMEOUT=${SENDSPIN_CONFIGURE_TIMEOUT:-600}
BUILD_TIMEOUT=${SENDSPIN_BUILD_TIMEOUT:-1800}
TEST_TIMEOUT=${SENDSPIN_TEST_TIMEOUT:-600}
RUN_TIMEOUT=${SENDSPIN_RUN_TIMEOUT:-300}

[[ -f "$SENDSPIN_SOURCE_LOCK" ]] || { echo "ERROR: SOURCE.lock not found: $SENDSPIN_SOURCE_LOCK" >&2; exit 1; }
[[ -d "$SENDSPIN_ARCHIVE_DIR" ]] || { echo "ERROR: staged archive dir missing: $SENDSPIN_ARCHIVE_DIR" >&2; exit 1; }
[[ -f "$UI_SENDSPIN_CMAKE_DIR/CMakeLists.txt" ]] || {
    echo "ERROR: UI adapter CMakeLists not found under: $UI_SENDSPIN_CMAKE_DIR" >&2; exit 1; }
[[ -f "$UI_SENDSPIN_CMAKE_DIR/../../../tests/test_sendspin_sdk.cpp" ]] || {
    echo "ERROR: UI fixture tests/test_sendspin_sdk.cpp not found relative to $UI_SENDSPIN_CMAKE_DIR" >&2
    exit 1; }
[[ ! -e "$OUTPUT" ]] || { echo "ERROR: refusing to overwrite output: $OUTPUT" >&2; exit 1; }
command -v cmake >/dev/null 2>&1 || { echo "ERROR: cmake is required" >&2; exit 1; }
command -v "$PYTHON" >/dev/null 2>&1 || { echo "ERROR: python3 is required" >&2; exit 1; }
command -v "$CXX" >/dev/null 2>&1 || { echo "ERROR: host C++ compiler '$CXX' not found" >&2; exit 1; }

mkdir -p "$OUTPUT"
work="$OUTPUT/work"
cleanup() {
    if [[ "$KEEP_WORK" != 1 ]]; then rm -rf "$work"; fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# 0. ARMHF toolchain: verify the Product staged prefix from the locked archive
#    bytes BEFORE any target compiler/binutils runs (fail closed; no fallback).
# ---------------------------------------------------------------------------
if [[ "$SKIP_ARM" != "1" ]]; then
    armhf_toolchain_gate
fi

# ---------------------------------------------------------------------------
# 1. Provenance: verify pinned source identity, regenerate verified sources.
# ---------------------------------------------------------------------------
echo "== verify + stage pinned sources =="
if [[ -n "${SENDSPIN_STAGED_SOURCES_DIR:-}" ]]; then
    # Consume a Product-staged tree *by path*: confirm its bytes equal the pinned
    # archives before use. A declared receipt is never trusted on its own.
    "$PYTHON" "$HERE/verify_sendspin_sources.py" --lock "$SENDSPIN_SOURCE_LOCK" \
        --archive-dir "$SENDSPIN_ARCHIVE_DIR" --compare-tree "$SENDSPIN_STAGED_SOURCES_DIR" \
        --compare-work "$work/compare" --map-json "$work/source-map.json" \
        --patch-dir "$HERE/patches" \
        > "$OUTPUT/source-stage.log" 2>&1
    map_resolve() {
        "$PYTHON" - "$work/source-map.json" "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["entries"][sys.argv[2]])
PY
    }
    SDK_DIR=$(map_resolve "identity::sdk")
    AJ_DIR=$(map_resolve "ArduinoJson")
    MF_DIR=$(map_resolve "micro-flac")
    IX_DIR=$(map_resolve "IXWebSocket")
    NC_DIR=$(map_resolve "noise-c")
else
    "$PYTHON" "$HERE/verify_sendspin_sources.py" --lock "$SENDSPIN_SOURCE_LOCK" \
        --archive-dir "$SENDSPIN_ARCHIVE_DIR" --stage-out "$work/sources" \
        --receipt "$work/stage-receipt.json" --patch-dir "$HERE/patches" \
        > "$OUTPUT/source-stage.log" 2>&1
    SDK_DIR="$work/sources/identity/sdk"
    AJ_DIR="$work/sources/ArduinoJson"
    MF_DIR="$work/sources/micro-flac"
    IX_DIR="$work/sources/IXWebSocket"
    NC_DIR="$work/sources/noise-c"
fi
for d in "$SDK_DIR" "$AJ_DIR" "$MF_DIR" "$IX_DIR" "$NC_DIR"; do
    [[ -d "$d" ]] || { echo "ERROR: verified source directory missing after staging: $d" >&2; exit 1; }
done
[[ -f "$MF_DIR/lib/micro-ogg-demuxer/CMakeLists.txt" ]] || {
    echo "ERROR: verified micro-ogg-demuxer submodule is not populated" >&2; exit 1; }

hook_args=(
    "-DSENDSPIN_SDK_DIR=$SDK_DIR"
    "-DSENDSPIN_ARDUINOJSON_DIR=$AJ_DIR"
    "-DSENDSPIN_MICRO_FLAC_DIR=$MF_DIR"
    "-DSENDSPIN_IXWEBSOCKET_DIR=$IX_DIR"
    "-DSENDSPIN_NOISE_C_DIR=$NC_DIR"
)

# ---------------------------------------------------------------------------
# 2. Host fixture
# ---------------------------------------------------------------------------
if [[ "$SKIP_HOST" != "1" ]]; then
    echo "== host configure =="
    timeout "$CONFIGURE_TIMEOUT" cmake -S "$UI_SENDSPIN_CMAKE_DIR" -B "$work/host" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
        "${hook_args[@]}" > "$OUTPUT/host-configure.log" 2>&1
    echo "== host build =="
    timeout "$BUILD_TIMEOUT" cmake --build "$work/host" --target sendspin_adapter_tests \
        -j "$JOBS" > "$OUTPUT/host-build.log" 2>&1
    echo "== host ctest =="
    timeout "$TEST_TIMEOUT" ctest --test-dir "$work/host" --output-on-failure \
        > "$OUTPUT/host-ctest.log" 2>&1
    timeout "$RUN_TIMEOUT" "$work/host/sendspin_adapter_tests" > "$OUTPUT/host-fixture.log" 2>&1
    cp "$work/host/sendspin_adapter_tests" "$OUTPUT/sendspin_adapter_tests.host"
    grep -E 'playback\]|partial\]|all checks passed|FAIL' "$OUTPUT/host-fixture.log" >&2 || true
fi

# ---------------------------------------------------------------------------
# 3. armhf fixture: verified staged prefix + enforced ELF closure + reviewed runtime
# ---------------------------------------------------------------------------
if [[ "$SKIP_ARM" != "1" ]]; then
    # The staged prefix + tool paths were archive-backed verified in step 0.
    # Sanitize the *target* lane only: drop inherited header/compiler search
    # paths and set the loader path explicitly to the verified prefix support
    # libs, so an inherited loader path cannot bypass the reviewed toolchain.
    # The host lane above keeps its ambient environment untouched.
    arm_env=(env -u CPATH -u C_INCLUDE_PATH -u CPLUS_INCLUDE_PATH -u LIBRARY_PATH \
        -u GCC_EXEC_PREFIX -u COMPILER_PATH "LD_LIBRARY_PATH=$ARM_LD_LIBRARY_PATH")

    echo "== validate compile sysroot (glibc bound) =="
    "$PYTHON" - "$ARM_SYSROOT" "$HERE" <<'PY' | tee "$OUTPUT/sysroot-check.log"
import pathlib, sys
sys.path.insert(0, sys.argv[2])
import verify_sendspin_runtime as v
# The compile sysroot is a compile input, not the reviewed runtime closure: this
# only bounds its glibc to the reviewed maximum.
print("compile sysroot:", v.assert_reviewed_glibc(pathlib.Path(sys.argv[1])))
PY

    echo "== probe compile sysroot linkability =="
    probe_src="$work/sysroot-probe.c"
    probe_elf="$work/sysroot-probe.elf"
    printf 'int main(void){return 0;}\n' > "$probe_src"
    if ! timeout "$CONFIGURE_TIMEOUT" "${arm_env[@]}" "${ARM_CROSS_PREFIX}gcc" \
            --sysroot="$ARM_SYSROOT" "$probe_src" -o "$probe_elf" \
            > "$OUTPUT/compile-sysroot-probe.log" 2>&1; then
        cat "$OUTPUT/compile-sysroot-probe.log" >&2
        echo "ERROR: the verified ARMHF prefix is not a usable compile sysroot for" >&2
        echo "       ${ARM_CROSS_PREFIX}gcc (SYSROOT=$ARM_SYSROOT)." >&2
        exit 1
    fi

    toolchain="$work/arm-toolchain.cmake"
    {
        echo "set(CMAKE_SYSTEM_NAME Linux)"
        echo "set(CMAKE_SYSTEM_PROCESSOR arm)"
        # Quote every derived path: CMake parses an unquoted value containing a
        # space (or a ';') as a *list*, so the compiler/binutils path would no
        # longer be a full path.  --sysroot is supplied through CMAKE_SYSROOT
        # (which CMake quotes correctly) instead of being embedded in a flags
        # string that would split on a spaced sysroot.
        echo "set(CMAKE_C_COMPILER \"${ARM_CROSS_PREFIX}gcc\")"
        echo "set(CMAKE_CXX_COMPILER \"${ARM_CROSS_PREFIX}g++\")"
        # Absolute just-verified binutils paths (no PATH/COMPILER_PATH search).
        echo "set(CMAKE_AR \"${ARM_CROSS_PREFIX}ar\")"
        echo "set(CMAKE_RANLIB \"${ARM_CROSS_PREFIX}ranlib\")"
        echo "set(CMAKE_NM \"${ARM_CROSS_PREFIX}nm\")"
        echo "set(CMAKE_OBJCOPY \"${ARM_CROSS_PREFIX}objcopy\")"
        echo "set(CMAKE_OBJDUMP \"${ARM_CROSS_PREFIX}objdump\")"
        echo "set(CMAKE_STRIP \"${ARM_CROSS_PREFIX}strip\")"
        echo "set(CMAKE_SYSROOT \"${ARM_SYSROOT}\")"
        echo "set(CMAKE_FIND_ROOT_PATH \"${ARM_SYSROOT}\")"
        echo "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)"
        echo "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)"
        echo "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)"
        echo "set(CMAKE_C_FLAGS_INIT \"-O2\")"
        echo "set(CMAKE_CXX_FLAGS_INIT \"-O2\")"
        # Bind the C++ runtime statically so the *only* dynamic closure is the
        # reviewed glibc/loader (libstdc++/libgcc are not in the mdns runtime).
        echo "set(CMAKE_EXE_LINKER_FLAGS_INIT \"-static-libstdc++ -static-libgcc -Wl,--no-undefined\")"
    } > "$toolchain"

    echo "== armhf configure =="
    timeout "$CONFIGURE_TIMEOUT" "${arm_env[@]}" cmake -S "$UI_SENDSPIN_CMAKE_DIR" -B "$work/arm" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_TOOLCHAIN_FILE="$toolchain" "${hook_args[@]}" \
        > "$OUTPUT/arm-configure.log" 2>&1
    echo "== armhf build =="
    timeout "$BUILD_TIMEOUT" "${arm_env[@]}" cmake --build "$work/arm" --target sendspin_adapter_tests \
        -j "$JOBS" > "$OUTPUT/arm-build.log" 2>&1
    cp "$work/arm/sendspin_adapter_tests" "$OUTPUT/sendspin_adapter_tests.armhf"

    readelf_bin="${ARM_CROSS_PREFIX}readelf"
    {
        echo "## file"; file "$OUTPUT/sendspin_adapter_tests.armhf"
        echo "## readelf -h"; "${arm_env[@]}" "$readelf_bin" -h "$OUTPUT/sendspin_adapter_tests.armhf"
        echo "## readelf -l (interpreter)"; "${arm_env[@]}" "$readelf_bin" -l "$OUTPUT/sendspin_adapter_tests.armhf" | grep -i interpreter || true
        echo "## readelf -d (NEEDED)"; "${arm_env[@]}" "$readelf_bin" -d "$OUTPUT/sendspin_adapter_tests.armhf" | grep -E 'NEEDED|SONAME' || true
        echo "## size"; stat -c '%s bytes' "$OUTPUT/sendspin_adapter_tests.armhf"
    } > "$OUTPUT/elf-report.txt" 2>&1
    grep -E 'Class:|Machine:|interpreter|NEEDED' "$OUTPUT/elf-report.txt" >&2 || true

    # ---- enforce the locked ELF NEEDED/loader closure (fail closed) -------
    echo "== enforce locked ELF NEEDED closure =="
    if run_elf_closure "$SENDSPIN_SOURCE_LOCK" "$OUTPUT/elf-report.txt" \
            "$OUTPUT/elf-closure.json" > "$OUTPUT/elf-closure.log" 2>&1; then
        cat "$OUTPUT/elf-closure.log" >&2
        echo "elf_closure=pass" >> "$OUTPUT/elf-report.txt"
    else
        cat "$OUTPUT/elf-closure.log" >&2
        echo "ERROR: ARM ELF NEEDED/loader does not match SOURCE.lock runtime_requirements (fail closed)." >&2
        exit 1
    fi

    # ---- reviewed runtime closure + resolved symbol versions --------------
    if [[ -n "${SENDSPIN_MDNS_LOCK:-}" && -n "${SENDSPIN_MDNS_ARCHIVES_DIR:-}" ]]; then
        echo "== reviewed runtime closure + symbol versions =="
        runtime_args=(
            --lock "$SENDSPIN_MDNS_LOCK"
            --archives "$SENDSPIN_MDNS_ARCHIVES_DIR"
            --stage-out "$work/runtime-root"
            --packages libc6,libgcc-s1
            --readelf "$readelf_bin"
            --binary "$OUTPUT/sendspin_adapter_tests.armhf"
            --timeout "$RUN_TIMEOUT"
        )
        if [[ -n "$QEMU_ARM" && -x "$QEMU_ARM" ]]; then
            runtime_args+=(--qemu "$QEMU_ARM")
        fi
        "${arm_env[@]}" "$PYTHON" "$HERE/verify_sendspin_runtime.py" "${runtime_args[@]}" \
            > "$OUTPUT/runtime-closure.log" 2>&1
        cat "$OUTPUT/runtime-closure.log" >&2
        echo "symbol_closure=pass" >> "$OUTPUT/elf-report.txt"
    else
        echo "ERROR: reviewed runtime inputs absent; cannot prove ARM closure." >&2
        echo "       set SENDSPIN_MDNS_LOCK and SENDSPIN_MDNS_ARCHIVES_DIR." >&2
        exit 1
    fi

    # ---- execute the fixture under the exact reviewed loader --------------
    if [[ -n "$QEMU_ARM" && -x "$QEMU_ARM" ]]; then
        "${arm_env[@]}" "$PYTHON" - "$HERE" "$work/runtime-root" "$OUTPUT/sendspin_adapter_tests.armhf" "$QEMU_ARM" "$RUN_TIMEOUT" <<'PY' \
            > "$OUTPUT/qemu-smoke.log" 2>&1
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import verify_sendspin_runtime as v
rc, out = v.run_under_reviewed_loader(pathlib.Path(sys.argv[4]), pathlib.Path(sys.argv[2]),
                                      pathlib.Path(sys.argv[3]), int(sys.argv[5]))
sys.stdout.write(out)
raise SystemExit(rc)
PY
        echo "qemu_smoke=pass" >> "$OUTPUT/elf-report.txt"
        grep -E 'playback\]|partial\]|all checks passed' "$OUTPUT/qemu-smoke.log" >&2 || true
    else
        echo "qemu_smoke=skipped (no emulator supplied via QEMU_ARM)" >&2
        echo "qemu_smoke=skipped" >> "$OUTPUT/elf-report.txt"
    fi
fi

echo "== build_sendspin: OK =="
