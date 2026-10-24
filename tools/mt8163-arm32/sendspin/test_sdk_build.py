#!/usr/bin/env python3
"""LibreEcho Sendspin — Task 2 SDK/ARM32 feasibility test (fail-closed).

This runner makes the Task 2 claims explicit and checkable, and it must never
report green when it did not actually prove them:

  * gate: the build entry point requires byte-level source identity, the
    Product *reviewed* archive-backed ARMHF staged toolchain prefix (verified by
    the Product materializer before any target tool runs) plus the locked .deb
    archive pool, the reviewed runtime closure, bounded jobs/timeouts, and a
    statically-bound C++ runtime (so the dynamic closure is exactly the reviewed
    glibc/loader);
  * negative: missing inputs, a missing ARMHF prefix, and a tampered source
    archive all fail closed (non-zero), not "OK (skipped)"; the focused
    consumer suite (test_sendspin_toolchain_consumer.py) adds the full ARMHF
    prefix-rejection matrix (tampered prefix + rewritten receipt, lock mismatch,
    corrupt/missing deb, conflicting sysroot/toolchain, quoted paths, inherited
    search-path contamination);
  * red/green: the source-, runtime- and toolchain-consumer verifier suites
    (their own negative tests) are executed here;
  * integration (provisioned only): host fixture, real ELF/NEEDED checks,
    resolved symbol-version closure, and execution under the exact reviewed
    loader.

Exit status:
  0  provisioned run passed, OR a host-only run whose host lane ran and whose
     ARM integration tests are reported honestly as not-run, OR an explicitly
     acknowledged unprovisioned run (SENDSPIN_ALLOW_UNPROVISIONED=1);
  2  no ARM integration pass was produced and SENDSPIN_REQUIRE_ARM=1 (which
     dominates every acknowledgement/host-only success), or the inputs are
     unprovisioned and the run was not acknowledged — fail closed;
  1  any check failed, or zero tests ran.

The ARM lane never consumes a caller SYSROOT/CROSS_PREFIX: it derives
SYSROOT/CROSS_PREFIX/LD_LIBRARY_PATH from the just-verified prefix. A host-only
run can omit ARM provisioning but can never emit an ARM pass.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD_SCRIPT = HERE / "build_sendspin.sh"
SOURCE_VERIFY = HERE / "verify_sendspin_sources.py"
RUNTIME_VERIFY = HERE / "verify_sendspin_runtime.py"
SOURCE_LOCK = HERE / "SOURCE.lock"
VERIFIER_TESTS = (HERE / "test_sendspin_source_verify.py",
                  HERE / "test_sendspin_runtime_verify.py",
                  HERE / "test_sendspin_toolchain_consumer.py")

# The consumer suite's source-backed classes: their fixtures consume the pinned
# real sendspin source archive, so they legitimately skip when the host lane is
# unprovisioned.  No other verifier test may skip in that context, and a
# provisioned run must execute these classes too.
CONSUMER_MODULE = "test_sendspin_toolchain_consumer"
UNAVAILABLE_SOURCE_CONSUMER_CLASSES = (
    "ToolchainConsumerNegativeTests",
    "ToolchainConsumerBootstrapTests",
)

# One verifier suite is executed in a child interpreter that prints a single
# machine-readable result line; the parent judges the run from that structured
# payload plus the child exit code, never from a prose tail like "OK$".
_NESTED_RESULT_MARKER = "SENDPIN_NESTED_RESULT="
_NESTED_RESULT_LAUNCHER = r'''
import importlib, json, os, sys, unittest


class _StructuredResult(unittest.TestResult):
    def __init__(self):
        super().__init__(stream=open(os.devnull, "w"), verbosity=0)
        self.skipped_ids = []

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.skipped_ids.append(test.id())


def _main():
    module = importlib.import_module(sys.argv[1])
    suite = unittest.TestLoader().loadTestsFromModule(module)
    result = _StructuredResult()
    suite.run(result)
    payload = {
        "module": sys.argv[1],
        "tests_run": result.testsRun,
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skipped": list(result.skipped_ids),
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        # unexpectedSuccesses holds test *instances* (unlike expectedFailures,
        # which holds (test, exc) tuples); iterating it as tuples raised
        # TypeError and dropped the structured result entirely.
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
        "successful": result.wasSuccessful(),
    }
    print("SENDPIN_NESTED_RESULT=" + json.dumps(payload))
    return 0 if result.wasSuccessful() else 1


sys.exit(_main())
'''

# Host lane inputs and the ARMHF verified staged-prefix consumer interface (see
# build_sendspin.sh).  There is no caller SYSROOT/CROSS_PREFIX any more: the ARM
# lane consumes the Product archive-backed prefix contract.
HOST_PROVISION_ENV = (
    "SENDSPIN_ARCHIVE_DIR",
    "UI_SENDSPIN_CMAKE_DIR",
)
ARM_PROVISION_ENV = (
    "SENDSPIN_PRODUCT_ROOT",
    "SENDSPIN_ARMHF_PREFIX",
    "SENDSPIN_ARMHF_ARCHIVE_DIR",
    "SENDSPIN_MDNS_LOCK",
    "SENDSPIN_MDNS_ARCHIVES_DIR",
)
PROVISION_ENV = HOST_PROVISION_ENV + ARM_PROVISION_ENV

_SESSION: dict = {}


def _present(name: str) -> str | None:
    value = os.environ.get(name)
    if not value:
        return None
    return value if Path(value).exists() else None


def _host_ok() -> bool:
    return all(_present(name) for name in HOST_PROVISION_ENV)


def _arm_ok() -> bool:
    return all(_present(name) for name in ARM_PROVISION_ENV)


def _provisioned() -> bool:
    return _host_ok() and _arm_ok()


def setUpModule() -> None:  # noqa: N802
    _SESSION["host_ok"] = _host_ok()
    _SESSION["arm_ok"] = _arm_ok()
    _SESSION["provisioned"] = _SESSION["host_ok"] and _SESSION["arm_ok"]
    # Host-only: the host fixture can run (SKIP_ARM=1) but the ARM lane cannot
    # emit a pass without the verified staged toolchain prefix.
    _SESSION["host_only"] = _SESSION["host_ok"] and not _SESSION["arm_ok"]
    _SESSION["allow_unprovisioned"] = os.environ.get("SENDSPIN_ALLOW_UNPROVISIONED") == "1"
    _SESSION["require_arm"] = os.environ.get("SENDSPIN_REQUIRE_ARM") == "1"
    if not _SESSION["host_ok"]:
        return
    out = Path(tempfile.mkdtemp(prefix="sendspin-sdk-build-")) / "output"
    env = dict(os.environ)
    if _SESSION["host_only"]:
        env["SKIP_ARM"] = "1"
    proc = subprocess.run(["bash", str(BUILD_SCRIPT), str(out)], env=env,
                          capture_output=True, text=True, timeout=2400)
    _SESSION.update(out=out, proc=proc)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:])
        sys.stderr.write(proc.stderr[-4000:])


def _out() -> Path:
    if "out" not in _SESSION:
        raise unittest.SkipTest("build did not run (inputs not provisioned)")
    return _SESSION["out"]  # type: ignore[return-value]


def _text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _run_nested_module(module: Path, cwd: Path) -> tuple[int, dict | None, str]:
    """Run one verifier suite in a child interpreter and read its structured
    result.  Returns ``(returncode, payload_or_None, combined_output)``; a crash
    or bad import yields ``payload is None`` and a non-zero return code."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-c", _NESTED_RESULT_LAUNCHER, module.stem],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=300)
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith(_NESTED_RESULT_MARKER):
            payload = json.loads(line[len(_NESTED_RESULT_MARKER):])
    return proc.returncode, payload, proc.stdout + proc.stderr


