#!/usr/bin/env python3
"""Tests for the production Radar-Puffin DTB hardware contract."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import verify_radar_puffin_dtb as verifier


VALID_DTS = r'''
/dts-v1/;

/ {
    #address-cells = <1>;
    #size-cells = <1>;

    codec_mclk: puffin-codec-mclk {
        compatible = "fixed-clock";
        #clock-cells = <0>;
        clock-frequency = <9600000>;
    };

    soc {
        #address-cells = <1>;
        #size-cells = <1>;
        ranges;

        topckgen: topckgen@10000000 {
            compatible = "mediatek,mt8163-topckgen", "syscon";
            reg = <0x10000000 0x1000>;
            #clock-cells = <1>;
        };

        scpsys: scpsys@10006000 {
            compatible = "mediatek,mt8163-scpsys", "syscon";
            reg = <0x10006000 0x1000>;
            #power-domain-cells = <1>;
        };

        pio: pinctrl@10005000 {
            compatible = "mediatek,mt8163-pinctrl";
            reg = <0x10005000 0x1000>;
            gpio-controller;
            #gpio-cells = <2>;

            audexamphigh: audexamphigh {
                pins_cmd_dat { pinmux = <0x7a00>; output-high; };
            };
            audexamplow: audexamplow {
                pins_cmd_dat { pinmux = <0x7a00>; output-low; };
            };
            audexampdacmuxhigh: audexampdacmuxhigh {
                pins_cmd_dat { pinmux = <0x7c00>; output-high; };
            };
            audexampdacmuxlow: audexampdacmuxlow {
                pins_cmd_dat { pinmux = <0x7c00>; output-low; };
            };
            pmic_idle: pmic_idle {};
            pmic_active: pmic_active {};
            i2s_idle: i2s_idle {};
            i2s_active: i2s_active {};
            mclk: mclk {};
        };

        pmic: pmic@1000d000 { compatible = "mediatek,mt6323"; reg = <0x1000d000 0x1000>; };
        audiosys: audiosys@11220000 {
            compatible = "mediatek,mt8163-audiosys", "syscon";
            reg = <0x11220000 0x1000>;
            #clock-cells = <1>;
        };
        consys@18070000 {
            compatible = "mediatek,mt8163-consys";
            reg = <0x18070000 0x200>;
            clocks = <&topckgen 3>;
            clock-names = "bus";
            status = "okay";
        };
        usb@11200000 {
            compatible = "mediatek,mt8163-usb20", "mediatek,mtk-musb";
            reg = <0x11200000 0x10000>;
            interrupt-names = "mc";
            dr_mode = "peripheral";
            status = "okay";
        };
        led-controller@3c {
            compatible = "issi,is31fl3236";
            reg = <0x3c 0x1>;
            status = "okay";
        };
        codec@18 {
            compatible = "ti,tlv320aic32x4";
            reg = <0x18 0x1>;
            clocks = <&codec_mclk>;
            clock-names = "mclk";
            status = "okay";
        };
        spi-audio@0 {
            compatible = "amzn-mtk,spi-audio-pltfm";
            reg = <0 1>;
            status = "okay";
        };
        afe: mt_soc_dl1_pcm@11220000 {
            compatible = "mediatek,mt8163-soc-pcm-dl1";
            reg = <0x11220000 0x1000>;
            clocks = <&topckgen 0>, <&topckgen 1>, <&topckgen 2>,
                     <&topckgen 3>, <&topckgen 4>, <&topckgen 5>,
                     <&topckgen 6>, <&topckgen 7>, <&audiosys 3>;
            clock-names = "aud_infra_clk", "top_mux_audio",
                          "top_mux_audio_intbus", "aud_mux1_clk",
                          "aud_mux2_clk", "apmixed_apll1_clk",
                          "apmixed_apll2_clk", "top_clk26m_clk",
                          "aud_24m_clk";
            power-domains = <&scpsys 5>;
            mediatek,pmic = <&pmic>;
            pinctrl-names = "audpmicclk-speaker-mode0",
                            "audpmicclk-speaker-mode1",
                            "audi2s1-speaker-mode0",
                            "audi2s1-speaker-mode1",
                            "extamp-pullhigh", "extamp-pulllow",
                            "cmmclk-mclk", "extamp-dacmux-pullhigh",
                            "extamp-dacmux-pulllow";
            pinctrl-0 = <&pmic_idle>;
            pinctrl-1 = <&pmic_active>;
            pinctrl-2 = <&i2s_idle>;
            pinctrl-3 = <&i2s_active>;
            pinctrl-4 = <&audexamphigh>;
            pinctrl-5 = <&audexamplow>;
            pinctrl-6 = <&mclk>;
            pinctrl-7 = <&audexampdacmuxhigh>;
            pinctrl-8 = <&audexampdacmuxlow>;
            status = "okay";
        };
    };
};
'''


@unittest.skipUnless(shutil.which("dtc") and shutil.which("fdtget"), "dtc tools required")
class RadarPuffinDtbTests(unittest.TestCase):
    def compile_dts(self, source: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        dts = root / "fixture.dts"
        dtb = root / "fixture.dtb"
        dts.write_text(source, encoding="utf-8")
        subprocess.run(
            ["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(dtb), str(dts)],
            check=True,
        )
        return dtb

    def test_accepts_complete_audio_and_hardware_contract(self) -> None:
        verifier.verify_dtb(self.compile_dts(VALID_DTS))

    def test_accepts_composite_audio_pinctrl_states(self) -> None:
        source = VALID_DTS
        for old, new in (
            ("pinctrl-0 = <&pmic_idle>;", "pinctrl-0 = <&pmic_idle &mclk>;"),
            ("pinctrl-1 = <&pmic_active>;", "pinctrl-1 = <&pmic_active &mclk>;"),
            ("pinctrl-2 = <&i2s_idle>;", "pinctrl-2 = <&i2s_idle &mclk>;"),
            ("pinctrl-3 = <&i2s_active>;", "pinctrl-3 = <&i2s_active &mclk>;"),
            ("pinctrl-4 = <&audexamphigh>;", "pinctrl-4 = <&audexamphigh &mclk>;"),
            ("pinctrl-5 = <&audexamplow>;", "pinctrl-5 = <&audexamplow &mclk>;"),
            ("pinctrl-7 = <&audexampdacmuxhigh>;", "pinctrl-7 = <&audexampdacmuxhigh &mclk>;"),
            ("pinctrl-8 = <&audexampdacmuxlow>;", "pinctrl-8 = <&audexampdacmuxlow &mclk>;"),
        ):
            source = source.replace(old, new)
        verifier.verify_dtb(self.compile_dts(source))

    def test_accepts_other_pinctrl_state_in_audio_composite(self) -> None:
        source = VALID_DTS.replace(
            "            mclk: mclk {};",
            "            mclk: mclk {};\n            camdefault: camdefault {};",
        ).replace(
            "pinctrl-4 = <&audexamphigh>;",
            "pinctrl-4 = <&audexamphigh &camdefault>;",
        )
        verifier.verify_dtb(self.compile_dts(source))

    def test_rejects_conflicting_external_amp_pinctrl_states(self) -> None:
        dtb = self.compile_dts(VALID_DTS.replace(
            "pinctrl-4 = <&audexamphigh>;",
            "pinctrl-4 = <&audexamphigh &audexamplow>;",
        ))
        with self.assertRaisesRegex(verifier.ContractError, "contradictory audexamplow"):
            verifier.verify_dtb(dtb)

    def test_rejects_non_pinctrl_composite_reference(self) -> None:
        dtb = self.compile_dts(VALID_DTS.replace(
            "pinctrl-4 = <&audexamphigh>;",
            "pinctrl-4 = <&audexamphigh &codec_mclk>;",
        ))
        with self.assertRaisesRegex(verifier.ContractError, "not a pinctrl state"):
            verifier.verify_dtb(dtb)

    def test_rejects_topckgen_without_syscon(self) -> None:
        dtb = self.compile_dts(
            VALID_DTS.replace(
                'compatible = "mediatek,mt8163-topckgen", "syscon";',
                'compatible = "mediatek,mt8163-topckgen";',
            )
        )
        with self.assertRaisesRegex(verifier.ContractError, "TOPCKGEN"):
            verifier.verify_dtb(dtb)

    def test_rejects_direct_amp_gpio_that_bypasses_accepted_pinctrl(self) -> None:
        dtb = self.compile_dts(
            VALID_DTS.replace(
                '            pinctrl-names = "audpmicclk-speaker-mode0",',
                '            extamp-gpios = <&pio 122 0>;\n'
                '            pinctrl-names = "audpmicclk-speaker-mode0",',
            )
        )
        with self.assertRaisesRegex(verifier.ContractError, "extamp-gpios"):
            verifier.verify_dtb(dtb)

    def test_rejects_missing_audio_24m_clock(self) -> None:
        dtb = self.compile_dts(
            VALID_DTS.replace('"aud_24m_clk";', '"bad_clock";')
        )
        with self.assertRaisesRegex(verifier.ContractError, "aud_24m"):
            verifier.verify_dtb(dtb)

    def test_rejects_codec_without_physical_mclk_binding(self) -> None:
        dtb = self.compile_dts(
            VALID_DTS.replace(
                '            clocks = <&codec_mclk>;\n'
                '            clock-names = "mclk";\n',
                "",
            )
        )
        with self.assertRaisesRegex(verifier.ContractError, "codec MCLK"):
            verifier.verify_dtb(dtb)

    def test_rejects_wrong_physical_codec_mclk_rate(self) -> None:
        dtb = self.compile_dts(
            VALID_DTS.replace("clock-frequency = <9600000>;", "clock-frequency = <26000000>;")
        )
        with self.assertRaisesRegex(verifier.ContractError, "9.6 MHz"):
            verifier.verify_dtb(dtb)

    def test_rejects_wrong_external_amp_pin(self) -> None:
        dtb = self.compile_dts(VALID_DTS.replace('pinmux = <0x7a00>', 'pinmux = <0x1c00>'))
        with self.assertRaisesRegex(verifier.ContractError, "external amp"):
            verifier.verify_dtb(dtb)


if __name__ == "__main__":
    unittest.main()
