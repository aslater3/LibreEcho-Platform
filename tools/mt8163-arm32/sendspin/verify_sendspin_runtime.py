#!/usr/bin/env python3
"""LibreEcho Sendspin — reviewed ARMHF runtime closure staging + enforcement.

The Task 2 ARM bar is closure through the *reviewed* LibreEcho ARMHF runtime
(Product ``build/inputs/mdns-packages.lock``), not merely an arbitrary modern
libc. This module:

  * stages the reviewed runtime root rootlessly from the pinned ``.deb`` closure
    (each archive's SHA-256 is checked against the package lock before extraction),
  * asserts the staged glibc is the reviewed version (no 2.4x ad-hoc sysroot),
  * and enforces resolved symbol *versions*: every versioned symbol the fixture
    requires must actually be *defined* in the staged runtime libraries, so a
    max-label comparison or a host ``ldd`` cannot pass a broken closure.

Run the fixture under the staged loader with ``--run`` to prove resolution with
the exact reviewed interpreter. No network, no privileged install.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REVIEWED_MAX_GLIBC = (2, 39)
SONAME_RE = re.compile(r"^libc\.so\.6$|^libm\.so\.6$|^libpthread\.so\.0$|^libdl\.so\.2$"
                       r"|^librt\.so\.1$|^libgcc_s\.so\.1$")


class RuntimeVerificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_package_lock(path: Path) -> list[dict]:
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    packages = data["packages"] if isinstance(data, dict) else data
    for record in packages:
        for key in ("package", "file", "sha256"):
            if not record.get(key):
                raise RuntimeVerificationError(f"package lock record missing {key}: {record}")
    return packages


def verify_and_stage(lock: list[dict], archives_dir: Path, out_root: Path,
                     packages: list[str]) -> dict[str, str]:
    """Extract the requested reviewed packages after verifying each archive hash."""
    by_name = {record["package"]: record for record in lock}
    staged: dict[str, str] = {}
    for name in packages:
        record = by_name.get(name)
        if record is None:
            raise RuntimeVerificationError(f"{name} is not pinned in the reviewed runtime lock")
        archive = Path(archives_dir) / record["file"]
        if not archive.is_file():
            raise RuntimeVerificationError(f"reviewed runtime archive missing: {archive}")
        actual = sha256_file(archive)
        if actual != record["sha256"]:
            raise RuntimeVerificationError(
                f"{name}: archive sha256 {actual} != pinned {record['sha256']}")
        subprocess.run(["dpkg-deb", "-x", str(archive), str(out_root)], check=True,
                       timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        staged[name] = actual
    return staged


_NON_TARGET_ARCH_DIRS = ("x86_64-linux-gnu", "aarch64-linux-gnu", "x86_64", "aarch64",
                        "arm64", "arm-linux-gnueabi")


def find_library(root: Path, soname: str) -> Path:
    """Locate an armhf runtime library, never a host-arch library of the same name.

    Standard sysroot/multiarch locations are searched first, then the gcc-cross
    sysroot layout; a bounded rglob is the last resort. This avoids matching an
    unrelated nested copy inside a very large sysroot tree.
    """
    root = Path(root)
    priority = [
        root / "usr/lib/arm-linux-gnueabihf" / soname,
        root / "lib/arm-linux-gnueabihf" / soname,
        root / "usr/lib" / soname,
        root / "lib" / soname,
        root / "usr/arm-linux-gnueabihf/lib" / soname,
    ]
    for candidate in priority:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    candidates = [
        p for p in sorted(root.rglob(soname))
        if p.is_file() and not p.is_symlink()
        and not any(part in _NON_TARGET_ARCH_DIRS for part in p.parts)
    ]
    if not candidates:
        raise RuntimeVerificationError(f"{soname} not present in staged runtime root {root}")
    tuple_dir = [p for p in candidates if "arm-linux-gnueabihf" in p.parts]
    return (tuple_dir or candidates)[0]


def glibc_version(libc: Path) -> tuple[int, int]:
    data = Path(libc).read_bytes()
    match = re.search(rb"release version (\d+)\.(\d+)", data)
    if not match:
        raise RuntimeVerificationError(f"cannot read glibc version from {libc}")
    return int(match.group(1)), int(match.group(2))


def assert_reviewed_glibc(root: Path, expected: tuple[int, int] = REVIEWED_MAX_GLIBC) -> str:
    libc = find_library(root, "libc.so.6")
    version = glibc_version(libc)
    if version > expected:
        raise RuntimeVerificationError(
            f"staged glibc {version[0]}.{version[1]} is newer than reviewed {expected[0]}.{expected[1]}")
    loader = find_library(root, "ld-linux-armhf.so.3")
    return f"glibc {version[0]}.{version[1]} loader={loader}"


# --- symbol-version enforcement --------------------------------------------
def _section(text: str, header: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if header in line:
            capturing = True
            continue
        if capturing and line.strip().startswith("Version ") and "section" in line:
            break
        if capturing:
            out.append(line)
    return "\n".join(out)


def required_versions(readelf_text: str) -> dict[str, set[str]]:
    """Parse ``readelf -V`` version-needs blocks -> {library: {versions}}."""
    needs: dict[str, set[str]] = {}
    current: str | None = None
    for line in _section(readelf_text, "Version needs section").splitlines():
        start = re.search(r"File:\s*(\S+)\s+Cnt:", line)
        if start:
            current = start.group(1)
            needs.setdefault(current, set())  # type: ignore[arg-type]
            continue
        name = re.search(r"Name:\s*(\S+)\s+Flags:", line)
        if name and current is not None:
            needs[current].add(name.group(1))
    return needs


def defined_versions(readelf_text: str) -> set[str]:
    """Parse ``readelf -V`` version-definition blocks -> {versions}."""
    defined: set[str] = set()
    for line in _section(readelf_text, "Version definition section").splitlines():
        name = re.search(r"Name:\s*(\S+)\s*$", line)
        if name:
            defined.add(name.group(1))
    return defined


def check_symbol_closure(required: dict[str, set[str]], defined_by_lib: dict[str, set[str]]) -> None:
    """Require every needed (library, version) to be defined by that runtime lib."""
    for library, versions in sorted(required.items()):
        have = defined_by_lib.get(library)
        if have is None:
            raise RuntimeVerificationError(f"{library} is absent from the reviewed runtime")
        missing = sorted(v for v in versions if v not in have)
        if missing:
            raise RuntimeVerificationError(
                f"{library} in the reviewed runtime does not define {missing}")


def collect_defined(root: Path, required: dict[str, set[str]], readelf: str) -> dict[str, set[str]]:
    """Read the exported version set of every runtime library the fixture needs."""
    defined: dict[str, set[str]] = {}
    for library in required:
        lib_path = find_library(root, library)
        text = subprocess.run([readelf, "-V", str(lib_path)], check=True, capture_output=True,
                              text=True, timeout=60).stdout
        defined[library] = defined_versions(text)
    return defined


def run_under_reviewed_loader(qemu: Path, root: Path, binary: Path, timeout: int) -> tuple[int, str]:
    loader = find_library(root, "ld-linux-armhf.so.3")
    # Debian/Ubuntu armhf packages land under the multiarch tuple directory.
    search = [root / "usr/lib/arm-linux-gnueabihf", root / "usr/lib", root / "lib"]
    library_path = ":".join(str(p) for p in search)
    proc = subprocess.run([str(qemu), str(loader), "--library-path", library_path, str(binary)],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--archives", required=True, type=Path)
    parser.add_argument("--stage-out", required=True, type=Path)
    parser.add_argument("--packages", default="libc6,libgcc-s1")
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    try:
        lock = load_package_lock(args.lock)
        staged = verify_and_stage(lock, args.archives, args.stage_out,
                                  [p for p in args.packages.split(",") if p])
        summary = assert_reviewed_glibc(args.stage_out)
        print(f"reviewed runtime staged ({summary}); packages={sorted(staged)}")
        if args.binary is not None:
            text = subprocess.run([args.readelf, "-V", str(args.binary)], check=True,
                                  capture_output=True, text=True, timeout=60).stdout
            required = required_versions(text)
            if not required:
                raise RuntimeVerificationError("fixture declares no versioned library needs")
            defined = collect_defined(args.stage_out, required, args.readelf)
            check_symbol_closure(required, defined)
            print(f"symbol closure OK: {sorted(required)}")
            if args.qemu is not None:
                rc, output = run_under_reviewed_loader(args.qemu, args.stage_out, args.binary,
                                                        args.timeout)
                print(output.strip())
                if rc != 0:
                    raise RuntimeVerificationError(f"fixture failed under reviewed loader (rc={rc})")
                print("reviewed-loader run OK")
        return 0
    except (RuntimeVerificationError, subprocess.SubprocessError) as exc:
        print(f"RUNTIME CLOSURE FAILURE: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
