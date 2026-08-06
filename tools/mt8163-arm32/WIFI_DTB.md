# Pinned MT8163 Wi-Fi candidate DTB

`build_wifi_dtb.py` extracts the exact EVT DTB from the pinned v184 stock
16 MiB boot envelope and adds the following properties:

```dts
clocks = <0x5 0x3>;
clock-names = "bus";
```

It retains the stock PWRAP resource and clock names (the 6.1 driver accepts
both binding spellings), links the TLV320 codec's two required supplies to the
existing always-on `ldo_vio18`, supplies the AFE's MT6323 PMIC phandle, and
adds the fixed 9.6 MHz codec MCLK provider required by the physical
SENINF/camera-MCLK path:

```dts
/soc/imgsys@15000000 {
    status = "okay";
};
/soc/pwrap@1000d000/mt6323 {
    interrupt-parent = <0x09>;
    interrupts = <0x18 0x04>;
    mt6323keys {
        compatible = "mediatek,mt6323-keys";
        power { linux,keycodes = <0x74>; };
        home { linux,keycodes = <0x72>; };
    };
};
/soc/i2c@11009000/tlv320aic32x4@18 {
    iov-supply = <0x37>;
    ldoin-supply = <0x37>;
    clocks = <0x49>;
    clock-names = "mclk";
};
/clocks/puffin_codec_mclk {
    compatible = "fixed-clock";
    #clock-cells = <0>;
    clock-frequency = <0x927c00>; /* 9.6 MHz */
    phandle = <0x49>;
};
/soc/mt_soc_dl1_pcm@11220000 {
    mediatek,pmic = <0x48>;
    /* No extamp-gpios: Radar-Puffin selects GPIO122 by pinctrl. */
    /* No dacmux-gpios: 3.18's missing DAC-mux state is a tolerated no-op. */
};

/* extamp-pullhigh/low each retain the stock GPIO122 state */

The Radar PMIC clock states also contain the I2S1 data/LRCK/BCLK and codec
MCLK pinmux groups as complete composite states. Linux pinctrl selection
replaces the previous state rather than merging state fragments.
```

It deliberately retains the stock three-resource CONSYS tuple, including the
`0x10001000` base used by the active genpd driver's EMI-remap path.  Before
writing an output, the tool validates the source and stock hashes, confirms
both new properties were absent, applies them with `fdtput`, reads the final
values with `fdtget`, enforces the 64 KiB LK limit, and pins the complete final
DTB hash.  It refuses to overwrite an existing output.

```sh
python3 -B tools/mt8163-arm32/build_wifi_dtb.py \
  --source-boot /home/andy/workspace/echo-evidence/v184-stock32-parity/boot-v184-stock32-parity-stock.img \
  --output /tmp/giza-evt-stock-bus-clock.dtb
```

The APLL2-enabled output from dtc tools 1.7.0 is 52,602 bytes:

```text
7b87f4b570897801e1bd4503586b0619349698ac89e9e0ba386287f314dfe671
```

This is a raw DTB input for the recovery-image builder, not a boot image and
not a flashable artifact by itself.
