"""Restart to fastboot through Kaeru's one-shot misc command (Platform #195 follow-up).

On the pinned amonet v2.0.0 chain expdb holds the Kaeru payload, so the old
expdb FASTBOOT_PLEASE marker bricks the unit.  Kaeru instead reads the Android
bootloader_message command at misc offset 0, enters fastboot for
``bootonce-bootloader`` and clears it, so a plain reboot from fastboot returns
to LibreEcho.  These tests run the real ``libreecho-bootctl reboot-bootloader``
against file-backed partitions and pin the init routing.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
INIT = TOOLS / "initramfs/libreecho-init"
FIXTURE = TOOLS / "ota/test_bootctl_reboot_bootloader.c"

KAERU_HEADER = bytes.fromhex("88168858") + (0x3abc0).to_bytes(4, "little") + b"LK" + bytes(38)
BCB = bytes([0, ord("A"), ord("B"), ord("B"), 1, 0x8F, 0x8E])
COMMAND = b"bootonce-bootloader"


def run(command):
    return subprocess.run(command, capture_output=True, text=True)


class RebootBootloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.work.name) / "bootctl-reboot-bootloader"
        result = run(["cc", "-Wall", "-Wextra", "-Werror", "-o", str(cls.binary), str(FIXTURE)])
        if result.returncode:
            raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def fixture(self, root, *, layout="pinned", expdb=KAERU_HEADER, misc_command=b"",
                expdb_name="expdb", expdb_sectors=20480):
        names = {"pinned": ("boot_a", "boot_b"), "amonet": ("boot_a_x", "boot_b_x")}[layout]
        records = {7: (expdb_name, expdb_sectors), 8: ("misc", 1025), 9: ("persist", 32768),
                   10: (names[0], 32768), 11: (names[1], 32768), 16: ("userdata", 2153472)}
        if layout == "amonet":
            records.update({17: ("boot_a", 225280), 18: ("boot_b", 225280)})
        (root / "dev").mkdir()
        for index, (name, size) in records.items():
            directory = root / f"mmcblk0p{index}"
            directory.mkdir()
            (directory / "size").write_text(f"{size}\n")
            (directory / "uevent").write_text(f"DEVTYPE=partition\nPARTNAME={name}\n")
            (root / "dev" / f"mmcblk0p{index}").write_bytes(b"")
        misc = bytearray(b"\xa5" * 1024 * 4)
        misc[0:32] = misc_command.ljust(32, b"\0")
        misc[512 + 0x160:512 + 0x160 + len(BCB)] = BCB
        (root / "dev/mmcblk0p8").write_bytes(bytes(misc))
        expdb_bytes = expdb + b"\x5a" * 4096
        (root / "dev/mmcblk0p7").write_bytes(expdb_bytes)
        return bytes(misc), expdb_bytes

    def request(self, root):
        return run([str(self.binary), str(root), "reboot-bootloader"])

    def opens(self, root):
        return (root / "opens.log").read_text().splitlines()

    def test_writes_only_the_one_shot_command_and_preserves_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            misc_before, expdb_before = self.fixture(root)
            result = self.request(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "misc_command=bootonce-bootloader")
            misc_after = (root / "dev/mmcblk0p8").read_bytes()
            self.assertEqual(misc_after[0:32], COMMAND.ljust(32, b"\0"))
            # Only the 32-byte command changed: BCB, the rest of the
            # bootloader_message and everything beyond are byte-identical.
            self.assertEqual(misc_after[32:], misc_before[32:])
            self.assertEqual(misc_after[512 + 0x160:512 + 0x167], BCB)
            self.assertEqual((root / "dev/mmcblk0p7").read_bytes(), expdb_before)
            self.assertNotIn(b"FASTBOOT_PLEASE", misc_after)

    def test_expdb_is_only_ever_opened_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            self.assertEqual(self.request(root).returncode, 0)
            opens = self.opens(root)
            self.assertIn("/dev/mmcblk0p7 ro", opens)
            self.assertNotIn("/dev/mmcblk0p7 rw", opens)
            self.assertIn("/dev/mmcblk0p8 rw", opens)

    def test_repeat_request_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, misc_command=COMMAND)
            self.assertEqual(self.request(root).returncode, 0)
            self.assertEqual((root / "dev/mmcblk0p8").read_bytes()[0:32], COMMAND.ljust(32, b"\0"))

    def assert_refused_without_write(self, root, misc_before, expdb_before):
        result = self.request(root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual((root / "dev/mmcblk0p8").read_bytes(), misc_before)
        self.assertEqual((root / "dev/mmcblk0p7").read_bytes(), expdb_before)
        self.assertFalse(any(line.endswith(" rw") for line in self.opens(root)), self.opens(root))
        return result

    def test_refuses_when_kaeru_payload_is_absent(self):
        for expdb in (bytes(48), b"FASTBOOT_PLEASE\0" + bytes(32),
                      bytes.fromhex("88168858") + bytes(4) + b"XX" + bytes(38)):
            with self.subTest(expdb=expdb[:16]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                misc, exp = self.fixture(root, expdb=expdb)
                result = self.assert_refused_without_write(root, misc, exp)
                self.assertIn("Kaeru payload not proven", result.stderr)

    def test_refuses_when_expdb_identity_does_not_match(self):
        for kwargs in ({"expdb_name": "misc2"}, {"expdb_sectors": 20479}):
            with self.subTest(**kwargs), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                misc, exp = self.fixture(root, **kwargs)
                self.assert_refused_without_write(root, misc, exp)

    def test_refuses_on_the_legacy_amonet_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            misc, exp = self.fixture(root, layout="amonet")
            result = self.assert_refused_without_write(root, misc, exp)
            self.assertIn("pinned Kaeru chain", result.stderr)

    def test_never_overwrites_another_pending_bootloader_command(self):
        for command in (b"boot-recovery", b"FASTBOOT_PLEASE", b"bootonce-bootloaderX"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                misc, exp = self.fixture(root, misc_command=command)
                result = self.request(root)
                self.assertEqual(result.returncode, 1)
                self.assertEqual((root / "dev/mmcblk0p8").read_bytes(), misc)
                self.assertEqual((root / "dev/mmcblk0p7").read_bytes(), exp)


class InitRoutingTests(unittest.TestCase):
    def setUp(self):
        text = INIT.read_text()
        start = text.index("reboot_request_loop()")
        self.loop = text[start:text.index("reboot_request_loop &", start)]

    def test_fastboot_request_goes_through_bootctl_and_reboots_only_on_success(self):
        branch = self.loop[self.loop.index("fastboot)"):self.loop.index("reboot)")]
        self.assertIn("/usr/local/sbin/libreecho-bootctl reboot-bootloader", branch)
        accepted = branch.index("reboot-request-fastboot-accepted")
        self.assertLess(branch.index("libreecho-bootctl reboot-bootloader"), accepted)
        self.assertLess(accepted, branch.index("$BB reboot -f"))
        refused = branch.index("reboot-request-fastboot-refused")
        self.assertLess(branch.index("$BB reboot -f"), refused)
        self.assertEqual(branch.count("$BB reboot -f"), 1)

    def test_init_never_names_expdb_or_the_sticky_marker(self):
        self.assertNotIn("FASTBOOT_PLEASE", self.loop)
        self.assertNotIn("mmcblk0p7", self.loop)
        self.assertNotIn("dd ", self.loop)


if __name__ == "__main__":
    unittest.main()
