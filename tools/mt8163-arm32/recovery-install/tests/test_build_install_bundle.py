#!/usr/bin/env python3
"""Host-side tests for the initial-install bundle builder.

No device and no TWRP: these prove the bundle is well-formed, manifest-driven,
reproducible and fails closed on bad input. Whether the installer *works* is a
hardware question and is answered on a device, not here.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import build_install_bundle as builder  # noqa: E402

SRC = HERE.parent / "src"


class BundleBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="le-install-test-"))
        self.assets = self.work / "assets"
        self.assets.mkdir()
        self.out = self.work / "out"
        self._make_assets(self.assets)

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    @staticmethod
    def _asset(path: Path) -> dict:
        return {"name": path.name, "sha256": builder.sha256_file(path),
                "size": path.stat().st_size}

    def _make_assets(self, directory: Path, release: str = "0.14.0-test") -> None:
        boot = directory / "libreecho-radar-puffin-0.14.0-boot.img"
        boot.write_bytes(b"\0" * (builder.BOOT_SLOT_SECTORS * 512))
        payload = directory / "libreecho-radar-puffin-0.14.0-tts.squashfs"
        payload.write_bytes(b"tts-payload-bytes")
        manifest = directory / "libreecho-radar-puffin-0.14.0-tts.manifest.json"
        manifest.write_bytes(b'{"feature":"tts"}')
        key = directory / "libreecho-radar-puffin-0.14.0-ota-public-key.hex"
        key.write_bytes(b"a" * 64)
        # The signed manifest and its detached signature, as the OTA bundle ships
        # them. boot_sha256 must match the boot image actually being shipped.
        (directory / "manifest").write_text(
            "version=0.14.0\n"
            f"boot_sha256={builder.sha256_file(boot)}\n"
            "feature_ids=tts\n")
        (directory / "manifest.sig").write_bytes(b"signature")
        install = {
            "schema": builder.SCHEMA,
            "release": release,
            "board": "radar_puffin",
            "soc": "mt8163",
            "image_profile": "ota",
            "service_profile": "production",
            "boot": self._asset(boot),
            "ota_public_key": self._asset(key),
            "features": [{"name": "tts",
                          "payload": self._asset(payload),
                          "manifest": self._asset(manifest)}],
            "amonet": {"repository": "https://example.invalid/amonet",
                       "tag": "0" * 40, "commit": "0" * 40},
        }
        (directory / builder.INSTALL_MANIFEST_NAME).write_text(json.dumps(install))

    def build(self, assets: Path | None = None, out: Path | None = None, release: str = "") -> dict:
        return builder.assemble(assets or self.assets, out or self.out, SRC, release, 2153472)

    # --- happy path --------------------------------------------------------
    def test_bundle_is_assembled_and_self_checks(self) -> None:
        summary = self.build()
        self.assertTrue((self.out / builder.ZIP_NAME).is_file())
        self.assertTrue((self.out / builder.MANIFEST_NAME).is_file())
        self.assertEqual(summary["release"], "0.14.0-test")
        self.assertEqual(summary["features"], ["tts"])
        self.assertIn(builder.INSTALL_MANIFEST_NAME, summary["payload_files"])

    def test_manifest_pins_every_asset_and_drives_staging(self) -> None:
        self.build()
        text = (self.out / builder.MANIFEST_NAME).read_text()
        declared: dict[str, str] = {}
        for line in text.splitlines():
            if line.startswith(("payload=", "staging=", "install_manifest=")):
                for name, digest in builder._declared_assets(line):
                    declared[name] = digest
        self.assertTrue(declared, text)
        for name, digest in declared.items():
            self.assertEqual(builder.sha256_file(self.out / name), digest, name)
        self.assertIn("device=radar_puffin", text)
        self.assertIn("image_profile=ota", text)
        self.assertIn("userdata_sectors=2153472", text)
        staging = [l for l in text.splitlines() if l.startswith("staging=")]
        self.assertEqual(len(staging), 1)
        # <feature>:<payload>:<sha>:<manifest>:<sha> - the device needs the name
        # to know which feature directory the pair belongs in.
        self.assertEqual(staging[0].split("=", 1)[1].split(":")[0], "tts")

    def test_zip_has_the_recovery_entry_points(self) -> None:
        self.build()
        with zipfile.ZipFile(self.out / builder.ZIP_NAME) as archive:
            names = set(archive.namelist())
        self.assertIn("META-INF/com/google/android/update-binary", names)
        self.assertIn("META-INF/com/google/android/updater-script", names)

    def test_the_installer_is_self_contained(self) -> None:
        """TWRP's unzip is toybox's: it ignored the member pattern and destination
        and extracted nothing, while still exiting 0. An installer that unpacked
        its own scripts therefore failed before running a single check, so the
        entry point must carry every phase and the zip nothing to unpack."""
        self.build()
        with zipfile.ZipFile(self.out / builder.ZIP_NAME) as archive:
            body = archive.read("META-INF/com/google/android/update-binary").decode()
            names = archive.namelist()
        self.assertTrue(body.startswith("#!/sbin/sh"))
        for phase in ("check_device()", "check_payloads()", "resize_userdata()",
                      "format_userdata()", "write_boot_slots()", "stage_features()"):
            self.assertIn(phase, body, phase)
        # the manifest-driven staging: the transaction's own input, not a live-tree copy
        self.assertIn("$BUNDLE_DIR/manifest", body)
        self.assertIn("lookup_digest", body)
        self.assertEqual(sorted(names), [
            "META-INF/com/google/android/update-binary",
            "META-INF/com/google/android/updater-script",
        ])

    def test_update_binary_carries_the_execute_bit(self) -> None:
        self.build()
        with zipfile.ZipFile(self.out / builder.ZIP_NAME) as archive:
            info = archive.getinfo("META-INF/com/google/android/update-binary")
        self.assertTrue(info.external_attr >> 16 & 0o111, "TWRP requires it executable")

    # --- reproducibility ---------------------------------------------------
    def test_the_same_inputs_give_a_byte_identical_zip(self) -> None:
        first = self.build(out=self.work / "out-a")
        second = self.build(out=self.work / "out-b")
        self.assertEqual(first["zip_sha256"], second["zip_sha256"])

    def test_a_changed_payload_changes_the_bundle_manifest(self) -> None:
        self.build()
        before = (self.out / builder.MANIFEST_NAME).read_text()
        (self.assets / "libreecho-radar-puffin-0.14.0-tts.squashfs").write_bytes(b"different")
        with self.assertRaises(builder.BuildError):
            # the install manifest still describes the old bytes: refuse
            self.build()
        self.assertTrue(before)

    # --- manifest-driven, fails closed -------------------------------------
    def test_a_manifest_that_does_not_describe_the_files_is_refused(self) -> None:
        (self.assets / "libreecho-radar-puffin-0.14.0-tts.squashfs").write_bytes(b"tampered")
        with self.assertRaises(builder.BuildError) as raised:
            self.build()
        self.assertIn("tts.squashfs", str(raised.exception))

    def test_a_missing_install_manifest_is_refused(self) -> None:
        (self.assets / builder.INSTALL_MANIFEST_NAME).unlink()
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_an_unknown_manifest_key_is_refused(self) -> None:
        path = self.assets / builder.INSTALL_MANIFEST_NAME
        data = json.loads(path.read_text())
        data["surprise"] = True
        path.write_text(json.dumps(data))
        with self.assertRaises(builder.BuildError) as raised:
            self.build()
        self.assertIn("keys differ", str(raised.exception))

    def test_a_feature_absent_from_the_assets_is_refused(self) -> None:
        (self.assets / "libreecho-radar-puffin-0.14.0-tts.manifest.json").unlink()
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_an_unsafe_feature_name_is_refused(self) -> None:
        path = self.assets / builder.INSTALL_MANIFEST_NAME
        data = json.loads(path.read_text())
        data["features"][0]["name"] = "../../etc"
        path.write_text(json.dumps(data))
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_a_wrong_sized_boot_image_is_refused(self) -> None:
        (self.assets / "libreecho-radar-puffin-0.14.0-boot.img").write_bytes(b"\0" * 4096)
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_a_release_that_disagrees_with_the_manifest_is_refused(self) -> None:
        with self.assertRaises(builder.BuildError) as raised:
            self.build(release="0.14.0-something-else")
        self.assertIn("does not match", str(raised.exception))

    def test_a_matching_release_argument_is_accepted(self) -> None:
        self.build(release="0.14.0-test")

    # --- bundle verification ------------------------------------------------
    def test_check_bundle_catches_a_tampered_payload(self) -> None:
        self.build()
        (self.out / "libreecho-radar-puffin-0.14.0-tts.squashfs").write_bytes(b"tampered")
        problems = builder.check_bundle(self.out)
        self.assertTrue(any("digest mismatch" in p for p in problems), problems)

    def test_check_bundle_catches_an_undeclared_file(self) -> None:
        self.build()
        (self.out / "extra-payload.squashfs").write_bytes(b"undeclared")
        problems = builder.check_bundle(self.out)
        self.assertTrue(any("not declared" in p for p in problems), problems)

    def test_check_bundle_catches_a_missing_payload(self) -> None:
        self.build()
        (self.out / "libreecho-radar-puffin-0.14.0-tts.manifest.json").unlink()
        problems = builder.check_bundle(self.out)
        self.assertTrue(any("absent" in p for p in problems), problems)

    # --- tar input ---------------------------------------------------------
    def test_assets_are_read_from_an_initial_install_tar(self) -> None:
        staging = self.work / "release"
        staging.mkdir()
        with tarfile.open(staging / "libreecho-radar-puffin-0.14.0-initial-install.tar", "w") as tar:
            for path in sorted(self.assets.iterdir()):
                tar.add(path, arcname=path.name)
        out = self.work / "out-tar"
        self.build(assets=staging, out=out)
        self.assertTrue((out / builder.ZIP_NAME).is_file())

    def test_a_tar_with_a_traversal_path_is_refused(self) -> None:
        staging = self.work / "evil"
        staging.mkdir()
        with tarfile.open(staging / "evil-initial-install.tar", "w") as tar:
            info = tarfile.TarInfo("../escape.img")
            payload = b"x"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with self.assertRaises(builder.BuildError):
            self.build(assets=staging, out=self.work / "out-evil")


if __name__ == "__main__":
    unittest.main(verbosity=2)
