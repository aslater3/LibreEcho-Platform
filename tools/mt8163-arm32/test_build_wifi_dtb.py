#!/usr/bin/env python3
"""Regression tests for the legacy DTB transformer."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import build_wifi_dtb as transformer


LEGACY_GROUPS = tuple(
    sorted(
        {
            path.removeprefix(f"{transformer.PINCTRL_NODE}/").split("/", 1)[0]
            for path in transformer.PINCTRL_LEGACY_PINS_NODES
        }
    )
)



def _pinctrl_fixture() -> str:
    slew_groups = {
        "audexamphigh",
        "audexamplow",
        "audexampdacmuxhigh",
        "audexampdacmuxlow",
    }
    leaves: dict[str, set[str]] = {group: set() for group in LEGACY_GROUPS}
    for path in transformer.PINCTRL_LEGACY_PINS_NODES:
        group, leaf = path.removeprefix(f"{transformer.PINCTRL_NODE}/").split("/", 1)
        leaves[group].add(leaf)

    lines = [
        "        pio: pinctrl@10005000 {",
        '            compatible = "mediatek,mt8163-pinctrl";',
        "            reg = <0x10005000 0x1000>;",
        "            gpio-controller;",
        "            #gpio-cells = <2>;",
        "            phandle = <0x155>;",
        "            linux,phandle = <0x155>;",
        '            pinctrl-names = "default";',
        "            pinctrl-0 = <0x101>;",
    ]
    for index, group in enumerate(LEGACY_GROUPS, start=0x101):
        lines.extend(
            [
                f"            {group} {{",
                f"                phandle = <0x{index:x}>;",
                f"                linux,phandle = <0x{index:x}>;",
            ]
        )
        for leaf in sorted(leaves[group]):
            lines.append(f"                {leaf} {{")
            lines.append("                    pins = <0x1234 0x5678>;")
            if group in slew_groups:
                lines.append("                    slew-rate = <1>;")
            lines.append("                };")
        lines.append("            };")
    lines.append("        };")
    return "\n".join(lines)


FIXTURE_DTS = """/dts-v1/;

