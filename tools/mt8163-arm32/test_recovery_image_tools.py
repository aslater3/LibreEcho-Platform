#!/usr/bin/env python3
"""Fail-closed unit tests for the MT8163 recovery image tools."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
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
        }
        return importer, data, source, firmware, environment, records

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
        self.assertTrue({"busybox", "wpa-supplicant", "musl", "tinyalsa", "libsodium", "mt8163-audio-fpga"}.issubset(component_ids))
        core_notice = (core / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn("owner's", core_notice)
        self.assertIn("`system_a`", core_notice)
        for required in (
            "GPL-2.0-only.txt", "Apache-2.0.txt", "wpa_supplicant-BSD.txt",
            "musl-1.2.5-COPYRIGHT.txt", "libsodium-1.0.18-LICENSE.txt",
        ):
            self.assertTrue((core / required).is_file(), required)

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
        self.assertIn("libreecho-adbd-compat.h", builder_text)
        self.assertTrue((adbd_dir / "compat/libreecho-adbd-compat.h").is_file())
        self.assertNotIn("stock-root", builder_text)
        self.assertIn("Apache License", notice.read_text())
        self.assertIn("source_commit=", source_lock.read_text())

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

    def test_audio_tools_are_pinned_and_gpu_input_wait_is_interruptible(self) -> None:
        tools = TOOLS_DIR / "audio-tools"
        self.assertTrue((tools / "tinyplay").is_file())
        self.assertTrue((tools / "tinycap").is_file())
        self.assertTrue((tools / "tinymix").is_file())
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

    def test_network_tools_are_pinned_and_manual_only(self) -> None:
        builder_script = TOOLS_DIR / "network-tools/build_wireless_tools.sh"
        self.assertTrue(builder_script.is_file())
        self.assertTrue(os.access(builder_script, os.X_OK))
        pipeline_build = pipeline_text("build.sh")
        pipeline_status = pipeline_text("status.sh")
        pipeline_flash = pipeline_text("flash.sh")
        self.assertIn("build_wireless_tools.sh", pipeline_build)
        self.assertIn("--iwconfig", pipeline_build)
        self.assertIn("--expected-iwconfig-sha256", pipeline_status)
        self.assertIn("--expected-iwconfig-sha256", pipeline_flash)

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
                "source": "system/vendor/firmware/ROMv2_lm_patch_1_0_hdr.bin",
                "mode": 0o644, "size": 128720,
                "sha256": "b4460117f51a43f3284594ec08d8c8861ecc0e42b17820987da03ecabdebac1e",
            },
            "ROMv2_lm_patch_1_1_hdr.bin": {
                "source": "system/vendor/firmware/ROMv2_lm_patch_1_1_hdr.bin",
                "mode": 0o644, "size": 50148,
                "sha256": "10c4ed22a10b8a136bffd7ffce4d552300d76f8e593627d2a9841c3b11a5697e",
            },
            "WIFI_RAM_CODE_8163": {
                "source": "system/vendor/firmware/WIFI_RAM_CODE_8163",
                "mode": 0o644, "size": 373840,
                "sha256": "9669cc9b03cfdc5e8fd4fd6e14c4c4050e8c196738ca4707eea12f14a6a8e64c",
            },
            "WMT_SOC.cfg": {
                "source": "system/vendor/firmware/WMT_SOC.cfg",
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
            'services="logd networkd timed audiod micd waked sttd ledd btd airplayd ttsd agentd web"',
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
        self.assertIn("log audio-startup-disabled", init_script)

    def test_streaming_voice_services_start_warm_in_dependency_order(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        service_line = (
            'services="logd networkd timed audiod micd waked sttd ledd btd '
            'airplayd ttsd agentd web"'
        )
        self.assertIn(service_line, init_script)
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
        self.assertIn("ui-web-production-loopback-fallback", source)
        self.assertIn("ui-web-production-authenticated-lan", source)
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

    def test_ota_manifest_accepts_and_validates_service_profile(self) -> None:
        updater = (TOOLS_DIR / "initramfs/libreecho-update").read_text()
        self.assertIn(
            "boot_sha256 feature_policy image_profile service_profile'",
            updater,
        )
        self.assertIn("diagnostic|production", updater)
        self.assertIn("die manifest_service_profile", updater)

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
            'if [ -n "$rolled_back" ] && [ "$version" = "$rolled_back" ]; then',
            fetcher,
        )
        self.assertIn("check_status_write error", fetcher)
        self.assertIn("404) die asset_missing true", fetcher)
        self.assertNotIn("state_write update-held-after-rollback", fetcher)

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
            "libreecho-radar-puffin-stable.ota.tar"
        )
        source = (TOOLS_DIR / "initramfs/ota-source.conf").read_text()
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn(expected, source)
        self.assertIn(expected, fetcher)
        self.assertNotIn("LibreEcho-Platform/releases", source + fetcher)

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
