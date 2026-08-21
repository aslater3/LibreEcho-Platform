#!/usr/bin/env python3
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("initial_install_vm", HERE / "initial_install_vm.py")
assert SPEC and SPEC.loader
VM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VM)


class InitialInstallVmTests(unittest.TestCase):
    def test_brom_gpt_rewrite_preserves_existing_device_contents(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "emmc.img"
            state = root / "transaction.json"
            VM.create_physical(image)
            gpt = VM.read_gpt(image)
            sentinel = b"PERSISTENT-DEVICE-STATE"
            with image.open("r+b") as stream:
                stream.seek(gpt[16][0] * VM.SECTOR + 4096)
                stream.write(sentinel)
            VM.brom_install(image, state)
            with image.open("rb") as stream:
                stream.seek(gpt[16][0] * VM.SECTOR + 4096)
                self.assertEqual(stream.read(len(sentinel)), sentinel)

    def test_default_boot_uses_canonical_android_v0_geometry(self):
        with tempfile.TemporaryDirectory() as td:
            boot = Path(td) / "boot.img"
            VM.make_boot(boot)
            header = boot.read_bytes()[:64]
            kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, _, second_addr, tags_addr, page_size, _, _ = struct.unpack("<10I", header[8:48])
            self.assertEqual(boot.stat().st_size, VM.BOOT_BYTES)
            self.assertEqual(kernel_size, 0)
            self.assertEqual(kernel_addr, 0x40008000)
            self.assertEqual(ramdisk_size, 0)
            self.assertEqual(ramdisk_addr, 0x43478000)
            self.assertEqual(second_addr, 0x40F00000)
            self.assertEqual(tags_addr, 0x48000000)
            self.assertEqual(page_size, 0x800)

    def test_physical_gpt_transforms_and_both_payloads_read_back(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "emmc.img"
            boot = root / "boot.img"
            state = root / "transaction.json"
            VM.make_boot(boot)
            VM.create_physical(image)
            physical = VM.read_gpt(image)
            self.assertEqual(physical[10][2], "boot_a")
            self.assertEqual(physical[11][2], "boot_b")
            VM.brom_install(image, state)
            logical = VM.read_gpt(image)
            self.assertEqual(logical[10][2], "boot_a_x")
            self.assertEqual(logical[11][2], "boot_b_x")
            self.assertEqual(logical[17][2], "boot_a")
            self.assertEqual(logical[18][2], "boot_b")
            self.assertEqual(logical[16][1], 0x209C00)
            VM.fastboot_flash(image, boot, "a", state)
            VM.fastboot_flash(image, boot, "b", state)
            result = VM.verify(image, boot, state)
            self.assertEqual(result["payload_readback"], "PASS")
            self.assertEqual(result["sha256"], VM.sha256(boot))

    def test_fastboot_rejects_non_reviewed_image_geometry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "emmc.img"
            state = root / "transaction.json"
            bad_boot = root / "bad.img"
            bad_boot.write_bytes(b"too small")
            VM.create_physical(image)
            VM.brom_install(image, state)
            with self.assertRaisesRegex(RuntimeError, "exactly 16 MiB"):
                VM.fastboot_flash(image, bad_boot, "a", state)

    def test_state_records_real_device_derived_redirect_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "emmc.img"
            state = root / "transaction.json"
            VM.create_physical(image)
            VM.brom_install(image, state)
            record = state.read_text()
            self.assertIn('"boot_a": "boot_a_x"', record)
            self.assertIn('"boot_b": "boot_b_x"', record)
            self.assertIn("/dev/mock-brom", record)


if __name__ == "__main__":
    unittest.main(verbosity=2)
