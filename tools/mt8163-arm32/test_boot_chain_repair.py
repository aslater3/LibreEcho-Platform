#!/usr/bin/env python3
"""End-to-end host tests for initramfs/libreecho-boot-chain.

A fake device is built from files: sysfs nodes, file-backed partitions, a sparse
whole-disk image carrying a synthetic legacy GPT, and the real host-built
libreecho-bootctl for every GPT operation. Payloads are random bytes with
matching synthetic pins (BOOT_CHAIN_PINS), so no vendor binary is involved.
The script runs under BOOT_CHAIN_TEST=1, which a production run never sets.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from test_boot_chain_gpt import RADAR_DISK_SECTORS, RADAR_LEGACY, build_disk, names, read_side

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "initramfs/libreecho-boot-chain"
BOOTCTL_SOURCE = ROOT / "ota/libreecho_bootctl.c"
BUSYBOX = shutil.which("busybox")
MIB = 1024 * 1024
# Partition sizes in bytes (real sizes, sparse files).
PART_BYTES = {
    "lk_a": MIB, "lk_b": MIB, "tee1": 5 * MIB, "tee2": 5 * MIB,
    "expdb": 10 * MIB, "misc": 0x80200, "recovery": 16 * MIB,
}
PART_NUMBERS = {name: number for number, (name, _, _) in RADAR_LEGACY.items()}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob(seed: str, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def preloader(seed: str) -> bytes:
    data = bytearray(b"EMMC_BOOT" + blob(seed, MIB - 9))
    data[0x600:0x800] = bytes(0x200)  # reviewed images have sector 3 zero-filled
    return bytes(data)


K32 = {"lk": blob("k32-lk", 372368), "tz": blob("k32-tz", 2641920), "pl": preloader("k32-pl")}


def kaeru_blob(seed: str, size: int) -> bytes:
    return bytes.fromhex("88168858") + blob(seed, size - 4)


BOARDS = {
    "radar_puffin": {
        "zip": "amonet-radar-v1.0.0.zip", "kaeru": "radar-kaeru.bin", "chain": "kaeru-radar-v1",
        "preloader.img": preloader("r-pl"), "lk.bin": blob("r-lk", 356856),
        "tz.img": blob("r-tz", 1781760), "tee-payload.bin": blob("r-pay", 1107464),
        "radar-kaeru.bin": kaeru_blob("r-kaeru", 264544), "twrp.img": blob("r-twrp", 14424064),
    },
    "biscuit": {
        "zip": "amonet-biscuit-v2.0.0.zip", "kaeru": "biscuit-kaeru.bin", "chain": "kaeru-biscuit-v2",
        "preloader.img": preloader("b-pl"), "lk.bin": blob("b-lk", 359744),
        "tz.img": blob("b-tz", 1781760), "tee-payload.bin": blob("b-pay", 1107464),
        "biscuit-kaeru.bin": kaeru_blob("b-kaeru", 266640), "twrp.img": blob("b-twrp", 13568000),
    },
}


def zip_bytes(board: str) -> bytes:
    path = Path(tempfile.mkdtemp()) / "z.zip"
    files = BOARDS[board]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("preloader.img", "lk.bin", "tz.img", "tee-payload.bin", files["kaeru"], "twrp.img"):
            archive.writestr(f"amonet/bin/{name}", files[name])
        archive.writestr("amonet/README.md", "synthetic")
    data = path.read_bytes()
    shutil.rmtree(path.parent)
    return data


ZIPS = {board: zip_bytes(board) for board in BOARDS}


def pins_file() -> str:
    lines = [f"K32_LK_SIZE={len(K32['lk'])}", f"K32_LK_SHA={sha(K32['lk'])}",
             f"K32_TZ_SIZE={len(K32['tz'])}", f"K32_TZ_SHA={sha(K32['tz'])}",
             f"K32_PL_SHA={sha(K32['pl'])}", "board_pins()", "{", '    case "$1" in']
    for board, files in BOARDS.items():
        lines += [f"        {board})",
                  f"            ZIP={files['zip']}", f"            ZIP_SIZE={len(ZIPS[board])}",
                  f"            ZIP_SHA={sha(ZIPS[board])}", f"            KAERU={files['kaeru']}",
                  f"            PL_SHA={sha(files['preloader.img'])}"]
        for var, name in (("LK", "lk.bin"), ("TZ", "tz.img"), ("PAY", "tee-payload.bin"),
                          ("KAERU", files["kaeru"]), ("TWRP", "twrp.img")):
            lines += [f"            {var}_SIZE={len(files[name])}", f"            {var}_SHA={sha(files[name])}"]
        lines += [f"            CHAIN_NAME={files['chain']}", "            ;;"]
    lines += ["        *) return 1 ;;", "    esac", "}"]
    return "\n".join(lines) + "\n"


@unittest.skipUnless(BUSYBOX and (shutil.which("cc") or shutil.which("gcc")), "busybox and cc required")
class BootChainRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = Path(tempfile.mkdtemp(prefix="boot-chain-build."))
        cls.bootctl = cls.build / "bootctl-host"
        subprocess.run([shutil.which("cc") or shutil.which("gcc"), "-std=c99",
                        "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror", "-O2",
                        "-DLIBREECHO_BOOTCTL_HOST_TEST", "-o", str(cls.bootctl), str(BOOTCTL_SOURCE)],
                       check=True, timeout=120)
        (cls.build / "pins.sh").write_text(pins_file())

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.build, ignore_errors=True)

    # --- fake device -------------------------------------------------------
    def make_device(self, *, serial="G2A0SYNTHETIC000", lk=None, gpt_fail=False, upload=True,
                    board="radar_puffin", recovery_readonly=False) -> Path:
        root = Path(tempfile.mkdtemp(prefix="boot-chain."))
        self.addCleanup(shutil.rmtree, root, True)
        sys_block, dev, state = root / "sys", root / "dev", root / "data/boot-chain"
        for path in (sys_block, dev, state / "upload"):
            path.mkdir(parents=True)
        for name, number in PART_NUMBERS.items():
            node = sys_block / f"mmcblk0p{number}"
            node.mkdir()
            (node / "uevent").write_text(f"MAJOR=179\nMINOR={number}\nDEVNAME=mmcblk0p{number}\nDEVTYPE=partition\nPARTN={number}\nPARTNAME={name}\n")
            first, last = RADAR_LEGACY[number][1:]
            (node / "size").write_text(f"{last - first + 1}\n")
        for name, size in PART_BYTES.items():
            path = dev / f"mmcblk0p{PART_NUMBERS[name]}"
            with path.open("wb") as handle:
                handle.truncate(size)
        self.write_part(dev, "lk_a", lk if lk is not None else K32["lk"])
        self.write_part(dev, "lk_b", lk if lk is not None else K32["lk"])
        self.write_part(dev, "tee1", K32["tz"])
        self.write_part(dev, "tee2", blob("stock-tee2", 1781760))
        self.write_part(dev, "expdb", bytes(15) + blob("expdb-tail", 512))
        self.write_part(dev, "recovery", blob("stock-recovery", 9 * MIB))
        misc = bytearray(PART_BYTES["misc"])
        misc[0:32] = b"boot-recovery".ljust(32, b"\0")
        misc[0x360:0x367] = b"\x00ABB\x01" + bytes([0x0E | 0x80, 0x0F | 0x80])
        self.write_part(dev, "misc", bytes(misc))
        boot0 = bytearray(4 * MIB)
        boot0[:MIB] = K32["pl"]
        boot0[0x6C4:0x6C8] = b"17\0\0"  # this unit's bootcount
        (dev / "mmcblk0boot0").write_bytes(bytes(boot0))
        (sys_block / "mmcblk0boot0").mkdir()
        (sys_block / "mmcblk0boot0/force_ro").write_text("1\n")
        build_disk(dev / "mmcblk0", RADAR_LEGACY, RADAR_DISK_SECTORS)
        (sys_block / "mmcblk0").mkdir()
        (sys_block / "mmcblk0/size").write_text(f"{RADAR_DISK_SECTORS}\n")
        (root / "serial").write_text(serial)
        (root / "profile").write_text("ota\n")
        (root / "drop_caches").write_text("")
        (root / "mounts").write_text("")
        fake = root / "bootctl"
        fake.write_text(
            "#!/bin/sh\n"
            f'case "$1" in\n'
            f'  gpt-restore) [ "$2" = --write ] && [ -e "{root}/gpt-fail" ] && exit 1\n'
            f'     LIBREECHO_GPT_DEVICE="{dev}/mmcblk0" exec "{self.bootctl}" "$@" ;;\n'
            f'  gpt-*) LIBREECHO_GPT_DEVICE="{dev}/mmcblk0" exec "{self.bootctl}" "$@" ;;\n'
            '  status) printf "schema=1\\nselected_slot=b\\nslot_a_success=1\\nslot_b_success=1\\n" ;;\n'
            "  *) exit 2 ;;\nesac\n")
        fake.chmod(0o755)
        if gpt_fail:
            (root / "gpt-fail").write_text("")
        if upload:
            (state / "upload" / BOARDS[board]["zip"]).write_bytes(ZIPS[board])
        if recovery_readonly:
            (dev / f"mmcblk0p{PART_NUMBERS['recovery']}").chmod(0o444)
        self.root = root
        return root

    @staticmethod
    def write_part(dev: Path, name: str, data: bytes) -> None:
        with (dev / f"mmcblk0p{PART_NUMBERS[name]}").open("r+b") as handle:
            handle.write(data)

    def part(self, name: str) -> bytes:
        return (self.root / "dev" / f"mmcblk0p{PART_NUMBERS[name]}").read_bytes()

    def env(self) -> dict[str, str]:
        r = self.root
        return dict(os.environ, BOOT_CHAIN_TEST="1", BOOT_CHAIN_BB=BUSYBOX,
                    BOOT_CHAIN_BOOTCTL=str(r / "bootctl"), BOOT_CHAIN_STATE=str(r / "data/boot-chain"),
                    BOOT_CHAIN_SYS_BLOCK=str(r / "sys"), BOOT_CHAIN_DEV=str(r / "dev"),
                    BOOT_CHAIN_IDME_SERIAL=str(r / "serial"), BOOT_CHAIN_PROFILE_FILE=str(r / "profile"),
                    BOOT_CHAIN_DROP_CACHES=str(r / "drop_caches"),
                    BOOT_CHAIN_REBOOT_REQUEST=str(r / "reboot.request"),
                    BOOT_CHAIN_PROC_MOUNTS=str(r / "mounts"), BOOT_CHAIN_DATA_MOUNT=str(r),
                    BOOT_CHAIN_ASSISTANT_PAYLOAD=str(r / "missing-assistant"),
                    BOOT_CHAIN_PINS=str(self.build / "pins.sh"))

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([BUSYBOX, "sh", str(SCRIPT), *args], env=self.env(), text=True,
                              capture_output=True, timeout=120)

    def status(self) -> dict[str, str]:
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def wait_done(self) -> dict[str, str]:
        state = self.root / "data/boot-chain/repair-state"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if state.exists():
                values = dict(l.split("=", 1) for l in state.read_text().splitlines() if "=" in l)
                if values.get("result") in ("failed", "rebooting"):
                    lock = self.root / "data/boot-chain/repair.lock"
                    for _ in range(50):
                        if not lock.exists():
                            break
                        time.sleep(0.1)
                    return values
            time.sleep(0.2)
        self.fail("repair did not finish: " + (self.root / "data/boot-chain/repair.log").read_text())

    def snapshot(self) -> dict[str, str]:
        dev = self.root / "dev"
        return {path.name: sha(path.read_bytes()) for path in sorted(dev.iterdir())}

    # --- tests -------------------------------------------------------------
    def test_status_on_legacy_radar_offers_repair(self) -> None:
        self.make_device()
        values = self.status()
        self.assertEqual(values["layout"], "legacy-amonet")
        self.assertEqual(values["chain"], "legacy-0.13")
        self.assertEqual(values["board"], "radar_puffin")
        self.assertEqual(values["board_confidence"], "confirmed")
        self.assertEqual(values["expdb"], "empty")
        self.assertEqual(values["repair"], "available", values)
        self.assertEqual(values["package"], "amonet-radar-v1.0.0.zip")
        self.assertNotIn("SYNTHETIC", (self.root / "data/boot-chain/status").read_text())

    def test_repair_moves_radar_onto_kaeru_and_requests_reboot(self) -> None:
        self.make_device()
        boot0_before = (self.root / "dev/mmcblk0boot0").read_bytes()
        misc_before = self.part("misc")
        result = self.run_script("repair-start", "--board", "radar_puffin")
        self.assertEqual(result.stdout.strip(), "started=1", result.stdout + result.stderr)
        state = self.wait_done()
        self.assertEqual(state["result"], "rebooting", (self.root / "data/boot-chain/repair.log").read_text())
        files = BOARDS["radar_puffin"]
        self.assertEqual(self.part("lk_a")[:len(files["lk.bin"])], files["lk.bin"])
        self.assertEqual(self.part("lk_b")[:len(files["lk.bin"])], files["lk.bin"])
        self.assertEqual(self.part("tee1")[:len(files["tee-payload.bin"])], files["tee-payload.bin"])
        self.assertEqual(self.part("tee2")[:len(files["tz.img"])], files["tz.img"])
        self.assertEqual(self.part("expdb")[:len(files["radar-kaeru.bin"])], files["radar-kaeru.bin"])
        self.assertEqual(self.part("recovery")[:len(files["twrp.img"])], files["twrp.img"])
        boot0 = (self.root / "dev/mmcblk0boot0").read_bytes()
        expected = bytearray(files["preloader.img"])
        expected[0x600:0x800] = boot0_before[0x600:0x800]  # bootcount sector kept
        self.assertEqual(boot0[:MIB], bytes(expected))
        self.assertEqual(boot0[MIB:], boot0_before[MIB:])
        misc = self.part("misc")
        self.assertEqual(misc[:0x340], bytes(0x340))
        self.assertEqual(misc[0x340:], misc_before[0x340:])
        table = names(read_side(self.root / "dev/mmcblk0", 1)[1])
        self.assertEqual(table[10][0], "boot_a")
        self.assertEqual(table[11][0], "boot_b")
        self.assertNotIn(17, table)
        self.assertEqual(table[16], RADAR_LEGACY[16])
        self.assertEqual((self.root / "reboot.request").read_text(), "reboot\n")
        backup = self.root / "data/boot-chain/backup"
        check = subprocess.run([BUSYBOX, "sha256sum", "-c", "SHA256SUMS"], cwd=backup,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertEqual((backup / "lk_a.img").read_bytes()[:len(K32["lk"])], K32["lk"])
        self.assertFalse((self.root / "data/boot-chain/payload").exists())

    def test_board_mismatch_refuses_without_writes(self) -> None:
        self.make_device()
        before = self.snapshot()
        result = self.run_script("repair-start", "--board", "biscuit")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR:board_mismatch", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_serial_family_blocks_repair(self) -> None:
        self.make_device(serial="X999SYNTHETIC000")
        values = self.status()
        self.assertEqual(values["board"], "unknown")
        self.assertEqual(values["repair"], "blocked")
        self.assertEqual(values["reason"], "board_unconfirmed")
        before = self.snapshot()
        self.assertIn("ERROR:board_unconfirmed", self.run_script("repair-start", "--board", "radar_puffin").stdout)
        self.assertEqual(self.snapshot(), before)

    def test_radar_serial_on_biscuit_sized_disk_is_unconfirmed(self) -> None:
        self.make_device()
        (self.root / "dev/mmcblk0").unlink()
        layout = dict(RADAR_LEGACY)
        layout[17] = ("boot_a", 0x6DDC00, 0x714BFF)
        layout[18] = ("boot_b", 0x714C00, 0x74BBFF)
        build_disk(self.root / "dev/mmcblk0", layout, 0x74C000)
        values = self.status()
        self.assertEqual(values["board"], "unknown")
        self.assertEqual(values["reason"], "board_unconfirmed")

    def test_unrecognised_bootloader_blocks_repair(self) -> None:
        self.make_device(lk=blob("someone-elses-lk", 372368))
        values = self.status()
        self.assertEqual(values["chain"], "unknown")
        self.assertEqual(values["reason"], "bootloader_unrecognised")

    def test_failure_in_critical_window_restores_every_region(self) -> None:
        self.make_device(gpt_fail=True)
        before = self.snapshot()
        self.assertEqual(self.run_script("repair-start", "--board", "radar_puffin").stdout.strip(), "started=1")
        state = self.wait_done()
        self.assertEqual(state["result"], "failed")
        self.assertEqual(state["reason"], "write_failed_restored",
                         (self.root / "data/boot-chain/repair.log").read_text())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "reboot.request").exists())
        # The unit is back on its legacy chain and the repair can be offered again.
        values = self.status()
        self.assertEqual(values["chain"], "legacy-0.13")
        self.assertEqual(values["repair"], "failed")

    def test_unwritable_emmc_aborts_before_critical_writes(self) -> None:
        self.make_device(recovery_readonly=True)
        before = self.snapshot()
        self.run_script("repair-start", "--board", "radar_puffin")
        state = self.wait_done()
        self.assertEqual(state["reason"], "emmc_readonly")
        self.assertEqual(self.snapshot(), before)

    def test_missing_package_without_client_fails_cleanly(self) -> None:
        self.make_device(upload=False)
        before = self.snapshot()
        self.run_script("repair-start", "--board", "radar_puffin")
        state = self.wait_done()
        self.assertEqual(state["reason"], "download_client_missing")
        self.assertEqual(self.snapshot(), before)

    def test_tampered_package_is_rejected(self) -> None:
        self.make_device()
        upload = self.root / "data/boot-chain/upload/amonet-radar-v1.0.0.zip"
        data = bytearray(upload.read_bytes())
        data[-30] ^= 0xFF
        upload.write_bytes(bytes(data))
        before = self.snapshot()
        self.run_script("repair-start", "--board", "radar_puffin")
        state = self.wait_done()
        self.assertIn(state["reason"], ("download_client_missing", "package_mismatch"))
        self.assertEqual(self.snapshot(), before)

    def test_biscuit_repair_uses_biscuit_package(self) -> None:
        self.make_device(serial="G090SYNTHETIC000", board="biscuit")
        self.assertEqual(self.status()["package"], "amonet-biscuit-v2.0.0.zip")
        self.assertEqual(self.run_script("repair-start", "--board", "biscuit").stdout.strip(), "started=1")
        self.assertEqual(self.wait_done()["result"], "rebooting")
        files = BOARDS["biscuit"]
        self.assertEqual(self.part("expdb")[:len(files["biscuit-kaeru.bin"])], files["biscuit-kaeru.bin"])

    def test_repaired_unit_reports_done_and_refuses_second_repair(self) -> None:
        self.make_device()
        self.run_script("repair-start", "--board", "radar_puffin")
        self.wait_done()
        # After reboot the kernel sees the restored table: rename the sysfs nodes.
        sys_block = self.root / "sys"
        (sys_block / "mmcblk0p10/uevent").write_text("PARTNAME=boot_a\n")
        (sys_block / "mmcblk0p11/uevent").write_text("PARTNAME=boot_b\n")
        shutil.rmtree(sys_block / "mmcblk0p17")
        shutil.rmtree(sys_block / "mmcblk0p18")
        values = self.status()
        self.assertEqual(values["layout"], "stock")
        self.assertEqual(values["chain"], "kaeru-radar-v1")
        self.assertEqual(values["recovery"], "twrp-radar")
        self.assertEqual(values["expdb"], "kaeru")
        self.assertEqual(values["repair"], "done")
        self.assertIn("ERROR:repaired", self.run_script("repair-start", "--board", "radar_puffin").stdout)

    def test_production_ignores_test_overrides(self) -> None:
        self.make_device()
        env = self.env()
        env.pop("BOOT_CHAIN_TEST")
        result = subprocess.run([BUSYBOX, "sh", "-c", f"grep -n 'BOOT_CHAIN_' {SCRIPT} | grep -v 'BOOT_CHAIN_TEST\\|^[0-9]*:    [A-Z_]*=\\${{BOOT_CHAIN_\\|BOOT_CHAIN_PINS'"],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.stdout, "", "BOOT_CHAIN_* read outside the test-mode block")


if __name__ == "__main__":
    unittest.main()
