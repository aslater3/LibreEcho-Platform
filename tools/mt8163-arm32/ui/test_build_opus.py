#!/usr/bin/env python3
"""Tests for the pinned static Opus build helper (ui/build_opus.sh).

The helper is exercised for its fail-closed behaviour: pinned source hashes,
the mandatory explicit prefix, the identity cache, and the refusal paths. It
never downloads, so these tests use fabricated archives for the refusal cases
and only run a full build when the real archives are supplied explicitly
through LIBREECHO_OPUS_TEST_ARCHIVES.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "build_opus.sh"
LOCK_PATH = HERE / "opus" / "SOURCE.lock"
LICENSE_DIR = HERE / "opus" / "licenses"
DECODE_TEST_C = HERE / "opus" / "test_decode_host.c"
COMPONENTS = ("libogg", "opus", "opusfile")


def run_builder(args, timeout=120):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def compiler() -> str | None:
    return shutil.which("cc") or shutil.which("gcc")


def compiler_target(cc: str) -> str:
    return subprocess.run(
        [cc, "-dumpmachine"], capture_output=True, text=True, check=True
    ).stdout.strip()


def base_archive_args(*paths: str) -> list[str]:
    return [
        "--ogg-archive", paths[0],
        "--opus-archive", paths[1],
        "--opusfile-archive", paths[2],
    ]


class SourceLockTests(unittest.TestCase):
    def test_lock_pins_three_sources_with_urls_and_hashes(self) -> None:
        data = lock()
        self.assertEqual(set(data["components"]), set(COMPONENTS))
        for name in COMPONENTS:
            component = data["components"][name]
            with self.subTest(component=name):
                self.assertTrue(component["source_url"].startswith("https://"))
                self.assertRegex(component["source_sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(component["license_sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(component["license"], "BSD-3-Clause")

    def test_committed_license_copies_match_the_pinned_hashes(self) -> None:
        data = lock()
        for name in COMPONENTS:
            component = data["components"][name]
            copy = LICENSE_DIR / component["license_copy"]
            with self.subTest(component=name):
                self.assertTrue(copy.is_file(), copy)
                digest = hashlib.sha256(copy.read_bytes()).hexdigest()
                self.assertEqual(digest, component["license_sha256"])

    def test_builder_never_fetches_sources(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        # Comments may name the dependencies that are being *excluded*; only
        # executable lines decide whether the builder could fetch anything.
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        for token in ("curl", "wget", "git clone", "pip install", "git fetch"):
            with self.subTest(token=token):
                self.assertNotIn(token, code)


class ShellTests(unittest.TestCase):
    def test_builder_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_decode_test_source_is_present(self) -> None:
        body = DECODE_TEST_C.read_text(encoding="utf-8")
        self.assertIn("op_open_file", body)
        self.assertIn("op_read_float", body)


class UsageTests(unittest.TestCase):
    def test_output_prefix_is_required(self) -> None:
        result = run_builder(
            ["--ogg-archive", "a", "--opus-archive", "b", "--opusfile-archive", "c"]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)

    def test_unknown_option_is_rejected(self) -> None:
        result = run_builder(["--not-an-option"])
        self.assertEqual(result.returncode, 2)


class RefusalTests(unittest.TestCase):
    def _fabricated(self, root: Path) -> list[str]:
        paths = []
        for name in ("ogg.tar.gz", "opus.tar.gz", "opusfile.tar.gz"):
            path = root / name
            path.write_bytes(b"not the pinned upstream archive")
            paths.append(str(path))
        return paths

    def test_wrong_source_hash_is_refused_before_any_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archives = self._fabricated(root)
            prefix = root / "prefix"
            result = run_builder(base_archive_args(*archives) + ["--output", str(prefix)])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("hash mismatch", result.stderr)
            self.assertFalse(prefix.exists())

    def test_missing_archive_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = run_builder(
                base_archive_args("no1", "no2", "no3") + ["--output", str(root / "p")]
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing or unsafe", result.stderr)

    def test_existing_prefix_without_identity_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "prefix"
            prefix.mkdir()
            result = run_builder(base_archive_args("a", "b", "c") + ["--output", str(prefix)])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no identity record", result.stderr)

    @unittest.skipUnless(compiler(), "no host C compiler available")
    def test_identity_mismatch_is_refused(self) -> None:
        cc = compiler()
        assert cc is not None
        data = lock()
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "prefix"
            prefix.mkdir()
            (prefix / "opus-identity.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "name": data["name"],
                        "target": "definitely-not-" + compiler_target(cc),
                        "config": data["config"],
                        "archives": {
                            name: data["components"][name]["source_sha256"]
                            for name in COMPONENTS
                        },
                    }
                )
                + "\n"
            )
            result = run_builder(
                base_archive_args("a", "b", "c") + ["--output", str(prefix)]
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("identity mismatch", result.stderr)

    @unittest.skipUnless(compiler(), "no host C compiler available")
    def test_identity_cache_hit_needs_no_archives(self) -> None:
        cc = compiler()
        assert cc is not None
        data = lock()
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "prefix"
            prefix.mkdir()
            (prefix / "opus-identity.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "name": data["name"],
                        "target": compiler_target(cc),
                        "config": data["config"],
                        "archives": {
                            name: data["components"][name]["source_sha256"]
                            for name in COMPONENTS
                        },
                    }
                )
                + "\n"
            )
            # The archives do not exist: a matching identity is a cache hit and
            # must short-circuit before the archives are ever read.
            result = run_builder(
                base_archive_args("no1", "no2", "no3")
                + ["--output", str(prefix), "--cc", cc]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("opus_identity_cache=hit", result.stdout)


class ToolchainResolutionTests(unittest.TestCase):
    """Tools may be named by PATH, not just by an explicit path.

    The hosted CI invokes the builder with bare cross-prefixed names
    (``--cc arm-linux-gnueabihf-gcc --ar arm-linux-gnueabihf-ar``).  Those must
    be resolved on PATH; treating them as a literal path relative to the current
    directory rejects a perfectly available toolchain.
    """

    def _cross_bindir(self, root: Path) -> Path:
        cc = compiler()
        ar = shutil.which("ar")
        if not cc or not ar:
            self.skipTest("host cc/ar unavailable")
        bindir = root / "fake-cross-bin"
        bindir.mkdir()
        (bindir / "arm-linux-gnueabihf-gcc").symlink_to(cc)
        (bindir / "arm-linux-gnueabihf-ar").symlink_to(ar)
        return bindir

    def test_bare_cross_tool_names_resolve_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir = self._cross_bindir(root)
            archives = []
            for name in ("ogg.tar.gz", "opus.tar.gz", "opusfile.tar.gz"):
                path = root / name
                path.write_bytes(b"not the pinned upstream archive")
                archives.append(str(path))
            env = dict(os.environ)
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                [
                    "bash", str(SCRIPT), *base_archive_args(*archives),
                    "--output", str(root / "prefix"),
                    "--cc", "arm-linux-gnueabihf-gcc",
                    "--ar", "arm-linux-gnueabihf-ar",
                ],
                capture_output=True, text=True, env=env, timeout=120,
            )
            # Both bare names must resolve, so the builder reaches the next
            # fail-closed gate (the fabricated archive hashes) instead of
            # rejecting the archiver as unavailable.
            self.assertNotIn("is unavailable", result.stderr)
            self.assertIn("hash mismatch", result.stderr)

    def test_unresolvable_archiver_is_still_refused(self) -> None:
        cc = compiler()
        if not cc:
            self.skipTest("no host C compiler available")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bash = shutil.which("bash") or "/bin/bash"
            env = dict(os.environ)
            env["PATH"] = str(root)  # nothing outside the (empty) dir resolves
            result = subprocess.run(
                [
                    bash, str(SCRIPT), *base_archive_args("a", "b", "c"),
                    "--output", str(root / "prefix"),
                    "--cc", cc, "--ar", "arm-linux-gnueabihf-ar",
                ],
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("archiver is unavailable", result.stderr)


class EndToEndTests(unittest.TestCase):
    """Full host build + real decode. Opt in with the pinned archives."""

    def test_host_build_decodes_ogg_opus(self) -> None:
        root = os.environ.get("LIBREECHO_OPUS_TEST_ARCHIVES")
        if not root:
            self.skipTest("set LIBREECHO_OPUS_TEST_ARCHIVES to the pinned archives dir")
        cc = compiler()
        if not cc:
            self.skipTest("no host C compiler available")
        archives = Path(root)
        names = ("libogg-1.3.5.tar.gz", "opus-1.4.tar.gz", "opusfile-0.12.tar.gz")
        if not all((archives / name).is_file() for name in names):
            self.skipTest("pinned opus archives not found in LIBREECHO_OPUS_TEST_ARCHIVES")
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "prefix"
            built = run_builder(
                base_archive_args(*[str(archives / n) for n in names])
                + ["--output", str(prefix), "--cc", cc, "--jobs", "4"],
                timeout=590,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertTrue((prefix / "lib" / "libopusfile.a").is_file())
            # The HTTP/TLS-free contract: no URL entry point in the archive.
            symbols = subprocess.run(
                ["nm", "--defined-only", str(prefix / "lib" / "libopusfile.a")],
                capture_output=True,
                text=True,
            ).stdout
            self.assertNotIn("op_open_url", symbols)
            binary = Path(td) / "test_decode"
            compiled = subprocess.run(
                [
                    cc, "-O2", "-o", str(binary), str(DECODE_TEST_C),
                    "-I", str(prefix / "include" / "opus"),
                    "-I", str(prefix / "include"),
                    str(prefix / "lib" / "libopusfile.a"),
                    str(prefix / "lib" / "libopus.a"),
                    str(prefix / "lib" / "libogg.a"),
                    "-lm",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            decoded = subprocess.run(
                [str(binary), td], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(decoded.returncode, 0, decoded.stderr)
            self.assertIn("opus_decode=ok", decoded.stdout)
            self.assertIn("non_opus=rejected", decoded.stdout)


if __name__ == "__main__":
    unittest.main()
