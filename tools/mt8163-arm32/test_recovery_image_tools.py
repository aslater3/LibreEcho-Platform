#!/usr/bin/env python3
"""Fail-closed unit tests for the MT8163 recovery image tools."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent


def pipeline_text(name: str) -> str:
    pipeline_root = os.environ.get("LIBREECHO_PIPELINE_ROOT")
    if not pipeline_root:
        raise unittest.SkipTest("set LIBREECHO_PIPELINE_ROOT for pipeline contract tests")
    root = Path(pipeline_root)
    path = root / name
    if not path.is_file():
        raise unittest.SkipTest(f"canonical pipeline unavailable: {path}")
    return path.read_text()


def pipeline_file(name: str) -> Path:
    pipeline_root = os.environ.get("LIBREECHO_PIPELINE_ROOT")
    if not pipeline_root:
        raise unittest.SkipTest("set LIBREECHO_PIPELINE_ROOT for pipeline contract tests")
    path = Path(pipeline_root) / name
    if not path.is_file():
        raise unittest.SkipTest(f"canonical pipeline unavailable: {path}")
    return path


def kernel_source_file(relative: str) -> Path:
    kernel_root = os.environ.get("LIBREECHO_KERNEL_SRC")
    if not kernel_root:
        raise unittest.SkipTest("set LIBREECHO_KERNEL_SRC for kernel contract tests")
    path = Path(kernel_root) / relative
    if not path.is_file():
        raise unittest.SkipTest(f"kernel source unavailable: {path}")
    return path


def load_tool(name: str):
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"mt8163_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = load_tool("build_recovery_image")
verifier = load_tool("verify_recovery_image")


def newc_member(name: bytes, mode: int = stat.S_IFREG | 0o644,
                payload: bytes = b"") -> bytes:
    name_field = name + b"\0"
    values = (
        1, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(name_field), 0,
    )
    header = b"070701" + b"".join(f"{value:08x}".encode() for value in values)
    record = header + name_field
    record += b"\0" * (-len(record) & 3)
    record += payload
    record += b"\0" * (-len(record) & 3)
    return record


def newc_archive(*members: bytes, tail: bytes = b"") -> bytes:
    return b"".join(members) + newc_member(b"TRAILER!!!", 0) + tail


class TtsProcessModeTests(unittest.TestCase):
    def test_external_tts_manifest_matches_cancellable_production_default(self) -> None:
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        self.assertIn('"in_process": False', builder)
        self.assertIn('tts.get("in_process") is not False', verifier_source)


class NewcTests(unittest.TestCase):
    def test_canonical_member_and_zero_padding(self) -> None:
        entries = verifier.parse_newc(
            newc_archive(newc_member(b"./foo", payload=b"value"), tail=b"\0" * 17)
        )
        self.assertEqual(entries["foo"].data, b"value")

    def test_unsafe_or_ambiguous_names_are_rejected(self) -> None:
        unsafe_names = (
            b"/absolute", b"../escape", b"a/../escape", b"././alias",
            b"a//alias", b"a/./alias", b"interior\0nul",
        )
        for name in unsafe_names:
            with self.subTest(name=name), self.assertRaises(SystemExit):
                verifier.parse_newc(newc_archive(newc_member(name)))

    def test_duplicate_normalized_member_is_rejected(self) -> None:
        archive = newc_archive(newc_member(b"foo"), newc_member(b"./foo"))
        with self.assertRaises(SystemExit):
            verifier.parse_newc(archive)

    def test_duplicate_trailer_and_nonzero_tail_are_rejected(self) -> None:
        for archive in (
            newc_archive() + newc_member(b"TRAILER!!!", 0),
            newc_archive(tail=b"\x01"),
            newc_member(b"TRAILER!!!", 0) + newc_member(b"late"),
        ):
            with self.subTest(size=len(archive)), self.assertRaises(SystemExit):
                verifier.parse_newc(archive)


class SymlinkTests(unittest.TestCase):
    @staticmethod
    def entry(name: str, mode: int, payload: bytes = b""):
        return verifier.Entry(name, mode, 0, 0, 0, payload)

    def test_relative_in_tree_symlink_is_accepted(self) -> None:
        entries = {
            "bin": self.entry("bin", stat.S_IFDIR | 0o755),
            "bin/target": self.entry("bin/target", stat.S_IFREG | 0o755),
            "bin/link": self.entry("bin/link", stat.S_IFLNK | 0o777, b"target"),
        }
        verifier.validate_symlinks(entries)

    def test_absolute_escape_dangling_and_loop_are_rejected(self) -> None:
        cases = (
            {"link": self.entry("link", stat.S_IFLNK | 0o777, b"/outside")},
            {"nested/link": self.entry("nested/link", stat.S_IFLNK | 0o777, b"../../outside")},
            {"link": self.entry("link", stat.S_IFLNK | 0o777, b"missing")},
            {
                "one": self.entry("one", stat.S_IFLNK | 0o777, b"two"),
                "two": self.entry("two", stat.S_IFLNK | 0o777, b"one"),
            },
        )
        for entries in cases:
            with self.subTest(entries=tuple(entries)), self.assertRaises(SystemExit):
                verifier.validate_symlinks(entries)

    def test_member_beneath_symlink_parent_is_rejected(self) -> None:
        entries = {
            "real": self.entry("real", stat.S_IFDIR | 0o755),
            "alias": self.entry("alias", stat.S_IFLNK | 0o777, b"real"),
            "alias/file": self.entry("alias/file", stat.S_IFREG | 0o644),
        }
        with self.assertRaises(SystemExit):
            verifier.validate_archive_tree(entries)


class SourceTests(unittest.TestCase):
    @staticmethod
    def vendor_import_fixture(
            root: Path, source_symlink_component: str | None = None
    ) -> tuple[Path, Path, Path, Path, dict[str, str], tuple[tuple[str, str, bytes], ...]]:
        importer = TOOLS_DIR / "initramfs/libreecho-vendor-import"
        data = root / "data"
        source = root / "system-a"
        firmware = root / "firmware"
        data.mkdir()
        firmware.mkdir()
        records = (
            ("system/vendor/firmware/ROMv2_lm_patch_1_0_hdr.bin", "ROMv2_lm_patch_1_0_hdr.bin", b"patch-zero"),
            ("system/vendor/firmware/ROMv2_lm_patch_1_1_hdr.bin", "ROMv2_lm_patch_1_1_hdr.bin", b"patch-one"),
            ("system/vendor/firmware/WIFI_RAM_CODE_8163", "WIFI_RAM_CODE_8163", b"wifi-code"),
            ("system/vendor/firmware/WMT_SOC.cfg", "WMT_SOC.cfg", b"wmt-config"),
        )
        if source_symlink_component == "system":
            outside = root / "outside-system"
            payload_root = outside / "vendor/firmware"
            payload_root.mkdir(parents=True)
            source.mkdir()
            (source / "system").symlink_to(outside, target_is_directory=True)
        elif source_symlink_component == "vendor":
            outside = root / "outside-vendor"
            payload_root = outside / "firmware"
            payload_root.mkdir(parents=True)
            (source / "system").mkdir(parents=True)
            (source / "system/vendor").symlink_to(outside, target_is_directory=True)
        elif source_symlink_component == "firmware":
            outside = root / "outside-firmware"
            outside.mkdir()
            (source / "system/vendor").mkdir(parents=True)
            (source / "system/vendor/firmware").symlink_to(
                outside, target_is_directory=True
            )
            payload_root = outside
        elif source_symlink_component is None:
            payload_root = source / "system/vendor/firmware"
            payload_root.mkdir(parents=True)
        else:
            raise ValueError(f"unsupported source symlink component: {source_symlink_component}")
        lines = []
        for source_name, target_name, payload in records:
            (payload_root / Path(source_name).name).write_bytes(payload)
            lines.append(
                f"{hashlib.sha256(payload).hexdigest()}|{len(payload)}|"
                f"{source_name}|{target_name}\n"
            )
        specification = root / "assets.tsv"
        specification.write_text("".join(lines))
        environment = {
            **os.environ,
            "LIBREECHO_VENDOR_TEST_MODE": "1",
            "DATA_ROOT": str(data),
            "LIBREECHO_VENDOR_SOURCE_ROOT": str(source),
            "LIBREECHO_VENDOR_FIRMWARE_ROOT": str(firmware),
            "LIBREECHO_VENDOR_SPEC": str(specification),
            "LIBREECHO_VENDOR_STAGE_PARENT": str(root / "vendor-stage"),
            "LIBREECHO_VENDOR_STATUS_PATH": str(root / "run/libreecho/vendor-import.status"),
        }
        return importer, data, source, firmware, environment, records

    def test_platform_root_documentation_is_project_specific(self) -> None:
        repo_root = TOOLS_DIR.parents[1]
        readme = repo_root / "README.md"
        historical = repo_root / "docs/historical/upstream-linux-3x-README"
        self.assertTrue(readme.is_file())
        self.assertTrue(historical.is_file())
        source = readme.read_text()
        normalized = source.casefold()
        for required in (
            "libreecho-platform",
            "libreecho-linux-6.1",
            "libreecho-ui",
            "libreecho-build",
            "owner-local firmware",
            "historical linux 3.18",
            "libreecho_pipeline_root",
        ):
            self.assertIn(required, normalized)
        self.assertNotIn("/home/andy/", source)
        self.assertIn("Linux kernel release 3.x", historical.read_text())

    def test_release_tools_cannot_bundle_startup_audio(self) -> None:
        for name in ("build_recovery_image.py", "verify_recovery_image.py"):
            source = (TOOLS_DIR / name).read_text()
            with self.subTest(name=name):
                self.assertNotIn("windows95", source.lower())
                self.assertNotIn("startup_audio", source)
                self.assertNotIn("startup_playback", source)
                self.assertNotIn("--startup-audio", source)

        pipeline_root = pipeline_file("build.sh").parent
        self.assertFalse((pipeline_root / "inputs/windows95-startup.wav").exists())
        self.assertNotIn(
            "windows95", (pipeline_root / "inputs/SHA256SUMS").read_text().lower()
        )

    def test_connectivity_helpers_strip_toolchain_debug_paths(self) -> None:
        source = (TOOLS_DIR / "connectivity/build_connectivity_helpers.sh").read_text()
        self.assertIn("-Wl,--strip-all", source)
        self.assertIn("contains a host/build path", source)

    def test_wakeword_runtime_uses_explicit_re2_archive(self) -> None:
        source = (TOOLS_DIR / "wakeword/build_runtime.sh").read_text()
        self.assertIn(
            "${LIBREECHO_WAKE_RE2_ARCHIVE:?ERROR:", source
        )
        self.assertIn('RE2_ARCHIVE="$RE2_ARCHIVE"', source)

    def test_wakeword_runtime_maps_paths_for_reproducible_archives(self) -> None:
        # ORT logging macros expand __FILE__ into compiled objects.  Without
        # canonical path prefixes, every per-run build directory produces
        # different bytes for identical inputs, and the fail-closed wake-ort
        # component cache refuses to store over its own key.  Both the build
        # directory and the source checkout must be rewritten to stable
        # prefixes on every reduced-ORT build.
        source = (TOOLS_DIR / "wakeword/build_runtime.sh").read_text()
        self.assertIn(
            'ort_repro_flags="-ffile-prefix-map=$ORT_BUILD=ort-build '
            '-ffile-prefix-map=$ORT_SOURCE=ort-src"',
            source,
        )
        self.assertIn(
            "-DCMAKE_C_FLAGS=\"-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard "
            "-ffunction-sections -fdata-sections $ort_repro_flags\"",
            source,
        )
        self.assertIn(
            "-DCMAKE_CXX_FLAGS=\"-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard "
            "-ffunction-sections -fdata-sections $ort_repro_flags\"",
            source,
        )
        # The reduced-ORT build must never run without the reproducibility
        # flags attached to the compile lines.
        self.assertNotIn(
            '-DCMAKE_C_FLAGS="-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard '
            '-ffunction-sections -fdata-sections"',
            source,
        )

    def test_wakeword_speex_metadata_uses_canonical_prefix(self) -> None:
        # The cached wake-runtime payload includes the SpeexDSP install prefix,
        # which is a per-run directory.  The static archive and headers carry
        # no path bytes, but libtool/pkg-config metadata does; rewrite it to a
        # canonical prefix so identical inputs keep identical output bytes.
        source = (TOOLS_DIR / "wakeword/build_runtime.sh").read_text()
        self.assertIn(
            "prefix=/opt/libreecho/speexdsp", source
        )
        self.assertIn(
            "libdir='/opt/libreecho/speexdsp/lib'", source
        )
        self.assertIn('$SPEEX_PREFIX/lib/pkgconfig/speexdsp.pc', source)
        self.assertIn('$SPEEX_PREFIX/lib/libspeexdsp.la', source)

    def test_wakeword_runtime_snapshots_relink_objects(self) -> None:
        source = (TOOLS_DIR / "wakeword/build_runtime.sh").read_text()
        self.assertIn(
            "${LIBREECHO_WAKE_RELINK_OUTPUT:?ERROR:", source
        )
        for object_name in (
            "waked.wake.arm.o", "voice_aec.wake.arm.o",
            "voice_reference.wake.arm.o", "voice_dsp.wake.arm.o",
            "voice_stream.wake.arm.o", "wake_worker.wake.arm.o",
            "wake_led.wake.arm.o", "adapter_client.wake.arm.o",
            "adapter_server.wake.arm.o", "log.wake.arm.o",
            "wake_engine_onnx.arm.o",
        ):
            self.assertIn(object_name, source)
        self.assertIn("wakeword_relink_object_count=%s", source)

    def test_pinned_source_rejects_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").mkdir()
            (root / "real/file").write_bytes(b"pinned")
            (root / "alias").symlink_to("real", target_is_directory=True)
            self.assertEqual(
                builder.pinned_source(root, "real/file", "test"), root / "real/file"
            )
            with self.assertRaises(SystemExit):
                builder.pinned_source(root, "alias/file", "test")
            (root / "file-link").symlink_to("real/file")
            with self.assertRaises(SystemExit):
                builder.pinned_source(root, "file-link", "test")

    def test_pinned_source_rejects_noncanonical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("../file", "/file", "a//file", "a/./file"):
                with self.subTest(relative=relative), self.assertRaises(SystemExit):
                    builder.pinned_source(root, relative, "test")

    def test_vendor_firmware_is_locally_imported_runtime_only(self) -> None:
        importer = TOOLS_DIR / "initramfs/libreecho-vendor-import"
        specification = (
            TOOLS_DIR / "initramfs/vendor-assets/mt8163-v181-stock-v1.tsv"
        )
        self.assertTrue(importer.is_file())
        self.assertTrue(specification.is_file())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            source = root / "system-a"
            firmware = root / "firmware"
            data.mkdir()
            firmware.mkdir()
            records = (
                ("system/vendor/firmware/ROMv2_lm_patch_1_0_hdr.bin", "ROMv2_lm_patch_1_0_hdr.bin", b"patch-zero"),
                ("system/vendor/firmware/ROMv2_lm_patch_1_1_hdr.bin", "ROMv2_lm_patch_1_1_hdr.bin", b"patch-one"),
                ("system/vendor/firmware/WIFI_RAM_CODE_8163", "WIFI_RAM_CODE_8163", b"wifi-code"),
                ("system/vendor/firmware/WMT_SOC.cfg", "WMT_SOC.cfg", b"wmt-config"),
            )
            spec = root / "assets.tsv"
            lines = []
            for source_name, target_name, payload in records:
                source_path = source / source_name
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_bytes(payload)
                lines.append(
                    f"{hashlib.sha256(payload).hexdigest()}|{len(payload)}|"
                    f"{source_name}|{target_name}\n"
                )
            spec.write_text("".join(lines))
            environment = {
                **os.environ,
                "LIBREECHO_VENDOR_TEST_MODE": "1",
                "DATA_ROOT": str(data),
                "LIBREECHO_VENDOR_SOURCE_ROOT": str(source),
                "LIBREECHO_VENDOR_FIRMWARE_ROOT": str(firmware),
                "LIBREECHO_VENDOR_SPEC": str(spec),
                "LIBREECHO_VENDOR_STAGE_PARENT": str(root / "vendor-stage"),
                "LIBREECHO_VENDOR_STATUS_PATH": str(root / "run/libreecho/vendor-import.status"),
            }

            first = subprocess.run(
                ["/bin/sh", str(importer)], env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            for _source_name, target_name, payload in records:
                self.assertEqual((firmware / target_name).read_bytes(), payload)
                self.assertFalse((firmware / target_name).is_symlink())
            self.assertEqual((firmware / "WIFI_RAM_CODE").read_bytes(), b"wifi-code")
            self.assertFalse((firmware / "WIFI_RAM_CODE").is_symlink())
            for _source_name, target_name, _payload in records:
                self.assertEqual(stat.S_IMODE((firmware / target_name).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((firmware / "WIFI_RAM_CODE").stat().st_mode), 0o600)
            self.assertFalse((data / "libreecho/vendor").exists())
            self.assertFalse(Path(environment["LIBREECHO_VENDOR_STAGE_PARENT"]).exists())

            legacy_cache = data / "libreecho/vendor/mt8163-v181-stock-v1"
            legacy_cache.mkdir(parents=True)
            (legacy_cache / "WMT_SOC.cfg").write_bytes(b"legacy-generated-cache")
            cleanup = subprocess.run(
                ["/bin/sh", str(TOOLS_DIR / "initramfs/libreecho-data-cleanup")],
                env={**os.environ, "LIBREECHO_DATA_TEST_MODE": "1", "DATA_ROOT": str(data)},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
            self.assertFalse((data / "libreecho/vendor").exists())

            # Runtime-only delivery must depend on the owner's source partition
            # again after every reboot; no reusable vendor bytes remain in /data.
            subprocess.run(["rm", "-rf", str(source)], check=True)
            subprocess.run(["rm", "-rf", str(firmware)], check=True)
            firmware.mkdir()
            second = subprocess.run(
                ["/bin/sh", str(importer)], env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertFalse((firmware / "WIFI_RAM_CODE").exists())

    def test_vendor_import_probes_stock_system_root_layouts(self) -> None:
        for layout in ("etc/firmware", "vendor/firmware", "system/etc/firmware"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                importer, _data, source, firmware, environment, records = (
                    self.vendor_import_fixture(root)
                )
                original = source / "system/vendor/firmware"
                replacement = source / layout
                replacement.parent.mkdir(parents=True, exist_ok=True)
                original.rename(replacement)
                result = subprocess.run(
                    ["/bin/sh", str(importer)], env=environment,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"source_layout={layout}", result.stdout)
                for _source_name, target_name, payload in records:
                    self.assertEqual((firmware / target_name).read_bytes(), payload)

    def test_vendor_import_force_is_one_boot_structurally_gated_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            importer, data, source, firmware, environment, _records = (
                self.vendor_import_fixture(root)
            )
            shutil.rmtree(source / "system")
            payload_root = source / "etc/firmware"
            payload_root.mkdir(parents=True)
            patch_zero = bytearray(4096)
            patch_zero[22:28] = bytes.fromhex("8a0022000600")
            patch_one = bytearray(4096)
            patch_one[22:28] = bytes.fromhex("8a0021000ef0")
            forced = {
                "ROMv2_lm_patch_1_0_hdr.bin": bytes(patch_zero),
                "ROMv2_lm_patch_1_1_hdr.bin": bytes(patch_one),
                "WIFI_RAM_CODE_8163": b"W" * 8192,
                "WMT_SOC.cfg": b"coex_wmt_ant_mode=0\n",
            }
            for name, payload in forced.items():
                (payload_root / name).write_bytes(payload)
            force_marker = data / "libreecho/config/vendor-import-force-next-boot"
            force_marker.parent.mkdir(parents=True)
            force_marker.write_text("force-unverified-owner-local-import-v1\n")
            force_marker.chmod(0o600)
            environment["LIBREECHO_VENDOR_FORCE_MARKER"] = str(force_marker)
            result = subprocess.run(
                ["/bin/sh", str(importer)], env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("verification=forced-unverified", result.stdout)
            self.assertFalse(force_marker.exists())
            status = Path(environment["LIBREECHO_VENDOR_STATUS_PATH"]).read_text()
            self.assertIn("state=ready\n", status)
            self.assertIn("verification=forced-unverified\n", status)
            for name, payload in forced.items():
                self.assertEqual((firmware / name).read_bytes(), payload)

    def test_vendor_import_force_rejects_incompatible_patch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            importer, data, source, firmware, environment, _records = (
                self.vendor_import_fixture(root)
            )
            for patch in (source / "system/vendor/firmware").glob("ROMv2_*.bin"):
                patch.write_bytes(b"X" * 4096)
            force_marker = data / "libreecho/config/vendor-import-force-next-boot"
            force_marker.parent.mkdir(parents=True)
            force_marker.write_text("force-unverified-owner-local-import-v1\n")
            force_marker.chmod(0o600)
            environment["LIBREECHO_VENDOR_FORCE_MARKER"] = str(force_marker)
            result = subprocess.run(
                ["/bin/sh", str(importer)], env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("VENDOR_IMPORT_FORCED_SET_INCOMPATIBLE", result.stderr)
            self.assertFalse((firmware / "WIFI_RAM_CODE").exists())

    def test_vendor_import_rejects_symlinked_transient_stage_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            importer, _data, _source, firmware, environment, _records = (
                self.vendor_import_fixture(root)
            )
            stage_parent = Path(environment["LIBREECHO_VENDOR_STAGE_PARENT"])
            outside = root / "outside-stage"
            outside.mkdir()
            stage_parent.symlink_to(outside, target_is_directory=True)

            imported = subprocess.run(
                ["/bin/sh", str(importer)], env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(imported.returncode, 0)
            self.assertIn("VENDOR_IMPORT_STAGE_PATH_SYMLINK", imported.stderr)
            self.assertFalse((firmware / "WIFI_RAM_CODE").exists())

    def test_vendor_import_rejects_symlinked_stock_source_parent(self) -> None:
        for component in ("system", "vendor", "firmware"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                importer, _data, _source, firmware, environment, _records = (
                    self.vendor_import_fixture(
                        Path(temporary), source_symlink_component=component
                    )
                )
                imported = subprocess.run(
                    ["/bin/sh", str(importer)], env=environment,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertNotEqual(imported.returncode, 0)
                self.assertIn("VENDOR_IMPORT_SOURCE_PATH_SYMLINK", imported.stderr)
                self.assertFalse((firmware / "WIFI_RAM_CODE").exists())

    def test_vendor_import_rejects_insecure_existing_modes(self) -> None:
        cases = (
            ("runtime-file", "runtime-file", 0o644, "VENDOR_IMPORT_RUNTIME_MODE_MISMATCH"),
            ("runtime-alias", "runtime-alias", 0o644, "VENDOR_IMPORT_RUNTIME_MODE_MISMATCH"),
        )
        for label, target_kind, insecure_mode, expected_error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                importer, _data, _source, firmware, environment, _records = (
                    self.vendor_import_fixture(root)
                )
                first = subprocess.run(
                    ["/bin/sh", str(importer)], env=environment,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                targets = {
                    "runtime-file": firmware / "WMT_SOC.cfg",
                    "runtime-alias": firmware / "WIFI_RAM_CODE",
                }
                targets[target_kind].chmod(insecure_mode)
                reused = subprocess.run(
                    ["/bin/sh", str(importer)], env=environment,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertNotEqual(reused.returncode, 0)
                self.assertIn(expected_error, reused.stderr)

    def test_imported_wifi_firmware_reaches_kernel_literal_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            (stage / "etc").mkdir()
            (stage / "lib/firmware").mkdir(parents=True)
            wifi = stage / "lib/firmware/WIFI_RAM_CODE"
            wifi.write_bytes(b"wifi-code")

            symlinks = builder.add_connectivity_runtime_symlinks(stage)

            compatibility_root = stage / "etc/firmware"
            self.assertTrue(compatibility_root.is_symlink())
            self.assertEqual(os.readlink(compatibility_root), "../lib/firmware")
            self.assertEqual((compatibility_root / "WIFI_RAM_CODE").read_bytes(), b"wifi-code")
            self.assertEqual(symlinks, {"etc/firmware": "../lib/firmware"})

    def test_vendor_import_precedes_wmt_and_wifi_activation(self) -> None:
        source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        userdata = source.index("if userdata_mount; then")
        vendor_import = source.index("/usr/local/sbin/libreecho-vendor-import", userdata)
        vendor_ready = source.index("log vendor-assets-ready", vendor_import)
        wmt_nodes = source.index("log wmt-nodes-created", vendor_ready)
        activation_definition = source.index("\nstart_wifi_network()\n", wmt_nodes)
        activation_end = source.index("\n}\n", activation_definition)
        vendor_gate = source.index(
            'if [ "${VENDOR_ASSETS_OK:-0}" -ne 1 ]; then', activation_definition
        )
        sequence_call = source.index("\n    start_wifi_network_sequence\n", vendor_gate)
        startup_call = source.index("\n    start_wifi_network &\n", activation_end)
        self.assertLess(userdata, vendor_import)
        self.assertLess(vendor_import, vendor_ready)
        self.assertLess(vendor_ready, wmt_nodes)
        self.assertLess(wmt_nodes, activation_definition)
        self.assertLess(activation_definition, vendor_gate)
        self.assertLess(vendor_gate, sequence_call)
        self.assertLess(sequence_call, activation_end)
        self.assertLess(activation_end, startup_call)

    def test_wifi_runtime_contract_matches_kernel_source(self) -> None:
        driver = kernel_source_file(
            "drivers/misc/mediatek/mt8163-connectivity/conn_soc/drv_wlan/"
            "mt_wifi/wlan/os/linux/gl_kal.c"
        ).read_text()
        config = kernel_source_file(
            "drivers/misc/mediatek/mt8163-connectivity/conn_soc/drv_wlan/"
            "mt_wifi/wlan/include/config.h"
        ).read_text()
        wmt = kernel_source_file(
            "drivers/misc/mediatek/mt8163-connectivity/conn_soc/common/linux/pri/"
            "wmt_dev.c"
        ).read_text()
        importer = (TOOLS_DIR / "initramfs/libreecho-vendor-import").read_text()

        self.assertIn('#define CFG_FW_FILENAME             "WIFI_RAM_CODE"', config)
        self.assertIn(
            'filp_open("/etc/firmware/" CFG_FW_FILENAME, O_RDONLY, 0)', driver
        )
        self.assertIn("wifi_source=$STAGE/WIFI_RAM_CODE_8163", importer)
        self.assertIn("alias=$FIRMWARE_ROOT/WIFI_RAM_CODE", importer)

        open_start = wmt.index("static int WMT_open(")
        open_end = wmt.index("\n}\n", open_start)
        init_start = wmt.index("static int WMT_init(")
        init_end = wmt.index("\n}\n", init_start)
        userspace_start = wmt.index("static INT32 wmt_userspace_init(void)\n{")
        userspace_end = wmt.index("\n}\n", userspace_start)
        self.assertIn("wmt_userspace_init();", wmt[open_start:open_end])
        self.assertNotIn("wmt_lib_init();", wmt[init_start:init_end])
        self.assertIn("wmt_lib_init();", wmt[userspace_start:userspace_end])

    def test_pipeline_contains_no_stock_connectivity_root(self) -> None:
        pipeline_root = pipeline_file("build.sh").parent
        source = (pipeline_root / "build.sh").read_text()
        self.assertNotIn("stock-root-v181", source)
        self.assertNotIn("--connectivity-stock-root", source)
        self.assertFalse((pipeline_root / "inputs/stock-root-v181").exists())

    def test_feature_packagers_require_explicit_pipeline_root(self) -> None:
        for feature in ("airplay", "assistant", "tts", "wakeword", "stt"):
            source = (TOOLS_DIR / feature / "package_feature.sh").read_text()
            with self.subTest(feature=feature):
                self.assertIn("LIBREECHO_PIPELINE_ROOT", source)
                self.assertNotIn("../../../../pipeline", source)

    def test_release_payloads_embed_exact_license_closure(self) -> None:
        tts = TOOLS_DIR / "tts"
        self.assertIn("CC-BY-SA-4.0", (tts / "THIRD_PARTY_NOTICES.md").read_text())
        self.assertIn("OpenSLR SLR83", (tts / "NORTHERN-MALE-MODEL-CARD.md").read_text())
        self.assertTrue((tts / "CC-BY-SA-4.0.txt").stat().st_size > 10000)
        tts_packager = (tts / "package_feature.sh").read_text()
        self.assertIn("northern-male", tts_packager)
        self.assertNotIn("ALAN_MODEL", tts_packager)
        for required in (
            "THIRD_PARTY_NOTICES.md",
            "NORTHERN-MALE-MODEL-CARD.md",
            "SOUTHERN-FEMALE-MODEL-CARD.md",
            "CC-BY-SA-4.0.txt",
        ):
            self.assertIn(required, tts_packager)

        wakeword = TOOLS_DIR / "wakeword"
        wake_notice = (wakeword / "MODEL-LICENSE.txt").read_text()
        self.assertIn("public payload", wake_notice)
        self.assertIn("NonCommercial-ShareAlike", wake_notice)
        self.assertTrue((wakeword / "CC-BY-NC-SA-4.0.txt").stat().st_size > 10000)
        self.assertIn(
            "CC-BY-NC-SA-4.0.txt",
            (wakeword / "package_feature.sh").read_text(),
        )

        airplay_builder = (TOOLS_DIR / "airplay/build_airplay.sh").read_text()
        airplay_packager = (TOOLS_DIR / "airplay/package_feature.sh").read_text()
        for required in ("COMPONENTS.tsv", "debian", "nqptp", "shairport-sync", "ffmpeg", "tinyalsa"):
            self.assertIn(required, airplay_builder)
            self.assertIn(required, airplay_packager)

        common = TOOLS_DIR / "third-party-licenses"
        for required in (
            "RUNTIME-NOTICES.txt", "ONNX-Runtime-MIT.txt",
            "sherpa-onnx-Apache-2.0.txt", "SpeexDSP-COPYING.txt",
            "OpenSSL-copyright", "glibc-copyright", "gcc-runtime-copyright",
        ):
            self.assertTrue((common / required).is_file(), required)
        for feature in ("stt", "tts", "wakeword"):
            packager = (TOOLS_DIR / feature / "package_feature.sh").read_text()
            self.assertIn("COMMON_LICENSE_DIR", packager)
            self.assertIn("runtime_license_root", packager)
        assistant_packager = (TOOLS_DIR / "assistant/package_feature.sh").read_text()
        self.assertIn("OpenSSL-copyright", assistant_packager)
        self.assertIn("THIRD_PARTY_NOTICES.txt", assistant_packager)

        core = TOOLS_DIR / "initramfs/usr/local/share/licenses/libreecho-core"
        components = json.loads((core / "COMPONENTS.json").read_text())
        component_ids = {component["id"] for component in components["components"]}
        self.assertTrue({"busybox", "wpa-supplicant", "libnl", "musl", "tinyalsa", "libsodium", "mt8163-audio-fpga"}.issubset(component_ids))
        audio = next(component for component in components["components"] if component["id"] == "mt8163-audio-fpga")
        self.assertTrue(audio["included_in_public_artifact"])
        self.assertEqual(audio["redistribution_status"], "blocked")
        core_notice = (core / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn("owner's", core_notice)
        self.assertIn("`system_a`", core_notice)
        self.assertIn("audio-capable candidate includes", core_notice)
        self.assertNotIn("public base therefore makes no speaker or microphone claim", core_notice)
        for required in (
            "GPL-2.0-only.txt", "Apache-2.0.txt", "wpa_supplicant-BSD.txt",
            "wireless-tools-copyright", "wireless-regdb-copyright",
            "musl-1.2.5-COPYRIGHT.txt", "libsodium-1.0.18-LICENSE.txt",
        ):
            self.assertTrue((core / required).is_file(), required)

    def test_public_core_runtime_is_rebuilt_from_locked_source(self) -> None:
        expected = {
            "busybox": (
                "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4",
                "build_busybox.sh",
            ),
            "musl": (
                "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4",
                "build_musl.sh",
            ),
            "wpa-supplicant": (
                "20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f",
                "build_wpa_supplicant.sh",
            ),
        }
        for component, (archive_sha256, builder_name) in expected.items():
            component_dir = TOOLS_DIR / component
            lock = json.loads((component_dir / "SOURCE.lock").read_text())
            self.assertEqual(lock["source_sha256"], archive_sha256)
            builder = component_dir / builder_name
            self.assertTrue(builder.is_file(), builder)
            self.assertTrue(builder.stat().st_mode & 0o111, builder)
            builder_source = builder.read_text()
            self.assertIn("source_sha256", builder_source)
            self.assertIn("-ffile-prefix-map", builder_source)
            self.assertIn("source.json", builder_source)

        image_builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        image_verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        for expected_argument in (
            "--expected-busybox-sha256",
            "--expected-musl-loader-sha256",
        ):
            self.assertIn(expected_argument, image_builder)
            self.assertIn(expected_argument, image_verifier)
        for obsolete_hash in (
            "d4c8fd2aea01abd851c703f39b29c0de748b2751e4e1a85cae570fa53ad8f4fb",
            "1063871174f1bd4f08f4d330e20b07aeb0820327ee739a4d8d1b644df842cb6b",
        ):
            self.assertNotIn(obsolete_hash, image_builder)
            self.assertNotIn(obsolete_hash, image_verifier)

        connectivity = TOOLS_DIR / "connectivity"
        connectivity_builder = (connectivity / "build_connectivity_helpers.sh").read_text()
        self.assertTrue((connectivity / "SOURCE.lock").is_file())
        for helper in (
            "wmt_configure", "wmt_responder", "wmt_bt_on",
            "wmt_stock_compat", "wmt_launcher",
        ):
            self.assertTrue((connectivity / f"{helper}.c").is_file())
            self.assertIn(helper, connectivity_builder)
        self.assertIn("GPL-2.0-only", connectivity_builder)

        audio_tools = TOOLS_DIR / "audio-tools"
        audio_lock = json.loads((audio_tools / "SOURCE.lock").read_text())
        self.assertEqual(
            audio_lock["source_sha256"],
            "dc75977453304fcce0b91cbfd2b27942641c93479f87898d230cdc440a042d4f",
        )
        self.assertEqual(
            audio_lock["patch_sha256"],
            "f63cacae35b0eb5291c8f4fa49c276c7aac3927426e529c50d74ab244c3ba7aa",
        )
        audio_builder = (audio_tools / "build_audio_tools.sh").read_text()
        for tool in ("tinyplay", "tinycap", "tinymix"):
            self.assertIn(tool, audio_builder)
        self.assertIn("tinyalsa-source.json", audio_builder)
        self.assertIn("--fuzz=0", audio_builder)
        self.assertIn(
            "/home/buildozer/aports/main/musl/src/musl-1.2.5",
            audio_builder,
        )
        self.assertIn(
            "grep -E '/home/|libreecho-tinyalsa-build'",
            audio_builder,
        )
        self.assertIn(
            "grep -Fvx \"$allowed_musl_provenance\"",
            audio_builder,
        )
        self.assertFalse(any((audio_tools / tool).exists() for tool in ("tinyplay", "tinycap", "tinymix")))

        pipeline = pipeline_text("build.sh")
        self.assertIn("build_connectivity_helpers.sh", pipeline)
        self.assertIn("build_audio_tools.sh", pipeline)
        self.assertNotIn('$INPUTS/connectivity-helpers', pipeline)
        self.assertNotIn('AUDIO_TOOLS_DIR="$TOOLS_DIR/audio-tools"', pipeline)
        self.assertNotIn('sha256sum -c SHA256SUMS', pipeline)
        self.assertIn('verify_pinned_input', pipeline)
        for product_field in (
            'product_git_head=', 'product_git_state=',
            'product_git_diff_sha256=', 'public_release_mode=',
        ):
            self.assertIn(product_field, pipeline)

    def test_busybox_builder_pins_utc_timezone(self) -> None:
        builder = (
            TOOLS_DIR / "busybox" / "build_busybox.sh"
        ).read_text()
        self.assertIn("export TZ=UTC", builder)

    def test_busybox_builder_sanitizes_host_linker_environment(self) -> None:
        builder = (
            TOOLS_DIR / "busybox" / "build_busybox.sh"
        ).read_text()
        self.assertIn("unset LD_LIBRARY_PATH", builder)

    def test_busybox_builder_allows_only_runtime_home_template(self) -> None:
        builder = (
            TOOLS_DIR / "busybox" / "build_busybox.sh"
        ).read_text()
        self.assertIn("grep -E '/home/|libreecho-busybox-build'", builder)
        self.assertIn("grep -Fvx '/home/%s'", builder)

    def test_wpa_builder_sanitizes_host_environment_before_extraction(self) -> None:
        builder = (
            TOOLS_DIR / "wpa-supplicant" / "build_wpa_supplicant.sh"
        ).read_text()
        self.assertIn("unset LD_LIBRARY_PATH", builder)
        self.assertLess(
            builder.index("unset LD_LIBRARY_PATH"),
            builder.index('tar -xf "$ARCHIVE"'),
        )

    def test_wpa_builder_emits_static_non_pie(self) -> None:
        builder = (
            TOOLS_DIR / "wpa-supplicant" / "build_wpa_supplicant.sh"
        ).read_text()
        self.assertRegex(builder, r'LDFLAGS=["\x27]-static -no-pie')
        self.assertIn("Type:[[:space:]]+EXEC", builder)

    def test_wpa_builder_requires_exported_linux_uapi_headers(self) -> None:
        builder = (
            TOOLS_DIR / "wpa-supplicant" / "build_wpa_supplicant.sh"
        ).read_text()
        self.assertIn("--kernel-headers DIR", builder)
        self.assertIn("KERNEL_HEADERS=", builder)
        self.assertIn("-idirafter $KERNEL_HEADERS", builder)
        self.assertIn("kernel_uapi_sha256", builder)
        self.assertNotIn("/usr/arm-linux-gnueabihf/include", builder)
        image_builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        self.assertIn("--wpa-source-metadata", image_builder)
        self.assertIn("wpa source provenance is missing or mismatched", verifier)

    def test_wpa_supplicant_prefers_nl80211_with_wext_fallback(self) -> None:
        config = (
            TOOLS_DIR / "wpa-supplicant" / "wpa_supplicant-2.10.config"
        ).read_text()
        builder = (
            TOOLS_DIR / "wpa-supplicant" / "build_wpa_supplicant.sh"
        ).read_text()
        source_lock = json.loads(
            (TOOLS_DIR / "wpa-supplicant" / "SOURCE.lock").read_text()
        )
        wifi = (TOOLS_DIR / "initramfs/libreecho-wifi").read_text()
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        self.assertIn("CONFIG_DRIVER_NL80211=y", config)
        self.assertIn("CONFIG_LIBNL32=y", config)
        self.assertIn("CONFIG_DRIVER_WEXT=y", config)
        self.assertIn("--libnl-archive FILE", builder)
        self.assertIn("libnl_source_sha256", builder)
        self.assertIn('LIBNL_INC="$libnl_src/include"', builder)
        self.assertIn(
            'EXTRA_CFLAGS="$common_cflags -I$libnl_src/include', builder
        )
        self.assertIn("for tool in ar ranlib strip; do", builder)
        self.assertIn('AR="$wrappers/ar"', builder)
        self.assertIn('RANLIB="$wrappers/ranlib"', builder)
        self.assertIn('STRIP="$wrappers/strip"', builder)
        self.assertEqual(source_lock["drivers"], ["nl80211", "wext"])
        self.assertEqual(source_lock["libnl_version"], "3.11.0")
        self.assertEqual(
            source_lock["libnl_source_sha256"],
            "2a56e1edefa3e68a7c00879496736fdbf62fc94ed3232c0baba127ecfa76874d",
        )
        self.assertIn("-Dnl80211,wext", wifi)
        image_builder = (
            TOOLS_DIR / "build_recovery_image.py"
        ).read_text()
        self.assertIn("WPA_CONFIG_SHA256", image_builder)
        self.assertIn('wpa_metadata["drivers"] != ["nl80211", "wext"]', image_builder)
        self.assertIn('wpa_metadata["libnl_source_sha256"] != LIBNL_SOURCE_SHA256', image_builder)
        self.assertIn("LIBNL_SOURCE_SHA256", verifier)
        self.assertIn("--expected-wpa-supplicant-sha256", verifier)
        self.assertIn("expected_wpa_supplicant_sha256", verifier)
        self.assertIn(
            'source_record.get("libnl_source_sha256") != LIBNL_SOURCE_SHA256',
            verifier,
        )
        self.assertIn(
            'source_record.get("libnl_source_url") != LIBNL_SOURCE_URL',
            verifier,
        )

    def test_feature_policy_is_immutable_and_fail_closed(self) -> None:
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        pipeline = pipeline_text("build.sh")
        for source in (builder, verifier, updater, pipeline):
            self.assertIn("feature_policy", source)
        self.assertIn("--feature-policy", builder)
        self.assertIn("--expected-feature-policy", verifier)
        self.assertIn("/etc/libreecho/feature-policy", init)
        self.assertIn("feature exclusion requires --service-profile diagnostic", pipeline)
        self.assertIn('FEATURE_POLICY="${LIBREECHO_FEATURE_POLICY:-preserve}"', pipeline)

    def test_redistributable_policy_is_four_payloads_without_wakeword(self) -> None:
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        ota = (TOOLS_DIR / "ota/make_ota_bundle.py").read_text()
        for source in (builder, verifier, updater, ota):
            self.assertIn("redistributable", source)
        self.assertIn("redistributable policy requires external AirPlay, TTS, STT, and assistant payloads", builder)
        self.assertIn("wakeword payload inputs are forbidden by feature_policy=redistributable", builder)
        self.assertIn("redistributable feature policy manifest mismatch", verifier)
        self.assertIn("preserve|redistributable|community-noncommercial|exclude", init)
        self.assertIn('if [ "$FEATURE_POLICY" = preserve ] ||', init)
        self.assertIn("ui-services-redistributable-without-wakeword", init)

    def test_community_noncommercial_policy_is_five_payloads_with_wakeword(self) -> None:
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        ota = (TOOLS_DIR / "ota/make_ota_bundle.py").read_text()
        for source in (builder, verifier, init, updater, ota):
            self.assertIn("community-noncommercial", source)
        self.assertIn(
            "community-noncommercial policy requires external AirPlay, TTS, "
            "wakeword, STT, and assistant payloads",
            builder,
        )
        self.assertIn("community-noncommercial feature policy manifest mismatch", verifier)
        self.assertIn("ui-services-community-noncommercial-with-wakeword", init)

    def test_recovery_init_pin_matches_policy_source(self) -> None:
        init_hash = hashlib.sha256(
            (TOOLS_DIR / "initramfs/libreecho-init").read_bytes()
        ).hexdigest()
        pins = {
            "build_recovery_image.py": "RECOVERY_INIT_SHA256",
            "verify_recovery_image.py": "INIT_SHA256",
        }
        for name, constant in pins.items():
            source = (TOOLS_DIR / name).read_text()
            self.assertIn(f'{constant} = "{init_hash}"', source)

    def test_pid1_traps_the_signals_busybox_reboot_sends(self) -> None:
        # Issue aslater3/LibreEcho#105: busybox halt/poweroff/reboot do their
        # work by signalling PID 1, assuming busybox init is there to catch it.
        # PID 1 here is this shell script, so without traps the signal is
        # discarded and `reboot` exits 0 having done nothing -- which reads as
        # success and made a documented recovery step a silent no-op.
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn("signal_transition()", init)
        self.assertIn("trap 'signal_transition reboot'   TERM", init)
        self.assertIn("trap 'signal_transition reboot'   INT", init)
        self.assertIn("trap 'signal_transition halt'     USR1", init)
        self.assertIn("trap 'signal_transition poweroff' USR2", init)
        # Each signal maps to the matching forced transition.
        self.assertIn("$BB halt -f", init)
        self.assertIn("$BB poweroff -f", init)
        # The traps must be installed in the main shell, after the recovery
        # supervisor starts and before PID 1 parks in its idle loop.
        supervisor = init.index("log reboot-supervisor-started")
        installed = init.index("log reboot-signal-traps-installed")
        idle_loop = init.index("$BB sleep 3600 &")
        self.assertLess(supervisor, installed)
        self.assertLess(installed, idle_loop)

    def test_pid1_idle_loop_is_interruptible_by_a_trap(self) -> None:
        # A trap only runs between commands, so a foreground `sleep 3600` would
        # sit on the signal for up to an hour. Backgrounding the sleep and
        # waiting on it makes the transition immediate, because wait is
        # interruptible.
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn("$BB sleep 3600 &\n    wait $!", init)
        self.assertNotIn("\n    $BB sleep 3600\ndone", init)

    def test_pid1_signal_delivery_harness(self) -> None:
        harness = TOOLS_DIR / "test_pid1_signal_transitions.py"
        result = subprocess.run(
            [sys.executable, str(harness)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for signal_name in ("TERM", "INT", "USR1", "USR2"):
            self.assertIn(f" {signal_name} ->", result.stdout)

    def test_health_confirm_restart_record_is_persistent(self) -> None:
        # Issue #41: the OTA health-confirm worker must leave persistent
        # evidence before forcing a reboot, so a worker restart is
        # distinguishable from a hardware watchdog reset.
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        # The record is written atomically to the persistent userdata
        # filesystem (not tmpfs) and synced before the reboot.
        self.assertIn("/data/libreecho/update/restart-record", init)
        self.assertIn("restart-record.tmp", init)
        self.assertIn("reason=ota-health-confirm-failed", init)
        # The failing check, attempt count, and slot state are recorded so the
        # restart can be triaged after the fact.
        self.assertIn("last_check=", init)
        self.assertIn("attempts=$attempt", init)
        self.assertIn("pending_slot=$pending_slot", init)
        self.assertIn("selected_slot=$selected_slot", init)
        # The state file reflects the restart so OTA status is truthful.
        self.assertIn("state=restarting", init)
        self.assertIn("detail=health-confirm-failed:$last_check", init)
        # The record must be written BEFORE the worker's reboot, not after.
        # Scope to the worker function: earlier unrelated reboot paths must
        # not satisfy the ordering check.
        worker_start = init.index("ota_health_confirm_worker()")
        # Slice to the function's own closing brace rather than a fixed byte
        # count: a fixed window silently falls out of scope as soon as the
        # worker grows, which turns an ordering assertion into a length test.
        worker_end = init.index("\n}", worker_start) + 2
        worker = init[worker_start:worker_end]
        restart_idx = worker.index("$BB reboot -f")
        record_idx = worker.index("/data/libreecho/update/restart-record")
        self.assertLess(record_idx, restart_idx)
        # libreecho-update status surfaces the persistent record.
        self.assertIn("restart-record", updater)
        self.assertIn("restart_record=1", updater)

    def test_ota_bundle_signs_redistributable_policy(self) -> None:
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boot = root / "boot.img"
            boot_bytes = b"ANDROID!" + bytes((16 * 1024 * 1024) - 8)
            boot.write_bytes(boot_bytes)
            key = SigningKey.generate()
            private = root / "private.hex"
            public = root / "public.hex"
            build_manifest = root / "build-manifest.json"
            private.write_text(key.encode().hex() + "\n")
            public.write_text(key.verify_key.encode().hex() + "\n")
            build_manifest.write_text(json.dumps({
                "image_profile": "ota",
                "service_profile": "production",
                "feature_policy": "redistributable",
                "update_channel": "dev",
                "output": {
                    "sha256": hashlib.sha256(boot_bytes).hexdigest(),
                    "size": len(boot_bytes),
                },
            }))
            output = root / "update.ota.tar"
            subprocess.run([
                sys.executable, str(TOOLS_DIR / "ota/make_ota_bundle.py"),
                "--boot-image", str(boot), "--build-manifest", str(build_manifest),
                "--version", "test-v1",
                "--signing-key", str(private), "--public-key", str(public),
                "--service-profile", "production", "--feature-policy",
                "redistributable", "--update-channel", "dev", "--output", str(output),
            ], check=True, capture_output=True, text=True)
            with tarfile.open(output, "r:") as archive:
                manifest = archive.extractfile("manifest").read().decode()
            self.assertIn("feature_policy=redistributable\n", manifest)

    def test_ota_bundle_signs_community_noncommercial_policy(self) -> None:
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boot = root / "boot.img"
            boot_bytes = b"ANDROID!" + bytes((16 * 1024 * 1024) - 8)
            boot.write_bytes(boot_bytes)
            key = SigningKey.generate()
            private = root / "private.hex"
            public = root / "public.hex"
            build_manifest = root / "build-manifest.json"
            private.write_text(key.encode().hex() + "\n")
            public.write_text(key.verify_key.encode().hex() + "\n")
            build_manifest.write_text(json.dumps({
                "image_profile": "ota",
                "service_profile": "production",
                "feature_policy": "community-noncommercial",
                "update_channel": "dev",
                "output": {
                    "sha256": hashlib.sha256(boot_bytes).hexdigest(),
                    "size": len(boot_bytes),
                },
            }))
            output = root / "update.ota.tar"
            subprocess.run([
                sys.executable, str(TOOLS_DIR / "ota/make_ota_bundle.py"),
                "--boot-image", str(boot), "--build-manifest", str(build_manifest),
                "--version", "test-v1",
                "--signing-key", str(private), "--public-key", str(public),
                "--service-profile", "production", "--feature-policy",
                "community-noncommercial", "--update-channel", "dev", "--output", str(output),
            ], check=True, capture_output=True, text=True)
            with tarfile.open(output, "r:") as archive:
                manifest = archive.extractfile("manifest").read().decode()
            self.assertIn("feature_policy=community-noncommercial\n", manifest)

    def test_ota_bundle_rejects_excluded_features_with_production_services(self) -> None:
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boot = root / "boot.img"
            boot_bytes = b"ANDROID!" + bytes((16 * 1024 * 1024) - 8)
            boot.write_bytes(boot_bytes)
            key = SigningKey.generate()
            private = root / "private.hex"
            public = root / "public.hex"
            build_manifest = root / "build-manifest.json"
            private.write_text(key.encode().hex() + "\n")
            public.write_text(key.verify_key.encode().hex() + "\n")
            build_manifest.write_text(json.dumps({
                "image_profile": "ota",
                "service_profile": "diagnostic",
                "feature_policy": "exclude",
                "update_channel": "dev",
                "output": {
                    "sha256": hashlib.sha256(boot_bytes).hexdigest(),
                    "size": len(boot_bytes),
                },
            }))
            result = subprocess.run([
                sys.executable, str(TOOLS_DIR / "ota/make_ota_bundle.py"),
                "--boot-image", str(boot), "--build-manifest", str(build_manifest),
                "--version", "test-v1", "--signing-key", str(private),
                "--public-key", str(public), "--service-profile", "production",
                "--feature-policy", "exclude", "--update-channel", "dev", "--output", str(root / "update.ota.tar"),
            ], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("feature exclusion requires the diagnostic service profile", result.stderr)

    def test_ota_bundle_rejects_policy_not_bound_to_boot_manifest(self) -> None:
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boot = root / "boot.img"
            boot_bytes = b"ANDROID!" + bytes((16 * 1024 * 1024) - 8)
            boot.write_bytes(boot_bytes)
            key = SigningKey.generate()
            private = root / "private.hex"
            public = root / "public.hex"
            build_manifest = root / "build-manifest.json"
            private.write_text(key.encode().hex() + "\n")
            public.write_text(key.verify_key.encode().hex() + "\n")
            build_manifest.write_text(json.dumps({
                "image_profile": "ota",
                "service_profile": "production",
                "feature_policy": "preserve",
                "update_channel": "dev",
                "output": {
                    "sha256": hashlib.sha256(boot_bytes).hexdigest(),
                    "size": len(boot_bytes),
                },
            }))
            result = subprocess.run([
                sys.executable, str(TOOLS_DIR / "ota/make_ota_bundle.py"),
                "--boot-image", str(boot), "--build-manifest", str(build_manifest),
                "--version", "test-v1", "--signing-key", str(private),
                "--public-key", str(public), "--service-profile", "production",
                "--feature-policy", "redistributable", "--update-channel", "dev",
                "--output", str(root / "update.ota.tar"),
            ], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("feature policy does not match build manifest", result.stderr)

    def test_stock_userspace_is_replaced_by_source_built_adbd(self) -> None:
        builder_source = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        for dead_name in (
            "sepolicy", "file_contexts.bin", "property_contexts",
            "service_contexts", "seapp_contexts", "selinux_version",
            "ueventd.rc", "ueventd.mt8163.rc",
        ):
            self.assertNotIn(dead_name, builder_source)
            self.assertNotIn(dead_name, verifier_source)
        self.assertNotIn("STOCK_FILES", builder_source)
        self.assertIn("copy_adbd", builder_source)
        self.assertIn('"source_license"', builder_source)
        self.assertIn("stock_userspace", verifier_source)

    def test_adbd_is_source_built_and_notice_bound(self) -> None:
        adbd_dir = TOOLS_DIR / "adbd"
        builder = adbd_dir / "build_adbd.sh"
        notice = adbd_dir / "NOTICE"
        source_lock = adbd_dir / "SOURCE.lock"
        self.assertTrue(builder.is_file())
        self.assertTrue(notice.is_file())
        self.assertTrue(source_lock.is_file())
        builder_text = builder.read_text()
        self.assertIn("ADBD_SOURCE", builder_text)
        self.assertIn("ADBD_SOURCE_COMMIT", builder_text)
        self.assertIn("-DADB_HOST=0", builder_text)
        self.assertIn("-DALLOW_ADBD_ROOT=1", builder_text)
        self.assertIn("-static", builder_text)
        self.assertIn("-ffile-prefix-map=", builder_text)
        self.assertIn("-fdebug-prefix-map=", builder_text)
        self.assertIn("--kernel-headers", builder_text)
        self.assertIn("--test-ffs-root", builder_text)
        self.assertIn('"kernel_headers": "exported-linux-uapi"', builder_text)
        self.assertNotIn('"kernel_headers": kernel_headers', builder_text)
        self.assertIn("libreecho-adbd-compat.h", builder_text)
        self.assertTrue((adbd_dir / "compat/libreecho-adbd-compat.h").is_file())
        self.assertNotIn("stock-root", builder_text)
        self.assertIn("Apache License", notice.read_text())
        self.assertIn("source_commit=", source_lock.read_text())

    def test_adbd_compat_header_coexists_with_kernel_uapi_prctl(self) -> None:
        # musl's <sys/prctl.h> re-declares struct prctl_mm_map and PR_*
        # macros that the exported kernel UAPI <linux/prctl.h> also defines,
        # and adb.c includes the UAPI header directly. The compat header is
        # force-included into every translation unit, so it must not pull in
        # musl's sys/prctl.h; it declares prctl() directly instead. A
        # reintroduction of that include breaks every musl-based adbd build
        # with a hard redefinition error (regression from the hosted public
        # build lane).
        adbd_dir = TOOLS_DIR / "adbd"
        compat = adbd_dir / "compat/libreecho-adbd-compat.h"
        compat_text = compat.read_text()
        self.assertNotIn("#include <sys/prctl.h>", compat_text)
        self.assertIn("int prctl(", compat_text)

    def test_pipeline_builds_adbd_without_stock_root(self) -> None:
        pipeline_root = pipeline_file("build.sh").parent
        source = (pipeline_root / "build.sh").read_text()
        self.assertIn("ADBD_BUILDER", source)
        self.assertIn("--adbd", source)
        self.assertIn("LIBREECHO_ADBD_KERNEL_HEADERS", source)
        self.assertIn("LIBREECHO_KERNEL_ZIMAGE_OVERRIDE", source)
        self.assertIn("LIBREECHO_AIRPLAY_PAYLOAD_OVERRIDE", source)
        self.assertIn("adopt_feature_payload", source)
        self.assertNotIn('"$INPUTS/stock-root-v184"', source)
        self.assertNotIn("--stock-root", source)

    def test_boot_envelope_is_generated_without_stock_input(self) -> None:
        generator = TOOLS_DIR / "generate_boot_envelope.py"
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        pipeline = pipeline_text("build.sh")
        self.assertTrue(generator.is_file())
        self.assertIn("--boot-envelope", builder)
        self.assertIn("--boot-envelope", verifier)
        self.assertIn("generate_boot_envelope.py", pipeline)
        self.assertNotIn("stock-boot-v184.img", pipeline)
        self.assertNotIn("--source-boot", builder)
        self.assertNotIn("--source-boot", verifier)

    def test_connectivity_image_contains_requirements_not_vendor_bytes(self) -> None:
        builder_source = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        init_source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        for source in (builder_source, verifier_source):
            self.assertNotIn("CONNECTIVITY_STOCK_FILES", source)
            self.assertNotIn("connectivity-stock-root", source)
            self.assertNotIn("system/vendor/bin/wmt_loader", source)
            self.assertNotIn("system/vendor/bin/wmt_launcher", source)
        self.assertNotIn("system/bin/linker", builder_source)
        self.assertIn("stock Android connectivity userspace remains embedded", verifier_source)
        self.assertIn('"vendor_delivery": "owner-device-local-extraction"', builder_source)
        self.assertIn("embedded_vendor_file_count", builder_source)
        self.assertIn("libreecho-vendor-import", init_source)
        self.assertIn("VENDOR_ASSETS_OK", init_source)
        self.assertLess(
            init_source.index("libreecho-vendor-import"),
            init_source.index("start_wifi_network_sequence"),
        )

    def test_audio_tools_are_source_built_and_gpu_input_wait_is_interruptible(self) -> None:
        tools = TOOLS_DIR / "audio-tools"
        self.assertTrue((tools / "build_audio_tools.sh").is_file())
        self.assertTrue((tools / "SOURCE.lock").is_file())
        self.assertTrue((tools / "tinyalsa-mt8163.patch").is_file())
        self.assertFalse(any((tools / name).exists() for name in ("tinyplay", "tinycap", "tinymix")))
        pipeline_build = pipeline_text("build.sh")
        self.assertIn("--tinyplay", pipeline_build)
        self.assertIn("--tinymix", pipeline_build)
        gpufreq = (
            TOOLS_DIR.parent.parent
            / "drivers/misc/mediatek/base/power/mt8163/mt_gpufreq.c"
        ).read_text()
        self.assertIn("wait_event_interruptible(mt_gpufreq_input_boost_wq", gpufreq)
        self.assertNotIn("set_current_state(TASK_INTERRUPTIBLE)", gpufreq)
        self.assertIn("wake_up_process(mt_gpufreq_up_task)", gpufreq)

        spi_pcm = (
            TOOLS_DIR.parent.parent
            / "sound/soc/mediatek/mt_soc_audio_8163_amzn/amzn-spi-pcm"
            / "amzn-mt-spi-pcm.c"
        ).read_text()
        self.assertIn("struct device *dma_dev = rtd->platform->dev", spi_pcm)
        self.assertIn("dma_dev->coherent_dma_mask = DMA_BIT_MASK(64)", spi_pcm)
        self.assertIn("SNDRV_DMA_TYPE_DEV, dma_dev", spi_pcm)

    def test_shared_audio_engine_starts_dma_before_releasing_amp(self) -> None:
        engine = TOOLS_DIR / "airplay/audio_engine.c"
        source = engine.read_text()
        self.assertIn("#define PERIOD_SIZE 2048U", source)
        self.assertIn(".start_threshold = 1U", source)
        first_write = source.index(
            "write_period(pcm, output, &reference, first_activity)"
        )
        second_write = source.index(
            "write_period(pcm, second, &reference, second_activity)"
        )
        amp_enable = source.index("enable_output_controls(card)")
        self.assertLess(first_write, amp_enable)
        self.assertLess(second_write, amp_enable)
        self.assertIn(
            "pcm_writei(pcm, samples, PERIOD_SIZE)", source
        )
        self.assertIn(
            "le_aec_reference_publish(reference, samples, PERIOD_SIZE",
            source,
        )
        self.assertIn("(void)disable_output_controls(card,", source)

    def test_shared_audio_engine_builds_puffin_priority_bus(self) -> None:
        engine = (TOOLS_DIR / "airplay/audio_engine.c").read_text()
        producer = (TOOLS_DIR / "airplay/airplay_audio.c").read_text()
        downmix = (TOOLS_DIR / "airplay/puffin_downmix.h").read_text()

        self.assertIn('"media", "system", "announcement", "alarm"', engine)
        self.assertIn("#define MEDIA_DUCK_Q15 8231", engine)
        self.assertIn("source == SOURCE_MEDIA && alarm_active", engine)
        self.assertIn("source == SOURCE_MEDIA && higher_priority", engine)
        self.assertIn("puffin_render_mono(dynamics, mixed)", engine)
        self.assertIn('#define LED_SOCKET "/run/libreecho/led.sock"', engine)
        self.assertIn('\\"owner\\":\\"announcement\\"', engine)
        self.assertIn("sync_announcement_led(sources", engine)
        self.assertIn("errno != EINPROGRESS && errno != EAGAIN", engine)
        self.assertIn("poll(&pollfd, 1, 0)", engine)
        self.assertIn("getsockopt(fd, SOL_SOCKET, SO_ERROR", engine)
        self.assertIn("#define VISUALIZER_FRAME_PERIODS 2U", engine)
        self.assertIn('\\"cmd\\":\\"visualizer\\"', engine)
        self.assertIn('\\"action\\":\\"frame\\"', engine)
        self.assertIn('\\"action\\":\\"stop\\"', engine)
        self.assertIn('\\"owner\\":\\"music\\"', engine)
        self.assertIn("process_music_visualizer(&visualizer, sources", engine)
        self.assertIn("higher_priority_active(sources)", engine)
        self.assertIn("sources[SOURCE_MEDIA].received == 0", engine)
        analyzer = (TOOLS_DIR / "airplay/audio_visualizer.c").read_text()
        analyzer_header = (
            TOOLS_DIR / "airplay/audio_visualizer.h"
        ).read_text()
        self.assertIn("#define AUDIO_VISUALIZER_BANDS 12U", analyzer_header)
        self.assertIn("static const struct band_coefficients", analyzer)
        self.assertIn("FILTER_INPUT_SHIFT 8", analyzer)
        self.assertNotIn("sin(", analyzer)
        status = (TOOLS_DIR / "airplay/playback_status.c").read_text()
        self.assertIn('"%s/status.json"', status)
        self.assertIn("rename(status->temporary_path, status->path)", status)
        self.assertIn("status->last_mask == bus_mask", status)
        self.assertIn("fchmod(fd, 0644)", status)
        self.assertNotIn("metadata", status.lower())
        airplay_builder = (
            TOOLS_DIR / "airplay/build_airplay.sh"
        ).read_text()
        self.assertIn('"$AUDIO_VISUALIZER_SOURCE"', airplay_builder)
        self.assertIn('"$PLAYBACK_STATUS_SOURCE"', airplay_builder)
        runtime_check = (
            TOOLS_DIR / "airplay/runtime_check_root.sh"
        ).read_text()
        self.assertIn("AIRPLAY_RUNTIME_AUDIO_STATUS_MISSING", runtime_check)
        self.assertIn("AIRPLAY_RUNTIME_LED_SOCKET_NOT_BOUND", runtime_check)
        self.assertIn('DEFAULT_MEDIA_FIFO "/run/libreecho-audio/media.pcm"', producer)
        self.assertNotIn("pcm_open(", producer)
        self.assertIn("#define PUFFIN_OUTPUT_TRIM_Q15 46341", downmix)
        self.assertIn("#define PUFFIN_OUTPUT_CEILING 32767", downmix)
        self.assertIn("struct puffin_dynamics", downmix)
        self.assertIn("(int32_t)samples[frame * 2]", downmix)
        self.assertIn("(int32_t)samples[frame * 2 + 1]", downmix)
        self.assertIn("mixed /= 2", downmix)
        self.assertIn("PUFFIN_OUTPUT_CEILING << 15", downmix)
        self.assertIn("samples[frame * 2] = mono", downmix)
        self.assertIn("samples[frame * 2 + 1] = mono", downmix)

    def test_airplay_volume_owns_codec_master_only_while_active(self) -> None:
        engine = (TOOLS_DIR / "airplay/audio_engine.c").read_text()
        producer = (TOOLS_DIR / "airplay/airplay_audio.c").read_text()

        self.assertIn('#define AIRPLAY_ACTIVE_FILE "airplay.active"', engine)
        self.assertIn('#define AIRPLAY_VOLUME_FILE "airplay.volume"', engine)
        self.assertIn("airplay_is_active(root)", engine)
        self.assertIn("airplay_volume_to_mixer(root)", engine)
        self.assertIn("saved_volume", engine)
        self.assertIn("set_pcm_volume(card, requested)", engine)
        self.assertIn("disable_output_controls(card,", engine)
        self.assertIn("DEFAULT_AIRPLAY_ACTIVE_FILE", producer)
        self.assertIn("These hooks are retained for compatibility", producer)
        self.assertNotIn("set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, active)", producer)
        self.assertIn("DEFAULT_AIRPLAY_VOLUME_FILE", producer)
        self.assertIn("set_volume(DEFAULT_AIRPLAY_VOLUME_FILE, argv[2])", producer)
        self.assertIn("set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 1)", producer)
        self.assertIn("set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 0)", producer)

    def test_puffin_speaker_profile_matches_stock_dump(self) -> None:
        kernel = TOOLS_DIR.parent.parent
        codec = (kernel / "sound/soc/codecs/tlv320aic32x4.c").read_text()
        match = re.search(
            r"static const u8 puffin_ext_speaker_biquad\[\] = \{(.*?)\};",
            codec,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        profile = bytes(int(value) for value in re.findall(r"\d+", match.group(1)))
        self.assertEqual(len(profile), 117)
        self.assertEqual(
            hashlib.sha256(profile).hexdigest(),
            "cd2d86f0ab713efa842420d08bf92149e4d610ce2090847e2308eb088ba84610",
        )
        self.assertIn("pConfigRegs = biquad_settings_regs", codec)

        platform = (
            kernel
            / "sound/soc/mediatek/mt_soc_audio_8163_amzn"
            / "mt_soc_pcm_dl1_i2s0Dl1.c"
        ).read_text()
        prepare = platform.split(
            "static void mtk_I2S0dl1_board_prepare(void)", 1
        )[1].split("static void mtk_I2S0dl1_board_start(void)", 1)[0]
        self.assertIn("AudDrv_GPIO_DACMUX_Select(0)", prepare)
        self.assertNotIn("AudDrv_GPIO_DACMUX_Select(1)", prepare)

    def test_relink_builders_remap_temporary_source_paths(self) -> None:
        airplay_builder = (TOOLS_DIR / "airplay/build_airplay.sh").read_text()
        self.assertIn(
            "-ffile-prefix-map=$work=/usr/src/libreecho-airplay",
            airplay_builder,
        )
        self.assertIn(
            "-fdebug-prefix-map=$work=/usr/src/libreecho-airplay",
            airplay_builder,
        )
        self.assertIn('"$ffmpeg_source/config.h" "$work"', airplay_builder)
        self.assertIn(
            "--disable-avdevice --disable-avfilter --disable-postproc --disable-swscale",
            airplay_builder,
        )
        self.assertIn(
            '"install-lib${library}-static"',
            airplay_builder,
        )
        self.assertIn(
            '"install-lib${library}-headers"',
            airplay_builder,
        )
        self.assertIn(
            '"install-lib${library}-pkgconfig"',
            airplay_builder,
        )
        self.assertNotIn(
            'make DESTDIR="$SYSROOT" install-libs install-headers',
            airplay_builder,
        )
        # The pinned dependency sysroot is immutable on the runner.  FFmpeg's
        # static install writes DESTDIR into the sysroot, so the builder must
        # stage a writable build-local copy and never write into the pinned input.
        self.assertIn('work_sysroot="$work/sysroot"', airplay_builder)
        self.assertIn('cp -a -- "$SYSROOT" "$work_sysroot"', airplay_builder)
        self.assertIn('chmod -R u+w -- "$work_sysroot"', airplay_builder)
        self.assertIn('SYSROOT="$work_sysroot"', airplay_builder)
        # A symlinked sysroot argument must be rejected: cp -a would preserve
        # the link and the recursive chmod would make the pinned tree writable.
        self.assertIn(
            '[[ -d "$SYSROOT" && ! -L "$SYSROOT" ]]', airplay_builder
        )
        self.assertIn(
            '[[ -d "$work_sysroot" && ! -L "$work_sysroot" ]]', airplay_builder
        )
        self.assertIn(
            'before.replace(work, "/usr/src/libreecho-airplay")',
            airplay_builder,
        )
        assistant_builder = (TOOLS_DIR / "assistant/build_curl.sh").read_text()
        self.assertIn(
            "-ffile-prefix-map=$work=/usr/src/libreecho-curl",
            assistant_builder,
        )
        self.assertIn(
            "-fdebug-prefix-map=$work=/usr/src/libreecho-curl",
            assistant_builder,
        )

    def test_connectivity_helper_pins_match_source_built_outputs(self) -> None:
        expected = {
            "sbin/wmt_configure": (
                25744,
                "e0ff85f0ac2cb2b98718556470444cafd1fcd8865cdba27aa67e2c7d7a3303e0",
            ),
            "sbin/wmt_responder": (
                21648,
                "46170ddc1d1ddf21a85ec16df129aac47a258a439bc9e6ed061d1e5942aa48eb",
            ),
            "sbin/wmt_bt_on": (
                21648,
                "985320b270149cd27bc59d7f34d0da829817f225a4e712037633517c843cc745",
            ),
            "sbin/wmt_stock_compat": (
                21648,
                "7e3afe31b706029ebf6e271f5cda6e3880cfc5b184abb052a190662759708c87",
            ),
            "sbin/wmt_launcher": (
                21648,
                "65cb5c0c49bb61aec657c114cf67269e398bf41ff7b70a4abb8eb0ec36ff2c99",
            ),
        }
        builder_pins = {
            target: (specification[1], specification[2])
            for target, specification in builder.CONNECTIVITY_HELPERS.items()
        }
        self.assertEqual(builder_pins, expected)
        self.assertEqual(verifier.CONNECTIVITY_HELPERS, expected)
        self.assertEqual(
            sum(int(item["size"]) for item in builder.CONNECTIVITY_ASSET_REQUIREMENTS.values()),
            552507,
        )
        self.assertEqual(
            sum(int(item["size"]) for item in verifier.CONNECTIVITY_ASSET_REQUIREMENTS.values()),
            552507,
        )

    def test_network_tools_are_pinned_and_manual_only(self) -> None:
        builder_script = TOOLS_DIR / "network-tools/build_wireless_tools.sh"
        self.assertTrue(builder_script.is_file())
        self.assertTrue(os.access(builder_script, os.X_OK))
        lock = json.loads((TOOLS_DIR / "network-tools/SOURCE.lock").read_text())
        self.assertEqual(lock["version"], "30~pre9")
        self.assertEqual(
            lock["source_sha256"],
            "abd9c5c98abf1fdd11892ac2f8a56737544fe101e1be27c6241a564948f34c63",
        )
        builder_source = builder_script.read_text()
        self.assertIn("--archive FILE", builder_source)
        self.assertIn("--kernel-headers DIR", builder_source)
        self.assertIn("--native-root DIR", builder_source)
        self.assertIn("wireless-tools-source.json", builder_source)
        self.assertIn("wireless-tools-COPYING", builder_source)
        self.assertIn("-static -no-pie", builder_source)
        self.assertIn("Type:[[:space:]]+EXEC", builder_source)
        self.assertIn(
            "/home/buildozer/aports/main/musl/src/musl-1.2.5",
            builder_source,
        )
        self.assertIn("grep -Fvx", builder_source)
        self.assertIn("'/home/'", builder_source)
        self.assertIn("'libreecho-wireless-tools-build'", builder_source)
        self.assertNotIn("curl --fail", builder_source)
        regdb_builder = TOOLS_DIR / "network-tools/wireless-regdb/build_wireless_regdb.sh"
        self.assertTrue(regdb_builder.is_file())
        self.assertTrue(os.access(regdb_builder, os.X_OK))
        regdb_lock = json.loads((regdb_builder.parent / "SOURCE.lock").read_text())
        self.assertEqual(regdb_lock["version"], "2025.10.07")
        self.assertEqual(
            regdb_lock["source_sha256"],
            "d4c872a44154604c869f5851f7d21d818d492835d370af7f58de8847973801c3",
        )
        self.assertIn("regulatory.db.p7s", regdb_builder.read_text())
        sodium_builder = TOOLS_DIR / "ota/build_libsodium.sh"
        self.assertTrue(sodium_builder.is_file())
        self.assertTrue(os.access(sodium_builder, os.X_OK))
        sodium_lock = json.loads((TOOLS_DIR / "ota/SOURCE.lock").read_text())
        self.assertEqual(sodium_lock["version"], "1.0.18")
        self.assertEqual(
            sodium_lock["source_sha256"],
            "d59323c6b712a1519a5daf710b68f5e7fde57040845ffec53850911f10a5d4f4",
        )
        pipeline_build = pipeline_text("build.sh")
        pipeline_status = pipeline_text("status.sh")
        pipeline_flash = pipeline_text("flash.sh")
        self.assertIn("build_wireless_tools.sh", pipeline_build)
        self.assertIn("--iwconfig", pipeline_build)
        self.assertIn("--iwconfig-source-metadata", pipeline_build)
        self.assertIn("--expected-iwconfig-sha256", pipeline_status)
        self.assertIn("--expected-iwconfig-sha256", pipeline_flash)
        image_builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        self.assertIn("--iwconfig-source-metadata", image_builder)
        self.assertIn("wireless-tools-COPYING", image_builder)
        self.assertIn("wireless-tools-COPYING", verifier)

    def test_ssh_password_hash_is_salted_and_private(self) -> None:
        dropbear_builder = TOOLS_DIR / "ssh/build_dropbear.sh"
        self.assertIn("-DUSE_DEV_PTMX", dropbear_builder.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "hash"
            valid.write_text("$6$LibreEchoTest$0123456789012345678901234567890123456789012\n")
            valid.chmod(0o600)
            self.assertEqual(
                builder.read_ssh_password_hash(valid),
                "$6$LibreEchoTest$0123456789012345678901234567890123456789012",
            )
            for value in ("password\n", "!locked\n", "\n", "$6$missing-checksum\n"):
                invalid = root / ("invalid-" + str(len(value)))
                invalid.write_text(value)
                invalid.chmod(0o600)
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    builder.read_ssh_password_hash(invalid)
            valid.chmod(0o622)
            with self.assertRaises(SystemExit):
                builder.read_ssh_password_hash(valid)


class VendorAssetContractTests(unittest.TestCase):
    def test_local_asset_contract_is_shared_and_contains_no_payload(self) -> None:
        expected = {
            "ROMv2_lm_patch_1_0_hdr.bin": {
                "source": "etc/firmware/ROMv2_lm_patch_1_0_hdr.bin",
                "mode": 0o644, "size": 127596,
                "sha256": "36d7edc7095f4cdfdaaa9c67061cf079199a55be85ab76ce82c9e9bcb34824a2",
            },
            "ROMv2_lm_patch_1_1_hdr.bin": {
                "source": "etc/firmware/ROMv2_lm_patch_1_1_hdr.bin",
                "mode": 0o644, "size": 50952,
                "sha256": "cabdb842d354dd123d2d3d939f06304c1cfb045f407b5880444be326ded16d8d",
            },
            "WIFI_RAM_CODE_8163": {
                "source": "etc/firmware/WIFI_RAM_CODE_8163",
                "mode": 0o644, "size": 373840,
                "sha256": "9669cc9b03cfdc5e8fd4fd6e14c4c4050e8c196738ca4707eea12f14a6a8e64c",
            },
            "WMT_SOC.cfg": {
                "source": "etc/firmware/WMT_SOC.cfg",
                "mode": 0o644, "size": 119,
                "sha256": "302bd4462de99c028c04092e561c1500d65582ce42a93c4c72ccae6e2c99013d",
            },
        }
        verifier_expected = {
            name: {key: value for key, value in record.items() if key != "mode"}
            for name, record in expected.items()
        }
        self.assertEqual(builder.CONNECTIVITY_ASSET_REQUIREMENTS, expected)
        self.assertEqual(verifier.CONNECTIVITY_ASSET_REQUIREMENTS, verifier_expected)
        specification = TOOLS_DIR / "initramfs/vendor-assets/mt8163-v181-stock-v1.tsv"
        expected_text = "".join(
            f"{record['sha256']}|{record['size']}|{record['source']}|{name}\n"
            for name, record in expected.items()
        )
        self.assertEqual(specification.read_text(), expected_text)

    def test_vendor_importer_is_externally_pinned(self) -> None:
        importer = TOOLS_DIR / "initramfs/libreecho-vendor-import"
        actual = hashlib.sha256(importer.read_bytes()).hexdigest()
        self.assertEqual(builder.CONNECTIVITY_IMPORTER_SHA256, actual)
        self.assertEqual(verifier.CONNECTIVITY_IMPORTER_SHA256, actual)

    def test_vendor_firmware_policy_documents_no_redistribution(self) -> None:
        policy = TOOLS_DIR / "initramfs/vendor-assets/README.md"
        text = policy.read_text()
        self.assertIn("not distributed", text)
        self.assertIn("does not grant redistribution rights", text)
        self.assertIn("read-only system_a", text)

    def test_verifier_requires_wlan_firmware_compatibility_path(self) -> None:
        expected = {"etc/firmware": "../lib/firmware"}
        entries = {
            "etc": verifier.Entry("etc", stat.S_IFDIR | 0o755, 0, 0, 0, b""),
            "etc/firmware": verifier.Entry(
                "etc/firmware", stat.S_IFLNK | 0o777, 0, 0, 0, b"../lib/firmware"
            ),
            "lib": verifier.Entry("lib", stat.S_IFDIR | 0o755, 0, 0, 0, b""),
            "lib/firmware": verifier.Entry(
                "lib/firmware", stat.S_IFDIR | 0o755, 0, 0, 0, b""
            ),
        }
        verifier.validate_connectivity_runtime_symlinks(entries, {"symlinks": expected})

        with self.assertRaises(SystemExit):
            verifier.validate_connectivity_runtime_symlinks(
                {name: entry for name, entry in entries.items() if name != "etc/firmware"},
                {"symlinks": expected},
            )
        with self.assertRaises(SystemExit):
            verifier.validate_connectivity_runtime_symlinks(
                {**entries, "etc/firmware": verifier.Entry(
                    "etc/firmware", stat.S_IFLNK | 0o777, 0, 0, 0, b"../tmp"
                )},
                {"symlinks": expected},
            )
        with self.assertRaises(SystemExit):
            verifier.validate_connectivity_runtime_symlinks(
                {**entries, "etc/firmware": verifier.Entry(
                    "etc/firmware", stat.S_IFLNK | 0o755, 0, 0, 0,
                    b"../lib/firmware"
                )},
                {"symlinks": expected},
            )
        with self.assertRaises(SystemExit):
            verifier.validate_connectivity_runtime_symlinks(
                {name: entry for name, entry in entries.items() if name != "lib/firmware"},
                {"symlinks": expected},
            )

    def test_manifest_comparison_rejects_boolean_for_integer(self) -> None:
        expected = {
            "patch": {
                "header": "8a00",
                "route": "21000ef0",
                "patch_count": 2,
                "download_seq": 1,
                "address": "00000ef0",
            }
        }
        changed = {"patch": {**expected["patch"], "download_seq": True}}
        self.assertFalse(verifier.strictly_equal(changed, expected))
        self.assertTrue(verifier.strictly_equal(expected, expected))


class PolicyTests(unittest.TestCase):
    @staticmethod
    def control(name: str, payload: bytes):
        return verifier.Entry(name, stat.S_IFREG | 0o644, 0, 0, 0, payload)

    def test_android_init_wifi_activation_is_rejected(self) -> None:
        entries = {"rogue.rc": self.control("rogue.rc", b"write\t/dev/wmtWifi 1\n")}
        with self.assertRaises(SystemExit):
            verifier.validate_no_connectivity_autostart(entries)

    def test_interactive_profile_sets_path_and_identifies_libreecho(self) -> None:
        profile = (TOOLS_DIR / "initramfs/profile").read_text()
        self.assertIn("export PATH=/bin:/sbin:/system/bin:/usr/bin:/usr/sbin", profile)
        self.assertIn("LibreEcho Development OS", profile)
        self.assertIn("PS1='libreecho# '", profile)

    def test_service_profile_is_immutable_and_selects_graph(self) -> None:
        init_source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        builder_source = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        pipeline_build = pipeline_text("build.sh")
        pipeline_readme = pipeline_text("README.md")

        self.assertIn("/etc/libreecho/service-profile", init_source)
        self.assertIn("case \"$SERVICE_PROFILE_VALUE\" in", init_source)
        self.assertIn("diagnostic)", init_source)
        self.assertIn("production)", init_source)
        self.assertIn('services="logd timed web"', init_source)
        self.assertIn(
            'services="logd networkd timed audiod micd waked sttd ledd buttond btd airplayd ttsd agentd web"',
            init_source,
        )
        self.assertIn("--service-profile", builder_source)
        self.assertIn('"service_profile"] = service_profile', builder_source)
        self.assertIn('"etc/libreecho/image-profile", "etc/libreecho/service-profile"', builder_source)
        self.assertIn("--expected-service-profile", verifier_source)
        self.assertIn("etc/libreecho/service-profile", verifier_source)
        self.assertIn("LIBREECHO_SERVICE_PROFILE", pipeline_build)
        self.assertIn("--service-profile", pipeline_build)
        self.assertIn("--expected-service-profile", pipeline_build)
        self.assertIn("service_profile=$SERVICE_PROFILE", pipeline_build)
        self.assertIn("--profile ota --service-profile production", pipeline_readme)

    def test_startup_audio_is_disabled_by_default(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertNotIn("startup_audio_worker", init_script)
        self.assertNotIn("--startup-audio", init_script)
        self.assertIn("log audio-startup-disabled", init_script)

    def test_ui_bundle_startup_contract_is_fail_closed(self) -> None:
        valid_led = "\n".join((
            "DAEMON=/usr/local/sbin/libreecho-ledd",
            "PIDFILE=/var/run/libreecho-ledd.pid",
            "STARTUP_READY=${STARTUP_READY:-/run/libreecho/startup-ready}",
            "ARGS=${ARGS:---foreground --socket $SOCKET --startup-animation --startup-ready $STARTUP_READY}",
            "start_service() {",
            '    start-stop-daemon -S -b -m -p "$PIDFILE" -x "$DAEMON" -- $ARGS',
            "}",
            "case \"${1:-}\" in",
            "    start) start_service ;;",
            "esac",
        )) + "\n"
        valid_web = "\n".join((
            "STARTUP_READY=${STARTUP_READY:-/run/libreecho/startup-ready}",
            "STARTUP_READY_TIMEOUT_TICKS=${STARTUP_READY_TIMEOUT_TICKS:-600}",
            "BT_READY_PATH=${BT_READY_PATH:-/run/libreecho/bluetooth-ready}",
            "bluetooth_integration_state() {",
            "    printf 'disabled\\n'",
            "}",
            "bluetooth_ready() {",
            "    case \"$(bluetooth_integration_state)\" in",
            "        disabled) return 0 ;;",
            "        enabled) [ -f \"$BT_READY_PATH\" ] ;;",
            "        *) return 1 ;;",
            "    esac",
            "}",
            "startup_services_ready() {",
            "    for socket in network audio mic led airplay; do",
            '        [ -S "/run/libreecho/$socket.sock" ] || return 1',
            "    done",
            "    case \"$(bluetooth_integration_state)\" in",
            "        disabled) ;;",
            "        enabled)",
            "            [ -s /var/run/libreecho-btd.pid ] || return 1",
            "            [ -S /run/libreecho/bluetooth.sock ] || return 1",
            "            bluetooth_ready || return 1",
            "            ;;",
            "        *) return 1 ;;",
            "    esac",
            "}",
            "mark_startup_ready() {",
            "    count=0",
            "    while :; do",
            "        if startup_services_ready; then",
            '            tmp="$STARTUP_READY.tmp"',
            "            printf 'schema=1\\n' >\"$tmp\"",
            '            mv -f "$tmp" "$STARTUP_READY"',
            "            return 0",
            "        fi",
            "        sleep 0.1",
            "        count=$((count + 1))",
            '        [ "$count" -lt "$STARTUP_READY_TIMEOUT_TICKS" ] || count=0',
            "    done",
            "}",
            "start_service() { :; }",
            "case \"${1:-}\" in",
            "    start) start_service\n        mark_startup_ready >/dev/null 2>&1 & ;;",
            "esac",
        )) + "\n"
        valid_agentd = "\n".join((
            "AGENT_DEPENDENCY_TIMEOUT_SECONDS=${AGENT_DEPENDENCY_TIMEOUT_SECONDS:-90}",
            "AGENT_DEPENDENCY_POLL_SECONDS=${AGENT_DEPENDENCY_POLL_SECONDS:-1}",
            "dependency_sockets_ready() {",
            "    [ -S \"$WAKE_SOCKET\" ] &&",
            "        [ -S \"$STT_SOCKET\" ] &&",
            "        [ -S \"$AUDIO_SOCKET\" ] &&",
            "        [ -S \"$TTS_SOCKET\" ]",
            "}",
            "missing_dependency_sockets() {",
            "    printf 'wakeword,stt,audio,tts\\n'",
            "}",
            "wait_for_dependency_sockets() {",
            "    elapsed=0",
            "    while ! dependency_sockets_ready; do",
            '        if [ "$elapsed" -ge "$AGENT_DEPENDENCY_TIMEOUT_SECONDS" ]; then',
            '            echo "dependencies not ready after ${AGENT_DEPENDENCY_TIMEOUT_SECONDS}s: $(missing_dependency_sockets)" >&2',
            "            return 1",
            "        fi",
            "        remaining=$((AGENT_DEPENDENCY_TIMEOUT_SECONDS - elapsed))",
            "        poll_seconds=$AGENT_DEPENDENCY_POLL_SECONDS",
            '        if [ "$poll_seconds" -gt "$remaining" ]; then',
            "            poll_seconds=$remaining",
            "        fi",
            '        sleep "$poll_seconds"',
            "        elapsed=$((elapsed + poll_seconds))",
            "    done",
            "}",
            "start_service() {",
            "    wait_for_dependency_sockets || {",
            "        unmount_runtime",
            "        return 1",
            "    }",
            "}",
            "case \"${1:-}\" in",
            "    start) start_service ;;",
            "esac",
        )) + "\n"

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            led = bundle / "etc/init.d/libreecho-ledd.init"
            web = bundle / "etc/init.d/libreecho-web.init"
            agentd = bundle / "etc/init.d/libreecho-agentd.init"
            led.parent.mkdir(parents=True)
            led.write_text(valid_led)
            web.write_text(valid_web)
            agentd.write_text(valid_agentd)

            builder.validate_ui_startup_contract(bundle)

            one_shot_web = valid_web
            for marker in (
                "STARTUP_READY_TIMEOUT_TICKS=${STARTUP_READY_TIMEOUT_TICKS:-600}\n",
                "    while :; do\n",
                "        sleep 0.1\n",
                "        count=$((count + 1))\n",
                '        [ "$count" -lt "$STARTUP_READY_TIMEOUT_TICKS" ] || count=0\n',
            ):
                one_shot_web = one_shot_web.replace(marker, "")
            loop_done = one_shot_web.rfind("    done\n")
            self.assertGreater(loop_done, -1)
            one_shot_web = one_shot_web[:loop_done] + one_shot_web[loop_done + len("    done\n"):]
            web.write_text(one_shot_web)
            with self.assertRaisesRegex(SystemExit, "STARTUP_READY_TIMEOUT_TICKS"):
                builder.validate_ui_startup_contract(bundle)
            web.write_text(valid_web)

            noncanonical_led = valid_led.replace(
                "/run/libreecho/startup-ready", "/tmp/startup-ready"
            )
            noncanonical_web = valid_web.replace(
                "/run/libreecho/startup-ready", "/tmp/startup-ready"
            )
            led.write_text(noncanonical_led)
            web.write_text(noncanonical_web)
            with self.assertRaisesRegex(SystemExit, "canonical readiness path"):
                builder.validate_ui_startup_contract(bundle)
            led.write_text(valid_led)
            web.write_text(valid_web)

            malformed_web = valid_web.replace(
                '        [ "$count" -lt "$STARTUP_READY_TIMEOUT_TICKS" ] || count=0\n'
                "    done\n",
                '        [ "$count" -lt "$STARTUP_READY_TIMEOUT_TICKS" ] || count=0\n',
            )
            web.write_text(malformed_web)
            with self.assertRaisesRegex(SystemExit, "invalid shell syntax"):
                builder.validate_ui_startup_contract(bundle)
            web.write_text(valid_web)

            web.write_text(valid_web.replace(
                '        [ -S "/run/libreecho/$socket.sock" ] || return 1\n',
                '        : "$socket"\n',
            ))
            with self.assertRaisesRegex(SystemExit, "startup contract missing"):
                builder.validate_ui_startup_contract(bundle)
            web.write_text(valid_web)

            web.write_text(valid_web.replace(
                '            printf \'schema=1\\n\' >"$tmp"\n'
                '            mv -f "$tmp" "$STARTUP_READY"\n',
                '            mv -f "$tmp" "$STARTUP_READY"\n'
                '            printf \'schema=1\\n\' >"$tmp"\n',
            ))
            with self.assertRaisesRegex(SystemExit, "ordering is invalid"):
                builder.validate_ui_startup_contract(bundle)
            web.write_text(valid_web)

            led.write_text(valid_led.replace(
                "/run/libreecho/startup-ready",
                "/run/libreecho/other-ready",
            ))
            with self.assertRaisesRegex(SystemExit, "different readiness paths"):
                builder.validate_ui_startup_contract(bundle)
            led.write_text(valid_led)

            led.write_text(valid_led.replace("--startup-animation ", ""))
            with self.assertRaisesRegex(SystemExit, "startup-animation"):
                builder.validate_ui_startup_contract(bundle)

    def test_streaming_voice_services_start_warm_in_dependency_order(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        service_line = (
            'services="logd networkd timed audiod micd waked sttd ledd buttond btd '
            'airplayd ttsd agentd web"'
        )
        self.assertIn(service_line, init_script)
        self.assertLess(service_line.index("audiod"), service_line.index("buttond"))
        self.assertLess(service_line.index("ledd"), service_line.index("buttond"))
        self.assertLess(service_line.index("waked"), service_line.index("sttd"))
        self.assertLess(service_line.index("sttd"), service_line.index("agentd"))
        self.assertLess(service_line.index("ttsd"), service_line.index("agentd"))

    def test_hostname_is_derived_from_audited_idme_serial(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn("/data/libreecho/config/web-config.json", init_script)
        self.assertIn('"hostname_persisted"', init_script)
        self.assertIn("hostname_source=persisted", init_script)
        self.assertIn("if=/proc/idme/serial", init_script)
        self.assertIn("serial_suffix=${serial#\"$serial_prefix\"}", init_script)
        self.assertIn('hostname="LibreEcho-$serial_suffix"', init_script)
        self.assertIn("/proc/sys/kernel/hostname", init_script)
        self.assertIn('log "hostname-set:$hostname:$hostname_source"', init_script)

    def test_wifi_configuration_uses_persistent_secret_store(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn(
            "/data/libreecho/config/wpa_supplicant.conf", init_script
        )
        self.assertIn("update_config=1", init_script)
        self.assertIn('WIFI_CONF="$wifi_profile"', init_script)

    def test_wifi_regulatory_database_is_packaged_and_verified(self) -> None:
        expected = {
            "regulatory.db": "5560f4f0fdac7d1bb2adf8d4d083f39e3bee5ba55192feadadc091df55a813eb",
            "regulatory.db.p7s": "5dd27969661bed1e021ce435f535a53f201705bda14c2dba0db6353d1cdc6fff",
        }
        for name, digest in expected.items():
            path = TOOLS_DIR / "initramfs" / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        for name in expected:
            self.assertIn(f'"{name}": ("lib/firmware/{name}", 0o644)', builder)
            self.assertIn(f'"{name}": 0o644', verifier_source)
            self.assertIn(f'"{name}": "lib/firmware/{name}"', verifier_source)

    def test_device_node_setup_is_not_activation(self) -> None:
        entries = {
            "libreecho-init": self.control(
                "libreecho-init", b"mknod /dev/wmtWifi c 190 0\nchmod 0660 /dev/wmtWifi\n"
            )
        }
        verifier.validate_no_connectivity_autostart(entries)

    def test_linux61_adb_uses_configfs_functionfs_before_binding_udc(self) -> None:
        source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertNotIn("/sys/class/android_usb/android0", source)
        self.assertIn("/sys/kernel/config/usb_gadget/libreecho", source)
        self.assertIn("mount -t configfs configfs /sys/kernel/config", source)
        self.assertIn("functions/ffs.adb", source)
        self.assertIn("configs/c.1/ffs.adb", source)
        self.assertIn("mount -t functionfs -o uid=2000,gid=2000 adb /dev/usb-ffs/adb", source)
        self.assertIn("configfs-mounted", source)
        self.assertIn("functionfs-mounted", source)
        self.assertIn("adbd-started:", source)
        self.assertNotIn("--root_seclabel", source)
        self.assertIn("export ADB_TRACE=all", source)
        self.assertIn("adbd-diagnostic-begin", source)
        self.assertIn("adbd-task:", source)
        self.assertIn("adbd-fd:", source)
        self.assertIn("adbd-log:", source)
        self.assertIn("adbd-exit-status:", source)
        self.assertIn("configfs-udc-bound:", source)
        self.assertIn("functionfs-ready", source)
        create = source.index('G=/sys/kernel/config/usb_gadget/libreecho')
        ffs_mount = source.index("mount -t functionfs", create)
        adbd = source.index("/sbin/adbd ", ffs_mount)
        endpoints = source.index("/dev/usb-ffs/adb/ep1", adbd)
        bind = source.index('> "$G/UDC"', endpoints)
        self.assertLess(create, ffs_mount)
        self.assertLess(ffs_mount, adbd)
        self.assertLess(adbd, endpoints)
        self.assertLess(endpoints, bind)

    def test_production_pipeline_requires_full_mt8163_hardware_closure(self) -> None:
        """Regression: a generic FunctionFS kernel must not pass as MT8163-ready."""
        pipeline_build = pipeline_text("build.sh")
        required_lines = (
            "require_config USB_MUSB_MEDIATEK y",
            "require_config USB_CONFIGFS y",
            "require_config USB_CONFIGFS_F_FS y",
            "require_config USB_CONFIGFS_RNDIS y",
            "require_config PINCTRL_MT8163 y",
            "require_config MTK_MT8163_CONSYS y",
            "require_config MTK_COMBO_WIFI y",
            "require_config LEDS_CLASS_MULTICOLOR y",
            "require_config LEDS_IS31FL32XX y",
            "require_config MTK_MT8163_BLUEZ_HCI y",
            "require_config MFD_MT6397 y",
            "require_config REGULATOR_MT6323 y",
            "require_config POWER_RESET_MT6323 y",
            "require_config RTC_DRV_MT6397 y",
            "require_config KEYBOARD_MTK_PMIC y",
            "require_config PWM_MEDIATEK y",
            "require_config NVMEM_MTK_EFUSE y",
            "require_config IIO_ST_LSM6DSX y",
            "require_config AMZ_PRIVACY y",
            "require_config SND_SOC_TLV320AIC32X4 y",
            "require_config SND_SOC_TLV320AIC32X4_I2C y",
            "require_config SND_SOC_AMZN_MT8163_SPI_AUDIO y",
            "require_config SND_SOC_MT8163_RADAR_PUFFIN y",
            "require_config LEDS_MT6323 y",
            "require_config FILE_LOCKING y",
            "require_config INOTIFY_USER y",
            "require_config IP_MULTICAST y",
            "require_config BLK_DEV_LOOP y",
            "require_config SQUASHFS_LZ4 y",
        )
        for required in required_lines:
            with self.subTest(required=required):
                self.assertIn(required, pipeline_build)
        self.assertNotIn("require_config USB_FUNCTIONFS y", pipeline_build)
        self.assertIn(
            'KERNEL_DTB="$KERNEL_OUT/arch/arm/boot/dts/libreecho-radar-puffin.dtb"',
            pipeline_build,
        )
        self.assertIn('cp -- "$KERNEL_DTB" "$RUN/libreecho-radar-puffin.dtb"', pipeline_build)
        self.assertIn('DTB_VERIFIER="$TOOLS_DIR/verify_radar_puffin_dtb.py"', pipeline_build)
        self.assertIn('python3 -B "$DTB_VERIFIER" --dtb "$KERNEL_DTB"', pipeline_build)
        self.assertIn("RADAR_PUFFIN_DAC_PROCESSING_BLOCK", pipeline_build)
        self.assertIn("AFE_APLL2_DIV0_SEL_4", pipeline_build)
        self.assertIn("AFE_I05_TO_O03", pipeline_build)
        self.assertIn('cp -- "$KERNEL_OUT/.config" "$RUN/kernel.config"', pipeline_build)
        self.assertIn('kernel_config_sha="$(sha256sum "$RUN/kernel.config"', pipeline_build)
        self.assertIn("kernel_config=$RUN/kernel.config", pipeline_build)
        self.assertIn("kernel_config_sha256=$kernel_config_sha", pipeline_build)
        self.assertNotIn("WIFI_DTB_SHA256=", pipeline_build)
        self.assertEqual(
            pipeline_build.count('ui_diff_sha="$(source_state_sha256 "$UI_SOURCE")"'),
            1,
        )
        ui_builder = (TOOLS_DIR / "ui/build_ui_bundle.sh").read_text()
        stable_untracked_hash = (
            'sha256sum "$repository/$relative" | awk \'{print $1}\''
        )
        self.assertIn(stable_untracked_hash, pipeline_build)
        self.assertIn(stable_untracked_hash, ui_builder)

    def test_usb_diagnostic_boot_keeps_connectivity_manual(self) -> None:
        source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        defconfig = (TOOLS_DIR.parent.parent / "arch/arm/configs/mt8163_arm32_defconfig").read_text()
        self.assertIn("CONFIG_KEYBOARD_GPIO=y", defconfig)
        self.assertIn("create_input_nodes()", source)
        self.assertIn("/dev/input/$name", source)
        self.assertIn("input-devnodes-created", source)
        self.assertIn("log init-ready-pid1-managed", source)
        self.assertIn("start_wifi_network &", source)
        self.assertIn("wifi-network-worker-started-after-adb", source)
        self.assertIn("/tmp/wifi.activation.claim", source)
        self.assertIn('$BB mkdir /tmp/wifi.activation.claim', source)
        self.assertIn("wifi_request_loop()", source)
        self.assertIn("/tmp/wifi.request", source)
        self.assertIn("wifi_request_loop &", source)
        self.assertIn("wifi-request-supervisor-started", source)
        self.assertIn("wifi-request-start-accepted", source)
        self.assertIn("wifi-request-duplicate-rejected", source)
        self.assertIn("LIBREECHO_WIFI_RC=", source)
        self.assertLess(
            source.index("log init-ready-pid1-managed"),
            source.index("wifi_request_loop &"),
        )
        self.assertIn("SERVICE_PROFILE=diagnostic", source)
        self.assertIn('SERVICE_PROFILE_VALUE=$($BB cat /etc/libreecho/service-profile', source)
        self.assertIn("service-profile-invalid-fallback-diagnostic", source)
        policy = source.index('if [ "$SERVICE_PROFILE" = diagnostic ]; then')
        manual = source.index("log wifi-network-policy-manual-single-shot", policy)
        automatic = source.index("start_wifi_network &", policy)
        automatic_log = source.index("log wifi-network-worker-started-after-adb", automatic)
        self.assertLess(manual, automatic)
        self.assertLess(automatic, automatic_log)
        self.assertIn('services="logd timed web"', source)
        self.assertIn("ui-connectivity-services-disabled-for-diagnostic-profile", source)
        self.assertNotIn("USB_DIAGNOSTIC_MODE=1", source)
        self.assertNotIn("--allow-insecure-lan", source)
        self.assertNotIn("ui-web-production-loopback-fallback", source)
        self.assertIn("ui-web-production-lan", source)
        self.assertIn("web_listen=0.0.0.0:8080", source)
        self.assertNotIn("if [ -r /data/libreecho/config/users ]; then", source)
        self.assertNotIn("libreecho-update-fetch watch", source)
        self.assertIn(
            'if [ "$IMAGE_PROFILE" = ota ] && [ "$SERVICE_PROFILE" = production ]; then',
            source,
        )
        self.assertIn("ota-background-workers-disabled-for-diagnostic-profile", source)
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        self.assertIn('"activation": "manual-single-shot-after-adb"', builder)
        self.assertIn(
            'network.get("activation") != "manual-single-shot-after-adb"',
            verifier_source,
        )
        self.assertIn("reboot-supervisor-started", source)
        self.assertIn("/tmp/reboot.request", source)
        self.assertIn("runme-timeout", source)
        self.assertIn("/tmp/runme.cancel", source)
        self.assertIn("wmt_stock_compat", source)
        self.assertIn("--no-function-on", source)
        self.assertIn("--ok --once", source)
        self.assertIn("pidof wmt_launcher", source)
        self.assertIn("timeout 30", source)
        self.assertIn("/sbin/libreecho-wifi", source)
        self.assertIn("/etc/udhcpc.script", (TOOLS_DIR / "initramfs/libreecho-wifi").read_text())
        self.assertNotIn("/system/vendor/bin/wmt_loader >/tmp/wifi-wmt-loader.log", source)

    def test_production_ota_health_worker_checks_full_service_graph(self) -> None:
        source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn('ota_health_confirm_worker &', source)
        self.assertIn('[ "$SERVICE_PROFILE" = production ]', source)
        for socket_path in (
            "/run/libreecho/network.sock",
            "/run/libreecho/audio.sock",
            "/run/libreecho/mic.sock",
            "/run/libreecho/led.sock",
            "/run/libreecho/bluetooth.sock",
            "/run/libreecho/stt.sock",
            "/run/libreecho/tts.sock",
            "/run/libreecho/agent.sock",
            "/run/libreecho/airplay.sock",
        ):
            self.assertIn(socket_path, source)
        self.assertIn("ota-health-services-not-ready", source)
        self.assertIn("ota-background-workers-disabled-for-diagnostic-profile", source)

    def test_userdata_mount_is_identity_checked_and_non_destructive(self) -> None:
        source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn("USERDATA=/dev/mmcblk0p16", source)
        self.assertIn("PARTNAME=userdata", source)
        self.assertIn("2137088", source)
        self.assertIn("mount -t ext4 -o rw,nosuid,nodev,noatime", source)
        self.assertIn("userdata-mount-failed", source)
        self.assertNotIn("mkfs", source)
        self.assertLess(source.index("userdata-mounted"), source.index("start_ui_services"))

    def test_time_service_is_packaged_and_started(self) -> None:
        builder_source = (TOOLS_DIR / "build_recovery_image.py").read_text()
        bundle_source = (TOOLS_DIR / "ui/build_ui_bundle.sh").read_text()
        init_source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        for expected in (
            "libreecho-timed", "libreecho-timed.init", "etc/libreecho/ntp.conf",
        ):
            self.assertIn(expected, builder_source)
            self.assertIn(expected, bundle_source)
        self.assertIn(
            'services="logd networkd timed audiod', init_source
        )

    def test_remote_wyoming_clients_use_pinned_musl_runtime(self) -> None:
        bundle_source = (TOOLS_DIR / "ui/build_ui_bundle.sh").read_text()
        builder_source = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        for source in (bundle_source, builder_source, verifier_source):
            self.assertIn("libreecho-sttd-wyoming", source)
            self.assertIn("libreecho-ttsd-wyoming", source)
            self.assertIn("/lib/ld-musl-armhf.so.1", source)
            self.assertIn("libc.musl-armv7.so.1", source)

    def test_fresh_install_confirms_its_own_boot_slot(self) -> None:
        # A clean install (BROM/Amonet or factory) writes no update record.
        # The bootloader still decrements the slot try counter every boot and
        # refuses the slot once it reaches zero while success is 0, so the
        # health worker must confirm the running slot rather than returning
        # early when there is no pending OTA.
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertNotIn(
            "[ -r /data/libreecho/update/pending ] || return 0", init
        )
        self.assertIn("CONFIRM_MODE=first-boot", init)
        self.assertIn("CONFIRM_MODE=ota", init)
        self.assertIn(
            'libreecho-bootctl confirm "$selected_slot"', init
        )
        self.assertIn("first-boot-slot-confirmed", init)
        self.assertIn("first-boot-slot-already-confirmed", init)
        # The confirmation must sit behind the same health gate the OTA path
        # uses, never in front of it.
        self.assertLess(
            init.index('log "first-boot-confirm-pending:$selected_slot"'),
            init.index('[ "$passed" -eq 3 ]'),
        )
        # A first boot must never reboot: there is no previously confirmed
        # slot to fall back to.
        self.assertLess(
            init.index('log "first-boot-confirm-failed:$last_check"'),
            init.index("ota-health-confirm-failed-rebooting"),
        )
        self.assertIn(
            'if [ "$CONFIRM_MODE" = ota ]; then', init
        )

    def test_fresh_install_requires_marker_and_persistent_hash_transaction(self) -> None:
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        cleanup = (TOOLS_DIR / "initramfs/libreecho-data-cleanup").read_text()
        self.assertIn("first-install-confirm", builder)
        self.assertIn("first-install-marker-absent-or-invalid", init)
        self.assertIn("first-install.pending", init)
        self.assertIn("boot_sha256=", init)
        self.assertIn("first-install.confirmed", init)
        self.assertIn("$BB sync", init)
        self.assertIn("first-install.pending", cleanup)
        self.assertIn("first-install.confirmed", cleanup)
        self.assertNotIn("no pending OTA", init)

    def test_fresh_install_finalization_preserves_pending_on_commit_failure(self) -> None:
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        finalization = init[init.index("first_install_confirmed="):init.index("fi\n        # Never restart", init.index("first_install_confirmed="))]
        self.assertIn("if $BB sed", finalization)
        self.assertIn("$BB mv", finalization)
        self.assertIn("$BB rm -f \"$first_install_confirmed\"", finalization)
        self.assertIn("first-install-confirmation-record-finalization-failed", finalization)
        self.assertLess(finalization.index("$BB mv"), finalization.index("$BB rm -f /data/libreecho/update/first-install.pending"))

    def test_ota_vm_asserts_each_phase_and_resets_bcb(self) -> None:
        vm = TOOLS_DIR / "ota-test-vm"
        init = (vm / "build-initramfs.sh").read_text()
        boot = (vm / "boot-test.sh").read_text()
        self.assertIn("R=/work/initramfs", init)
        self.assertIn("data,tmp,tools", init)
        self.assertIn("reset_bcb()", init)
        self.assertGreaterEqual(init.count("reset_bcb"), 3)
        self.assertIn("assert_rc", init)
        self.assertIn("assert_output", init)
        self.assertNotIn("| $B head", init)
        self.assertNotIn("| $B tail", init)
        self.assertIn("qemu_rc=$?", boot)
        self.assertIn("wait $QPID", boot)
        self.assertNotIn('echo "qemu exited rc=$?"', boot)

    def test_bootctl_can_report_running_slot_from_bootloader_hint(self) -> None:
        bootctl = (TOOLS_DIR / "ota/libreecho_bootctl.c").read_text()
        init = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn("running_slot", bootctl)
        self.assertIn("status [a|b]", bootctl)
        self.assertIn("androidboot.slot_suffix", init)
        self.assertIn('libreecho-bootctl status "$running_slot"', init)
        self.assertIn("running-slot", init)

    def test_emulation_defaults_to_loopback_published_authenticated_web(self) -> None:
        emulation = TOOLS_DIR / "emulation"
        for name in ("entrypoint.sh", "entrypoint-mock.sh"):
            source = (emulation / name).read_text()
            self.assertIn("--listen 0.0.0.0:8080", source)
            self.assertNotIn("--allow-insecure-lan", source)
        for name in ("README.md", "build.sh"):
            source = (emulation / name).read_text()
            self.assertIn("127.0.0.1:8080:8080", source)
            self.assertNotIn("-p 8080:8080", source)

    def test_ota_target_slots_are_identity_checked_before_block_io(self) -> None:
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        self.assertIn("target_device_for_slot()", updater)
        self.assertIn("target_partname=boot_a_x", updater)
        self.assertIn("target_partname=boot_b_x", updater)
        self.assertIn('grep -qx "PARTNAME=$target_partname"', updater)
        self.assertIn('cat "$target_sys/size"', updater)
        self.assertIn('= "$BOOT_SECTORS"', updater)
        self.assertLess(
            updater.index("target_device_for_slot \"$target\""),
            updater.index('dd if="$STAGING/boot.img" of="$target_device"'),
        )
        self.assertLess(
            updater.rindex("target_device_for_slot \"$slot\""),
            updater.rindex('dd if="$target_device" bs=4096 count=4096'),
        )

    def test_ota_manual_installer_seeds_persistent_channel(self) -> None:
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        self.assertIn("CHANNEL_FILE=$UPDATE_ROOT/automatic-updates", updater)
        self.assertIn("channel_value()", updater)
        self.assertIn("write_channel()", updater)
        setup = updater[updater.index("require_userdata()"):updater.index("target_device_for_slot()")]
        self.assertIn('seed_channel', setup)
        self.assertIn('channel_value()', updater)
        self.assertIn('write_channel()', updater)

        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        self.assertIn(
            "boot_sha256 feature_policy image_profile service_profile update_channel'",
            updater,
        )
        self.assertIn("diagnostic|production", updater)
        self.assertIn("update_channel", updater)
        self.assertIn("dev|stable", updater)
        self.assertIn("update_channel=$UPDATE_CHANNEL", updater)
        self.assertIn("channel_value()", updater)
        self.assertIn("write_channel()", updater)
        self.assertLess(
            updater.index('update_channel=$($BB cat "$PACKAGED_CHANNEL_FILE" 2>/dev/null)', updater.index('confirm_pending()')),
            updater.index('update_channel=$(channel_value)', updater.index('confirm_pending()')),
        )
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn("installed_channel", fetcher)
        self.assertIn("rolled_back_channel", fetcher)
        self.assertIn("INSTALL_LOCK=$ROOT/install.lock", fetcher)
        self.assertIn("userdata_not_mounted", fetcher)
        self.assertNotIn("pending_channel_race", fetcher)
        self.assertNotIn("migrate_pending_channel", fetcher)
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        self.assertIn("args.expected_update_channel, args.expected_busybox_sha256", verifier)
        self.assertIn("pending_channel_preserve", updater)
        self.assertIn("printf '%s\\n' \"channel=$selected_channel\"", updater)
        self.assertIn("die manifest_service_profile", updater)

    def test_ota_status_reports_effective_feature_payload_identity(self) -> None:
        """Status must expose preserved payload and running-daemon identities."""
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        for feature, daemon in (
            ("airplay2", "libreecho-audio-engine"),
            ("tts", "libreecho-ttsd"),
            ("wakeword", "libreecho-waked"),
            ("stt", "libreecho-sttd"),
            ("assistant", "libreecho-agentd"),
        ):
            self.assertIn(f"{feature}) daemon={daemon}", updater)
        self.assertIn("feature_root=/data/libreecho/features/$feature", updater)
        self.assertIn("payload=$feature_root/payload.squashfs", updater)
        self.assertIn("manifest=$feature_root/manifest.json", updater)
        for field in (
            "payload_sha256", "payload_size", "manifest_sha256",
            "running_daemon_sha256", "effective",
        ):
            self.assertIn(f"feature_${{feature}}_{field}", updater)
        self.assertIn("feature_status()", updater)
        self.assertIn("printf '%s\\n' \"$digest\"", updater)
        self.assertNotIn("printf '%s\\\\n' \"$digest\"", updater)
        self.assertIn('"/proc/$pid/exe"', updater)
        status_block = updater[updater.index("    status)"):]
        self.assertIn("feature_status", status_block)

    def test_preserve_ota_manifest_binds_payload_and_daemon_identities(self) -> None:
        """A preserve OTA must carry every identity that the device retains."""
        from nacl.signing import SigningKey

        daemon_paths = {
            "airplay2": ("airplay", "usr/local/sbin/libreecho-audio-engine"),
            "tts": ("tts", "usr/local/sbin/libreecho-ttsd"),
            "wakeword": ("wakeword", "usr/local/sbin/libreecho-waked"),
            "stt": ("stt", "usr/local/sbin/libreecho-sttd"),
            "assistant": ("assistant", "usr/local/sbin/libreecho-agentd"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boot = root / "boot.img"
            boot_bytes = b"ANDROID!" + bytes((16 * 1024 * 1024) - 8)
            boot.write_bytes(boot_bytes)
            key = SigningKey.generate()
            private = root / "private.hex"
            public = root / "public.hex"
            build_manifest = root / "build-manifest.json"
            private.write_text(key.encode().hex() + "\n")
            public.write_text(key.verify_key.encode().hex() + "\n")
            features = {}
            for feature, (manifest_feature, daemon) in daemon_paths.items():
                features[manifest_feature] = {
                    "payload": {
                        "sha256": "a" * 64,
                        "size": 123,
                        "manifest_sha256": "b" * 64,
                        "files": {daemon: {"sha256": "c" * 64}},
                    },
                }
            build_manifest.write_text(json.dumps({
                "image_profile": "ota",
                "service_profile": "production",
                "feature_policy": "preserve",
                "update_channel": "dev",
                "output": {
                    "sha256": hashlib.sha256(boot_bytes).hexdigest(),
                    "size": len(boot_bytes),
                },
                **features,
            }))
            output = root / "update.ota.tar"
            subprocess.run([
                sys.executable, str(TOOLS_DIR / "ota/make_ota_bundle.py"),
                "--boot-image", str(boot), "--build-manifest", str(build_manifest),
                "--version", "test-v1", "--signing-key", str(private),
                "--public-key", str(public), "--service-profile", "production",
                "--feature-policy", "preserve", "--update-channel", "dev",
                "--output", str(output),
            ], check=True, capture_output=True, text=True)
            with tarfile.open(output, "r:") as archive:
                manifest = archive.extractfile("manifest").read().decode()
            self.assertIn("manifest_version=2\n", manifest)
            for feature in daemon_paths:
                self.assertIn(f"feature_{feature}_payload_sha256={'a' * 64}\n", manifest)
                self.assertIn(f"feature_{feature}_payload_size=123\n", manifest)
                self.assertIn(f"feature_{feature}_manifest_sha256={'b' * 64}\n", manifest)
                self.assertIn(f"feature_{feature}_daemon_sha256={'c' * 64}\n", manifest)

    def test_preserve_ota_bundle_rejects_missing_retained_identity(self) -> None:
        """A preserve OTA without candidate daemon identities must fail closed."""
        from nacl.signing import SigningKey

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boot = root / "boot.img"
            boot_bytes = b"ANDROID!" + bytes((16 * 1024 * 1024) - 8)
            boot.write_bytes(boot_bytes)
            key = SigningKey.generate()
            private = root / "private.hex"
            public = root / "public.hex"
            build_manifest = root / "build-manifest.json"
            private.write_text(key.encode().hex() + "\n")
            public.write_text(key.verify_key.encode().hex() + "\n")
            build_manifest.write_text(json.dumps({
                "image_profile": "ota",
                "service_profile": "production",
                "feature_policy": "preserve",
                "update_channel": "dev",
                "output": {
                    "sha256": hashlib.sha256(boot_bytes).hexdigest(),
                    "size": len(boot_bytes),
                },
            }))
            result = subprocess.run([
                sys.executable, str(TOOLS_DIR / "ota/make_ota_bundle.py"),
                "--boot-image", str(boot), "--build-manifest", str(build_manifest),
                "--version", "test-v1", "--signing-key", str(private),
                "--public-key", str(public), "--service-profile", "production",
                "--feature-policy", "preserve", "--update-channel", "dev",
                "--output", str(root / "update.ota.tar"),
            ], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("preserve policy requires candidate feature identities", result.stderr)

    def test_preserve_installer_fails_before_boot_write_on_identity_mismatch(self) -> None:
        """The updater must gate retained daemon identity before any block write."""
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        self.assertIn("verify_preserved_feature_identity()", updater)
        self.assertIn("preserve_feature_payload_mismatch", updater)
        self.assertIn("preserve_feature_daemon_mismatch", updater)
        self.assertIn("feature_daemon_required()", updater)
        self.assertIn("integrations & 1", updater)
        self.assertIn("if ! feature_daemon_required \"$feature\"; then", updater)
        install = updater[updater.index('install_package()'):]
        verify_call = install.index('verify_preserved_feature_identity')
        boot_write = install.index('dd if="$STAGING/boot.img" of="$target_device"')
        self.assertLess(verify_call, boot_write)

    def test_preserve_installer_uses_installed_profile_for_transitions(self) -> None:
        """Daemon requirements must describe the running image, not the candidate."""
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        self.assertIn(
            'current=$($BB cat /etc/libreecho/service-profile 2>/dev/null)',
            updater,
        )
        self.assertIn("CURRENT_SERVICE_PROFILE=$current", updater)
        self.assertIn('[ "$CURRENT_SERVICE_PROFILE" != diagnostic ] || return 1', updater)
        self.assertNotIn('[ "${SERVICE_PROFILE:-production}" != diagnostic ] || return 1', updater)
        # A diagnostic -> production install must validate retained files but
        # must not require production daemons before the reboot boundary.
        self.assertIn('SERVICE_PROFILE=$(manifest_value service_profile)', updater)
        daemon_guard = updater[updater.index("feature_daemon_required()"):updater.index("write_preserved_feature_identity()")]
        self.assertNotIn("SERVICE_PROFILE=", daemon_guard)

    def test_preserve_pending_transaction_revalidates_after_staging(self) -> None:
        """Confirmation must use identities persisted before feature staging."""
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        install = updater[updater.index("install_package()"):updater.index("confirm_pending()")]
        writer = updater[updater.index("write_preserved_feature_identity()"):updater.index("verify_preserved_feature_identity()")]
        confirm = updater[updater.index("confirm_pending()"):]
        self.assertIn("write_preserved_feature_identity", install)
        for field in ("payload_sha256", "payload_size", "manifest_sha256", "daemon_sha256"):
            self.assertIn(f"feature_${{feature}}_${{field}}", writer)
        self.assertIn(
            "feature_policy=$($BB sed -n 's/^feature_policy=//p' \"$PENDING\")",
            confirm,
        )
        self.assertIn("verify_preserved_feature_identity pending", confirm)
        verify = updater[updater.index("verify_preserved_feature_identity()"):updater.index("clear_exact_development_marker()")]
        self.assertIn("preserved_identity_value", verify)
        self.assertIn("preserve_feature_payload_mismatch", verify)
        self.assertIn("preserve_feature_manifest_mismatch", verify)
        self.assertLess(
            confirm.index("verify_preserved_feature_identity pending"),
            confirm.index('"$BOOTCTL" confirm'),
        )

    def test_host_ota_path_is_explicit_and_uses_guarded_updater(self) -> None:
        host = pipeline_file("ota.sh").read_text()
        preflight = pipeline_file("ota-preflight-root.sh").read_text()
        install = pipeline_file("ota-install-root.sh").read_text()
        self.assertIn("mode=preflight", host)
        self.assertIn("--install", host)
        self.assertIn("--current", host)
        self.assertIn('CURRENT="${LIBREECHO_CURRENT:-$PIPELINE/out/CURRENT}"', host)
        self.assertIn('LIBREECHO_CURRENT="$CURRENT" "$PIPELINE/status.sh"', host)
        status = pipeline_file("status.sh").read_text()
        self.assertIn('CURRENT="${LIBREECHO_CURRENT:-$OUT/CURRENT}"', status)
        self.assertIn("ota_bundle_sha256", host)
        self.assertIn("ota-preflight-root.sh", host)
        self.assertIn("ota-install-root.sh", host)
        self.assertIn("--check-current", host)
        self.assertIn("--confirm-current", host)
        self.assertIn("ota-confirm-current-root.sh", host)
        confirm = pipeline_file("ota-confirm-current-root.sh").read_text()
        self.assertIn("CONFIRM_CURRENT=YES", confirm)
        self.assertIn("OTA_CURRENT_SLOT_CONFIRM_READY", confirm)
        self.assertIn("ALLOW_UNCONFIRMED=1", confirm)
        self.assertIn("libreecho-web.init status", confirm)
        self.assertIn("/dev/usb-ffs/adb/ep0", confirm)
        self.assertIn('confirm "$selected"', confirm)
        self.assertIn("selected_image_sha256", confirm)
        self.assertIn("printf 'reboot\\n' > /tmp/reboot.request", install)
        self.assertNotIn("printf 'ota\\n' > /tmp/reboot.request", install)
        self.assertNotIn("fastboot flash", host)
        for expected in ("mmcblk0p10", "boot_a_x", "mmcblk0p11", "boot_b_x", "32768"):
            self.assertIn(expected, preflight)
        self.assertIn("BOOTCTL=/usr/local/sbin/libreecho-bootctl", preflight)
        self.assertIn("$BOOTCTL status", preflight)
        self.assertIn("ota-preflight-root.sh", install)
        self.assertIn("libreecho-update-host", install)
        self.assertIn("sha256sum", install)

    def test_external_publisher_rejects_packaged_feature_drift(self) -> None:
        """The ramdisk payload identities must match the inherited base CURRENT."""
        publisher = pipeline_text("publish-external-candidate.sh")
        self.assertIn("for feature in ('airplay', 'tts', 'wakeword', 'stt', 'assistant')", publisher)
        self.assertIn("payload.get('sha256')", publisher)
        self.assertIn("payload.get('size')", publisher)
        self.assertIn("payload.get('manifest_sha256')", publisher)
        self.assertIn("check_feature_identity", publisher)
        for feature in ("airplay", "tts", "wakeword", "stt", "assistant"):
            self.assertIn(f"check_feature_identity {feature}", publisher)
        self.assertLess(
            publisher.index("check_feature_identity assistant"),
            publisher.index('mkdir -p "$RUN"'),
        )

    def test_pipeline_requires_distinct_tooling_source_for_legacy_kernel(self) -> None:
        """Canonical image/OTA tooling must be selected explicitly and independently."""
        build = pipeline_text("build.sh")
        self.assertIn(
            'TOOLING_SRC_INPUT="${LIBREECHO_TOOLING_SRC:?ERROR: set LIBREECHO_TOOLING_SRC explicitly}"',
            build,
        )
        self.assertIn('TOOLS_DIR="$TOOLING_SRC/tools/mt8163-arm32"', build)
        self.assertIn('git -C "$TOOLING_SRC" rev-parse --show-toplevel', build)
        self.assertIn('tooling_source=$TOOLING_SRC', build)
        self.assertIn('tooling_git_head=$tooling_head', build)
        self.assertIn('tooling_git_diff_sha256=$tooling_diffsha', build)
        self.assertNotIn('tooling_source=$KERNEL_SRC', build)

    def test_marker_contract_uses_explicit_repository_sources(self) -> None:
        build = pipeline_text("build.sh")
        marker = pipeline_text("check_marker_contract.sh")
        self.assertIn('"$KERNEL_SRC"', build)
        self.assertIn('"$TOOLING_SRC"', build)
        self.assertIn('KERNEL_SRC="${3:-}"', marker)
        self.assertIn('TOOLING_SRC="${4:-}"', marker)
        self.assertIn('if [[ "$PROFILE" == development ]]', marker)
        self.assertNotIn('ROOT/LibreEcho-Kernel', marker)

    def test_pipeline_builds_agent_daemon_from_clean_ui_commit(self) -> None:
        """The UI bundle must not depend on a stale prebuilt agent daemon."""
        build = pipeline_text("build.sh")
        ui_bundle = '"$UI_BUILDER" "$UI_SOURCE" "$RUN/ui-bundle"'
        agent_target = 'build/libreecho-agentd | tee "$RUN/assistant-daemon-build.log"'
        self.assertIn(agent_target, build)
        self.assertLess(build.index(ui_bundle), build.index(agent_target))
        self.assertLess(build.index(agent_target), build.index('AGENT_DAEMON="$UI_SOURCE/build/libreecho-agentd"'))

    def test_pipeline_build_can_preserve_canonical_current(self) -> None:
        """A diagnostic build must emit a local candidate without republishing."""
        build = pipeline_text("build.sh")
        candidate = 'cp -- "$tmp_current" "$RUN/CURRENT.candidate"'
        publish_gate = "if ((publish_current)); then"
        canonical_move = 'mv -f -- "$tmp_current" "$OUT/CURRENT"'
        self.assertIn("--no-publish", build)
        self.assertIn(candidate, build)
        self.assertIn(publish_gate, build)
        self.assertIn(canonical_move, build)
        self.assertLess(build.index(candidate), build.index(publish_gate))
        gated = build.split(publish_gate, 1)[1].split("fi", 1)[0]
        self.assertIn(canonical_move, gated)
        self.assertIn("candidate_record=$RUN/CURRENT.candidate", build)

    def test_host_ota_reboot_waits_for_published_root_result(self) -> None:
        """Do not let the reboot supervisor race /tmp/result publication."""
        host = pipeline_file("ota.sh").read_text()
        install_run = host.rindex(
            'ADB_SERIAL="$ADB_SERIAL" "$ROOT_RUNNER" "$PIPELINE/ota-install-root.sh"'
        )
        install_tail = host[install_run:]
        result_marker = "grep -qx 'OTA_INSTALLED_NOT_REBOOTED'"
        reboot_marker = "printf 'reboot\\\\n' > /tmp/reboot.request"
        self.assertIn(result_marker, install_tail)
        self.assertIn(reboot_marker, install_tail)
        result_gate = host.index(result_marker, install_run)
        reboot_request = host.index(reboot_marker, result_gate)
        self.assertIn("REBOOT_AFTER=0", host)
        self.assertLess(install_run, result_gate)
        self.assertLess(result_gate, reboot_request)
        self.assertIn("OTA_REBOOT_REQUEST_STAGED", host)

    def test_ota_fetch_failure_cannot_become_empty_rollback_hold(self) -> None:
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn("version=$(download_and_inspect) || return 1", fetcher)
        self.assertIn(
            'if [ -n "$rolled_back" ] && [ "$version" = "$rolled_back" ] && [ "$channel" = "$rolled_back_channel" ]; then',
            fetcher,
        )
        self.assertIn("check_status_write error", fetcher)
        self.assertIn("404) die asset_missing true", fetcher)
        self.assertNotIn("state_write update-held-after-rollback", fetcher)

    def test_ota_fetch_status_preserves_bounded_sanitized_curl_diagnostics(self) -> None:
        """Host-visible OTA failures identify the curl failure without leaking paths."""
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        for expected in (
            "CURL_STDERR=/run/libreecho/ota-curl.stderr",
            "CURL_DIAGNOSTIC_MAX=160",
            '"$CURL" --fail --location --silent --show-error',
            '--stderr "$CURL_STDERR"',
            "sanitize_status_value()",
            "error_exit=",
            "error_detail=",
            "http_status=",
            "6) die download_dns false",
            "7) die download_connect false",
            "23) die download_write unknown",
            "28) die download_timeout false",
            "35|51|53|54|58|59|60|64|66|77) die download_tls false",
            "47) die download_status true",
            "63) die download_size true",
            "*) die download_transport unknown",
            "4??|5??) die download_http true",
            "*) die download_status true",
            "if ! check_status_write error",
            'echo "ERROR_DETAIL:$error_detail" >&2',
        ):
            self.assertIn(expected, fetcher)
        self.assertIn("tr '\\r\\n' '  '", fetcher)
        self.assertIn("https\\?://", fetcher)
        self.assertIn('cut -c 1-"$CURL_DIAGNOSTIC_MAX"', fetcher)
        self.assertLess(fetcher.index('--stderr "$CURL_STDERR"'), fetcher.index("curl_rc=$?"))
        self.assertLess(fetcher.index('case "$curl_rc" in'), fetcher.index('case "$http_code" in'))
        self.assertLess(fetcher.index("sanitize_status_value()"), fetcher.index("check_status_write()"))
        self.assertNotIn('echo "error=$curl_stderr"', fetcher)

    def test_ota_watcher_checks_and_requires_automatic_update_opt_in(self) -> None:
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn('"$0" check >/tmp/ota-check.log 2>&1', fetcher)
        self.assertIn("automatic_updates_enabled", fetcher)
        self.assertIn('[ "$(check_value status)" = update-available ]', fetcher)
        self.assertIn("set_automatic_updates 1", fetcher)
        self.assertIn("set_automatic_updates 0", fetcher)
        self.assertNotIn('"$0" install >/tmp/ota-fetch.log 2>&1 || true\n        fi', fetcher)

    def test_ota_source_uses_product_release_repository(self) -> None:
        expected = (
            "https://github.com/aslater3/LibreEcho/releases/latest/download/"
            "libreecho-radar-puffin-dev.ota.tar"
        )
        source = (TOOLS_DIR / "initramfs/ota-source.conf").read_text()
        self.assertIn("channel=dev", source)
        self.assertIn("libreecho-radar-puffin-dev.ota.tar", source)
        builder = (TOOLS_DIR / "build_recovery_image.py").read_text()
        self.assertIn('r"libreecho-radar-puffin-(?:dev|stable)\\.ota\\.tar"', builder)
        self.assertIn('overlay_manifest["ota-source.conf"]', builder)
        verifier = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        self.assertIn('f"channel={expected_update_channel}"', verifier)
        self.assertIn('f"libreecho-radar-puffin-{expected_update_channel}.ota.tar"', verifier)
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn(expected, source)
        self.assertIn(
            'expected_url="https://github.com/aslater3/LibreEcho/releases/latest/download/libreecho-radar-puffin-$channel.ota.tar"',
            fetcher,
        )
        self.assertNotIn("LibreEcho-Platform/releases", source + fetcher)

    def test_ota_channel_persistence_selection_and_cleanup_contract(self) -> None:
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        cleanup = (TOOLS_DIR / "initramfs/libreecho-data-cleanup").read_text()

        self.assertIn("CHANNEL_FILE=$ROOT/automatic-updates", fetcher)
        self.assertNotIn("migrate_pending_channel", fetcher)
        self.assertIn("cleanup_locks\n    trap - EXIT", fetcher)
        self.assertIn("record_channel()", fetcher)
        self.assertIn("record_channel \"$ROOT/installed\"", fetcher)
        self.assertIn("install_lock\n    seed_channel\n    validate_source\n    install_unlock", fetcher)
        automatic = fetcher[fetcher.index("set_automatic_updates()"):fetcher.index("die()")]
        self.assertIn("fetch_lock\n    install_lock", automatic)

        self.assertLess(fetcher.index("seed_channel"), fetcher.index("check_or_install()"))

        expected_url = (
            'expected_url="https://github.com/aslater3/LibreEcho/releases/latest/download/'
            'libreecho-radar-puffin-$channel.ota.tar"'
        )
        self.assertIn(expected_url, fetcher)
        self.assertIn("url=$expected_url", fetcher)
        self.assertNotIn('url=$(config_value url)', fetcher)

        self.assertIn('set-channel)', fetcher)
        self.assertIn('[ "$#" -eq 2 ] || die usage', fetcher)
        self.assertIn('set_channel "$2"', fetcher)
        self.assertIn("channel_value()", fetcher)
        self.assertIn("write_channel \"$channel\"", fetcher)
        self.assertIn(
            'check_children "$DATA_ROOT/libreecho/update" \\\n    channel incoming',
            cleanup,
        )

    def test_userdata_cleanup_preserves_persisted_ota_channel(self) -> None:
        cleanup = TOOLS_DIR / "initramfs/libreecho-data-cleanup"
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            channel = data / "libreecho/update/channel"
            channel.parent.mkdir(parents=True)
            channel.write_text("stable\\n")
            result = subprocess.run(
                ["/bin/sh", str(cleanup)],
                env={
                    **os.environ,
                    "LIBREECHO_DATA_TEST_MODE": "1",
                    "DATA_ROOT": str(data),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(channel.read_text(), "stable\\n")
            self.assertIn("DATA_CLEANUP_OK", result.stdout)

    def _run_cleanup(self, data: Path):
        return subprocess.run(
            ["/bin/sh", str(TOOLS_DIR / "initramfs/libreecho-data-cleanup")],
            env={
                **os.environ,
                "LIBREECHO_DATA_TEST_MODE": "1",
                "DATA_ROOT": str(data),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_userdata_cleanup_accepts_the_timer_schedule(self) -> None:
        """timerd's saved schedule, and the temporary it renames over it.

        The temporary is allowlisted too because a crash between write and
        rename leaves it behind, and an unrecognised file that only appears
        after a crash is the worst kind to discover on a device.
        """
        for name in ("timers", "timers.tmp"):
            with tempfile.TemporaryDirectory() as temporary:
                data = Path(temporary) / "data"
                state = data / "libreecho/config" / name
                state.parent.mkdir(parents=True)
                state.write_text("countdown 1767225600 pasta\n")
                result = self._run_cleanup(data)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn("DATA_CLEANUP_OK", output)
                # Allowlisted, not merely tolerated. A tolerated file is
                # reported on every boot, and these lines go to stderr, so
                # checking stdout alone silently asserts nothing.
                self.assertNotIn("DATA_CLEANUP_TOLERATED", output)
                self.assertNotIn("DATA_CLEANUP_UNKNOWN", output)

    def test_userdata_cleanup_accepts_the_capture_mux_bypass_flag(self) -> None:
        """micd's capture-mux bypass flag must not fail the data contract.

        The instruction for using it is "create this file", and mkdir -p is an
        easy thing to type by mistake. An unrecognised directory under config/
        is not tolerated the way an unrecognised file is: it fails the
        contract, which blocks every service on the next boot with no network
        and no UI. Both shapes have to be accepted here.
        """
        for make in (lambda p: p.write_text(""), lambda p: p.mkdir()):
            with tempfile.TemporaryDirectory() as temporary:
                data = Path(temporary) / "data"
                flag = data / "libreecho/config/bypass-capture-mux"
                flag.parent.mkdir(parents=True)
                make(flag)
                result = self._run_cleanup(data)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn("DATA_CLEANUP_OK", output)
                # Allowlisted, not merely tolerated -- a tolerated file is
                # reported on every boot, and those lines go to stderr, so
                # checking stdout alone silently asserts nothing.
                self.assertNotIn("DATA_CLEANUP_TOLERATED", output)
                self.assertNotIn("DATA_CLEANUP_UNKNOWN", output)

    def test_schema2_disabled_record_is_exact(self) -> None:
        record = {
            "id": verifier.CONNECTIVITY_BUNDLE_ID,
            "enabled": False,
            "activation": "manual-gates-only",
            "autostart": False,
            "files": {},
            "helpers": {},
            "symlinks": {},
        }
        self.assertFalse(verifier.validate_connectivity({}, {"connectivity": record}, 2))
        changed = {**record, "autostart": True}
        with self.assertRaises(SystemExit):
            verifier.validate_connectivity({}, {"connectivity": changed}, 2)

    def test_disabled_connectivity_rejects_runtime_artifacts(self) -> None:
        record = {
            "id": verifier.CONNECTIVITY_BUNDLE_ID,
            "enabled": False,
            "activation": "manual-gates-only",
            "autostart": False,
            "files": {},
            "helpers": {},
            "symlinks": {},
        }
        cases = (
            verifier.Entry(
                "etc/firmware", stat.S_IFLNK | 0o777, 0, 0, 0,
                b"../lib/firmware"
            ),
            verifier.Entry(
                "lib/firmware/WIFI_RAM_CODE", stat.S_IFREG | 0o600,
                0, 0, 0, b"vendor"
            ),
            verifier.Entry(
                "lib/firmware/WIFI_RAM_CODE_8163", stat.S_IFREG | 0o600,
                0, 0, 0, b"vendor"
            ),
        )
        for entry in cases:
            with self.subTest(name=entry.name), self.assertRaises(SystemExit):
                verifier.validate_connectivity(
                    {entry.name: entry}, {"connectivity": record}, 2
                )

    def test_boolean_manifest_schema_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            verifier.manifest_schema({"schema_version": True})


class MkimgHeaderTests(unittest.TestCase):
    """Regression: LK rejects a KERNEL header whose name lacks a NUL byte."""

    @staticmethod
    def header(name_suffix: bytes = b"\x00\x00") -> bytes:
        hdr = bytearray(512)
        hdr[0:4] = bytes.fromhex("88168858")
        hdr[4:8] = (1024).to_bytes(4, "little")
        hdr[8:14] = b"KERNEL"
        hdr[14:14 + len(name_suffix)] = name_suffix
        return bytes(hdr)

    def test_null_terminated_name_is_accepted(self) -> None:
        verifier.validate_mkimg_header(self.header(b"\x00\x00"))

    def test_ff_filled_name_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(self.header(b"\xff\xff"))

    def test_missing_null_terminator_is_rejected(self) -> None:
        hdr = bytearray(self.header(b"\x00\x00"))
        hdr[14] = 0x41  # 'A' instead of NUL
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(bytes(hdr))

    def test_bad_magic_is_rejected(self) -> None:
        hdr = bytearray(self.header())
        hdr[0:4] = b"\x00\x00\x00\x00"
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(bytes(hdr))

    def test_wrong_name_is_rejected(self) -> None:
        hdr = bytearray(self.header())
        hdr[8:14] = b"ROOTFS"
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(bytes(hdr))


if __name__ == "__main__":
    unittest.main()
