#!/usr/bin/env python3
"""Build the pinned MT8163 EVT Wi-Fi/audio candidate DTB.

The output retains the stock resource tree and adds the CONSYS bus clock,
enables the MT8163 image-clock provider, adds the small codec-supply contract
required by the 6.1 audio drivers, exposes the legacy watchdog through the
6.1 generic Mediatek watchdog binding, and restores the named MUSB IRQ and
peripheral role needed by the 6.1 gadget path.  The complete output hash pins
that promise.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn


ANDROID_MAGIC = b"ANDROID!"
MKIMG_MAGIC = bytes.fromhex("88168858")
FDT_MAGIC = 0xD00DFEED

SOURCE_BOOT_SIZE = 0x1000000
SOURCE_BOOT_SHA256 = "c0f52a3b079d214495cd3dd22f92fd85695d1b868c58b491a2edb933bc4f6d1a"
PAGE_SIZE = 0x800
MKIMG_SIZE = 0x200
EVT_PAYLOAD_OFFSET = 0x585185
EVT_RAW_SIZE = 0xC875
STOCK_EVT_SHA256 = "f44630ba28f503dd7503bc7cffa2ee96a319acf2f58f1456bb6f5ff23d57dee1"

CONSYS_NODE = "/soc/consys@18070000"
INFRACFG_NODE = "/soc/infracfg@10001000"
PWRAP_NODE = "/soc/pwrap@1000d000"
MT6323_NODE = "/soc/pwrap@1000d000/mt6323"
MT6323_KEYS_NODE = MT6323_NODE + "/mt6323keys"
MT6323_POWER_KEY_NODE = MT6323_KEYS_NODE + "/power"
MT6323_HOME_KEY_NODE = MT6323_KEYS_NODE + "/home"
ACTION_KEY_NODE = "/action_key"
ACTION_BUTTON_NODE = ACTION_KEY_NODE + "/action@36"
PIO_NODE = "/soc/pinctrl@10005000"
ACTION_GPIO = 0x24
ACTION_GPIO_FLAGS = 0x01
ACTION_KEYCODE = 0x8A
MUTE_KEYCODE = (0x71,)
CODEC_NODE = "/soc/i2c@11009000/tlv320aic32x4@18"
CODEC_MCLK_NODE = "/clocks/puffin_codec_mclk"
CODEC_MCLK_PHANDLE = 0x49
CODEC_MCLK_RATE = 0x927c00
VIO18_NODE = "/soc/pwrap@1000d000/mt6323/regulators/ldo_vio18"
IMGSYS_NODE = "/soc/imgsys@15000000"
PINCTRL_NODE = "/soc/pinctrl@10005000"
SPI_CONTROLLER_NODE = "/soc/spi@1100a000"
SPI_AUDIO_NODE = SPI_CONTROLLER_NODE + "/spi@0"
AFE_NODE = "/soc/mt_soc_dl1_pcm@11220000"
AUDIOSYS_NODE = "/soc/audiosys@11220000"
AFE_PMIC_PHANDLE = 0x48
AUDIOSYS_PHANDLE = 0x4E
AUDIOSYS_AUDIO_24M_ID = 0x03
SCPSYS_NODE = "/soc/scpsys@10006000"
AFE_POWER_DOMAIN_ID = 0x05
PIO_PHANDLE = 0x09
# Radar-Puffin's stock audio DT controls the external amplifier on GPIO122.
# Keep this board-specific state; GPIO28/GPIO29 belong to connectivity.
RADAR_EXTAMP_PINMUX = 0x7A00
# The stock EVT DTB's DAC-mux contract is two named pinctrl states on GPIO124.
# Its legacy phandles 0x2f/0x30 collide with unrelated stock nodes (iddig_irq
# and scpsys), so retain the states but allocate unique 6.1 phandles.
RADAR_DACMUX_PINMUX = 0x7C00
RADAR_DACMUX_HIGH_PHANDLE = 0x4F
RADAR_DACMUX_LOW_PHANDLE = 0x50
# The stock EVT DTB contains two physically different I2S1 groups.  The FPGA
# bootstrap path is known-good on pins 72/73/74, while the populated speaker
# path is the 3.18 oracle's pins 46/47/48.  These states must not be shared:
# the SPI child initializes the FPGA before the AFE claims the speaker pins.
RADAR_FPGA_I2S_IDLE_AUDIO_PINMUX = (0x4800, 0x4900, 0x4A00)
RADAR_FPGA_I2S_ACTIVE_AUDIO_PINMUX = (0x4804, 0x4904, 0x4A04)
RADAR_SPEAKER_I2S_IDLE_AUDIO_PINMUX = (0x2E00, 0x2F00, 0x3000)
RADAR_SPEAKER_I2S_ACTIVE_AUDIO_PINMUX = (0x2E02, 0x2F02, 0x3002)
RADAR_SPEAKER_PMIC_IDLE_AUDIO_PINMUX = (
    0x0700, 0x0800, 0x0900, 0x2E00, 0x2F00, 0x3000, 0x7700,
)
RADAR_SPEAKER_PMIC_ACTIVE_AUDIO_PINMUX = (
    0x0701, 0x0801, 0x0901, 0x2E02, 0x2F02, 0x3002, 0x7701,
)
RADAR_SPEAKER_I2S_IDLE_PHANDLE = 0x4A
RADAR_SPEAKER_I2S_ACTIVE_PHANDLE = 0x4B
RADAR_SPEAKER_PMIC_IDLE_PHANDLE = 0x4C
RADAR_SPEAKER_PMIC_ACTIVE_PHANDLE = 0x4D
TOPRGU_NODE = "/soc/toprgu@10007000"
TOPCKGEN_NODE = "/soc/topckgen@10000000"
USB_NODE = "/soc/usb@11200000"
STOCK_CONSYS_REG = (
    0x0, 0x18070000, 0x0, 0x200,
    0x0, 0x10007000, 0x0, 0x100,
    0x0, 0x10001000, 0x0, 0x1000,
)
CONSYS_CLOCKS = (0x5, 0x3)
CONSYS_CLOCK_NAMES = "bus"
STOCK_PWRAP_REG_NAMES = "pwrap-base"
STOCK_PWRAP_CLOCK_NAMES = "spi pwrap"
AUDIO_SUPPLY_PHANDLE = (0x37,)
PMIC_EINT_PARENT = (0x09,)
PMIC_EINT = (0x18, 0x04)
POWER_KEYCODE = (0x74,)
HOME_KEYCODE = (0x72,)

MAX_FDT_TOTALSIZE = 0x10000
WIFI_EVT_SIZE = 52722
WIFI_EVT_SHA256 = "71ca42fbd21ea1c920a3992250b8b8f62e2c70564ba9f64deb8cab8d4ff458d2"
TOPRGU_COMPATIBLES = ("mediatek,mt8163-rgu", "mediatek,mt6589-wdt")

# The stock EVT blob is a 3.18-era DTB.  Its pinctrl groups use the old
# ``pins`` property, while the 6.1 MT8163 pinctrl parser reads ``pinmux``.
# Keep this list explicit: these are all of the stock pinctrl leaf nodes that
# carry that legacy property, and converting them makes the compatibility
# transformation auditable rather than applying a broad string rewrite.
PINCTRL_LEGACY_PINS_NODES = (
    f"{PINCTRL_NODE}/uart2_tx_set/pins_cmd_dat",
    f"{PINCTRL_NODE}/eint4/pins_cmd_dat",
    f"{PINCTRL_NODE}/bq24297_chg_en_pin/pins_cmd_dat",
    f"{PINCTRL_NODE}/audexampgain0/pins_cmd0_dat",
    f"{PINCTRL_NODE}/audexampgain0/pins_cmd1_dat",
    f"{PINCTRL_NODE}/audexampgain1/pins_cmd0_dat",
    f"{PINCTRL_NODE}/audexampgain1/pins_cmd1_dat",
    f"{PINCTRL_NODE}/audexampgain2/pins_cmd0_dat",
    f"{PINCTRL_NODE}/audexampgain2/pins_cmd1_dat",
    f"{PINCTRL_NODE}/audexampgain3/pins_cmd0_dat",
    f"{PINCTRL_NODE}/audexampgain3/pins_cmd1_dat",
    f"{PINCTRL_NODE}/audexamphigh/pins_cmd_dat",
    f"{PINCTRL_NODE}/audexamplow/pins_cmd_dat",
    f"{PINCTRL_NODE}/audi2s1mode0/pins_cmd0_dat",
    f"{PINCTRL_NODE}/audi2s1mode0/pins_cmd1_dat",
    f"{PINCTRL_NODE}/audi2s1mode0/pins_cmd2_dat",
    f"{PINCTRL_NODE}/audi2s1mode1/pins_cmd0_dat",
    f"{PINCTRL_NODE}/audi2s1mode1/pins_cmd1_dat",
    f"{PINCTRL_NODE}/audi2s1mode1/pins_cmd2_dat",
    f"{PINCTRL_NODE}/pmicclkmode0/pins_cmd0_dat",
    f"{PINCTRL_NODE}/pmicclkmode0/pins_cmd1_dat",
    f"{PINCTRL_NODE}/pmicclkmode0/pins_cmd2_dat",
    f"{PINCTRL_NODE}/pmicclkmode1/pins_cmd0_dat",
    f"{PINCTRL_NODE}/pmicclkmode1/pins_cmd1_dat",
    f"{PINCTRL_NODE}/pmicclkmode1/pins_cmd2_dat",
    f"{PINCTRL_NODE}/camdefault/pins_cmd_dat",
    f"{PINCTRL_NODE}/iddig_irq_init/pins_cmd_dat",
    f"{PINCTRL_NODE}/drvvbus_init/pins_cmd_dat",
    f"{PINCTRL_NODE}/drvvbus_low/pins_cmd_dat",
    f"{PINCTRL_NODE}/drvvbus_high/pins_cmd_dat",
    f"{PINCTRL_NODE}/audexampdacmuxhigh/pins_cmd_dat",
    f"{PINCTRL_NODE}/audexampdacmuxlow/pins_cmd_dat",
)

# The stock tree also carries slew-rate on the two amp-control GPIO groups
# (GPIO122 and GPIO124).  The MT8163 6.1 pinconf implementation rejects that
# setting for these groups; the known-good 6.1 audio DTB retains the GPIO
# direction but omits slew-rate.  Keep this list explicit and narrow.
PINCTRL_AUDIO_SLEW_UNSUPPORTED_NODES = (
    f"{PINCTRL_NODE}/audexamphigh/pins_cmd_dat",
    f"{PINCTRL_NODE}/audexamplow/pins_cmd_dat",
    f"{PINCTRL_NODE}/audexampdacmuxhigh/pins_cmd_dat",
    f"{PINCTRL_NODE}/audexampdacmuxlow/pins_cmd_dat",
)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"ERROR: {message}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")


def require_hash(label: str, data: bytes, expected: str) -> None:
    actual = sha256(data)
    if actual != expected:
        fail(f"{label} SHA-256 mismatch\nexpected={expected}\nactual={actual}")


def run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        fail(f"{label} failed: {detail}")
    return result


def fdt_hex_cells(fdtget: str, dtb: Path, node: str, property_name: str) -> tuple[int, ...]:
    output = run(
        [fdtget, "-t", "x", str(dtb), node, property_name],
        f"reading {node}/{property_name}",
    ).stdout.split()
    try:
        return tuple(int(cell, 16) for cell in output)
    except ValueError:
        fail(f"non-hex cell in {node}/{property_name}: {' '.join(output)}")


def fdt_string(fdtget: str, dtb: Path, node: str, property_name: str) -> str:
    return run(
        [fdtget, "-t", "s", str(dtb), node, property_name],
        f"reading {node}/{property_name}",
    ).stdout.rstrip("\n")


def fdt_strings(fdtget: str, dtb: Path, node: str, property_name: str) -> tuple[str, ...]:
    return tuple(
        run(
            [fdtget, "-t", "s", str(dtb), node, property_name],
            f"reading {node}/{property_name}",
        ).stdout.split()
    )


def remove_property(fdtput: str, dtb: Path, node: str, property_name: str,
                    label: str) -> None:
    run(
        [fdtput, "-d", str(dtb), node, property_name],
        f"removing {label}",
    )


def remove_property_if_present(fdtput: str, dtb: Path, node: str,
                               property_name: str, label: str) -> None:
    result = subprocess.run(
        [fdtput, "-d", str(dtb), node, property_name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode and "FDT_ERR_NOTFOUND" not in result.stderr:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        fail(f"removing {label} failed: {detail}")


def rename_hex_property(fdtget: str, fdtput: str, dtb: Path, node: str,
                        old_name: str, new_name: str, label: str) -> None:
    cells = fdt_hex_cells(fdtget, dtb, node, old_name)
    remove_property(fdtput, dtb, node, old_name, f"{label}/{old_name}")
    run(
        [
            fdtput, "-t", "x", str(dtb), node, new_name,
            *(f"0x{cell:x}" for cell in cells),
        ],
        f"restoring {label}/{new_name}",
    )


def remove_default_pinctrl_state(
    fdtget: str,
    fdtput: str,
    dtb: Path,
    node: str,
    label: str,
) -> None:
    """Make a legacy audio consumer usable with the 6.1 pinctrl core.

    The stock 3.18-derived nodes contain an empty ``default`` state and
    index all real states one slot later.  Linux 6.1 parses that empty state
    during device-link construction and emits invalid-map/dependency-cycle
    diagnostics.  The direct audio DTB used by the working 6.1 boots has no
    default state, so preserve its actual named states and compact the
    phandle properties to match it.
    """
    names = fdt_strings(fdtget, dtb, node, "pinctrl-names")
    if not names or names[0] != "default":
        fail(f"{label} does not have the expected legacy default pinctrl state")
    refs = [
        fdt_hex_cells(fdtget, dtb, node, f"pinctrl-{index}")
        for index in range(len(names))
    ]
    for property_name in ("pinctrl-names",) + tuple(
        f"pinctrl-{index}" for index in range(len(names))
    ):
        remove_property(fdtput, dtb, node, property_name, f"{label}/{property_name}")

    real_names = names[1:]
    run(
        [fdtput, "-t", "s", str(dtb), node, "pinctrl-names", *real_names],
        f"restoring named {label} pinctrl states",
    )
    for index, cells in enumerate(refs[1:]):
        run(
            [
                fdtput, "-t", "x", str(dtb), node, f"pinctrl-{index}",
                *(f"0x{cell:x}" for cell in cells),
            ],
            f"restoring {label}/pinctrl-{index}",
        )


def require_absent(fdtget: str, dtb: Path, node: str, property_name: str) -> None:
    result = subprocess.run(
        [fdtget, str(dtb), node, property_name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        fail(f"stock {node}/{property_name} is unexpectedly present")
    if "FDT_ERR_NOTFOUND" not in result.stderr:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        fail(f"could not prove stock {node}/{property_name} is absent: {detail}")


def require_present(fdtget: str, dtb: Path, node: str, property_name: str) -> None:
    result = subprocess.run(
        [fdtget, str(dtb), node, property_name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        fail(f"{node}/{property_name} is missing: {detail}")


def fdt_phandle(fdtget: str, dtb: Path, node: str) -> int:
    """Return the final phandle exposed by a named provider node."""
    for property_name in ("phandle", "linux,phandle"):
        try:
            values = fdt_hex_cells(fdtget, dtb, node, property_name)
        except SystemExit:
            continue
        if len(values) == 1:
            return values[0]
    fail(f"pinctrl provider has no usable phandle: {node}")


def power_domain_provider_phandle(fdtget: str, dtb: Path) -> int:
    """Resolve and validate the transformed MT8163 SCPSYS provider."""
    phandle = fdt_hex_cells(fdtget, dtb, SCPSYS_NODE, "phandle")
    linux_phandle = fdt_hex_cells(fdtget, dtb, SCPSYS_NODE, "linux,phandle")
    cells = fdt_hex_cells(fdtget, dtb, SCPSYS_NODE, "#power-domain-cells")
    if len(phandle) != 1 or linux_phandle != phandle:
        fail("transformed SCPSYS phandle/linux,phandle mismatch")
    if cells != (1,):
        fail("transformed SCPSYS does not expose one power-domain cell")
    return phandle[0]


def fdt_totalsize(data: bytes, label: str) -> int:
    if len(data) < 8:
        fail(f"{label} is shorter than an FDT header")
    magic, total = struct.unpack_from(">II", data)
    if magic != FDT_MAGIC:
        fail(f"{label} FDT magic mismatch: {magic:#010x}")
    if total != len(data):
        fail(f"{label} totalsize {total:#x} differs from file size {len(data):#x}")
    if total > MAX_FDT_TOTALSIZE:
        fail(f"{label} totalsize {total:#x} exceeds the 64 KiB LK envelope")
    return total


def verify_stock(fdtget: str, dtb: Path, data: bytes) -> None:
    require_hash("stock EVT DTB", data, STOCK_EVT_SHA256)
    fdt_totalsize(data, "stock EVT DTB")
    resources = fdt_hex_cells(fdtget, dtb, CONSYS_NODE, "reg")
    if resources != STOCK_CONSYS_REG:
        fail(f"stock CONSYS resource tuple mismatch: {resources!r}")
    if fdt_hex_cells(fdtget, dtb, INFRACFG_NODE, "phandle") != (CONSYS_CLOCKS[0],):
        fail("stock infracfg phandle is not 5")
    if fdt_string(fdtget, dtb, PWRAP_NODE, "reg-names") != STOCK_PWRAP_REG_NAMES:
        fail("stock PWRAP resource name is not pwrap-base")
    if fdt_string(fdtget, dtb, PWRAP_NODE, "clock-names") != STOCK_PWRAP_CLOCK_NAMES:
        fail("stock PWRAP clock names are not spi,pwrap")
    if fdt_hex_cells(fdtget, dtb, VIO18_NODE, "phandle") != AUDIO_SUPPLY_PHANDLE:
        fail("stock ldo_vio18 phandle is not 0x37")
    require_absent(fdtget, dtb, CODEC_NODE, "iov-supply")
    require_absent(fdtget, dtb, CODEC_NODE, "ldoin-supply")
    require_absent(fdtget, dtb, MT6323_NODE, "interrupt-parent")
    require_absent(fdtget, dtb, MT6323_NODE, "interrupts")
    require_absent(fdtget, dtb, MT6323_KEYS_NODE, "compatible")
    require_absent(fdtget, dtb, CONSYS_NODE, "clocks")
    require_absent(fdtget, dtb, CONSYS_NODE, "clock-names")


def verify_wifi(fdtget: str, dtb: Path, data: bytes) -> int:
    total = fdt_totalsize(data, "Wi-Fi EVT DTB")
    if len(data) != WIFI_EVT_SIZE:
        fail(f"Wi-Fi EVT DTB size mismatch: expected={WIFI_EVT_SIZE} actual={len(data)}")
    if fdt_strings(fdtget, dtb, TOPRGU_NODE, "compatible") != TOPRGU_COMPATIBLES:
        fail("Wi-Fi EVT DTB does not expose the MT8163 RGU through the 6.1 watchdog binding")
    require_absent(fdtget, dtb, TOPRGU_NODE, "interrupts")
    if fdt_string(fdtget, dtb, USB_NODE, "interrupt-names") != "mc":
        fail("Wi-Fi EVT DTB does not name the MUSB controller IRQ mc")
    if fdt_string(fdtget, dtb, USB_NODE, "dr_mode") != "peripheral":
        fail("Wi-Fi EVT DTB does not select peripheral MUSB mode")
    if "syscon" not in fdt_strings(fdtget, dtb, TOPCKGEN_NODE, "compatible"):
        fail("Wi-Fi EVT DTB does not expose TOPCKGEN as a syscon")
    if fdt_hex_cells(fdtget, dtb, CONSYS_NODE, "reg") != STOCK_CONSYS_REG:
        fail("Wi-Fi EVT DTB changed the stock CONSYS resource tuple")
    if fdt_hex_cells(fdtget, dtb, INFRACFG_NODE, "phandle") != (CONSYS_CLOCKS[0],):
        fail("Wi-Fi EVT DTB changed the infracfg phandle")
    if fdt_hex_cells(fdtget, dtb, CONSYS_NODE, "clocks") != CONSYS_CLOCKS:
        fail("Wi-Fi EVT DTB has the wrong CONSYS clocks cells")
    if fdt_string(fdtget, dtb, CONSYS_NODE, "clock-names") != CONSYS_CLOCK_NAMES:
        fail("Wi-Fi EVT DTB has the wrong CONSYS clock-names value")
    if fdt_string(fdtget, dtb, PWRAP_NODE, "reg-names") != STOCK_PWRAP_REG_NAMES:
        fail("Wi-Fi EVT DTB changed the stock PWRAP resource name")
    if fdt_string(fdtget, dtb, PWRAP_NODE, "clock-names") != STOCK_PWRAP_CLOCK_NAMES:
        fail("Wi-Fi EVT DTB changed the stock PWRAP clock names")
    pio_phandle = fdt_phandle(fdtget, dtb, PIO_NODE)
    if fdt_hex_cells(fdtget, dtb, MT6323_NODE, "interrupt-parent") != (pio_phandle,):
        fail("Wi-Fi EVT DTB has the wrong MT6323 interrupt parent")
    if fdt_hex_cells(fdtget, dtb, MT6323_NODE, "interrupts") != PMIC_EINT:
        fail("Wi-Fi EVT DTB has the wrong MT6323 PMIC EINT")
    if fdt_string(fdtget, dtb, MT6323_KEYS_NODE, "compatible") != "mediatek,mt6323-keys":
        fail("Wi-Fi EVT DTB is missing the MT6323 keys node")
    if fdt_hex_cells(fdtget, dtb, MT6323_POWER_KEY_NODE, "linux,keycodes") != MUTE_KEYCODE:
        fail("Wi-Fi EVT DTB has the wrong MT6323 mute key")
    if fdt_hex_cells(fdtget, dtb, MT6323_HOME_KEY_NODE, "linux,keycodes") != HOME_KEYCODE:
        fail("Wi-Fi EVT DTB has the wrong MT6323 home key")
    if fdt_string(fdtget, dtb, ACTION_KEY_NODE, "compatible") != "gpio-keys":
        fail("Wi-Fi EVT DTB is missing the action key node")
    if fdt_string(fdtget, dtb, ACTION_BUTTON_NODE, "label") != "Action Key":
        fail("Wi-Fi EVT DTB has the wrong action key label")
    if fdt_hex_cells(fdtget, dtb, ACTION_BUTTON_NODE, "linux,code") != (ACTION_KEYCODE,):
        fail("Wi-Fi EVT DTB has the wrong action key code")
    if fdt_hex_cells(fdtget, dtb, ACTION_BUTTON_NODE, "gpios") != (
        pio_phandle, ACTION_GPIO, ACTION_GPIO_FLAGS,
    ):
        fail("Wi-Fi EVT DTB action key is not bound to PIO GPIO36/KPCOL0")
    if fdt_hex_cells(fdtget, dtb, ACTION_BUTTON_NODE, "debounce-interval") != (0x14,):
        fail("Wi-Fi EVT DTB has the wrong action key debounce")
    if fdt_string(fdtget, dtb, IMGSYS_NODE, "status") != "okay":
        fail("Wi-Fi EVT DTB did not enable the MT8163 image-clock provider")
    for property_name in ("iov-supply", "ldoin-supply"):
        if fdt_hex_cells(fdtget, dtb, CODEC_NODE, property_name) != AUDIO_SUPPLY_PHANDLE:
            fail(f"Wi-Fi EVT DTB has the wrong codec {property_name} phandle")
    if fdt_strings(fdtget, dtb, SPI_AUDIO_NODE, "pinctrl-names") != (
        "audpmicclk-mode0", "audpmicclk-mode1", "audi2s1-mode0",
        "audi2s1-mode1", "extamp-pullhigh", "extamp-pulllow", "cmmclk-mclk",
    ):
        fail("Wi-Fi EVT DTB has the wrong SPI audio pinctrl states")
    if fdt_strings(fdtget, dtb, AFE_NODE, "pinctrl-names") != (
        "audpmicclk-speaker-mode0", "audpmicclk-speaker-mode1",
        "audi2s1-speaker-mode0", "audi2s1-speaker-mode1",
        "extamp-pullhigh", "extamp-pulllow", "cmmclk-mclk",
        "extamp-dacmux-pullhigh", "extamp-dacmux-pulllow",
    ):
        fail("Wi-Fi EVT DTB has the wrong AFE pinctrl states")
    if fdt_hex_cells(fdtget, dtb, AFE_NODE, "mediatek,pmic") != (AFE_PMIC_PHANDLE,):
        fail("Wi-Fi EVT DTB does not point the AFE at the MT6323 PMIC")
    if fdt_hex_cells(fdtget, dtb, AUDIOSYS_NODE, "phandle") != (AUDIOSYS_PHANDLE,):
        fail("Wi-Fi EVT DTB has the wrong audiosys phandle")
    afe_clocks = fdt_hex_cells(fdtget, dtb, AFE_NODE, "clocks")
    if afe_clocks[-2:] != (AUDIOSYS_PHANDLE, AUDIOSYS_AUDIO_24M_ID):
        fail("Wi-Fi EVT DTB AFE is not connected to audiosys/aud_24m")
    if fdt_strings(fdtget, dtb, AFE_NODE, "clock-names")[-1:] != ("aud_24m_clk",):
        fail("Wi-Fi EVT DTB AFE is missing aud_24m_clk")
    require_absent(fdtget, dtb, AFE_NODE, "extamp-gpios")
    require_absent(fdtget, dtb, AFE_NODE, "dacmux-gpios")
    for state, output_property, label in (
        ("audexamphigh", "output-high", "external-amp on"),
        ("audexamplow", "output-low", "external-amp off"),
    ):
        state_node = f"{PINCTRL_NODE}/{state}"
        if fdt_hex_cells(
            fdtget, dtb, f"{state_node}/pins_cmd_dat", "pinmux"
        ) != (RADAR_EXTAMP_PINMUX,):
            fail(f"Wi-Fi EVT DTB has the wrong {label} GPIO122 pin")
        require_present(
            fdtget, dtb, f"{state_node}/pins_cmd_dat", output_property
        )
    for state, output_property, phandle, label in (
        ("audexampdacmuxhigh", "output-high", RADAR_DACMUX_HIGH_PHANDLE,
         "DAC-mux high"),
        ("audexampdacmuxlow", "output-low", RADAR_DACMUX_LOW_PHANDLE,
         "DAC-mux low"),
    ):
        state_node = f"{PINCTRL_NODE}/{state}"
        if fdt_hex_cells(
            fdtget, dtb, f"{state_node}/pins_cmd_dat", "pinmux"
        ) != (RADAR_DACMUX_PINMUX,):
            fail(f"Wi-Fi EVT DTB has the wrong {label} GPIO124 pin")
        if fdt_hex_cells(fdtget, dtb, state_node, "phandle") != (phandle,):
            fail(f"Wi-Fi EVT DTB has the wrong {label} phandle")
        if fdt_hex_cells(fdtget, dtb, state_node, "linux,phandle") != (phandle,):
            fail(f"Wi-Fi EVT DTB has the wrong {label} linux,phandle")
        require_present(
            fdtget, dtb, f"{state_node}/pins_cmd_dat", output_property
        )
    for state, cells, label in (
        ("audi2s1mode0", RADAR_FPGA_I2S_IDLE_AUDIO_PINMUX, "FPGA idle I2S1"),
        ("audi2s1mode1", RADAR_FPGA_I2S_ACTIVE_AUDIO_PINMUX, "FPGA active I2S1"),
    ):
        for index, cell in enumerate(cells):
            node = f"{PINCTRL_NODE}/{state}/pins_cmd{index}_dat"
            if fdt_hex_cells(fdtget, dtb, node, "pinmux") != (cell,):
                fail(f"Wi-Fi EVT DTB has the wrong {label} pin {index}")
    for state, cells, label in (
        (
            "audi2s1speaker_mode0",
            RADAR_SPEAKER_I2S_IDLE_AUDIO_PINMUX,
            "speaker idle I2S1",
        ),
        (
            "audi2s1speaker_mode1",
            RADAR_SPEAKER_I2S_ACTIVE_AUDIO_PINMUX,
            "speaker active I2S1",
        ),
    ):
        for index, cell in enumerate(cells):
            node = f"{PINCTRL_NODE}/{state}/pins_cmd{index}_dat"
            if fdt_hex_cells(fdtget, dtb, node, "pinmux") != (cell,):
                fail(f"Wi-Fi EVT DTB has the wrong {label} pin {index}")
    for state, cells, label in (
        (
            "pmicclkspeaker_mode0",
            RADAR_SPEAKER_PMIC_IDLE_AUDIO_PINMUX,
            "speaker idle PMIC/I2S/MCLK",
        ),
        (
            "pmicclkspeaker_mode1",
            RADAR_SPEAKER_PMIC_ACTIVE_AUDIO_PINMUX,
            "speaker active PMIC/I2S/MCLK",
        ),
    ):
        for leaf, cell in zip(
            ("pins_cmd0_dat", "pins_cmd1_dat", "pins_cmd2_dat"), cells[:3],
        ):
            node = f"{PINCTRL_NODE}/{state}/{leaf}"
            if fdt_hex_cells(fdtget, dtb, node, "pinmux") != (cell,):
                fail(f"Wi-Fi EVT DTB has the wrong {label} {leaf}")
        node = f"{PINCTRL_NODE}/{state}/pins_i2s_dat"
        if fdt_hex_cells(fdtget, dtb, node, "pinmux") != tuple(cells[3:]):
            fail(f"Wi-Fi EVT DTB has the wrong {label} pins_i2s_dat")
    for property_name, phandle, label in (
        ("pinctrl-0", RADAR_SPEAKER_PMIC_IDLE_PHANDLE, "speaker PMIC idle"),
        ("pinctrl-1", RADAR_SPEAKER_PMIC_ACTIVE_PHANDLE, "speaker PMIC active"),
        ("pinctrl-2", RADAR_SPEAKER_I2S_IDLE_PHANDLE, "speaker I2S idle"),
        ("pinctrl-3", RADAR_SPEAKER_I2S_ACTIVE_PHANDLE, "speaker I2S active"),
    ):
        if fdt_hex_cells(fdtget, dtb, AFE_NODE, property_name) != (phandle,):
            fail(f"Wi-Fi EVT DTB does not connect AFE to {label}")
    if fdt_hex_cells(fdtget, dtb, MT6323_NODE, "phandle") != (AFE_PMIC_PHANDLE,):
        fail("Wi-Fi EVT DTB has the wrong MT6323 PMIC phandle")
    if fdt_string(fdtget, dtb, CODEC_MCLK_NODE, "compatible") != "fixed-clock":
        fail("Wi-Fi EVT DTB is missing the codec 9.6 MHz fixed clock")
    if fdt_hex_cells(fdtget, dtb, CODEC_MCLK_NODE, "#clock-cells") != (0,):
        fail("Wi-Fi EVT DTB has the wrong codec fixed-clock cell count")
    if fdt_hex_cells(fdtget, dtb, CODEC_MCLK_NODE, "clock-frequency") != (CODEC_MCLK_RATE,):
        fail("Wi-Fi EVT DTB has the wrong codec MCLK rate")
    if fdt_hex_cells(fdtget, dtb, CODEC_MCLK_NODE, "phandle") != (CODEC_MCLK_PHANDLE,):
        fail("Wi-Fi EVT DTB has the wrong codec MCLK phandle")
    if fdt_hex_cells(fdtget, dtb, CODEC_NODE, "clocks") != (CODEC_MCLK_PHANDLE,):
        fail("Wi-Fi EVT DTB codec is not connected to the 9.6 MHz MCLK")
    if fdt_string(fdtget, dtb, CODEC_NODE, "clock-names") != "mclk":
        fail("Wi-Fi EVT DTB codec clock name is not mclk")
    for property_name in (
        "audclk-gpio", "audmiso-gpio", "audmosi-gpio", "extspkamp-gpio",
        "i2s1clk-gpio", "i2s1dat-gpio", "i2s1mclk-gpio", "i2s1ws-gpio",
    ):
        require_absent(fdtget, dtb, AFE_NODE, property_name)
    for property_name, node, label in (
        ("pinctrl-4", f"{PINCTRL_NODE}/audexamphigh", "external-amp high"),
        ("pinctrl-5", f"{PINCTRL_NODE}/audexamplow", "external-amp low"),
        ("pinctrl-6", f"{PINCTRL_NODE}/camdefault", "codec CMMCLK"),
        ("pinctrl-7", f"{PINCTRL_NODE}/audexampdacmuxhigh", "DAC-mux high"),
        ("pinctrl-8", f"{PINCTRL_NODE}/audexampdacmuxlow", "DAC-mux low"),
    ):
        expected = fdt_phandle(fdtget, dtb, node)
        if fdt_hex_cells(fdtget, dtb, AFE_NODE, property_name) != (expected,):
            fail(f"Wi-Fi EVT DTB does not connect AFE to {label}")
    scpsys_phandle = power_domain_provider_phandle(fdtget, dtb)
    if fdt_hex_cells(fdtget, dtb, AFE_NODE, "power-domains") != (
        scpsys_phandle, AFE_POWER_DOMAIN_ID,
    ):
        fail("Wi-Fi EVT DTB changed the stock AFE power-domain provider")
    require_absent(fdtget, dtb, PINCTRL_NODE, "pinctrl-names")
    require_absent(fdtget, dtb, PINCTRL_NODE, "pinctrl-0")
    for node in PINCTRL_LEGACY_PINS_NODES:
        require_present(fdtget, dtb, node, "pinmux")
        require_absent(fdtget, dtb, node, "pins")
    for node in PINCTRL_AUDIO_SLEW_UNSUPPORTED_NODES:
        require_absent(fdtget, dtb, node, "slew-rate")
    require_present(fdtget, dtb, SPI_CONTROLLER_NODE, "mediatek,keep-clocks-on")
    require_hash("Wi-Fi EVT DTB", data, WIFI_EVT_SHA256)
    return total


def extract_stock_evt(source: bytes) -> bytes:
    if len(source) != SOURCE_BOOT_SIZE or source[:8] != ANDROID_MAGIC:
        fail("source is not the pinned 16 MiB Android boot envelope")
    require_hash("source boot envelope", source, SOURCE_BOOT_SHA256)

    kernel_size = struct.unpack_from("<I", source, 8)[0]
    kernel = source[PAGE_SIZE:PAGE_SIZE + kernel_size]
    if kernel[:4] != MKIMG_MAGIC:
        fail("source MediaTek KERNEL header is missing")
    payload_size = struct.unpack_from("<I", kernel, 4)[0]
    payload = kernel[MKIMG_SIZE:MKIMG_SIZE + payload_size]
    end = EVT_PAYLOAD_OFFSET + EVT_RAW_SIZE
    if end > len(payload):
        fail("stock EVT range lies outside the MediaTek kernel payload")
    return payload[EVT_PAYLOAD_OFFSET:end]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract the pinned stock EVT DTB and add the CONSYS bus clock "
            "and 6.1 PMIC/codec compatibility properties."
        )
    )
    parser.add_argument("--source-boot", type=Path, required=True,
                        help="pinned v184 16 MiB stock boot envelope")
    parser.add_argument("--output", type=Path, required=True,
                        help="new raw Wi-Fi candidate DTB (must not already exist)")
    args = parser.parse_args()

    fdtget = shutil.which("fdtget")
    fdtput = shutil.which("fdtput")
    if fdtget is None or fdtput is None:
        missing = ", ".join(
            name for name, path in (("fdtget", fdtget), ("fdtput", fdtput)) if path is None
        )
        fail(f"required device-tree tool not found in PATH: {missing}")
    if args.output.exists():
        fail(f"refusing to overwrite {args.output}")

    source = read(args.source_boot)
    stock = extract_stock_evt(source)
    with tempfile.TemporaryDirectory(prefix="libreecho-wifi-dtb-") as temporary:
        stock_path = Path(temporary) / "stock-evt.dtb"
        candidate_path = Path(temporary) / "wifi-evt.dtb"
        stock_path.write_bytes(stock)
        verify_stock(fdtget, stock_path, stock)

        shutil.copyfile(stock_path, candidate_path)
        run(
            [
                fdtput, "-t", "s", str(candidate_path), TOPCKGEN_NODE,
                "compatible", "mediatek,mt8163-topckgen", "syscon",
            ],
            "exposing TOPCKGEN as a syscon",
        )
        # Give the audio clock provider an explicit phandle, then expose its
        # 24.576-MHz APLL2/4 gate to the narrow DL1 AFE driver.  The stock EVT
        # blob has the provider node but no phandle and only the eight legacy
        # top-level clock references.
        for property_name in ("phandle", "linux,phandle"):
            run(
                [
                    fdtput, "-p", "-t", "x", str(candidate_path),
                    AUDIOSYS_NODE, property_name, f"0x{AUDIOSYS_PHANDLE:x}",
                ],
                f"adding the audiosys {property_name}",
            )
        afe_clocks = fdt_hex_cells(fdtget, candidate_path, AFE_NODE, "clocks")
        afe_clock_names = fdt_strings(fdtget, candidate_path, AFE_NODE, "clock-names")
        afe_clock_names = (*afe_clock_names, "aud_24m_clk")
        run(
            [
                fdtput, "-t", "x", str(candidate_path), AFE_NODE, "clocks",
                *(f"0x{cell:x}" for cell in (*afe_clocks, AUDIOSYS_PHANDLE,
                                               AUDIOSYS_AUDIO_24M_ID)),
            ],
            "connecting the AFE to audiosys/aud_24m",
        )
        run(
            [
                fdtput, "-t", "s", str(candidate_path), AFE_NODE,
                "clock-names", *afe_clock_names,
            ],
            "naming the AFE aud_24m clock",
        )
        # The stock EVT tree retains legacy vendor GPIO properties and empty
        # default pinctrl states that are not valid consumers for the 6.1
        # OF/pinctrl dependency parser.  The direct 6.1 audio DTB that
        # successfully registered mt-snd-card omits these properties.
        for property_name in (
            "audclk-gpio", "audmiso-gpio", "audmosi-gpio", "extspkamp-gpio",
            "i2s1clk-gpio", "i2s1dat-gpio", "i2s1mclk-gpio", "i2s1ws-gpio",
        ):
            remove_property(
                fdtput, candidate_path, AFE_NODE, property_name,
                f"legacy AFE {property_name}",
            )
        remove_default_pinctrl_state(
            fdtget, fdtput, candidate_path, SPI_AUDIO_NODE, "SPI audio"
        )
        remove_default_pinctrl_state(
            fdtget, fdtput, candidate_path, AFE_NODE, "AFE"
        )
        for property_name in ("phandle", "linux,phandle"):
            run(
                [
                    fdtput, "-p", "-t", "x", str(candidate_path),
                    MT6323_NODE, property_name, f"0x{AFE_PMIC_PHANDLE:x}",
                ],
                f"adding the MT6323 PMIC {property_name}",
            )
        run(
            [
                fdtput, "-p", "-t", "x", str(candidate_path), AFE_NODE,
                "mediatek,pmic", f"0x{AFE_PMIC_PHANDLE:x}",
            ],
            "linking the AFE to the MT6323 PMIC",
        )
        # Retain the stock Radar-Puffin external-amplifier state on GPIO122.
        # The AFE has no extamp-gpios property in this variant, so it selects
        # this pinctrl state rather than bypassing it with a descriptor.
        for state, output_property, label in (
            ("audexamphigh", "output-high", "external-amp on"),
            ("audexamplow", "output-low", "external-amp off"),
        ):
            state_node = f"{PINCTRL_NODE}/{state}"
            run(
                [
                    fdtput, "-t", "x", str(candidate_path),
                    f"{state_node}/pins_cmd_dat", "pinmux",
                    f"0x{RADAR_EXTAMP_PINMUX:x}",
                ],
                f"retaining {label} GPIO122",
            )
        # Keep the original 72/73/74 nodes for the FPGA consumer.  The AFE
        # receives separate states below so selecting its composite PMIC state
        # also selects the populated 46/47/48 speaker group and codec MCLK.
        for state, cells, label in (
            ("audi2s1mode0", RADAR_FPGA_I2S_IDLE_AUDIO_PINMUX, "FPGA idle I2S1"),
            ("audi2s1mode1", RADAR_FPGA_I2S_ACTIVE_AUDIO_PINMUX, "FPGA active I2S1"),
        ):
            for index, cell in enumerate(cells):
                node = f"{PINCTRL_NODE}/{state}/pins_cmd{index}_dat"
                run(
                    [
                        fdtput, "-t", "x", str(candidate_path), node,
                        "pinmux", f"0x{cell:x}",
                    ],
                    f"preserving {label} pin {index}",
                )
        for state, phandle, cells, label in (
            (
                "audi2s1speaker_mode0",
                RADAR_SPEAKER_I2S_IDLE_PHANDLE,
                RADAR_SPEAKER_I2S_IDLE_AUDIO_PINMUX,
                "speaker idle I2S1",
            ),
            (
                "audi2s1speaker_mode1",
                RADAR_SPEAKER_I2S_ACTIVE_PHANDLE,
                RADAR_SPEAKER_I2S_ACTIVE_AUDIO_PINMUX,
                "speaker active I2S1",
            ),
        ):
            state_node = f"{PINCTRL_NODE}/{state}"
            for property_name in ("linux,phandle", "phandle"):
                run(
                    [
                        fdtput, "-p", "-t", "x", str(candidate_path), state_node,
                        property_name, f"0x{phandle:x}",
                    ],
                    f"adding {label} {property_name}",
                )
            for index, cell in enumerate(cells):
                leaf = f"{state_node}/pins_cmd{index}_dat"
                run(
                    [
                        fdtput, "-p", "-t", "x", str(candidate_path), leaf,
                        "pinmux", f"0x{cell:x}",
                    ],
                    f"adding {label} pin {index}",
                )
                if state.endswith("mode1"):
                    run(
                        [
                            fdtput, "-p", "-t", "x", str(candidate_path), leaf,
                            "drive-strength", "0x4",
                        ],
                        f"adding {label} drive strength {index}",
                    )
        for state, phandle, cells, label in (
            (
                "pmicclkspeaker_mode0",
                RADAR_SPEAKER_PMIC_IDLE_PHANDLE,
                RADAR_SPEAKER_PMIC_IDLE_AUDIO_PINMUX,
                "speaker idle PMIC/I2S/MCLK",
            ),
            (
                "pmicclkspeaker_mode1",
                RADAR_SPEAKER_PMIC_ACTIVE_PHANDLE,
                RADAR_SPEAKER_PMIC_ACTIVE_AUDIO_PINMUX,
                "speaker active PMIC/I2S/MCLK",
            ),
        ):
            state_node = f"{PINCTRL_NODE}/{state}"
            for property_name in ("linux,phandle", "phandle"):
                run(
                    [
                        fdtput, "-p", "-t", "x", str(candidate_path), state_node,
                        property_name, f"0x{phandle:x}",
                    ],
                    f"adding {label} {property_name}",
                )
            for leaf, cell in zip(
                ("pins_cmd0_dat", "pins_cmd1_dat", "pins_cmd2_dat"), cells[:3],
            ):
                run(
                    [
                        fdtput, "-p", "-t", "x", str(candidate_path),
                        f"{state_node}/{leaf}", "pinmux", f"0x{cell:x}",
                    ],
                    f"adding {label} {leaf}",
                )
            run(
                [
                    fdtput, "-p", "-t", "x", str(candidate_path),
                    f"{state_node}/pins_i2s_dat", "pinmux",
                    *(f"0x{cell:x}" for cell in cells[3:]),
                ],
                f"adding {label} pins_i2s_dat",
            )
        run(
            [
                fdtput, "-t", "s", str(candidate_path), AFE_NODE,
                "pinctrl-names",
                "audpmicclk-speaker-mode0", "audpmicclk-speaker-mode1",
                "audi2s1-speaker-mode0", "audi2s1-speaker-mode1",
                "extamp-pullhigh", "extamp-pulllow", "cmmclk-mclk",
                "extamp-dacmux-pullhigh", "extamp-dacmux-pulllow",
            ],
            "renaming AFE speaker pinctrl states",
        )
        for index, phandle in enumerate(
            (
                RADAR_SPEAKER_PMIC_IDLE_PHANDLE,
                RADAR_SPEAKER_PMIC_ACTIVE_PHANDLE,
                RADAR_SPEAKER_I2S_IDLE_PHANDLE,
                RADAR_SPEAKER_I2S_ACTIVE_PHANDLE,
            )
        ):
            run(
                [
                    fdtput, "-t", "x", str(candidate_path), AFE_NODE,
                    f"pinctrl-{index}", f"0x{phandle:x}",
                ],
                f"linking AFE speaker pinctrl-{index}",
            )
        # The AFE's final stock amp-high, amp-low, CMMCLK, and generated
        # DAC-mux references are linked after every structural fdtput below.
        # fdtput may renumber legacy phandles while extending the blob, so
        # consumer references must be resolved from the final named provider
        # nodes rather than copied from legacy numeric values.

        for property_name, value in (
            ("compatible", "fixed-clock"),
            ("#clock-cells", "0x00"),
            ("clock-frequency", f"0x{CODEC_MCLK_RATE:x}"),
            ("phandle", f"0x{CODEC_MCLK_PHANDLE:x}"),
            ("linux,phandle", f"0x{CODEC_MCLK_PHANDLE:x}"),
        ):
            value_type = "s" if property_name == "compatible" else "x"
            run(
                [
                    fdtput, "-p", "-t", value_type, str(candidate_path),
                    CODEC_MCLK_NODE, property_name, value,
                ],
                f"adding codec MCLK {property_name}",
            )
        run(
            [
                fdtput, "-t", "x", str(candidate_path), CODEC_NODE, "clocks",
                f"0x{CODEC_MCLK_PHANDLE:x}",
            ],
            "linking the codec to its 9.6 MHz MCLK",
        )
        remove_property(
            fdtput, candidate_path, PINCTRL_NODE, "pinctrl-names",
            "pinctrl controller/pinctrl-names",
        )
        remove_property(
            fdtput, candidate_path, PINCTRL_NODE, "pinctrl-0",
            "pinctrl controller/pinctrl-0",
        )
        for node in PINCTRL_LEGACY_PINS_NODES:
            rename_hex_property(
                fdtget, fdtput, candidate_path, node, "pins", "pinmux",
                "legacy pinctrl group",
            )
        # The legacy-property conversion above restores each old ``pins``
        # value; explicitly retain the stock GPIO122 amp pin afterwards.
        for state in ("audexamphigh", "audexamplow"):
            run(
                [
                    fdtput, "-t", "x", str(candidate_path),
                    f"{PINCTRL_NODE}/{state}/pins_cmd_dat", "pinmux",
                    f"0x{RADAR_EXTAMP_PINMUX:x}",
                ],
                f"reasserting {state} GPIO122 after legacy conversion",
            )
        # fdtput may renumber pre-existing phandle properties while extending
        # the blob.  Reassert the stock DAC-mux identity and polarity after all
        # legacy-property conversion so AFE pinctrl-7/8 resolve to GPIO124.
        for state, phandle, output_property, opposite_property, label in (
            (
                "audexampdacmuxhigh", RADAR_DACMUX_HIGH_PHANDLE,
                "output-high", "output-low", "DAC-mux high",
            ),
            (
                "audexampdacmuxlow", RADAR_DACMUX_LOW_PHANDLE,
                "output-low", "output-high", "DAC-mux low",
            ),
        ):
            state_node = f"{PINCTRL_NODE}/{state}"
            for property_name in ("phandle", "linux,phandle"):
                run(
                    [
                        fdtput, "-p", "-t", "x", str(candidate_path),
                        state_node, property_name, f"0x{phandle:x}",
                    ],
                    f"restoring {label} {property_name}",
                )
            remove_property_if_present(
                fdtput, candidate_path, f"{state_node}/pins_cmd_dat",
                opposite_property, f"{label} {opposite_property}",
            )
            run(
                [
                    fdtput, "-p", str(candidate_path),
                    f"{state_node}/pins_cmd_dat", output_property,
                ],
                f"setting {label} polarity",
            )
        for node in PINCTRL_AUDIO_SLEW_UNSUPPORTED_NODES:
            remove_property(
                fdtput, candidate_path, node, "slew-rate",
                "unsupported audio amp slew-rate",
            )
        run(
            [
                fdtput, "-t", "s", str(candidate_path), TOPRGU_NODE,
                "compatible", *TOPRGU_COMPATIBLES,
            ],
            "adding the 6.1 MT8163 watchdog compatible",
        )
        remove_property(
            fdtput, candidate_path, TOPRGU_NODE, "interrupts",
            "legacy MT8163 watchdog bark IRQ",
        )
        run(
            [fdtput, "-t", "s", str(candidate_path), USB_NODE,
             "interrupt-names", "mc"],
            "adding the MUSB controller IRQ name",
        )
        run(
            [fdtput, "-t", "s", str(candidate_path), USB_NODE,
             "dr_mode", "peripheral"],
            "selecting peripheral MUSB mode",
        )
        run(
            [fdtput, str(candidate_path), SPI_CONTROLLER_NODE,
             "mediatek,keep-clocks-on"],
            "restoring the SPI audio clock-retention flag",
        )
        run(
            [fdtput, "-t", "x", str(candidate_path), CONSYS_NODE, "clocks", "5", "3"],
            "adding the CONSYS clocks property",
        )
        run(
            [fdtput, "-t", "s", str(candidate_path), CONSYS_NODE, "clock-names", "bus"],
            "adding the CONSYS clock-names property",
        )
        run(
            [fdtput, "-t", "s", str(candidate_path), IMGSYS_NODE, "status", "okay"],
            "enabling the MT8163 image-clock provider",
        )
        pio_phandle = fdt_phandle(fdtget, candidate_path, PIO_NODE)
        run(
            [
                fdtput, "-t", "x", str(candidate_path), MT6323_NODE,
                "interrupt-parent", f"0x{pio_phandle:x}",
            ],
            "adding the MT6323 interrupt parent",
        )
        run(
            [
                fdtput, "-t", "x", str(candidate_path), MT6323_NODE,
                "interrupts", "0x18", "0x04",
            ],
            "adding the MT6323 PMIC EINT",
        )
        run(
            [
                fdtput, "-p", "-t", "s", str(candidate_path), MT6323_KEYS_NODE,
                "compatible", "mediatek,mt6323-keys",
            ],
            "adding the MT6323 keys node",
        )
        run(
            [
                fdtput, "-p", "-t", "x", str(candidate_path),
                MT6323_KEYS_NODE, "mediatek,long-press-mode", "0x01",
            ],
            "adding the MT6323 long-press policy",
        )
        run(
            [
                fdtput, "-p", "-t", "x", str(candidate_path),
                MT6323_KEYS_NODE, "power-off-time-sec", "0x00",
            ],
            "adding the MT6323 power-off policy",
        )
        for node, keycode, label in (
            (MT6323_POWER_KEY_NODE, f"0x{MUTE_KEYCODE[0]:x}", "mute"),
            (MT6323_HOME_KEY_NODE, "0x72", "home"),
        ):
            run(
                [
                    fdtput, "-p", "-t", "x", str(candidate_path), node,
                    "linux,keycodes", keycode,
                ],
                f"adding the MT6323 {label} key",
            )
        run(
            [
                fdtput, "-p", "-t", "s", str(candidate_path), ACTION_KEY_NODE,
                "compatible", "gpio-keys",
            ],
            "adding the action key controller",
        )
        run(
            [
                fdtput, "-p", "-t", "s", str(candidate_path), ACTION_BUTTON_NODE,
                "label", "Action Key",
            ],
            "adding the action key label",
        )
        run(
            [
                fdtput, "-p", "-t", "x", str(candidate_path), ACTION_BUTTON_NODE,
                "linux,code", f"0x{ACTION_KEYCODE:x}",
            ],
            "adding the KEY_HELP action code",
        )
        run(
            [
                fdtput, "-p", "-t", "x", str(candidate_path), ACTION_BUTTON_NODE,
                "debounce-interval", "0x14",
            ],
            "adding the action key debounce",
        )
        for property_name in ("gpios",):
            run(
                [
                    fdtput, "-p", "-t", "x", str(candidate_path), ACTION_BUTTON_NODE,
                    property_name, f"0x{pio_phandle:x}", f"0x{ACTION_GPIO:x}",
                    f"0x{ACTION_GPIO_FLAGS:x}",
                ],
                "binding the action key to PIO GPIO36/KPCOL0",
            )
        for property_name in ("iov-supply", "ldoin-supply"):
            run(
                [
                    fdtput, "-t", "x", str(candidate_path), CODEC_NODE,
                    property_name, "0x37",
                ],
                f"adding the codec {property_name} property",
            )
        scpsys_phandle = power_domain_provider_phandle(fdtget, candidate_path)
        run(
            [
                fdtput, "-t", "x", str(candidate_path), AFE_NODE,
                "power-domains", f"0x{scpsys_phandle:x}",
                f"0x{AFE_POWER_DOMAIN_ID:x}",
            ],
            "restoring the stock AFE power-domain provider",
        )
        final_pinctrl_refs = (
            ("pinctrl-4", f"{PINCTRL_NODE}/audexamphigh", "external-amp high"),
            ("pinctrl-5", f"{PINCTRL_NODE}/audexamplow", "external-amp low"),
            ("pinctrl-6", f"{PINCTRL_NODE}/camdefault", "codec CMMCLK"),
            ("pinctrl-7", f"{PINCTRL_NODE}/audexampdacmuxhigh", "DAC-mux high"),
            ("pinctrl-8", f"{PINCTRL_NODE}/audexampdacmuxlow", "DAC-mux low"),
        )
        for property_name, node, label in final_pinctrl_refs:
            phandle = fdt_phandle(fdtget, candidate_path, node)
            run(
                [
                    fdtput, "-t", "x", str(candidate_path), AFE_NODE,
                    property_name, f"0x{phandle:x}",
                ],
                f"linking final AFE {label} {property_name}",
            )
        for property_name, node, label in final_pinctrl_refs:
            expected = fdt_phandle(fdtget, candidate_path, node)
            actual = fdt_hex_cells(fdtget, candidate_path, AFE_NODE, property_name)
            if actual != (expected,):
                fail(f"final AFE {property_name} does not reference {label}")
        # The action node and its GPIO property are structural additions.  Bind
        # both GPIO consumers from the final named PIO provider, then read back
        # after fdtput has had its last opportunity to resize the blob.
        for _attempt in range(3):
            final_pio_phandle = fdt_phandle(fdtget, candidate_path, PIO_NODE)
            for node, property_name, label in (
                (MT6323_NODE, "interrupt-parent", "final MT6323 interrupt parent"),
                (ACTION_BUTTON_NODE, "gpios", "final action key GPIO36/KPCOL0"),
            ):
                cells = (final_pio_phandle,) if node == MT6323_NODE else (
                    final_pio_phandle, ACTION_GPIO, ACTION_GPIO_FLAGS,
                )
                run(
                    [
                        fdtput, "-p", "-t", "x", str(candidate_path), node,
                        property_name, *(f"0x{cell:x}" for cell in cells),
                    ],
                    f"binding {label}",
                )
            rebound_pio_phandle = fdt_phandle(fdtget, candidate_path, PIO_NODE)
            if (
                fdt_hex_cells(fdtget, candidate_path, MT6323_NODE, "interrupt-parent")
                == (rebound_pio_phandle,)
                and fdt_hex_cells(fdtget, candidate_path, ACTION_BUTTON_NODE, "gpios")
                == (rebound_pio_phandle, ACTION_GPIO, ACTION_GPIO_FLAGS)
            ):
                break
        else:
            fail("final GPIO consumers do not reference the named PIO provider")
        candidate = candidate_path.read_bytes()
        total = verify_wifi(fdtget, candidate_path, candidate)

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as output:
            output.write(candidate)
    except OSError as exc:
        fail(f"cannot create {args.output}: {exc}")

    print(
        "wifi_dtb_contract=PASS "
        f"stock_sha256={STOCK_EVT_SHA256} "
        f"output_sha256={WIFI_EVT_SHA256} "
        f"totalsize={total} "
        f"node={CONSYS_NODE} "
        "reg=stock-three-resource clocks=5,3 clock-names=bus "
        "pwrap-reg-names=stock pwrap-clock-names=stock "
        "imgsys-status=okay mt6323-eint=pio-provider:0x18/0x04 "
        "audio-pmic-phandle=0x48 codec-mclk=9.6MHz codec-supplies=0x37"
    )


if __name__ == "__main__":
    main()
