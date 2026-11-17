#!/usr/bin/env python3
"""LibreEcho Sendspin — ARMHF toolchain-prefix *consumer* contract tests.

The Task 2 ARM build must no longer trust a caller-supplied ``SYSROOT`` /
``CROSS_PREFIX`` (or a host-local ``/usr`` or ``/mnt`` toolchain).  It consumes
the Product *reviewed* staged prefix contract instead:

* the caller names an explicit Product root (or explicit verifier + lock) and
  the staged prefix + locked ``.deb`` archive pool;
* the build invokes the real Product materializer's ``verify`` **before** any
  target compiler/binutils runs, so identity comes from the archive bytes, not
  from a receipt, a version string or a path name;
* only after an archive-backed PASS does it derive
  ``SYSROOT=<prefix>`` / ``CROSS_PREFIX=<prefix>/usr/bin/arm-linux-gnueabihf-``
  / ``LD_LIBRARY_PATH=<prefix>/usr/lib/x86_64-linux-gnu`` and use the absolute
  just-verified tool paths;
* target-compiler search-path influences (``CPATH``, ``C_INCLUDE_PATH``,
  ``CPLUS_INCLUDE_PATH``, ``LIBRARY_PATH``, ``GCC_EXEC_PREFIX``,
  ``COMPILER_PATH``) and an inherited loader path are sanitized for the ARM
  lane only — the host fixture keeps its ambient environment.

These tests run the **real** Product verifier against small, genuinely
structured fixture ``.deb`` archives (never the 55 MiB real pool), so they are
fast and exercise the shipped shell logic rather than a reimplementation.  The
only stand-ins are marker fixture tools used to observe *ordering and
environment*; stand-in output is never presented as a real compile success.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD_SCRIPT = HERE / "build_sendspin.sh"
# HERE = .../libreecho-sendspin/platform/tools/mt8163-arm32/sendspin
UI_ADAPTER = HERE.parents[3] / "ui/src/adapter/sendspin"


def _product_root() -> Path | None:
    env = os.environ.get("SENDSPIN_PRODUCT_ROOT")
    if env and (Path(env) / "build/ci/armhf_toolchain.py").is_file():
        return Path(env)
    candidate = HERE.parents[3] / "product"
    if (candidate / "build/ci/armhf_toolchain.py").is_file():
        return candidate
    return None


PRODUCT_ROOT = _product_root()
VERIFIER_PATH = PRODUCT_ROOT / "build/ci/armhf_toolchain.py" if PRODUCT_ROOT else None
# No hardcoded host path: the provisioned pinned-source archive dir comes from
# SENDSPIN_ARCHIVE_DIR; when it is absent the source-consuming tests skip (they
# never fabricate a real-source stage).
_SENDSPIN_ARCHIVE_NAME = "sendspin-cpp-8cdd4b38d029f3ef754756d494e0b53c42b81d75.tar.gz"
_env_archive = os.environ.get("SENDSPIN_ARCHIVE_DIR", "").strip()
REAL_SENDSPIN_ARCHIVE_DIR = (
    Path(_env_archive) if _env_archive else Path("/nonexistent/sendspin-archives"))


def _real_sources_available() -> bool:
    return (REAL_SENDSPIN_ARCHIVE_DIR / _SENDSPIN_ARCHIVE_NAME).is_file()


# ---------------------------------------------------------------------------
# Genuinely structured fixture .deb builder (stdlib only)
# ---------------------------------------------------------------------------

def _tar_gz(members) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for info, body in members:
            handle.addfile(info, io.BytesIO(body) if body is not None else None)
    return buffer.getvalue()


def _file(name, body, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(body)
    info.mode = mode
    info.type = tarfile.REGTYPE
    return (info, body)


def _dir(name, mode=0o755):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.size = 0
    return (info, None)


def _ar(members) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, body in members:
        encoded = name.encode() + b" " * (16 - len(name))
        header = (encoded + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
                  + b"100644".ljust(8) + str(len(body)).encode().ljust(10) + b"`\n")
        out += header + body
        if len(body) % 2:
            out += b"\n"
    return bytes(out)


def _make_deb(path: Path, package, version, architecture, members) -> Path:
    """Build a real .deb: ar(debian-binary, control.tar.gz, data.tar.gz)."""
    control = f"Package: {package}\nVersion: {version}\nArchitecture: {architecture}\n".encode()
    control_tar = _tar_gz([_file("./control", control)])
    data_tar = _tar_gz(members)
    path.write_bytes(_ar([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", control_tar),
        ("data.tar.gz", data_tar),
    ]))
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# The marker fixture compiler records the target-lane environment variables a
# build could use to smuggle in headers/tools/loader (never used to claim a real
# compile).  An env dump path is supplied per test via SENDSPIN_FIXTURE_ENV_DUMP.
_FIXTURE_ENV_VARS = ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH",
                     "GCC_EXEC_PREFIX", "COMPILER_PATH", "LD_LIBRARY_PATH")
_FIXTURE_TOOL = (
    "#!/bin/sh\n"
    "if [ -n \"${SENDSPIN_FIXTURE_ENV_DUMP:-}\" ]; then\n"
    "  {\n"
    "    echo \"invoked:$0\"\n"
    + "".join(
        f"    if printenv {v} >/dev/null 2>&1; then echo \"{v}=$(printenv {v})\"; fi\n"
        for v in _FIXTURE_ENV_VARS)
    + "  } >> \"$SENDSPIN_FIXTURE_ENV_DUMP\" 2>/dev/null\n"
    "fi\n"
    "exit 0\n"
).encode()

_FIXTURE_LIBC = b"GNU C Library (fixture stand-in) stable release version 2.39.\n"

_FIXTURE_TOOLS = ("gcc", "g++", "as", "ld", "ar", "ranlib", "nm",
                  "objcopy", "objdump", "strip", "readelf")


def _fixture_members(extra_tool=()):
    members = [
        _dir("usr"), _dir("usr/bin"), _dir("usr/lib"),
        _dir("usr/lib/x86_64-linux-gnu"),
        _dir("usr/arm-linux-gnueabihf"), _dir("usr/arm-linux-gnueabihf/lib"),
    ]
    for tool in _FIXTURE_TOOLS:
        members.append(_file(f"usr/bin/arm-linux-gnueabihf-{tool}", _FIXTURE_TOOL, 0o755))
    for name, body in extra_tool:
        members.append(_file(name, body, 0o755))
    members.append(_file("usr/lib/x86_64-linux-gnu/libfixture-support.so.1", b"fixture\n"))
    members.append(_file("usr/arm-linux-gnueabihf/lib/libc.so.6", _FIXTURE_LIBC))
    members.append(_file("usr/arm-linux-gnueabihf/lib/ld-linux-armhf.so.3", b"loader\n", 0o755))
    return members


def build_fixture_pool(root: Path, *, name="fixture-toolchain", version="1.0-1",
                       architecture="all", extra_tool=()):
    """Create a fixture archive pool + a lock the real Product verifier accepts."""
    archives = root / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    deb = archives / f"{name}_{version}_{architecture}.deb"
    _make_deb(deb, name, version, architecture, _fixture_members(extra_tool))
    record = {
        "name": name, "version": version, "architecture": architecture,
        "filename": deb.name, "pool_path": f"pool/main/f/{deb.name}",
        "url": f"https://example.invalid/ubuntu/pool/main/f/{deb.name}",
        "size": deb.stat().st_size, "sha256": _sha256(deb),
        "authentication": {
            "source": "fixture", "suite": "noble",
            "index": "main/binary-amd64/Packages.xz", "index_sha256": "a" * 64,
            "signing_fingerprint": "F6ECB3762474EDA9D21B7022871920D1991BC93C",
        },
    }
    lock = root / "lock.json"
    lock.write_text(json.dumps({
        "schema": _lock_schema(), "target": "arm-linux-gnueabihf-glibc-dynamic",
        "archives": [record], "host_requirements": {"target": "arm-linux-gnueabihf"},
        "provenance": {"compile_sysroot_pinned": False},
    }, indent=2) + "\n", encoding="utf-8")
    return archives, lock


def _lock_schema() -> str:
    if VERIFIER_PATH is None:
        raise unittest.SkipTest("Product verifier is not available")
    return _load_verifier().LOCK_SCHEMA


_LOADED: dict = {}


def _load_verifier():
    if "module" not in _LOADED:
        spec = importlib.util.spec_from_file_location("platform_fixture_armhf_toolchain",
                                                      VERIFIER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED["module"] = module
    return _LOADED["module"]


def _stage(lock: Path, archives: Path, prefix: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(VERIFIER_PATH), "stage", "--lock", str(lock),
         "--archives", str(archives), "--prefix", str(prefix)],
        capture_output=True, text=True, env=env, timeout=180)


def _clean_env(**overrides) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SENDSPIN_")
           and k not in ("SYSROOT", "CROSS_PREFIX", "LD_LIBRARY_PATH", "QEMU_ARM")}
    env.update(overrides)
    return env


def _build_env(archives: Path, lock: Path, prefix: Path, **overrides) -> dict:
    """The ARM consumer contract, with the real Product fixture verifier."""
    env = _clean_env(
        SENDSPIN_ARMHF_TOOLCHAIN_MODULE=str(VERIFIER_PATH),
        SENDSPIN_ARMHF_TOOLCHAIN_LOCK=str(lock),
        SENDSPIN_ARMHF_PREFIX=str(prefix),
        SENDSPIN_ARMHF_ARCHIVE_DIR=str(archives),
        UI_SENDSPIN_CMAKE_DIR=str(UI_ADAPTER),
        SENDSPIN_ARCHIVE_DIR=str(REAL_SENDSPIN_ARCHIVE_DIR),
        SKIP_HOST="1",
        SENDSPIN_CONFIGURE_TIMEOUT="60",
        SENDSPIN_ARMHF_VERIFY_TIMEOUT="120",
    )
    env.update(overrides)
    return env


def _run(env: dict, *, timeout=300):
    parent = Path(tempfile.mkdtemp(prefix="sendspin-consumer-"))
    out = parent / "output"
    proc = subprocess.run(["bash", str(BUILD_SCRIPT), str(out)], env=env,
                          capture_output=True, text=True, timeout=timeout)
    return proc, out


def _verify_log(out: Path) -> str:
    log = out / "armhf-toolchain-verify.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


# ---------------------------------------------------------------------------
# Static contract gates on the build entry point
# ---------------------------------------------------------------------------

class ToolchainConsumerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = BUILD_SCRIPT.read_text(encoding="utf-8")

    def test_requires_explicit_product_root_or_verifier(self) -> None:
        self.assertIn("SENDSPIN_PRODUCT_ROOT", self.script)
        self.assertIn("SENDSPIN_ARMHF_TOOLCHAIN_MODULE", self.script)
        self.assertIn("SENDSPIN_ARMHF_TOOLCHAIN_LOCK", self.script)

    def test_requires_staged_prefix_and_archive_pool(self) -> None:
        self.assertIn("SENDSPIN_ARMHF_PREFIX", self.script)
        self.assertIn("SENDSPIN_ARMHF_ARCHIVE_DIR", self.script)

    def test_has_no_host_toolchain_default_or_mnt_fallback(self) -> None:
        # No implicit /usr cross prefix and no /mnt fallback: the ARM path must
        # fail closed without the reviewed staged prefix. Any literal
        # arm-linux-gnueabihf- in code must be *derived* from the verified
        # prefix, never a hardcoded default.
        self.assertNotRegex(self.script, r"CROSS_PREFIX=\$\{CROSS_PREFIX:-/")
        self.assertNotRegex(self.script, r"SYSROOT=\$\{SYSROOT:-/")
        self.assertNotIn("/mnt/old-samsung", self.script)
        code = "\n".join(line for line in self.script.splitlines()
                         if not line.lstrip().startswith("#"))
        for line in code.splitlines():
            if "/usr/bin/arm-linux-gnueabihf-" in line:
                self.assertIn("SENDSPIN_ARMHF_PREFIX", line,
                              f"hardcoded toolchain path without the verified prefix: {line}")

    def test_invokes_product_verifier_before_any_target_tool(self) -> None:
        # Compare executable lines only; the header comment also names the files.
        code = "\n".join(line for line in self.script.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertIn("armhf_toolchain.py", code)
        self.assertIn("verify", code)
        verify_at = code.index("armhf-toolchain-verify.log")
        stage_at = code.index("verify_sendspin_sources.py")
        # The archive-backed prefix verification must precede all build work.
        self.assertLess(verify_at, stage_at)

    def test_derives_the_verified_prefix_contract(self) -> None:
        self.assertIn("/usr/bin/arm-linux-gnueabihf-", self.script)
        self.assertIn("/usr/lib/x86_64-linux-gnu", self.script)
        # The LD path must be derived from the verified prefix, not the caller.
        self.assertIn('arm-linux-gnueabihf-', self.script)

    def test_never_evals_env_output(self) -> None:
        self.assertNotRegex(self.script, r"(^|\s)eval(\s|$)")
        self.assertNotIn('"$module" env', self.script)
        self.assertNotIn("armhf_toolchain.py\" env", self.script)

    def test_sanitizes_target_compiler_search_paths(self) -> None:
        for var in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH",
                    "GCC_EXEC_PREFIX", "COMPILER_PATH"):
            self.assertIn(f"-u {var}", self.script)
        # The loader path is set explicitly (an inherited one cannot bypass).
        self.assertIn("LD_LIBRARY_PATH=", self.script)

    def test_uses_absolute_verified_tool_paths(self) -> None:
        self.assertIn("${ARM_CROSS_PREFIX}", self.script)
        for tool in ("CMAKE_AR", "CMAKE_RANLIB", "CMAKE_NM", "CMAKE_OBJCOPY",
                     "CMAKE_OBJDUMP", "CMAKE_STRIP"):
            self.assertIn(tool, self.script)


# ---------------------------------------------------------------------------
# Behavioral negative matrix: fail closed *before* any compiler runs
# ---------------------------------------------------------------------------

class ToolchainConsumerNegativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if PRODUCT_ROOT is None:
            raise unittest.SkipTest("Product verifier is not available")
        if not UI_ADAPTER.is_dir():
            raise unittest.SkipTest(f"UI adapter missing: {UI_ADAPTER}")
        # These tests drive the real script past the source-identity stage to the
        # ARM gate, so they need the provisioned pinned sources; without them the
        # script legitimately fails earlier and the matrix would be a false green.
        if not _real_sources_available():
            raise unittest.SkipTest(
                f"provisioned pinned sendspin sources missing: {REAL_SENDSPIN_ARCHIVE_DIR}")
        cls.root = Path(tempfile.mkdtemp(prefix="sendspin-consumer-fx-"))
        cls.archives, cls.lock = build_fixture_pool(cls.root)
        cls.marker_dir = cls.root / "markers"
        cls.marker_dir.mkdir()
        # A stand-in cmake first on PATH proves configure is never reached.
        cls.bin = cls.root / "bin"
        cls.bin.mkdir()
        stand_in = cls.bin / "cmake"
        stand_in.write_text(
            "#!/bin/sh\n"
            "if [ -n \"${SENDSPIN_CONFIGURE_MARKER:-}\" ]; then : > \"$SENDSPIN_CONFIGURE_MARKER\"; fi\n"
            "exit 1\n", encoding="utf-8")
        stand_in.chmod(0o755)
        cls.env_base = {"PATH": str(cls.bin) + os.pathsep + os.environ.get("PATH", "")}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def _prefix(self, name="prefix") -> Path:
        prefix = Path(tempfile.mkdtemp(prefix=f"sendspin-consumer-{name}-")) / "p"
        assert _stage(self.lock, self.archives, prefix).returncode == 0
        return prefix

    def _assert_no_compiler_or_configure(self, out: Path, dump: Path, marker: Path) -> None:
        self.assertFalse(dump.exists(), "target compiler was invoked on a refused input")
        self.assertFalse(marker.exists(), "configure was reached on a refused input")
        self.assertFalse((out / "arm-configure.log").exists())

    def test_missing_prefix_fails_closed_before_compiler(self) -> None:
        dump = self.marker_dir / "missing-prefix.env"
        marker = self.marker_dir / "missing-prefix.cmake"
        env = _build_env(self.archives, self.lock, self.root / "nonexistent-prefix",
                         SENDSPIN_FIXTURE_ENV_DUMP=str(dump),
                         SENDSPIN_CONFIGURE_MARKER=str(marker), **self.env_base)
        proc, out = _run(env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SENDSPIN_ARMHF_PREFIX", proc.stderr + proc.stdout)
        self._assert_no_compiler_or_configure(out, dump, marker)

    def test_missing_archive_pool_fails_closed_before_compiler(self) -> None:
        dump = self.marker_dir / "missing-arch.env"
        marker = self.marker_dir / "missing-arch.cmake"
        prefix = self._prefix("arch")
        env = _build_env(self.root / "nonexistent-archives", self.lock, prefix,
                         SENDSPIN_FIXTURE_ENV_DUMP=str(dump),
                         SENDSPIN_CONFIGURE_MARKER=str(marker), **self.env_base)
        proc, out = _run(env)
        self.assertNotEqual(proc.returncode, 0)
        self._assert_no_compiler_or_configure(out, dump, marker)

    def test_missing_verifier_fails_closed_before_compiler(self) -> None:
        dump = self.marker_dir / "missing-module.env"
        marker = self.marker_dir / "missing-module.cmake"
        prefix = self._prefix("module")
        env = _build_env(self.archives, self.lock, prefix,
                         SENDSPIN_ARMHF_TOOLCHAIN_MODULE=str(self.root / "no-such-verifier.py"),
                         SENDSPIN_FIXTURE_ENV_DUMP=str(dump),
                         SENDSPIN_CONFIGURE_MARKER=str(marker), **self.env_base)
        proc, out = _run(env)
        self.assertNotEqual(proc.returncode, 0)
        self._assert_no_compiler_or_configure(out, dump, marker)

    def test_conflicting_sysroot_override_is_rejected(self) -> None:
        prefix = self._prefix("sysroot")
        env = _build_env(self.archives, self.lock, prefix,
                         SYSROOT="/mnt/old-samsung", SKIP_HOST="1")
        proc, _ = _run(env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SYSROOT", proc.stderr + proc.stdout)

    def test_conflicting_cross_prefix_override_is_rejected(self) -> None:
        prefix = self._prefix("cross")
        env = _build_env(self.archives, self.lock, prefix,
                         CROSS_PREFIX="/usr/bin/arm-linux-gnueabihf-", SKIP_HOST="1")
        proc, _ = _run(env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CROSS_PREFIX", proc.stderr + proc.stdout)

    def test_tampered_prefix_with_rewritten_receipt_is_rejected(self) -> None:
        prefix = self._prefix("tamper")
        module = _load_verifier()
        # Mutate an extracted tool and fully rewrite the receipt to match.
        target = prefix / "usr/bin/arm-linux-gnueabihf-gcc"
        target.write_bytes(b"#!/bin/sh\nexit 0  # tampered\n")
        target.chmod(0o755)
        manifest = module.tree_manifest(prefix, exclude={module.RECEIPT_NAME})
        receipt_path = prefix / module.RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["tree"] = {"schema": manifest["schema"], "sha256": manifest["sha256"],
                           "files": manifest["files"], "directories": manifest["directories"],
                           "symlinks": manifest["symlinks"]}
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        env = _build_env(self.archives, self.lock, prefix, SKIP_HOST="1")
        proc, out = _run(env)
        self.assertNotEqual(proc.returncode, 0)
        log = _verify_log(out)
        self.assertNotIn("armhf_toolchain_verify=PASS", log)

    def test_lock_mismatch_is_rejected(self) -> None:
        prefix = self._prefix("lockmismatch")
        other = self.root / "other-lock.json"
        document = json.loads(self.lock.read_text(encoding="utf-8"))
        document["provenance"] = {"compile_sysroot_pinned": True}  # different bytes
        other.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        env = _build_env(self.archives, other, prefix, SKIP_HOST="1")
        proc, out = _run(env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("armhf_toolchain_verify=PASS", _verify_log(out))

    def test_corrupt_and_missing_archive_are_rejected(self) -> None:
        for kind in ("corrupt", "missing"):
            with self.subTest(kind=kind):
                pool = Path(tempfile.mkdtemp(prefix=f"sendspin-pool-{kind}-")) / "archives"
                shutil.copytree(self.archives, pool)
                deb = next(pool.glob("*.deb"))
                if kind == "corrupt":
                    data = bytearray(deb.read_bytes())
                    data[-1] ^= 0xFF
                    deb.write_bytes(bytes(data))
                else:
                    deb.unlink()
                prefix = Path(tempfile.mkdtemp(prefix=f"sendspin-p-{kind}-")) / "p"
                self.assertNotEqual(_stage(self.lock, pool, prefix).returncode, 0)
                env = _build_env(pool, self.lock, prefix, SKIP_HOST="1")
                proc, out = _run(env)
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("armhf_toolchain_verify=PASS", _verify_log(out))


# ---------------------------------------------------------------------------
# Positive / environment-sanitization evidence
# ---------------------------------------------------------------------------

class ToolchainConsumerBootstrapTests(unittest.TestCase):
    """A valid fixture contract must pass the archive-backed gate and enter the
    real build; the target lane's environment must be sanitized while the host
    lane is untouched.  The fixture tools are markers only: this never claims a
    real compile succeeded."""

    @classmethod
    def setUpClass(cls) -> None:
        if PRODUCT_ROOT is None:
            raise unittest.SkipTest("Product verifier is not available")
        if not UI_ADAPTER.is_dir():
            raise unittest.SkipTest(f"UI adapter missing: {UI_ADAPTER}")
        if not _real_sources_available():
            raise unittest.SkipTest(f"real sendspin archives missing: {REAL_SENDSPIN_ARCHIVE_DIR}")
        cls.root = Path(tempfile.mkdtemp(prefix="sendspin-consumer-boot-"))
        cls.archives, cls.lock = build_fixture_pool(cls.root)
        cls.markers = cls.root / "markers"
        cls.markers.mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def _prefix_under(self, parent: Path) -> Path:
        parent.mkdir(parents=True, exist_ok=True)
        prefix = parent / "p"
        self.assertEqual(_stage(self.lock, self.archives, prefix).returncode, 0)
        return prefix

    def test_valid_contract_passes_gate_and_target_env_is_sanitized(self) -> None:
        prefix = self._prefix_under(self.root / "plain")
        dump = self.markers / "env.dump"
        env = _build_env(self.archives, self.lock, prefix,
                         SENDSPIN_FIXTURE_ENV_DUMP=str(dump),
                         CPATH="/poison/include", C_INCLUDE_PATH="/poison/c",
                         CPLUS_INCLUDE_PATH="/poison/cxx", LIBRARY_PATH="/poison/lib",
                         GCC_EXEC_PREFIX="/poison/gcc/", COMPILER_PATH="/poison/compiler",
                         LD_LIBRARY_PATH="/poison/loader")
        proc, out = _run(env)
        # The archive-backed gate must have passed (the run may later stop at
        # the stand-in compile; that is never asserted as success).
        self.assertIn("armhf_toolchain_verify=PASS", _verify_log(out))
        self.assertTrue(dump.exists(), "target compiler was never invoked under a valid prefix")
        lines = dump.read_text(encoding="utf-8", errors="replace").splitlines()
        names = {line.split("=", 1)[0] for line in lines if "=" in line}
        for poisoned in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
                         "LIBRARY_PATH", "GCC_EXEC_PREFIX", "COMPILER_PATH"):
            self.assertNotIn(poisoned, names,
                             f"{poisoned} leaked into the target compiler environment")
        self.assertNotIn("/poison", "\n".join(lines))
        self.assertIn(f"LD_LIBRARY_PATH={prefix}/usr/lib/x86_64-linux-gnu", lines)

    def test_space_and_dollar_prefix_pass_the_gate_and_reach_the_compiler(self) -> None:
        # Gate-level coverage only: an ordinary space and a literal '$' must be
        # accepted and the target compiler invoked.  The end-to-end CMake
        # configure+compile+link proof for these paths lives in
        # ToolchainConsumerRealPrefixPathTests (fixture tools are markers and
        # never presented as a real compile).
        for label, dirname in (("space", "pre fix dir"), ("dollar", "pre$fix dir")):
            with self.subTest(label=label):
                prefix = self._prefix_under(self.root / dirname)
                dump = self.markers / f"env-{label}.dump"
                env = _build_env(self.archives, self.lock, prefix,
                                 SENDSPIN_FIXTURE_ENV_DUMP=str(dump))
                proc, out = _run(env)
                self.assertIn("armhf_toolchain_verify=PASS", _verify_log(out))
                self.assertTrue(dump.exists(), "compiler never invoked for a quoted path")


# ---------------------------------------------------------------------------
# Path-character safety of the generated arm-toolchain.cmake
# ---------------------------------------------------------------------------
# The generated CMake must not silently mis-parse the verified prefix path.
# Ordinary spaces and a literal '$' are supported (values are quoted and the
# sysroot is supplied through CMAKE_SYSROOT, not a flag string); characters
# CMake cannot represent in a quoted set() value -- ';', '"', '\', a newline,
# or a variable/generator expansion (${...}, $<...>, $ENV{...}, $CACHE{...})
# -- are rejected at the gate *before* any target compiler or configure runs.

class ToolchainConsumerPathRejectionTests(unittest.TestCase):
    """CMake-unsupported prefix characters fail closed at the gate, before the
    target compiler or configure is reached and before any output mutation."""

    HOSTILE = (
        ("semicolon", "a;b"),
        ("double-quote", 'a"b'),
        ("backslash", "a\\b"),
        ("brace-expansion", "a${x}b"),
        ("generator-expr", "a$<x>b"),
    )

    @classmethod
    def setUpClass(cls) -> None:
        if PRODUCT_ROOT is None or VERIFIER_PATH is None:
            raise unittest.SkipTest("Product verifier is not available")
        if not UI_ADAPTER.is_dir():
            raise unittest.SkipTest(f"UI adapter missing: {UI_ADAPTER}")
        cls.root = Path(tempfile.mkdtemp(prefix="sendspin-hostile-"))
        cls.archives, cls.lock = build_fixture_pool(cls.root)
        cls.markers = cls.root / "markers"
        cls.markers.mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def _prefix_under(self, parent: Path) -> Path:
        parent.mkdir(parents=True, exist_ok=True)
        prefix = parent / "p"
        self.assertEqual(_stage(self.lock, self.archives, prefix).returncode, 0)
        return prefix

    def test_cmake_hostile_prefix_characters_are_rejected_before_compiler(self) -> None:
        for label, hostile in self.HOSTILE:
            with self.subTest(label=label):
                prefix = self._prefix_under(self.root / hostile)
                dump = self.markers / f"env-{label}.dump"
                marker = self.markers / f"cmake-{label}.marker"
                env = _build_env(self.archives, self.lock, prefix,
                                 SENDSPIN_ARCHIVE_DIR=str(self.root),
                                 SENDSPIN_FIXTURE_ENV_DUMP=str(dump),
                                 SENDSPIN_CONFIGURE_MARKER=str(marker))
                proc, out = _run(env)
                self.assertNotEqual(proc.returncode, 0)
                # The archive-backed gate must refuse the prefix: it must never
                # reach the verifier PASS, the target compiler or configure.
                self.assertNotIn("armhf_toolchain_verify=PASS", _verify_log(out))
                self.assertNotIn("armhf_toolchain_verify=PASS", proc.stdout + proc.stderr)
                self.assertFalse(dump.exists(),
                                 f"{label}: target compiler ran on a refused prefix")
                self.assertFalse(marker.exists(),
                                 f"{label}: configure was reached on a refused prefix")
                self.assertFalse((out / "arm-configure.log").exists())
                self.assertIn("SENDSPIN_ARMHF_PREFIX", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
