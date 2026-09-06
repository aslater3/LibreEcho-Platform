#!/usr/bin/env python3
"""Source-order contracts for the integrated reboot-bound OTA path."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "initramfs/libreecho-update"
FETCH = ROOT / "initramfs/libreecho-update-fetch"
BOOT = ROOT / "initramfs/libreecho-init"
CLEANUP = ROOT / "initramfs/libreecho-data-cleanup"


class IntegratedOtaSourceTests(unittest.TestCase):
    def test_updater_prepares_journal_and_features_before_boot_write(self) -> None:
        source = INIT.read_text()
        prepare = source.index('"$FEATURE_TRANSACTION" preflight')
        boot_write = source.index('dd if="$STAGING/boot.img" of=')
        self.assertLess(prepare, boot_write)
        self.assertIn("feature-transaction", source)
        self.assertIn("bcb", source.lower())
        self.assertLess(source.index("feature verification"), boot_write) if "feature verification" in source else self.assertIn("feature", source[:boot_write])

    def test_updater_exposes_v2_capabilities_and_rejects_deferred_actions(self) -> None:
        source = INIT.read_text()
        self.assertIn("feature-transaction-v2", source)
        self.assertIn("runtime-capsules", source)
        self.assertIn("activation", source)
        self.assertIn("remove", source)

    def test_fetcher_stages_external_assets_directly_and_cleans_partial_files(self) -> None:
        source = FETCH.read_text()
        self.assertIn("staging/features", source)
        self.assertIn(".part", source)
        self.assertIn("feature", source)
        self.assertIn("sha256", source)

    def test_boot_and_cleanup_delegate_transaction_recovery(self) -> None:
        self.assertIn("feature-transaction", BOOT.read_text())
        cleanup = CLEANUP.read_text()
        self.assertIn("feature-commit", cleanup)
        self.assertIn("pending", cleanup)


if __name__ == "__main__":
    unittest.main()