def _nested_result_failures(payload: dict | None, returncode: int, *,
                           module_stem: str, allow_unavailable_source_skips: bool,
                           consumer_module: str = CONSUMER_MODULE,
                           allowed_skip_classes: tuple[str, ...] = UNAVAILABLE_SOURCE_CONSUMER_CLASSES,
                           output: str = "") -> list[str]:
    """Classify one nested suite's structured result.  An empty list means the
    run is acceptable; otherwise each returned string is a failure reason.

    Fail closed on: a non-zero child exit, a missing structured result, a run
    with zero tests, any failure/error/unexpected success, and *any* skip that is
    not one of the consumer's known unavailable-source classes while the host
    lane is unprovisioned.  A provisioned lane permits no skips at all.
    """
    if payload is None:
        tail = [line for line in output.strip().splitlines() if line.strip()][-5:]
        return [f"{module_stem}: nested suite produced no structured result"
                + (": " + " | ".join(tail) if tail else "")]
    failures: list[str] = []
    if returncode != 0:
        failures.append(f"{module_stem}: nested suite exited {returncode}")
    tests_run = payload.get("tests_run")
    if not isinstance(tests_run, int) or tests_run <= 0:
        failures.append(f"{module_stem}: nested suite ran zero tests")
    for kind in ("failures", "errors"):
        for test_id in payload.get(kind, []):
            failures.append(f"{module_stem}: nested {kind[:-1]} {test_id}")
    for test_id in payload.get("unexpected_successes", []):
        failures.append(f"{module_stem}: unexpected success {test_id}")
    skipped = payload.get("skipped", [])
    if skipped:
        allow_here = (module_stem == consumer_module
                      and allow_unavailable_source_skips)
        for test_id in skipped:
            if allow_here and any(name in test_id for name in allowed_skip_classes):
                continue
            failures.append(f"{module_stem}: unexpected skip {test_id}")
    if payload.get("successful") is False and not failures:
        failures.append(f"{module_stem}: nested suite reported unsuccessful")
    return failures


def _nested_module_failures(module: Path, cwd: Path, *,
                           allow_unavailable_source_skips: bool,
                           consumer_module: str = CONSUMER_MODULE,
                           allowed_skip_classes: tuple[str, ...] = UNAVAILABLE_SOURCE_CONSUMER_CLASSES
                           ) -> list[str]:
    returncode, payload, output = _run_nested_module(module, cwd)
    return _nested_result_failures(
        payload, returncode, module_stem=module.stem,
        allow_unavailable_source_skips=allow_unavailable_source_skips,
        consumer_module=consumer_module, allowed_skip_classes=allowed_skip_classes,
        output=output)


