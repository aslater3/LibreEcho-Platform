#!/usr/bin/env python3
"""Strict contract tests for the pinned Opus prefix consumed by build_ui_bundle.sh.

``build_ui_bundle.sh --verify-opus-prefix DIR`` is the exact routine the
production bundle runs before it links any Opus archive.  These tests pin its
fail-closed behaviour against real prefixes and against prefixes synthesized
with the host toolchain:

* a missing directory, a prefix without its identity or provenance records, and
  a tampered identity (name / target / config), a tampered source pin (against
  ``opus/SOURCE.lock``), a tampered archive hash, a non-ARM32 member, an
  HTTP/URL entry point, and the honest Opus *stub* archive are all refused;
* a real 32-bit ARM32 prefix (LIBREECHO_OPUS_ARM_PREFIX, default
  ``~/.hermes/cache/scratch/opus-arm-prefix``) is accepted.

The stubbed/HTTP archives are compiled from tiny C sources with the host
compiler, so the symbol-level contract is exercised even where no ARM toolchain
exists.  Nothing here downloads anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "build_ui_bundle.sh"
LOCK_PATH = HERE / "opus" / "SOURCE.lock"
COMPONENTS = ("libogg", "opus", "opusfile")
ARCHIVE_FILES = {"libogg": "libogg.a", "opus": "libopus.a", "opusfile": "libopusfile.a"}
HEADERS = (
    "ogg/ogg.h",
    "ogg/os_types.h",
    "ogg/config_types.h",
    "opus/opus.h",
    "opus/opus_multistream.h",
    "opus/opus_types.h",
    "opus/opus_defines.h",
    "opus/opusfile.h",
)
DEFAULT_ARM_PREFIX = Path.home() / ".hermes" / "cache" / "scratch" / "opus-arm-prefix"
ARM_PREFIX = Path(os.environ.get("LIBREECHO_OPUS_ARM_PREFIX", DEFAULT_ARM_PREFIX))
REQUIRED_DECODER_SYMBOLS: list[str] = [
    "op_open_callbacks",
    "op_read_stereo",
    "op_free",
    "op_channel_count",
]
HTTP_SYMBOLS: list[str] = ["op_open_url"]


def run_verify(prefix: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--verify-opus-prefix", str(prefix)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def compiler() -> str | None:
    return shutil.which("cc") or shutil.which("gcc")


def archiver() -> str | None:
    return shutil.which("ar")


def _write_archive(source_dir: Path, name: str, symbols: list[str]) -> Path:
    """Compile a tiny object and archive it (an empty symbol list yields a stub)."""
    cc = compiler()
    ar = archiver()
    assert cc is not None and ar is not None
    body = "".join("int %s(void) { return 0; }\n" % s for s in symbols)
    source = source_dir / ("%s.c" % name)
    source.write_text(body or "int %s_placeholder(void) { return 0; }\n" % name, encoding="utf-8")
    obj = source_dir / ("%s.o" % name)
    subprocess.run([cc, "-c", str(source), "-o", str(obj)], check=True, capture_output=True)
    archive = source_dir / ARCHIVE_FILES.get(name, "%s.a" % name)
    subprocess.run([ar, "rcs", str(archive), str(obj)], check=True, capture_output=True)
    return archive


def _synthetic_prefix(
    root: Path,
    *,
    identity_target: str = "arm-linux-musleabihf",
    identity_config: str | None = None,
    identity_archives: dict | None = None,
    source_target: str | None = None,
    source_pins: dict | None = None,
    source_artifacts: dict | None = None,
    opusfile_symbols: list[str] | None = None,
) -> Path:
    """Build a prefix that passes every check up to the point under test."""
    data = lock()
    prefix = root / "prefix"
    for header in HEADERS:
        path = prefix / "include" / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("/* synthetic */\n", encoding="utf-8")
    (prefix / "lib").mkdir(parents=True, exist_ok=True)
    work = root / "objects"
    work.mkdir()
    _write_archive(work, "libogg", [])
    _write_archive(work, "opus", [])
    file_symbols = list(opusfile_symbols) if opusfile_symbols is not None else list(
        REQUIRED_DECODER_SYMBOLS
    )
    _write_archive(work, "opusfile", file_symbols)
    for name in COMPONENTS:
        shutil.copyfile(work / ARCHIVE_FILES[name], prefix / "lib" / ARCHIVE_FILES[name])

    pins = {name: data["components"][name]["source_sha256"] for name in COMPONENTS}
    licenses = {name: data["components"][name]["license_sha256"] for name in COMPONENTS}
    artifacts = {
        name: hashlib.sha256((prefix / "lib" / ARCHIVE_FILES[name]).read_bytes()).hexdigest()
        for name in COMPONENTS
    }
    identity = {
        "schema": 1,
        "name": data["name"],
        "target": identity_target,
        "config": data["config"] if identity_config is None else identity_config,
        "archives": pins if identity_archives is None else identity_archives,
    }
    source = {
        "schema": 1,
        "name": data["name"],
        "target": identity_target if source_target is None else source_target,
        "config": identity["config"],
        "http_enabled": False,
        "components": {
            name: {
                "source_archive_sha256": pins[name] if source_pins is None else source_pins[name],
                "license_sha256": licenses[name],
            }
            for name in COMPONENTS
        },
        "artifacts": artifacts if source_artifacts is None else source_artifacts,
    }
    (prefix / "opus-identity.json").write_text(
        json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (prefix / "opus-source.json").write_text(
        json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return prefix


class ScriptWiringTests(unittest.TestCase):
    """The bundle must wire the pinned prefix and must not demand unused symbols."""

    def test_bundle_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bundle_passes_the_makefile_opus_interface(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("RADIOD_OPUS_PREFIX=\"$OPUS_ROOT\"", body)
        self.assertIn("RADIOD_OPUS_LIBS=\"$OPUS_LIBS\"", body)

    def test_bundle_does_not_demand_op_open_file(self) -> None:
        # libreecho-radiod calls the callback API; op_open_file is a helper the
        # daemon never uses, so the capability symbols must not require it.
        body = SCRIPT.read_text(encoding="utf-8")
        line = next(
            row
            for row in body.splitlines()
            if row.startswith("OPUS_DECODE_SYMBOLS=(")
        )
        self.assertIn("op_open_callbacks", line)
        self.assertNotIn("op_open_file", line)

    def test_capability_verify_precedes_the_strip(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(
            body.index('verify_radiod_opus_capability "$UI_SOURCE'),
            body.index('"$STRIP_BIN" --strip-unneeded'),
            "the strip-safe Opus capability proof must run before stripping",
        )

    def test_http_url_surface_is_forbidden(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        for symbol in ("op_open_url", "op_http_open"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, body)


class RefusalTests(unittest.TestCase):
    def test_missing_directory_is_refused(self) -> None:
        result = run_verify(Path("/nonexistent-opus-prefix-for-contract-test"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unavailable", result.stderr)

    def test_prefix_without_identity_record_is_refused(self) -> None:
        with self._tmp() as td:
            prefix = Path(td) / "prefix"
            prefix.mkdir()
            result = run_verify(prefix)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("identity record", result.stderr)

    def test_prefix_without_provenance_record_is_refused(self) -> None:
        with self._tmp() as td:
            prefix = Path(td) / "prefix"
            prefix.mkdir()
            (prefix / "opus-identity.json").write_text("{}\n", encoding="utf-8")
            result = run_verify(prefix)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("provenance record", result.stderr)

    @staticmethod
    def _tmp():
        import tempfile

        return tempfile.TemporaryDirectory()


class SyntheticPrefixTests(unittest.TestCase):
    """Exercise the pin/symbol contract with host-compiled archives."""

    def setUp(self) -> None:
        if compiler() is None or archiver() is None:
            self.skipTest("a host C compiler and ar are required for the synthetic prefix")
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_synthetic_whole_prefix_reaches_the_member_arch_check(self) -> None:
        # Everything passes until the archive members turn out to be host
        # objects: the ARM32 member pin is the last line of defence.
        prefix = _synthetic_prefix(self.root)
        result = run_verify(prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-ARM32 member", result.stderr)

    def test_config_mismatch_is_refused(self) -> None:
        prefix = _synthetic_prefix(self.root, identity_config="static,no-http")
        result = run_verify(prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config mismatch", result.stderr)

    def test_target_mismatch_is_refused(self) -> None:
        prefix = _synthetic_prefix(self.root, identity_target="x86_64-linux-gnu")
        result = run_verify(prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target mismatch", result.stderr)

    def test_source_pin_mismatch_is_refused(self) -> None:
        prefix = _synthetic_prefix(self.root, identity_archives={"libogg": "0" * 64,
                                                                 "opus": "0" * 64,
                                                                 "opusfile": "0" * 64})
        result = run_verify(prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source pin mismatch", result.stderr)

    def test_archive_hash_mismatch_is_refused(self) -> None:
        prefix = _synthetic_prefix(
            self.root, source_artifacts={"libogg": "0" * 64, "opus": "0" * 64,
                                         "opusfile": "0" * 64}
        )
        result = run_verify(prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match its provenance record", result.stderr)

    def test_honest_stub_archive_is_refused(self) -> None:
        # The capability-disabled stub carries no decoder symbols at all.
        prefix = _synthetic_prefix(self.root, opusfile_symbols=[])
        result = run_verify(prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing the decoder entry point", result.stderr)

    def test_http_url_entry_point_is_refused(self) -> None:
        prefix = _synthetic_prefix(
            self.root, opusfile_symbols=list(REQUIRED_DECODER_SYMBOLS) + list(HTTP_SYMBOLS)
        )
        result = run_verify(prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP/URL", result.stderr)


class RealArmPrefixTests(unittest.TestCase):
    def test_real_arm_prefix_is_accepted(self) -> None:
        if not ARM_PREFIX.is_dir():
            self.skipTest(
                "no real ARM32 Opus prefix at %s (set LIBREECHO_OPUS_ARM_PREFIX to "
                "the pinned build_opus.sh output to exercise the accept path)"
                % ARM_PREFIX
            )
        result = run_verify(ARM_PREFIX)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ui_opus_prefix=ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
