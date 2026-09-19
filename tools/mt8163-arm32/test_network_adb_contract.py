#!/usr/bin/env python3
"""Source-level contract tests for development-only open network ADB."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class NetworkAdbContractTests(unittest.TestCase):
    def test_adbd_builder_has_explicit_open_dev_mode(self) -> None:
        source = (ROOT / "adbd/build_adbd.sh").read_text()
        self.assertIn("--network-adb", source)
        self.assertIn("disabled|open-dev", source)
        self.assertIn("LIBREECHO_OPEN_NETWORK_ADB", source)
        self.assertIn("-fpermissive", source)
        self.assertIn('"authentication": authentication', source)

    def test_open_dev_patch_retains_tcp_5555_without_auth(self) -> None:
        patch = (ROOT / "adbd/libreecho-adbd.patch").read_text()
        compat = (ROOT / "adbd/compat/property_compat.c").read_text()
        self.assertIn("LIBREECHO_OPEN_NETWORK_ADB", patch)
        self.assertIn("local_init(port)", patch)
        self.assertIn("LIBREECHO_OPEN_NETWORK_ADB", compat)
        self.assertIn('result = "5555"', compat)
        self.assertIn('result = "0"', compat)

    def test_image_builder_gates_open_dev_to_dev_channel(self) -> None:
        source = (ROOT / "build_recovery_image.py").read_text()
        self.assertIn('"--network-adb"', source)
        self.assertIn('choices=("disabled", "open-dev")', source)
        self.assertIn("open network ADB is restricted to the dev channel", source)
        self.assertIn('manifest["network_adb"]', source)

    def test_verifier_checks_requested_network_adb_policy(self) -> None:
        source = (ROOT / "verify_recovery_image.py").read_text()
        self.assertIn('"--expected-network-adb"', source)
        self.assertIn("open-dev", source)
        self.assertIn("unauthenticated-root", source)

    def test_usb_launch_remains_single_process(self) -> None:
        source = (ROOT / "initramfs/libreecho-init").read_text()
        self.assertEqual(source.count("/sbin/adbd --device_banner=device"), 1)


if __name__ == "__main__":
    unittest.main()