class BuildContractGateTests(unittest.TestCase):
    """Static gates on the build entry point (independent of provisioning)."""

    def setUp(self) -> None:
        self.script = _text(BUILD_SCRIPT)

    def test_requires_pinned_archive_dir(self) -> None:
        self.assertIn("SENDSPIN_ARCHIVE_DIR:?", self.script)

    def test_requires_verified_arm_toolchain_prefix_without_default(self) -> None:
        # The ARM lane consumes the Product reviewed staged prefix; there is no
        # implicit /usr or /mnt fallback and no caller SYSROOT/CROSS_PREFIX default.
        self.assertIn("SENDSPIN_ARMHF_PREFIX is required", self.script)
        self.assertNotRegex(self.script, r"SYSROOT=\$\{SYSROOT:-[^}]+\}")
        self.assertNotRegex(self.script, r"CROSS_PREFIX=\$\{CROSS_PREFIX:-[^}]+\}")
        self.assertNotIn("/mnt/old-samsung", self.script)

    def test_consumes_product_verifier_before_target_tools(self) -> None:
        # The Product materializer's `verify` (archive-backed) must run before
        # any source staging/compilation and be receipted.
        self.assertIn("armhf_toolchain.py", self.script)
        self.assertIn("armhf-toolchain-verify.log", self.script)
        code = "\n".join(line for line in self.script.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertLess(code.index("armhf-toolchain-verify.log"),
                        code.index("verify_sendspin_sources.py"))

    def test_never_evaluates_env_output(self) -> None:
        # The consumer must never `eval` the verifier's `env` output.
        self.assertNotRegex(self.script, r"(^|\s)eval(\s|$)")

    def test_sanitizes_target_compiler_search_paths(self) -> None:
        # Inherited header/tool/loader search paths must be dropped for the ARM
        # lane so they cannot bypass the verified toolchain.
        for var in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH",
                    "GCC_EXEC_PREFIX", "COMPILER_PATH"):
            self.assertIn(f"-u {var}", self.script)

    def test_uses_absolute_verified_tool_paths(self) -> None:
        self.assertIn("${ARM_CROSS_PREFIX}", self.script)
        for tool in ("CMAKE_AR", "CMAKE_RANLIB", "CMAKE_OBJCOPY"):
            self.assertIn(tool, self.script)

    def test_verifies_source_identity_by_archive_bytes(self) -> None:
        self.assertIn("verify_sendspin_sources.py", self.script)
        self.assertIn("--stage-out", self.script)

    def test_stages_reviewed_runtime_closure(self) -> None:
        self.assertIn("verify_sendspin_runtime.py", self.script)
        self.assertIn("SENDSPIN_MDNS_LOCK", self.script)
        self.assertIn("runtime-root", self.script)

    def test_bounds_jobs_and_timeouts(self) -> None:
        self.assertIn("SENDSPIN_MAX_JOBS", self.script)
        for var in ("CONFIGURE_TIMEOUT", "BUILD_TIMEOUT", "TEST_TIMEOUT", "RUN_TIMEOUT"):
            self.assertIn(var, self.script)
        self.assertIn("timeout ", self.script)

    def test_links_cpp_runtime_statically_for_bounded_closure(self) -> None:
        self.assertIn("-static-libstdc++", self.script)
        self.assertIn("-static-libgcc", self.script)
        self.assertIn("--no-undefined", self.script)

    def test_enforces_locked_elf_needed_closure(self) -> None:
        # The build must compare the ARM ELF NEEDED/loader against SOURCE.lock
        # and fail closed on divergence, not merely describe the closure in a
        # document. The enforcement is the --elf-closure subcommand + receipt.
        self.assertIn("--elf-closure", self.script)
        self.assertIn("elf-closure.json", self.script)
        self.assertIn("runtime_requirements", self.script)

    def test_probes_compile_sysroot_linkability(self) -> None:
        # A partial/incorrect sysroot (e.g. <root>/usr/arm-linux-gnueabihf) can
        # pass a glibc-version check yet fail to link; the build must prove the
        # sysroot is usable before configuring.
        self.assertIn("not a usable compile sysroot", self.script)

    def test_compile_sysroot_is_not_described_as_the_reviewed_runtime(self) -> None:
        # Compile-input provenance (a host-local sysroot) is distinct from the
        # pinned reviewed runtime closure; the script must not conflate them.
        self.assertIn("compile sysroot", self.script.lower())
        self.assertNotIn("reviewed glibc-2.39 sysroot", self.script)


class FailClosedNegativeTests(unittest.TestCase):
    """Negative evidence: misprovisioned builds fail closed, never silent-green."""

    def _run(self, env: dict, name: str) -> subprocess.CompletedProcess:
        out = Path(tempfile.mkdtemp(prefix="sendspin-neg-")) / "output"
        return subprocess.run(["bash", str(BUILD_SCRIPT), str(out)], env=env,
                              capture_output=True, text=True, timeout=300)

    def _base_env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SENDSPIN_") and k not in PROVISION_ENV}
        env.pop("SKIP_HOST", None)
        env["SKIP_HOST"] = "1"
        return env

    def test_missing_archive_dir_fails_closed(self) -> None:
        env = self._base_env()
        env["UI_SENDSPIN_CMAKE_DIR"] = str(HERE.parents[3] / "ui/src/adapter/sendspin")
        proc = self._run(env, "missing-archive")
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_arm_prefix_fails_closed(self) -> None:
        # The ARM lane must fail closed without the Product verified staged
        # prefix (no /usr or /mnt fallback), before any target tool runs.
        env = self._base_env()
        env["UI_SENDSPIN_CMAKE_DIR"] = str(HERE.parents[3] / "ui/src/adapter/sendspin")
        env["SENDSPIN_ARCHIVE_DIR"] = tempfile.mkdtemp(prefix="sendspin-emptyarch-")
        proc = self._run(env, "missing-arm-prefix")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SENDSPIN_ARMHF_PREFIX", proc.stderr + proc.stdout)

    def test_tampered_source_archive_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archives = root / "archives"
            archives.mkdir()
            commit = "a" * 40
            archive = archives / f"lib-{commit}.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                payload = b"good"
                info = tarfile.TarInfo(f"lib-{commit}/f.txt")
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))
            lock = root / "LOCK"
            lock.write_text(json.dumps({"dependencies": [{
                "name": "lib", "commit": commit, "archive_sha256": "0" * 64,
                "archive_url": f"https://x/lib/{commit}.tar.gz"}]}), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SOURCE_VERIFY), "--lock", str(lock),
                                   "--archive-dir", str(archives), "--stage-out", str(root / "s"),
                                   "--receipt", str(root / "r.json")],
                                  capture_output=True, text=True, timeout=120)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("SOURCE IDENTITY FAILURE", proc.stderr)

    def test_missing_verified_tree_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "LOCK"
            lock.write_text(json.dumps({"dependencies": [{
                "name": "lib", "commit": "b" * 40, "archive_sha256": "0" * 64,
                "archive_url": "https://x/lib/b.tar.gz"}]}), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SOURCE_VERIFY), "--lock", str(lock),
                                   "--archive-dir", str(root), "--staged-dir", str(root),
                                   "--receipt", str(root / "missing.json"), "--verify-only"],
                                  capture_output=True, text=True, timeout=120)
            self.assertNotEqual(proc.returncode, 0)


EXPECTED_INTERPRETER = "/lib/ld-linux-armhf.so.3"