/ {
    #address-cells = <1>;
    #size-cells = <1>;

    clocks { };

    soc {
        #address-cells = <1>;
        #size-cells = <1>;
        ranges;

        topckgen@10000000 {
            compatible = "mediatek,mt8163-topckgen";
            reg = <0x10000000 0x1000>;
        };
        infracfg@10001000 {
            phandle = <0x05>;
            linux,phandle = <0x05>;
            reg = <0x10001000 0x1000>;
        };
        toprgu@10007000 {
            compatible = "legacy-rgu";
            interrupts = <0x01 0x02 0x04>;
        };
        scpsys@10006000 {
            phandle = <0x200>;
            linux,phandle = <0x200>;
            #power-domain-cells = <1>;
        };

        pwrap@1000d000 {
            reg-names = "pwrap-base";
            clock-names = "spi pwrap";
            mt6323 {
                regulators {
                    ldo_vio18 {
                        phandle = <0x37>;
                        linux,phandle = <0x37>;
                    };
                };
            };
        };

        imgsys@15000000 {
            status = "disabled";
        };
        audiosys@11220000 {
            compatible = "mediatek,mt8163-audiosys", "syscon";
        };
        consys@18070000 {
            reg = <0x00 0x18070000 0x00 0x200
                   0x00 0x10007000 0x00 0x100
                   0x00 0x10001000 0x00 0x1000>;
        };

        i2c@11009000 {
            tlv320aic32x4@18 {
                compatible = "ti,tlv320aic32x4";
                status = "okay";
            };
        };
        spi@1100a000 {
            spi@0 {
                pinctrl-names = "default", "audpmicclk-mode0", "audpmicclk-mode1",
                                "audi2s1-mode0", "audi2s1-mode1", "extamp-pullhigh",
                                "extamp-pulllow", "cmmclk-mclk";
                pinctrl-0 = <0x101>;
                pinctrl-1 = <0x101>;
                pinctrl-2 = <0x101>;
                pinctrl-3 = <0x101>;
                pinctrl-4 = <0x101>;
                pinctrl-5 = <0x101>;
                pinctrl-6 = <0x101>;
                pinctrl-7 = <0x101>;
            };
        };
        usb@11200000 {
            compatible = "mediatek,mt8163-usb20", "mediatek,mtk-musb";
            status = "okay";
        };

        mt_soc_dl1_pcm@11220000 {
            compatible = "mediatek,mt8163-soc-pcm-dl1";
            status = "okay";
            clocks = <1 2 3 4 5 6 7 8>;
            clock-names = "aud_infra_clk", "top_mux_audio",
                          "top_mux_audio_intbus", "aud_mux1_clk",
                          "aud_mux2_clk", "apmixed_apll1_clk",
                          "apmixed_apll2_clk", "top_clk26m_clk";
            power-domains = <0x200 0x05>;
            pinctrl-names = "default", "old0", "old1", "old2", "old3",
                            "old4", "old5", "old6", "old7", "old8";
            pinctrl-0 = <0x101>;
            pinctrl-1 = <0x101>;
            pinctrl-2 = <0x101>;
            pinctrl-3 = <0x101>;
            pinctrl-4 = <0x101>;
            pinctrl-5 = <0x101>;
            pinctrl-6 = <0x101>;
            pinctrl-7 = <0x101>;
            pinctrl-8 = <0x101>;
            pinctrl-9 = <0x101>;
            audclk-gpio = <1>;
            audmiso-gpio = <1>;
            audmosi-gpio = <1>;
            extspkamp-gpio = <1>;
            i2s1clk-gpio = <1>;
            i2s1dat-gpio = <1>;
            i2s1mclk-gpio = <1>;
            i2s1ws-gpio = <1>;
        };
""" + _pinctrl_fixture() + "\n    };\n};\n"


class WifiDtbTransformerTests(unittest.TestCase):
    def compile_fixture(self, root: Path) -> Path:
        source = root / "fixture.dts"
        output = root / "fixture.dtb"
        source.write_text(FIXTURE_DTS, encoding="utf-8")
        subprocess.run(
            ["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(output), str(source)],
            check=True,
        )
        return output

    def test_transformer_links_final_named_provider_phandles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="libreecho-wifi-transform-") as temporary:
            root = Path(temporary)
            stock = self.compile_fixture(root)
            source_boot = root / "source.img"
            output = root / "wifi-evt.dtb"
            source_boot.write_bytes(b"synthetic source envelope")
            seen_sha256 = ""
            seen_size = 0

            def verify_transformed(fdtget: str, dtb: Path, data: bytes) -> int:
                nonlocal seen_sha256, seen_size
                refs = (
                    ("pinctrl-4", "audexamphigh"),
                    ("pinctrl-5", "audexamplow"),
                    ("pinctrl-6", "camdefault"),
                    ("pinctrl-7", "audexampdacmuxhigh"),
                    ("pinctrl-8", "audexampdacmuxlow"),
                )
                for property_name, group in refs:
                    provider = transformer.fdt_phandle(
                        fdtget, dtb, f"{transformer.PINCTRL_NODE}/{group}"
                    )
                    actual = transformer.fdt_hex_cells(
                        fdtget, dtb, transformer.AFE_NODE, property_name
                    )
                    self.assertEqual(actual, (provider,), property_name)
                pio = transformer.fdt_phandle(fdtget, dtb, transformer.PIO_NODE)
                self.assertNotEqual(pio, 0x09)
                self.assertEqual(
                    transformer.fdt_hex_cells(
                        fdtget, dtb, transformer.ACTION_BUTTON_NODE, "gpios"
                    ),
                    (pio, transformer.ACTION_GPIO, transformer.ACTION_GPIO_FLAGS),
                )
                self.assertEqual(
                    transformer.fdt_hex_cells(
                        fdtget, dtb, transformer.ACTION_BUTTON_NODE, "linux,code"
                    ),
                    (transformer.ACTION_KEYCODE,),
                )
                self.assertEqual(
                    transformer.fdt_hex_cells(
                        fdtget, dtb, transformer.MT6323_POWER_KEY_NODE, "linux,keycodes"
                    ),
                    transformer.MUTE_KEYCODE,
                )
                self.assertEqual(
                    transformer.fdt_hex_cells(
                        fdtget, dtb, transformer.MT6323_NODE, "interrupt-parent"
                    ),
                    (pio,),
                )
                seen_sha256 = hashlib.sha256(data).hexdigest()
                seen_size = len(data)
                return transformer.fdt_totalsize(data, "synthetic Wi-Fi EVT DTB")

            with mock.patch.object(transformer, "extract_stock_evt", return_value=stock.read_bytes()), \
                    mock.patch.object(transformer, "verify_stock"), \
                    mock.patch.object(transformer, "verify_wifi", side_effect=verify_transformed):
                with mock.patch.object(sys, "argv", [
                    "build_wifi_dtb.py",
                    "--source-boot", str(source_boot),
                    "--output", str(output),
                ]):
                    transformer.main()

            self.assertTrue(output.is_file())
            self.assertGreater(seen_size, len(stock.read_bytes()))
            self.assertGreater(
                transformer.fdt_phandle(
                    "fdtget", output, f"{transformer.PINCTRL_NODE}/audexamphigh"
                ),
                0x100,
            )
            self.assertNotEqual(seen_sha256, "")


if __name__ == "__main__":
    unittest.main()
