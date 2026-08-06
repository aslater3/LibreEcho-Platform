#!/usr/bin/env python3
"""Fail-closed verifier for the LibreEcho Radar-Puffin production DTB.

This checks the hardware semantics that made the accepted Linux 6.1 image
usable.  It intentionally does not pin phandle numbers, which are build-order
artifacts, but it does pin the providers, clock IDs, GPIO pinmux values, and
accepted amp/DAC-mux control path.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn


TOPCKGEN = "/soc/topckgen@10000000"
SCPSYS = "/soc/scpsys@10006000"
PINCTRL = "/soc/pinctrl@10005000"
AFE = "/soc/mt_soc_dl1_pcm@11220000"
AUDIOSYS = "/soc/audiosys@11220000"
AUDIO_24M_ID = 3
AUDIO_POWER_DOMAIN_ID = 5
CODEC_MCLK_HZ = 9_600_000

AFE_CLOCK_NAMES = (
    "aud_infra_clk",
    "top_mux_audio",
    "top_mux_audio_intbus",
    "aud_mux1_clk",
    "aud_mux2_clk",
    "apmixed_apll1_clk",
    "apmixed_apll2_clk",
    "top_clk26m_clk",
    "aud_24m_clk",
)
AFE_PINCTRL_NAMES = (
    "audpmicclk-speaker-mode0",
    "audpmicclk-speaker-mode1",
    "audi2s1-speaker-mode0",
    "audi2s1-speaker-mode1",
    "extamp-pullhigh",
    "extamp-pulllow",
    "cmmclk-mclk",
    "extamp-dacmux-pullhigh",
    "extamp-dacmux-pulllow",
)


class ContractError(RuntimeError):
    """The DTB does not preserve the accepted hardware contract."""


def _run(dtb: Path, arguments: list[str], label: str) -> str:
    completed = subprocess.run(
        ["fdtget", *arguments, str(dtb)] if arguments[:1] == ["--version"] else
        ["fdtget", *arguments[:1], str(dtb), *arguments[1:]],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise ContractError(f"cannot read {label}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _strings(dtb: Path, node: str, prop: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["fdtget", str(dtb), node, prop],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise ContractError(f"missing {node}:{prop}")
    return tuple(completed.stdout.split())


def _cells(dtb: Path, node: str, prop: str) -> tuple[int, ...]:
    completed = subprocess.run(
        ["fdtget", "-t", "x", str(dtb), node, prop],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise ContractError(f"missing {node}:{prop}")
    try:
        return tuple(int(value, 16) for value in completed.stdout.split())
    except ValueError as exc:
        raise ContractError(f"invalid cells in {node}:{prop}") from exc


def _properties(dtb: Path, node: str) -> set[str]:
    completed = subprocess.run(
        ["fdtget", "-p", str(dtb), node],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise ContractError(f"missing node {node}")
    return set(completed.stdout.split())


def _children(dtb: Path, node: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["fdtget", "-l", str(dtb), node],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise ContractError(f"cannot list node {node}")
    return tuple(completed.stdout.split())


def _phandle(dtb: Path, node: str) -> int:
    for prop in ("phandle", "linux,phandle"):
        try:
            values = _cells(dtb, node, prop)
        except ContractError:
            continue
        if len(values) == 1:
            return values[0]
    raise ContractError(f"missing phandle on {node}")


def _node_for_phandle(dtb: Path, phandle: int) -> str:
    matches: list[str] = []
    for path in _walk(dtb):
        for prop in ("phandle", "linux,phandle"):
            if prop not in _properties(dtb, path):
                continue
            if _cells(dtb, path, prop) == (phandle,):
                matches.append(path)
                break
    if len(matches) != 1:
        raise ContractError(
            f"codec MCLK phandle 0x{phandle:x} resolves to {len(matches)} nodes"
        )
    return matches[0]


def _walk(dtb: Path) -> tuple[str, ...]:
    paths: list[str] = ["/"]
    for path in paths:
        for child in _children(dtb, path):
            paths.append(f"/{child}" if path == "/" else f"{path}/{child}")
    return tuple(paths)


def _compatible_nodes(dtb: Path, compatible: str) -> tuple[str, ...]:
    matches: list[str] = []
    for path in _walk(dtb):
        if "compatible" not in _properties(dtb, path):
            continue
        if compatible in _strings(dtb, path, "compatible"):
            matches.append(path)
    return tuple(matches)


def _require_enabled_compatible(dtb: Path, compatible: str, label: str) -> str:
    nodes = _compatible_nodes(dtb, compatible)
    if len(nodes) != 1:
        raise ContractError(
            f"{label} requires exactly one {compatible} node; found {len(nodes)}"
        )
    status = _strings(dtb, nodes[0], "status") if "status" in _properties(dtb, nodes[0]) else ("okay",)
    if status != ("okay",):
        raise ContractError(f"{label} node is not enabled: {nodes[0]}")
    return nodes[0]


def _require_absent(dtb: Path, node: str, prop: str) -> None:
    if prop in _properties(dtb, node):
        raise ContractError(
            f"{node}:{prop} must be absent; it bypasses the accepted pinctrl path"
        )


def _check_gpio_state(
    dtb: Path,
    group: str,
    pinmux: int,
    output_property: str,
    label: str,
) -> None:
    node = f"{PINCTRL}/{group}/pins_cmd_dat"
    if _cells(dtb, node, "pinmux") != (pinmux,):
        raise ContractError(f"{label} has the wrong pinmux; expected 0x{pinmux:x}")
    properties = _properties(dtb, node)
    if output_property not in properties:
        raise ContractError(f"{label} is missing {output_property}")
    opposite = "output-low" if output_property == "output-high" else "output-high"
    if opposite in properties:
        raise ContractError(f"{label} also declares contradictory {opposite}")


def verify_dtb(dtb: Path) -> None:
    dtb = dtb.resolve(strict=True)
    if not shutil.which("fdtget"):
        raise ContractError("fdtget is required")

    top_compat = _strings(dtb, TOPCKGEN, "compatible")
    if top_compat != ("mediatek,mt8163-topckgen", "syscon"):
        raise ContractError(
            "TOPCKGEN must expose mediatek,mt8163-topckgen as a syscon"
        )
    if _strings(dtb, AUDIOSYS, "compatible") != (
        "mediatek,mt8163-audiosys",
        "syscon",
    ):
        raise ContractError("AUDIOSYS must remain a syscon clock provider")

    if _strings(dtb, AFE, "clock-names") != AFE_CLOCK_NAMES:
        raise ContractError("AFE clock-names do not preserve the aud_24m closure")
    clocks = _cells(dtb, AFE, "clocks")
    if clocks[-2:] != (_phandle(dtb, AUDIOSYS), AUDIO_24M_ID):
        raise ContractError("AFE aud_24m clock does not reference AUDIOSYS clock 3")
    if _cells(dtb, AFE, "power-domains") != (
        _phandle(dtb, SCPSYS),
        AUDIO_POWER_DOMAIN_ID,
    ):
        raise ContractError("AFE does not retain MT8163 audio power domain 5")
    if _strings(dtb, AFE, "pinctrl-names") != AFE_PINCTRL_NAMES:
        raise ContractError("AFE pinctrl ordering does not match accepted playback")
    _require_absent(dtb, AFE, "extamp-gpios")
    _require_absent(dtb, AFE, "dacmux-gpios")

    pin_groups = (
        (4, "audexamphigh"),
        (5, "audexamplow"),
        (7, "audexampdacmuxhigh"),
        (8, "audexampdacmuxlow"),
    )
    for index, group in pin_groups:
        expected = _phandle(dtb, f"{PINCTRL}/{group}")
        if _cells(dtb, AFE, f"pinctrl-{index}") != (expected,):
            raise ContractError(f"AFE pinctrl-{index} does not reference {group}")

    _check_gpio_state(dtb, "audexamphigh", 0x7A00, "output-high", "external amp on")
    _check_gpio_state(dtb, "audexamplow", 0x7A00, "output-low", "external amp off")
    _check_gpio_state(
        dtb, "audexampdacmuxhigh", 0x7C00, "output-high", "DAC mux high"
    )
    _check_gpio_state(
        dtb, "audexampdacmuxlow", 0x7C00, "output-low", "DAC mux low"
    )

    _require_enabled_compatible(dtb, "mediatek,mt8163-soc-pcm-dl1", "AFE")
    codec = _require_enabled_compatible(dtb, "ti,tlv320aic32x4", "speaker codec")
    codec_properties = _properties(dtb, codec)
    if "clock-names" not in codec_properties or "clocks" not in codec_properties:
        raise ContractError("codec MCLK binding requires clocks and clock-names")
    if _strings(dtb, codec, "clock-names") != ("mclk",):
        raise ContractError("codec MCLK clock-names must contain exactly mclk")
    codec_clocks = _cells(dtb, codec, "clocks")
    if len(codec_clocks) != 1:
        raise ContractError("codec MCLK must reference exactly one zero-cell provider")
    codec_mclk = _node_for_phandle(dtb, codec_clocks[0])
    if _strings(dtb, codec_mclk, "compatible") != ("fixed-clock",):
        raise ContractError("codec MCLK provider must be a fixed-clock")
    if _cells(dtb, codec_mclk, "#clock-cells") != (0,):
        raise ContractError("codec MCLK provider must have #clock-cells = 0")
    if _cells(dtb, codec_mclk, "clock-frequency") != (CODEC_MCLK_HZ,):
        raise ContractError("codec MCLK must model the physical 9.6 MHz clock")
    _require_enabled_compatible(dtb, "amzn-mtk,spi-audio-pltfm", "microphone FPGA")
    _require_enabled_compatible(dtb, "mediatek,mt8163-consys", "Wi-Fi/CONSYS")
    usb = _require_enabled_compatible(dtb, "mediatek,mt8163-usb20", "USB gadget")
    if "mediatek,mtk-musb" not in _strings(dtb, usb, "compatible"):
        raise ContractError("USB node does not select the MediaTek MUSB glue")
    if _strings(dtb, usb, "dr_mode") != ("peripheral",):
        raise ContractError("USB node is not in peripheral mode")
    if "mc" not in _strings(dtb, usb, "interrupt-names"):
        raise ContractError("USB node is missing the MUSB mc interrupt")
    _require_enabled_compatible(dtb, "issi,is31fl3236", "LED ring")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtb", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify_dtb(args.dtb)
    except (ContractError, OSError) as exc:
        print(f"ERROR: radar-puffin DTB contract failed: {exc}")
        return 1
    print("radar_puffin_dtb_hardware_contract=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