def _synthetic_elf_report(interpreter: str, needed: list[str]) -> str:
    lines = ["## readelf -d (NEEDED)"]
    for lib in needed:
        lines.append(f" 0x00000001 (NEEDED)                     Shared library: [{lib}]")
    lines.append("## readelf -l (interpreter)")
    lines.append(f"      [Requesting program interpreter: {interpreter}]")
    return "\n".join(lines) + "\n"


def _minimal_lock(interpreter: str, needed: list[str]) -> dict:
    return {"runtime_requirements": {"interpreter": interpreter, "needed": list(needed)}}


class ElfClosureContractTests(unittest.TestCase):
    """The build must fail closed when the ARM ELF NEEDED/loader drifts from
    SOURCE.lock, and accept exactly the locked closure.

    These exercise the real production check the build runs
    (``build_sendspin.sh --elf-closure``) against synthetic ELF reports, so no
    provisioning and no cached artifact identity is required: the proof is the
    lock contents, never a remembered binary."""

    def _run(self, lock: dict, report: str) -> tuple[subprocess.CompletedProcess, dict | None]:
        tmp = Path(tempfile.mkdtemp(prefix="sendspin-elf-closure-"))
        lock_path = tmp / "SOURCE.lock"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        report_path = tmp / "elf-report.txt"
        report_path.write_text(report, encoding="utf-8")
        out = tmp / "elf-closure.json"
        proc = subprocess.run(["bash", str(BUILD_SCRIPT), "--elf-closure",
                               str(lock_path), str(report_path), str(out)],
                              capture_output=True, text=True, timeout=120)
        receipt = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
        return proc, receipt

    def test_legitimate_locked_needed_is_accepted(self) -> None:
        lock = json.loads(SOURCE_LOCK.read_text(encoding="utf-8"))
        rt = lock["runtime_requirements"]
        proc, receipt = self._run(
            {"runtime_requirements": {"interpreter": rt["interpreter"], "needed": rt["needed"]}},
            _synthetic_elf_report(rt["interpreter"], rt["needed"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt["match"])
        self.assertEqual(set(receipt["needed_observed"]), set(rt["needed"]))

    def test_missing_locked_library_is_rejected(self) -> None:
        lock = _minimal_lock(EXPECTED_INTERPRETER, ["libm.so.6", "libc.so.6", "ld-linux-armhf.so.3"])
        proc, receipt = self._run(
            lock, _synthetic_elf_report(EXPECTED_INTERPRETER, ["libm.so.6", "libc.so.6"]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ELF CLOSURE FAILURE", proc.stderr + proc.stdout)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertFalse(receipt["match"])
        self.assertIn("ld-linux-armhf.so.3", json.dumps(receipt["missing_needed"]))

    def test_unexpected_cpp_runtime_library_is_rejected(self) -> None:
        lock = _minimal_lock(EXPECTED_INTERPRETER, ["libm.so.6", "libc.so.6", "ld-linux-armhf.so.3"])
        proc, receipt = self._run(lock, _synthetic_elf_report(
            EXPECTED_INTERPRETER,
            ["libm.so.6", "libc.so.6", "ld-linux-armhf.so.3", "libstdc++.so.6"]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertFalse(receipt["match"])
        self.assertIn("libstdc++.so.6", json.dumps(receipt["extra_needed"]))

    def test_interpreter_drift_is_rejected(self) -> None:
        lock = _minimal_lock(EXPECTED_INTERPRETER, ["libm.so.6", "libc.so.6", "ld-linux-armhf.so.3"])
        proc, receipt = self._run(lock, _synthetic_elf_report(
            "/lib/ld-linux.so.3", ["libm.so.6", "libc.so.6", "ld-linux-armhf.so.3"]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertFalse(receipt["match"])

    def test_lock_separates_fixture_closure_from_future_daemon(self) -> None:
        lock = json.loads(SOURCE_LOCK.read_text(encoding="utf-8"))
        rt = lock["runtime_requirements"]
        self.assertEqual(rt["interpreter"], EXPECTED_INTERPRETER)
        self.assertEqual(set(rt["needed"]), {"libm.so.6", "libc.so.6", "ld-linux-armhf.so.3"})
        for lib in ("libstdc++.so.6", "libgcc_s.so.1"):
            self.assertNotIn(lib, rt["needed"])
        self.assertIn("future_daemon", rt)
        self.assertIn("static", rt["verified_closure"]["cpp_runtime"].lower())
        # Compile-input provenance is explicitly not pinned and not conflated
        # with the enforced runtime closure.
        self.assertFalse(rt["compile_sysroot"]["pinned"])

    def test_lock_preserves_eight_pins_and_a_closed_patch_inventory(self) -> None:
        import hashlib

        lock = json.loads(SOURCE_LOCK.read_text(encoding="utf-8"))
        pins = list(lock["identity"]) + [d["name"] for d in lock["dependencies"]]
        submodules = sum(len(d.get("submodules", [])) for d in lock["dependencies"])
        self.assertEqual(len(pins) + submodules, 8)
        # The applied-patch inventory is a closed, single-apply transform of the pinned
        # sources: every entry is a bare *.patch with a 64-hex digest aimed at a pinned
        # source, the declared set equals the *.patch files on disk, and each digest
        # matches the file. A missing/extra/corrupt entry would make the verifier refuse
        # the staged tree, so the lock is checked to agree with the patches directory.
        inventory = lock["patch_inventory"]
        self.assertEqual(inventory["schema"], "libreecho-sendspin-patch-inventory/1")
        lock_names = {f"identity::{key}" for key in lock["identity"]}
        lock_names |= {d["name"] for d in lock["dependencies"]}
        lock_names |= {f"{d['name']}::{sub['path']}"
                       for d in lock["dependencies"] for sub in d.get("submodules", [])}
        patch_dir = SOURCE_LOCK.parent / "patches"
        declared = []
        for record in inventory["applied"]:
            self.assertRegex(record["file"], r"^[^/]+\.patch$")
            self.assertRegex(record["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(record["target"], lock_names)
            declared.append(record["file"])
            path = patch_dir / record["file"]
            self.assertTrue(path.is_file(), f"declared patch missing: {path}")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record["sha256"])
        self.assertEqual(sorted(p.name for p in patch_dir.glob("*.patch")), sorted(declared))


class VerifierSuiteTests(unittest.TestCase):
    """Execute the verifier suites so their red/green negative tests always run."""

    def test_verifier_suites_pass(self) -> None:
        # The consumer's source-backed classes may skip only while the host lane
        # is unprovisioned (no pinned source archive); every other skip, a
        # zero-test run and a non-zero child exit are failures.
        allow_unavailable_source_skips = not _SESSION.get("host_ok")
        for module in VERIFIER_TESTS:
            with self.subTest(module=module.name):
                failures = _nested_module_failures(
                    module, HERE,
                    allow_unavailable_source_skips=allow_unavailable_source_skips)
                self.assertEqual(failures, [], f"{module.name}: " + "; ".join(failures))


class HostFixtureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _out()

    def test_host_ctest_passed(self) -> None:
        log = _text(_out() / "host-ctest.log")
        self.assertIn("100% tests passed", log)
        self.assertIn("0 tests failed", log)

    def test_host_fixture_delivered_real_decoded_playback(self) -> None:
        log = _text(_out() / "host-fixture.log")
        self.assertIn("all checks passed", log)
        self.assertNotIn("FAIL", log)
        match = re.search(r"real_writes=(\d+)", log)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 6)  # type: ignore[union-attr]


class ArmFixtureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _out()
        if not _SESSION.get("arm_ok"):
            raise unittest.SkipTest(
                "ARMHF toolchain prefix not provisioned (host-only lane); "
                "no ARM pass is claimed")

    def test_arm_binary_is_elf32_arm_with_expected_interpreter(self) -> None:
        report = _text(_out() / "elf-report.txt")
        self.assertRegex(report, r"Class:\s+ELF32")
        self.assertRegex(report, r"Machine:\s+ARM")
        self.assertIn("/lib/ld-linux-armhf.so.3", report)

    def test_arm_dynamic_closure_exactly_matches_locked_needed(self) -> None:
        lock = json.loads(SOURCE_LOCK.read_text(encoding="utf-8"))
        expected = set(lock["runtime_requirements"]["needed"])
        report = _text(_out() / "elf-report.txt")
        needed = set(re.findall(r"Shared library: \[([^\]]+)\]", report))
        self.assertTrue(needed)
        # The built armhf fixture's dynamic closure must be exactly the closure
        # SOURCE.lock enforces; the reviewed mdns runtime ships glibc + libgcc
        # only, and the C++ runtime is bound statically.
        self.assertEqual(needed, expected)
        self.assertNotIn("libstdc++.so.6", needed)
        self.assertNotIn("libgcc_s.so.1", needed)

    def test_locked_elf_closure_receipt_matches(self) -> None:
        receipt = json.loads(_text(_out() / "elf-closure.json"))
        self.assertTrue(receipt["match"])
        self.assertEqual(receipt["needed_observed"], receipt["needed_expected"])
        self.assertEqual(receipt["extra_needed"], [])
        self.assertEqual(receipt["missing_needed"], [])

    def test_reviewed_runtime_symbol_closure_verified(self) -> None:
        log = _text(_out() / "runtime-closure.log")
        self.assertIn("reviewed runtime staged (glibc 2.39", log)
        self.assertIn("symbol closure OK", log)

    def test_fixture_ran_under_reviewed_loader(self) -> None:
        report = _text(_out() / "elf-report.txt")
        log = _text(_out() / "qemu-smoke.log")
        self.assertIn("qemu_smoke=pass", report)
        self.assertIn("all checks passed", log)
        self.assertIn("sendspin_adapter_tests", log)

    def test_arm_toolchain_is_the_verified_staged_prefix(self) -> None:
        # The ARM lane must have verified the Product staged prefix from the
        # archive bytes (not a receipt/version/path), and must not fall back to
        # a host or /mnt toolchain.  The *host* fixture legitimately uses host
        # tools; this checks the ARM lane only.
        verify = _text(_out() / "armhf-toolchain-verify.log")
        self.assertIn("armhf_toolchain_verify=PASS", verify)
        self.assertIn("archives=30", verify)
        self.assertNotIn("/mnt/old-samsung", _text(_out() / "arm-configure.log"))


class ArmPrefixPathCharacterTests(unittest.TestCase):
    """A verified ARMHF prefix whose path contains an ordinary space and a
    literal ``$`` must drive a real CMake configure+compile+link.

    The shared reviewed prefix is never renamed or mutated: the real
    archive-backed toolchain is freshly re-staged into this test's own scratch
    directory (SKIP_HOST=1, ARM lane only).  CMake-unsupported characters are
    rejected at the gate by ``build_sendspin.sh`` and covered by the consumer
    suite's ``ToolchainConsumerPathRejectionTests``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not _SESSION.get("arm_ok"):
            raise unittest.SkipTest(
                "ARMHF toolchain prefix not provisioned; no ARM path pass is claimed")
        if not (_present("SENDSPIN_ARCHIVE_DIR") and _present("UI_SENDSPIN_CMAKE_DIR")):
            raise unittest.SkipTest("host lane inputs not provisioned (no ARM build possible)")
        cls.pool = Path(os.environ["SENDSPIN_ARMHF_ARCHIVE_DIR"])
        product = os.environ.get("SENDSPIN_PRODUCT_ROOT", "")
        cls.verifier = os.environ.get("SENDSPIN_ARMHF_TOOLCHAIN_MODULE") or (
            str(Path(product) / "build/ci/armhf_toolchain.py") if product else "")
        cls.lock = os.environ.get("SENDSPIN_ARMHF_TOOLCHAIN_LOCK") or (
            str(Path(product) / "build/inputs/armhf-cross-toolchain.lock.json") if product else "")
        if not (cls.verifier and Path(cls.verifier).is_file() and Path(cls.lock).is_file()):
            raise unittest.SkipTest("Product toolchain verifier/lock not available")
        cls._tmp = tempfile.TemporaryDirectory(prefix="sendspin-arm-path-")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_verified_prefix_with_space_and_dollar_builds(self) -> None:
        root = Path(self._tmp.name)
        prefix = root / "pre fix$dir" / "p"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        stage = subprocess.run(
            [sys.executable, self.verifier, "stage", "--lock", self.lock,
             "--archives", str(self.pool), "--prefix", str(prefix)],
            capture_output=True, text=True, timeout=600,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(stage.returncode, 0, stage.stderr[-2000:])

        out = root / "output"
        env = dict(os.environ)
        env.pop("SKIP_ARM", None)
        env.update(SENDSPIN_ARMHF_PREFIX=str(prefix),
                   SENDSPIN_ARMHF_ARCHIVE_DIR=str(self.pool),
                   SENDSPIN_ARMHF_TOOLCHAIN_MODULE=self.verifier,
                   SENDSPIN_ARMHF_TOOLCHAIN_LOCK=self.lock,
                   SKIP_HOST="1", KEEP_WORK="1")
        proc = subprocess.run(["bash", str(BUILD_SCRIPT), str(out)], env=env,
                              capture_output=True, text=True, timeout=1800)
        log = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, log[-4000:])

        report = _text(out / "elf-report.txt")
        self.assertRegex(report, r"Class:\s+ELF32")
        self.assertRegex(report, r"Machine:\s+ARM")
        self.assertIn("elf_closure=pass", report)
        self.assertTrue((out / "sendspin_adapter_tests.armhf").is_file(), log[-2000:])

        toolchain = out / "work" / "arm-toolchain.cmake"
        self.assertTrue(toolchain.is_file(), "generated arm-toolchain.cmake missing")
        text = _text(toolchain)
        # Every derived path is quoted, so CMake parses a spaced/'$' prefix as
        # one value instead of a list.
        self.assertIn(f'set(CMAKE_C_COMPILER "{prefix}/usr/bin/arm-linux-gnueabihf-gcc")', text)
        self.assertIn(f'set(CMAKE_CXX_COMPILER "{prefix}/usr/bin/arm-linux-gnueabihf-g++")', text)
        self.assertIn(f'set(CMAKE_SYSROOT "{prefix}")', text)
        self.assertIn(f'set(CMAKE_FIND_ROOT_PATH "{prefix}")', text)


# ---------------------------------------------------------------------------
# Verifier-suite nested-result regression harness
# ---------------------------------------------------------------------------
# The runner executes each verifier suite in a child interpreter and must judge
# it from a *structured result*, never from a prose tail: an unprovisioned
# consumer run legitimately reports "OK (skipped=2)" for its two source-backed
# classes (ToolchainConsumerNegativeTests / ToolchainConsumerBootstrapTests),
# while every other skip, a zero-test run or a non-zero child exit is a failure.
# These fixtures reproduce that exact shape with no host inputs.

def _fixture_module(directory: Path, name: str, classes: list[str]) -> Path:
    body = "import unittest\n\n\n" + "\n\n\n".join(classes) + "\n"
    path = directory / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def _fixture_passing_class(name: str, count: int = 1) -> str:
    lines = [f"class {name}(unittest.TestCase):"]
    for index in range(count):
        lines.append(f"    def test_pass_{index}(self):")
        lines.append("        self.assertTrue(True)")
    return "\n".join(lines)


def _fixture_skipping_class(name: str, reason: str) -> str:
    return (f"class {name}(unittest.TestCase):\n"
            f"    @classmethod\n"
            f"    def setUpClass(cls):\n"
            f"        raise unittest.SkipTest({reason!r})\n"
            f"\n"
            f"    def test_never_runs(self):\n"
            f"        raise AssertionError('a skipped class body must not execute')")


def _fixture_failing_class(name: str) -> str:
    return (f"class {name}(unittest.TestCase):\n"
            f"    def test_intentional_failure(self):\n"
            f"        self.fail('intentional nested failure')")


def _fixture_unexpected_success_class(name: str) -> str:
    """A class whose only test is an ``expectedFailure`` that unexpectedly
    passes, so ``TestResult.unexpectedSuccesses`` is populated (with the test
    *instance*)."""
    return (f"class {name}(unittest.TestCase):\n"
            f"    @unittest.expectedFailure\n"
            f"    def test_unexpectedly_passes(self):\n"
            f"        self.assertTrue(True)")


ALLOWED_SKIP_FIXTURE_CLASSES = ("AllowedSkipA", "AllowedSkipB")


class VerifierSuiteNestedGateTests(unittest.TestCase):
    """Failing-first regressions for the nested verifier-suite gate."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sendspin-nested-gate-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _allowed_skips_fixture(self) -> Path:
        return _fixture_module(self.dir, "fixture_allowed_skips", [
            _fixture_skipping_class("AllowedSkipA", "unavailable source A"),
            _fixture_skipping_class("AllowedSkipB", "unavailable source B"),
            _fixture_passing_class("PassingTests", 3),
        ])

    def test_fixture_reproduces_the_skipped_two_prose_shape(self) -> None:
        # The fixture must emit exactly the prose the old 'OK$' gate rejected.
        module = self._allowed_skips_fixture()
        proc = subprocess.run([sys.executable, "-m", "unittest", module.stem],
                              cwd=str(self.dir), capture_output=True, text=True,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stderr, r"OK \(skipped=2\)$")

    def test_allowed_unavailable_source_class_skips_are_accepted(self) -> None:
        # BUG: the runner rejected this legitimate "OK (skipped=2)" from the two
        # known unavailable-source consumer classes and returned exit 1.
        module = self._allowed_skips_fixture()
        failures = _nested_module_failures(
            module, self.dir, allow_unavailable_source_skips=True,
            consumer_module=module.stem, allowed_skip_classes=ALLOWED_SKIP_FIXTURE_CLASSES)
        self.assertEqual(failures, [], failures)

    def test_unexpected_skip_is_rejected(self) -> None:
        module = _fixture_module(self.dir, "fixture_unexpected_skip", [
            _fixture_skipping_class("UnexpectedSkip", "not an allowed class"),
            _fixture_passing_class("PassingTests", 2),
        ])
        failures = _nested_module_failures(
            module, self.dir, allow_unavailable_source_skips=True,
            consumer_module=module.stem, allowed_skip_classes=ALLOWED_SKIP_FIXTURE_CLASSES)
        self.assertTrue(any("UnexpectedSkip" in item for item in failures), failures)

    def test_allowed_class_skip_is_rejected_when_provisioned(self) -> None:
        module = self._allowed_skips_fixture()
        failures = _nested_module_failures(
            module, self.dir, allow_unavailable_source_skips=False,
            consumer_module=module.stem, allowed_skip_classes=ALLOWED_SKIP_FIXTURE_CLASSES)
        self.assertTrue(failures)

    def test_nested_failure_is_propagated(self) -> None:
        module = _fixture_module(self.dir, "fixture_failure", [
            _fixture_passing_class("PassingTests", 1),
            _fixture_failing_class("FailingTests"),
        ])
        failures = _nested_module_failures(
            module, self.dir, allow_unavailable_source_skips=True,
            consumer_module=module.stem, allowed_skip_classes=ALLOWED_SKIP_FIXTURE_CLASSES)
        self.assertTrue(any("FailingTests" in item for item in failures), failures)

    def test_nested_import_failure_is_propagated(self) -> None:
        module = self.dir / "fixture_broken.py"
        module.write_text("this is not valid python\n", encoding="utf-8")
        failures = _nested_module_failures(
            module, self.dir, allow_unavailable_source_skips=True,
            consumer_module=module.stem, allowed_skip_classes=ALLOWED_SKIP_FIXTURE_CLASSES)
        self.assertTrue(failures)

    def test_zero_tests_is_rejected(self) -> None:
        module = _fixture_module(self.dir, "fixture_zero", [])
        failures = _nested_module_failures(
            module, self.dir, allow_unavailable_source_skips=True,
            consumer_module=module.stem, allowed_skip_classes=ALLOWED_SKIP_FIXTURE_CLASSES)
        self.assertTrue(any("zero tests" in item for item in failures), failures)

    def test_unexpected_success_keeps_structured_result_and_is_rejected(self) -> None:
        # REGRESSION: TestResult.unexpectedSuccesses holds test *instances*, not
        # (test, exc) tuples.  Iterating it as tuples raised TypeError, produced
        # no SENDPIN_NESTED_RESULT line and lost the failing test id.  A nested
        # suite with a real @expectedFailure that unexpectedly succeeds must
        # still emit the structured result, retain the test id, exit non-zero
        # and be rejected.
        module = _fixture_module(self.dir, "fixture_unexpected_success", [
            _fixture_passing_class("PassingTests", 1),
            _fixture_unexpected_success_class("UnexpectedSuccessTests"),
        ])
        returncode, payload, output = _run_nested_module(module, self.dir)
        self.assertIsNotNone(payload, output)
        assert payload is not None
        # The id is module-qualified, so match on the retained suffix.
        self.assertTrue(any("UnexpectedSuccessTests.test_unexpectedly_passes" in test_id
                            for test_id in payload["unexpected_successes"]),
                        payload["unexpected_successes"])
        self.assertNotEqual(returncode, 0)
        failures = _nested_module_failures(
            module, self.dir, allow_unavailable_source_skips=True,
            consumer_module=module.stem, allowed_skip_classes=ALLOWED_SKIP_FIXTURE_CLASSES)
        self.assertTrue(any("unexpected success" in item
                            and "UnexpectedSuccessTests" in item for item in failures), failures)

    def test_real_consumer_module_matches_provisioning(self) -> None:
        # In this environment the consumer either runs fully (host provisioned)
        # or skips only its two source-backed classes.
        allow = not _SESSION.get("host_ok")
        failures = _nested_module_failures(
            HERE / f"{CONSUMER_MODULE}.py", HERE,
            allow_unavailable_source_skips=allow)
        self.assertEqual(failures, [], failures)

    def test_real_consumer_source_skip_is_rejected_when_provisioned(self) -> None:
        # A nominally provisioned host lane (SENDSPIN_ARCHIVE_DIR set) whose
        # pinned source archive is absent makes the consumer's classes skip; as
        # an unexpected skip in a provisioned context that must FAIL.
        empty = self.dir / "empty-archive"
        empty.mkdir()
        with mock.patch.dict(os.environ,
                             {"SENDSPIN_ARCHIVE_DIR": str(empty)}, clear=False):
            failures = _nested_module_failures(
                HERE / f"{CONSUMER_MODULE}.py", HERE,
                allow_unavailable_source_skips=False)
        self.assertTrue(
            any("ToolchainConsumer" in item for item in failures), failures)


# ---------------------------------------------------------------------------
# Runner exit-contract regressions (real CLI subprocesses)
# ---------------------------------------------------------------------------
# SENDSPIN_REQUIRE_ARM=1 must dominate every acknowledgement path: whenever no
# actual successful ARM fixture run occurred the process must exit non-zero.
# These tests run the *real* runner as a bounded child process in a clean env;
# the runner child loads this same module, so the guard env var stops it from
# recursing into another CLI probe.
CLI_CHILD_GUARD = "SENDSPIN_SDK_CLI_CHILD"


def _cli_env(**overrides) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SENDSPIN_")
           and k not in ("UI_SENDSPIN_CMAKE_DIR", "QEMU_ARM", "SYSROOT",
                         "CROSS_PREFIX", "LD_LIBRARY_PATH", "SKIP_ARM", "SKIP_HOST")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[CLI_CHILD_GUARD] = "1"
    env.update(overrides)
    return env


def _run_runner_cli(env: dict, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "test_sdk_build.py")],
                          cwd=str(HERE), env=env, capture_output=True, text=True,
                          timeout=timeout)


class RunnerExitContractTests(unittest.TestCase):
    """Real-CLI exit contract: SENDSPIN_REQUIRE_ARM=1 dominates acknowledgement."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get(CLI_CHILD_GUARD) == "1":
            raise unittest.SkipTest("runner CLI child probe (recursion guard)")

    def test_require_arm_dominates_acknowledged_unprovisioned_run(self) -> None:
        # Entirely unprovisioned + ALLOW_UNPROVISIONED=1 + REQUIRE_ARM=1 must be
        # exit 2, never an acknowledged exit 0.
        proc = _run_runner_cli(_cli_env(SENDSPIN_ALLOW_UNPROVISIONED="1",
                                        SENDSPIN_REQUIRE_ARM="1"))
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2, out[-4000:])
        self.assertNotIn("acknowledged via SENDSPIN_ALLOW_UNPROVISIONED=1", out)

    def test_require_arm_dominates_partial_arm_without_host(self) -> None:
        # The ARM env is present but the host lane is not, so no ARM fixture can
        # run; with REQUIRE_ARM=1 this must fail closed, never be acknowledged.
        proc = _run_runner_cli(_cli_env(
            SENDSPIN_ALLOW_UNPROVISIONED="1", SENDSPIN_REQUIRE_ARM="1",
            SENDSPIN_PRODUCT_ROOT=str(HERE), SENDSPIN_ARMHF_PREFIX=str(HERE),
            SENDSPIN_ARMHF_ARCHIVE_DIR=str(HERE), SENDSPIN_MDNS_LOCK=str(SOURCE_LOCK),
            SENDSPIN_MDNS_ARCHIVES_DIR=str(HERE)))
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2, out[-4000:])
        self.assertNotIn("acknowledged via SENDSPIN_ALLOW_UNPROVISIONED=1", out)

    def test_acknowledged_unprovisioned_run_without_require_arm_is_zero(self) -> None:
        proc = _run_runner_cli(_cli_env(SENDSPIN_ALLOW_UNPROVISIONED="1"))
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out[-4000:])
        self.assertIn("acknowledged via SENDSPIN_ALLOW_UNPROVISIONED=1", out)

    def test_unprovisioned_run_without_flags_fails_closed(self) -> None:
        proc = _run_runner_cli(_cli_env())
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2, out[-4000:])
        self.assertIn("integration tests did not run", out)


class ExitContractDecisionTests(unittest.TestCase):
    """Branch coverage for the pure exit decision, supplementing the real-CLI
    tests: the host-only and fully provisioned branches require a real build to
    reach through the CLI, so they are pinned directly here."""

    BASE = {"provisioned": False, "host_only": False,
            "allow_unprovisioned": False, "require_arm": False}

    def _code(self, **flags) -> int:
        session = dict(self.BASE)
        session.update(flags)
        return _exit_decision(session)[0]

    def test_actual_arm_run_is_zero(self) -> None:
        self.assertEqual(self._code(provisioned=True), 0)

    def test_actual_arm_run_with_require_arm_is_zero(self) -> None:
        self.assertEqual(self._code(provisioned=True, require_arm=True), 0)

    def test_require_arm_dominates_acknowledgement(self) -> None:
        self.assertEqual(self._code(require_arm=True, allow_unprovisioned=True), 2)

    def test_require_arm_dominates_host_only(self) -> None:
        self.assertEqual(self._code(require_arm=True, host_only=True), 2)

    def test_host_only_without_require_arm_is_zero(self) -> None:
        self.assertEqual(self._code(host_only=True), 0)

    def test_acknowledged_unprovisioned_without_require_arm_is_zero(self) -> None:
        self.assertEqual(self._code(allow_unprovisioned=True), 0)

    def test_plain_unprovisioned_fails_closed(self) -> None:
        self.assertEqual(self._code(), 2)


def _load_suite():
    return unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])


def _exit_decision(session: dict) -> tuple[int, str, bool]:
    """Pure exit-contract decision for a run whose test suite succeeded.

    Returns ``(exit_code, message, is_error)``.  An actual ARM fixture pass is
    only ever produced by a *fully provisioned* run; in every other
    configuration ``SENDSPIN_REQUIRE_ARM=1`` dominates and fails closed before
    any acknowledgement or host-only success is reported, so a required-ARM
    intent can never be demoted into exit 0.
    """
    if session.get("provisioned"):
        # The ARM lane actually built and ran; an ARM pass is claimed.
        return 0, "", False
    if session.get("require_arm"):
        return 2, ("FAIL: no verified ARM integration pass was produced and "
                   "SENDSPIN_REQUIRE_ARM=1 (fail closed)."), True
    if session.get("host_only"):
        # The host lane ran truthfully; the ARM lane cannot claim a pass without
        # the verified staged toolchain prefix.
        return 0, ("SENDSPIN: host lane ran; ARM integration tests NOT RUN "
                   "(no verified ARMHF staged toolchain prefix). No ARM pass is claimed."), False
    if session.get("allow_unprovisioned"):
        return 0, ("SENDSPIN: integration tests NOT RUN (inputs unprovisioned; "
                   "acknowledged via SENDSPIN_ALLOW_UNPROVISIONED=1)"), False
    return 2, ("FAIL: Sendspin SDK inputs are not provisioned; integration tests did not run. "
               "Set SENDSPIN_ALLOW_UNPROVISIONED=1 to acknowledge an unprovisioned run."), True


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(_load_suite())
    if result.testsRun == 0:
        print("FAIL: no tests ran", file=sys.stderr)
        return 1
    if not result.wasSuccessful():
        return 1
    code, message, is_error = _exit_decision(_SESSION)
    if message:
        print(message, file=sys.stderr if is_error else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
