#!/usr/bin/env python3
"""Command-compatible host adapters for the LibreEcho initial-install VM."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VM = HERE / "initial_install_vm.py"
import importlib.util
spec = importlib.util.spec_from_file_location("initial_install_vm", VM)
assert spec and spec.loader
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)

ROOT = Path(os.environ["LIBREECHO_EMULATOR_ROOT"])
IMAGE = ROOT / "emmc.img"
STATE = ROOT / "transaction.json"
SERIAL = "MOCK-BISCUIT"

def load_state():
    return json.loads(STATE.read_text())

def save_state(state):
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

def brom():
    boot = Path(os.environ["LIBREECHO_EMULATOR_BOOT"])
    vm.create_physical(IMAGE)
    vm.brom_install(IMAGE, STATE)
    print("Found port = /dev/mock-brom")
    print("all good")
    for line in ("Check GPT", "Inject payload", "Force fastboot", "Reboot to unlocked fastboot"):
        print(line)
    (ROOT / "uart.log").write_text("\n".join([
        "[0000] [GPT_LK]Success to find valid GPT.",
        "found lk_a at 0x00008000", "found lk_b at 0x00010000",
        "found expdb at 0x00018000", "found misc at 0x0001D000",
        "found boot_a_x at 0x00028000", "found boot_b_x at 0x00030000",
        "found recovery at 0x00038000", "found boot_a at 0x006D9C00",
        "found boot_b at 0x00710C00", "Read msg from misc: ",
        "Read msg from expdb: ", "Inject payload into boot_a/boot_b",
        "Read msg from expdb: FASTBOOT_PLEASE", "[10630] fastboot_init()",
        "[11000] fastboot: processing commands: fastboot_mode=2",
    ]) + "\n", encoding="ascii")

def fastboot(argv):
    args = [a for a in argv if a != "-s"]
    if "-s" in argv:
        i = argv.index("-s")
        args = argv[:i] + argv[i + 2:]
    if args == ["devices"]:
        print(f"{SERIAL}\tfastboot")
        return
    if args[:2] == ["getvar", "product"]:
        print("product: BISCUIT", file=sys.stderr); return
    if args[:2] == ["getvar", "partition-size:boot_a_x"] or args[:2] == ["getvar", "partition-size:boot_b_x"]:
        print("partition-size:" + args[1].split(":", 1)[1] + ": 0x01000000", file=sys.stderr); return
    if len(args) == 3 and args[0] == "flash":
        slot = args[1].removeprefix("boot_")
        vm.fastboot_flash(IMAGE, Path(args[2]), slot, STATE)
        print("OKAY")
        return
    if args[:2] == ["erase", "expdb"]:
        state = load_state(); state["expdb_erased"] = True; save_state(state); print("OKAY"); return
    if args == ["reboot"]:
        state = load_state(); state["fastboot_rebooted"] = True; save_state(state); print("rebooting"); return
    raise SystemExit(f"unsupported mock fastboot command: {argv!r}")

def adb(argv):
    if "get-state" in argv:
        if not load_state().get("qemu_boot_checked"):
            kernel = Path(os.environ["LIBREECHO_EMULATOR_KERNEL"])
            initramfs = Path(os.environ["LIBREECHO_EMULATOR_INITRAMFS"])
            vm.run_qemu_boot(IMAGE, initramfs, kernel, ROOT, vm.sha256(Path(os.environ["LIBREECHO_EMULATOR_BOOT"])))
            state = load_state(); state["qemu_boot_checked"] = True; save_state(state)
        print("device"); return
    if argv and argv[-1] == "devices":
        print("List of devices attached\n" + SERIAL + "\tdevice"); return
    if "forward" in argv:
        print("18080"); return
    if "push" in argv:
        if len(argv) >= 2 and argv[-1].endswith("libreecho-feature-stage.conf"):
            config = Path(argv[-2])
            state = load_state()
            state["last_feature"] = config.read_text().split("FEATURE_ID=", 1)[1].splitlines()[0]
            state["last_feature_sha"] = config.read_text().split("PAYLOAD_SHA256=", 1)[1].splitlines()[0]
            save_state(state)
        print("1 file pushed"); return
    if "shell" in argv:
        command = " ".join(argv[argv.index("shell") + 1:])
        state = load_state()
        if "cat /sys/class/block/mmcblk0p10/uevent" in command:
            print("PARTNAME=boot_a_x\\nPARTN=10"); return
        if "cat /sys/class/block/mmcblk0p11/uevent" in command:
            print("PARTNAME=boot_b_x\\nPARTN=11"); return
        if "FEATURE_STAGE" in command or "stage-feature-root.sh" in command:
            print("FEATURE_STAGE_OK:" + state.get("last_feature", "unknown")); return
        if "sha256sum /data/libreecho/features/" in command:
            print(state.get("last_feature_sha", "" ) + "  /data/libreecho/features/payload.squashfs"); return
        if "sha256sum /dev/mmcblk0p10" in command:
            print(f"{vm.sha256(Path(os.environ['LIBREECHO_EMULATOR_BOOT']))}  /dev/mmcblk0p10"); return
        if "sha256sum /dev/mmcblk0p11" in command:
            print(f"{vm.sha256(Path(os.environ['LIBREECHO_EMULATOR_BOOT']))}  /dev/mmcblk0p11"); return
        print("OK"); return
    raise SystemExit(f"unsupported mock adb command: {argv!r}")

def main():
    tool = os.environ.get("LIBREECHO_EMULATOR_TOOL", "") or Path(sys.argv[0]).name
    if tool in {"mock-brom", "brom", "emulator_tool.py"} or (len(sys.argv) == 1 and tool.endswith("emulator_tool.py")):
        brom(); return
    if tool in {"mock-fastboot", "fastboot"} or "fastboot" in tool:
        fastboot(sys.argv[1:]); return
    adb(sys.argv[1:])

if __name__ == "__main__":
    main()
