#!/usr/bin/env python3
"""Build-wiring contract for the Sendspin shared engine.

`build_airplay.sh` links `audio_engine.c` into the target
`libreecho-audio-engine` ELF.  The engine now depends on two helper translation
units: the framed Sendspin sink (`audio_sink.c`) and the bounded output-timing
ledger (`audio_timing.c`).  If either object is dropped from the compile/link
command the ELF fails to link (undefined `le_audio_sink_*` /
`le_audio_timing_*`) or silently omits required behaviour, and the dedicated
`test_audio_engine_sendspin.py` regression never runs.

Why a static wiring assertion rather than executing the shell build: the real
`build_airplay.sh` pipeline needs four pinned source archives plus a staged
sysroot, so its exact compile/link commands cannot be exercised host-side.  The
wiring is therefore asserted against the script text, and -- when the pinned
ARM32 toolchain and TinyALSA tree are present -- the same compile/link is
additionally performed for real against the pinned `libtinyalsa.a`, proving the
helper objects cross-compile and the engine ELF links as ARM32.

Only the pinned toolchain and TinyALSA tree are read; no archives, no device,
no privileged operation.
"""

import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
BUILD_SCRIPT = SOURCE_DIR / "build_airplay.sh"

ENGINE_SOURCES = (
    "audio_engine.c",
    "audio_sink.c",
    "audio_timing.c",
    "audio_visualizer.c",
    "playback_status.c",
    "aec_reference.c",
)


def build_audio_components_body() -> str:
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = text.index("build_audio_components() {")
    body_start = text.index("\n", start) + 1
    lines = []
    for line in text[body_start:].splitlines():
        if line == "}":
            break
        lines.append(line)
    return "\n".join(lines)


def check_static_wiring() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    body = build_audio_components_body()
    lines = body.splitlines()
    problems = []
    for var in ("AUDIO_SINK_SOURCE", "AUDIO_TIMING_SOURCE"):
        if f"{var}=" not in text:
            problems.append(f"${var} is not declared")
        if f'[[ -f "${var}" ]]' not in text:
            problems.append(f"no fail-closed existence guard for ${var}")
        if not re.search(rf'-c\s+"\${var}"', body):
            problems.append(f"${var} is not compiled in build_audio_components")
    for obj in ("audio_sink.o", "audio_timing.o"):
        if f"objects/{obj}" not in body:
            problems.append(f"{obj} is not compiled in build_audio_components")
    link_start = next(
        (i for i, line in enumerate(lines)
         if line.strip().startswith('"$CC" $bridge_cflags "$objects/audio_engine.o"')),
        None)
    if link_start is None:
        problems.append("engine link command not found in build_audio_components")
    else:
        link = "\n".join(lines[link_start:])
        for obj in ("audio_sink.o", "audio_timing.o"):
            if f"objects/{obj}" not in link:
                problems.append(f"{obj} missing from the engine link command")
    for name in ("test_audio_sink.py", "test_audio_timing.py",
                 "test_audio_engine_sendspin.py"):
        if f'python3 "$SCRIPT_DIR/{name}"' not in text:
            problems.append(f"{name} is not run by the build")
    for line in text.splitlines():
        if "test_audio_engine_sendspin.py" in line and line.strip() != \
                'python3 "$SCRIPT_DIR/test_audio_engine_sendspin.py"':
            problems.append(
                "test_audio_engine_sendspin.py is guarded/skipped: " + line.strip())
    if problems:
        raise SystemExit("build wiring contract failed: " + "; ".join(problems))
    print("build contract: sink/timing objects and tests wired into build_airplay.sh PASS")


def find_pinned_tinyalsa():
    explicit = os.environ.get("LE_AIRPLAY_TINYALSA_ROOT")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    base = Path("/mnt/old-samsung/tmp")
    if base.is_dir():
        candidates.extend(sorted(base.glob("*/tinyalsa-*")))
    for candidate in candidates:
        if ((candidate / "include/tinyalsa/pcm.h").is_file()
                and (candidate / "src/libtinyalsa.a").is_file()):
            return candidate
    return None


def check_arm_link() -> None:
    cc = Path(os.environ.get(
        "LE_AIRPLAY_ARM_CC", "/mnt/old-samsung/usr/bin/arm-linux-gnueabihf-gcc"))
    sysroot = Path(os.environ.get("LE_AIRPLAY_ARM_SYSROOT", "/mnt/old-samsung"))
    tinyalsa = find_pinned_tinyalsa()
    if not (cc.is_file() and os.access(cc, os.X_OK) and sysroot.is_dir() and tinyalsa):
        print(f"note: ARM toolchain/TinyALSA unavailable ({cc}), engine link check skipped")
        return
    with tempfile.TemporaryDirectory(prefix="le-engine-link-") as temp:
        root = Path(temp)
        objects = root / "objects"
        objects.mkdir()
        flags = [
            f"--sysroot={sysroot}", "-O2", "-std=c99",
            "-Wall", "-Wextra", "-Wpedantic",
            "-I", str(tinyalsa / "include"),
            "-I", str(sysroot / "usr/include/arm-linux-gnueabihf"),
            "-I", str(sysroot / "usr/include"),
        ]
        objs = []
        for source in ENGINE_SOURCES:
            obj = objects / (source[:-2] + ".o")
            subprocess.run([str(cc), *flags, "-c", str(SOURCE_DIR / source),
                            "-o", str(obj)], check=True, timeout=180)
            objs.append(str(obj))
        executable = root / "libreecho-audio-engine"
        subprocess.run([str(cc), *flags, *objs, str(tinyalsa / "src/libtinyalsa.a"),
                        "-ldl", "-lm", "-o", str(executable)], check=True, timeout=180)
        header = executable.read_bytes()[:20]
        if header[:4] != b"\x7fELF" or header[4] != 1:
            raise SystemExit("engine ELF is not 32-bit")
        if struct.unpack("<H", header[18:20])[0] != 40:
            raise SystemExit("engine ELF has unexpected e_machine")
        undefined = subprocess.run(
            ["/mnt/old-samsung/usr/bin/arm-linux-gnueabihf-nm", "-u", str(executable)],
            check=True, capture_output=True, text=True, timeout=60).stdout
        leaked = [line for line in undefined.splitlines()
                  if "le_audio" in line or line.strip().endswith(("pcm_open", "pcm_writei"))]
        if leaked:
            raise SystemExit("engine ELF has unresolved helper symbols: " + "; ".join(leaked))
        print("build contract: ARM32 engine ELF links against pinned TinyALSA PASS")


def main() -> None:
    check_static_wiring()
    check_arm_link()
    print("test_airplay_engine_build_contract_sendspin: PASS")


if __name__ == "__main__":
    main()
