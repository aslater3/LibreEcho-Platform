#!/usr/bin/env python3
"""Host regression checks for the amonet v2.0.0 expdb safety contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
KERNEL = ROOT.parents[1] / "drivers/misc/mediatek/echo_fastboot_marker.c"
KCONFIG = ROOT.parents[1] / "drivers/misc/mediatek/Kconfig"
DEFCONFIG = ROOT.parents[1] / "arch/arm/configs/mt8163_arm32_defconfig"
INIT = ROOT / "initramfs/libreecho-init"
UPDATE = ROOT / "initramfs/libreecho-update"
HELPER = ROOT / "adb-reboot-fastboot.sh"
OTA_README = ROOT / "ota/README.md"


class Issue195ExpdbSafetyTests(unittest.TestCase):
    def test_kernel_marker_only_resets_misc_bcb(self) -> None:
        source = KERNEL.read_text()
        self.assertNotIn("FASTBOOT_PLEASE", source)
        self.assertNotIn("EXPDB_PATH", source)
        self.assertNotIn("mmcblk0p7", source)
        self.assertIn('#define MISC_PATH\t"/dev/mmcblk0p8"', source)
        self.assertIn("#define BCB_OFFSET\t(512 + 0x160)", source)
        self.assertIn("write_file(MISC_PATH, bcb_reset, BCB_SIZE, BCB_OFFSET)", source)

    def test_development_initramfs_never_arms_or_clears_expdb(self) -> None:
        init = INIT.read_text()
        update = UPDATE.read_text()
        self.assertNotIn("FASTBOOT_PLEASE", init)
        self.assertNotIn("$EXPDB", init)
        self.assertNotIn("printf FASTBOOT_PLEASE", init)
        # Fastboot requests now go through Kaeru's one-shot misc command
        # (test_reboot_bootloader.py); init itself still never names expdb.
        self.assertIn("libreecho-bootctl reboot-bootloader", init)
        self.assertIn("reboot-request-fastboot-refused", init)
        # Removing the marker must not remove block-node creation: userdata
        # and the A/B slots are opened through these /dev/mmcblk0pN nodes.
        self.assertIn('$BB mknod "/dev/$name" b "$major" "$minor"', init)
        self.assertLess(init.index('mknod "/dev/$name" b'), init.index("userdata_mount()"))
        self.assertNotIn("FASTBOOT_PLEASE", update)
        self.assertNotIn("EXPDB_DEVICE", update)
        self.assertNotIn("clear_exact_development_marker", update)
        self.assertNotIn("expdb-sector.after", update)

    def test_fastboot_helper_only_issues_the_device_request(self) -> None:
        helper = HELPER.read_text()
        self.assertNotIn("FASTBOOT_PLEASE", helper)
        self.assertNotIn("mmcblk0p", helper)
        self.assertNotIn("dd ", helper)
        self.assertIn("printf fastboot > /tmp/reboot.request", helper)

    def test_kernel_contract_keeps_bcb_driver_but_forbids_expdb_marker(self) -> None:
        kconfig = KCONFIG.read_text()
        defconfig = DEFCONFIG.read_text()
        self.assertIn("config LIBREECHO_DEV_RECOVERY_MARKER", kconfig)
        self.assertIn("never writes or erases expdb", kconfig)
        self.assertIn("CONFIG_LIBREECHO_DEV_RECOVERY_MARKER=y", defconfig)

    def test_ota_documentation_matches_runtime_contract(self) -> None:
        readme = OTA_README.read_text()
        self.assertIn("never write or erase `expdb`", readme)
        self.assertIn("offset `0x360`", readme)
        self.assertIn("separate expdb fastboot escape", readme)
        self.assertNotIn("may still write the", readme)


if __name__ == "__main__":
    unittest.main()
