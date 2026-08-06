# LibreEcho MT8163 ARM32 Development OS Base

This directory builds the first verified ARM32 LibreEcho OS foundation. It is
the development control plane for building the final OS: a production-directed
ARM32 kernel plus a deliberately small bring-up userspace with root ADB,
fastboot escape, rollback protection, and staged connectivity tools.

It is **not** a disposable recovery image and it is not yet the finished OS.
Android `init` and stock static ARM32 `adbd` remain temporarily because they
provide a proven control plane while native LibreEcho services are added. Some
internal builder, verifier, service, and manifest names still contain
`recovery`; those compatibility names must be migrated atomically later rather
than renamed piecemeal.

For the normal edit/build/flash loop, root access, panic handling, fastboot
semantics, and the path toward the final OS, read
[ITERATION.md](ITERATION.md).

The image combines:

* the reviewed ARM32 `zImage` from `mt8163_arm32_defconfig`;
* the proven Android-v0 and MediaTek `KERNEL` envelope from the stock ARM32
  image;
* the pinned MT8163 EVT Wi-Fi DTB with the required CONSYS `bus` clock;
* the stock static ARM32 Android `init` and `adbd` binaries;
* ARM32 musl BusyBox, its loader, and 304 relative applet links; and
* the audited v97 recovery flow in `initramfs/libreecho-init`.

The builder pins every borrowed binary by SHA-256.  It also rejects non-ARM32
ELF files, a dynamic `init` or `adbd`, the wrong BusyBox interpreter, an
un-pinned DTB, an invalid zImage range, and overlapping physical ranges.
`qemu-arm-static` is used only at build time to obtain the applet list from the
pinned ARM32 BusyBox binary; the resulting target symlinks are then verified.

## Development control-plane behavior

Android `init` remains PID 1 temporarily so the stock property-backed root ADB
behavior is preserved. The `libreecho-recovery` compatibility service performs
these operations:

1. creates direct MMC aliases from sysfs, proves partition 7 is the expected
   20,480-sector `expdb`, writes exactly `FASTBOOT_PLEASE`, and reads it back;
2. creates the stable WMT aliases, leaving dynamically allocated `btif` to
   ueventd/devtmpfs;
3. starts the `/tmp/runme` to `/tmp/result` root command loop;
4. waits two seconds before configuring FunctionFS, starts ARM32 `adbd` with
   `--device_banner=device`, waits three seconds for descriptor publication,
   then enables the gadget and verifies FunctionFS endpoint readiness; and
5. stays alive even if ADB or an optional driver fails.

The ramdisk contains no HPS, CPU-online, cpufreq, or cpuidle forcing.  Those
workarounds can hide the real SMP and power-management state and are not part
of the recovery contract.

## Build the pinned ADB-parity candidate

The following inputs identify the reviewed candidate:

| Input | SHA-256 |
| --- | --- |
| stock 16 MiB boot envelope | `c0f52a3b079d214495cd3dd22f92fd85695d1b868c58b491a2edb933bc4f6d1a` |
| ARM32 zImage | `4e144959eb0ffaee91b37d05a0f871863a74f4abb1bad0474c2fec358d5176a6` |
| System.map | `527292112edd28e8facf2998eefe2224b08a05b193efc73634cd998e9113ba95` |
| ARM32 BusyBox | `d4c8fd2aea01abd851c703f39b29c0de748b2751e4e1a85cae570fa53ad8f4fb` |
| ARM32 musl loader | `1063871174f1bd4f08f4d330e20b07aeb0820327ee739a4d8d1b644df842cb6b` |

From the kernel repository root:

```sh
python3 tools/mt8163-arm32/build_recovery_image.py \
  --source-boot /home/andy/workspace/echo-evidence/v184-stock32-parity/boot-v184-stock32-parity-stock.img \
  --stock-root /home/andy/workspace/echo-evidence/v184-stock32-parity/rootadb-ramdisk-verify \
  --busybox /home/andy/.local/var/pmbootstrap/chroot_rootfs_amazon-radar/usr/bin/busybox \
  --musl-loader /home/andy/.local/var/pmbootstrap/chroot_rootfs_amazon-radar/usr/lib/ld-musl-armhf.so.1 \
  --zimage /tmp/libreecho-arm32-build.IJwk7v/arch/arm/boot/zImage \
  --system-map /tmp/libreecho-arm32-build.IJwk7v/System.map \
  --output /tmp/libreecho-arm32-recovery-v7.img
```

The reviewed build is reproducible byte-for-byte.  Its outputs are:

| Output | Size | SHA-256 |
| --- | ---: | --- |
| `libreecho-arm32-recovery-v7.img` | 16,777,216 | `1cf0ce6a7a80ad798e9d1675d57eb48a41cf80d705cd37f32d6a0bc3aedd30d4` |
| `libreecho-arm32-recovery-v7.ramdisk.cpio.gz` | 1,933,708 | `0306a285d634411f99d9937d9de52851f7024e11dd38a7164f2996455c98cfb6` |

Verify it independently:

```sh
python3 tools/mt8163-arm32/verify_recovery_image.py \
  --source-boot /home/andy/workspace/echo-evidence/v184-stock32-parity/boot-v184-stock32-parity-stock.img \
  --zimage /tmp/libreecho-arm32-build.IJwk7v/arch/arm/boot/zImage \
  --system-map /tmp/libreecho-arm32-build.IJwk7v/System.map \
  --ramdisk /tmp/libreecho-arm32-recovery-v7.ramdisk.cpio.gz \
  --manifest /tmp/libreecho-arm32-recovery-v7.manifest.json \
  --boot-image /tmp/libreecho-arm32-recovery-v7.img \
  --expected-boot-sha256 1cf0ce6a7a80ad798e9d1675d57eb48a41cf80d705cd37f32d6a0bc3aedd30d4 \
  --expected-connectivity-bundle none
```

Expected final line:

```text
arm32_recovery_image_contract=PASS android_v0=yes mtk_wrapper=yes zimage=yes evt_dtb=yes initramfs_arm32=yes fastboot_marker=yes root_adb_staged=yes runme=yes memory_disjoint=yes connectivity_bundle=no activation=manual-only status=PREPARED_NOT_FLASHED
```

## Historical v7 boot envelope

The verifier checks these disjoint physical ranges:

| Component | Range |
| --- | --- |
| loaded zImage plus padded DTB | `0x40008000-0x406c7b08` |
| decompressed kernel through BSS | ends at `0x40fb6784` |
| ATF reservation | `0x43000000-0x43030000` |
| compressed recovery ramdisk | `0x43478000-0x4365018c` |
| RAM console | starts at `0x44400000` |
| FDT handoff | `0x48000000-0x48010000` |

Loading this larger ramdisk at the stock ARM32 address `0x44000000` would put
it too close to, or into, the reserved RAM-console range.  The v97 address
`0x43478000` is therefore an explicit part of the image contract.

## ADB-parity device gates

The image is marked `PREPARED_NOT_FLASHED`.  Before advancing to connectivity,
capture all of the following from one boot:

1. UART reaches `fastboot-please-written` (which is emitted only after expdb
   identity and marker read-back pass), `functionfs-mounted`, `adbd-started`,
   `android-usb-enabled`, `functionfs-ready`, and
   `init-ready-pid1-managed`.
2. host `adb get-state` reports `device`, and ADB push/pull both work.
3. A pushed `/tmp/runme` produces `/tmp/result` as UID 0; interactive
   `adb shell` is not the acceptance path.
4. A controlled reboot returns to fastboot through the expdb marker.
5. The opposite boot slot remains untouched as rollback.

The Amonet shadow-slot wrapper is a separate artifact and is not generated or
flashed by these tools.

## Wi-Fi DTB stage

Omitting `--dtb` deliberately selects the exact stock EVT DTB for the ADB
parity gate.  The canonical pipeline now generates and supplies the pinned
Wi-Fi DTB by default; direct builder users must explicitly pass the Wi-Fi DTB
when testing connectivity.

The current source-built `giza_evt.dtb` is 66,135 bytes, which exceeds LK's
proven 64 KiB appended-FDT envelope by 599 bytes.  It must not be forced into
this image.

`build_wifi_dtb.py` instead extracts the pinned 51,317-byte stock EVT DTB,
retains its verified resource-2 base at `0x10001000` (so the driver's `+0x320`
EMI-remap access reaches `0x10001320`), and adds only the named
`CLK_INFRA_PMIC_CONN` `bus` clock, enables the 6.1 image-clock provider, and
adds the TLV320 regulator links.  The resulting raw DTB is 51,405 bytes and has pinned
SHA-256
`21bda31ad6acadcbe3df7439f1ba53976f2e7b9f553a46bef753d26448dda18b`.
See [WIFI_DTB.md](WIFI_DTB.md) for the reproducible command and the complete
fail-closed contract.

Supply that output to the recovery builder with both `--dtb` and
`--expected-dtb-sha256`.  Firmware activation remains a later, explicit gate:
first prove recovery/ADB stability, then CONSYS and BT-only stability, and only
then attempt one bounded Wi-Fi function-on operation.

The recovery image can now be built as a stable network-stack iteration by
supplying a private WPA profile. The profile is never committed; the generated
manifest records only its size and SHA-256. The image includes a static ARM32
`wpa_supplicant` 2.10, `libreecho-wifi`, and the BusyBox `udhcpc` lease hook.
When `/etc/wifi/wpa_supplicant.conf` is present, `libreecho-init` waits until
ADB/FunctionFS reaches `init-ready-pid1-managed`, then performs the proven
manual sequence recovered from session `e8c21eb3bbae`:

```text
ADB ready -> wmt_stock_compat --no-function-on
-> remove stale wmt_launcher processes
-> one wmt_launcher --device /dev/wmt --ok --once
-> one timed /dev/wmtWifi=1 -> wlan0
-> wpa_supplicant association -> udhcpc -> route/DNS
```

The automatic path deliberately does **not** invoke the stock
`/system/vendor/bin/wmt_loader`; that substitution returned 255 before creating
`wlan0` in the ARM32 image, while the manual path reached association, DHCP
lease `192.168.0.125/24`, gateway `192.168.0.1`, gateway ping, and external HTTP
verification. The stock loader and older configure/responder helpers remain
packaged for separately bounded diagnostic gates.

The current pinned `wpa_supplicant` is station-only; it explicitly reports
`AP mode support not included in the build`. Therefore this image does not yet
claim the future default AP/setup-page behavior. That requires a verified ARM32
`hostapd`/AP-capable wireless control binary plus a DHCP/DNS service and a small
HTTP setup service. The present profile is a static development station config,
kept private by the pipeline.


Bundle `mt8163-v181-stock-v1` adds the exact v181 Bionic runtime, WMT tools,
firmware, and three narrow gate helpers to the recovery image.  It does not
install the stock `init.connectivity.rc`, start WMT, or activate BT or Wi-Fi.
The builder and independent verifier require all 13 stock files and all three
helpers by size, SHA-256, ELF ABI, interpreter, and ordered dependencies.

The reviewed inputs are:

| Input | Size | Identity (SHA-256 unless noted) |
| --- | ---: | --- |
| kernel input commit | - | Git `88b8cf66f34d7cc52317e4bed6ac71caf43b0204` |
| ARM32 zImage | 7,011,040 | `fa6717058fa25337dfbd63be52bafda18552ecd7361fe3df9a78c3715e3e6718` |
| System.map | 2,820,781 | `1700f451d7931269974153749a9ab860906ffd3a86fc28a2f67e0b257b16c9d2` |
| kernel `.config` | - | `9cf3ca49533904b41423308bf3309cbe1ff4244df895bd03563813d6d48cd8c8` |
| pinned Wi-Fi/audio DTB | 51,405 | `21bda31ad6acadcbe3df7439f1ba53976f2e7b9f553a46bef753d26448dda18b` |
| stock v181 `system_a` provenance | - | `56540b3a9ac4437901a5510d9fb5e09b1a8d0cc229548f0b08bb5c22d78684fe` |
| extracted evidence manifest | - | `d1eedd04efe0dbc78853f2b0f9357c092b4ca66242648908c0369956538441eb` |

Build and check the static helpers first:

```sh
make -C tools/mt8163-arm32/connectivity \
  OUT_DIR=/tmp/libreecho-mt8163-connectivity all check
```

Then build the opt-in image:

```sh
python3 -B tools/mt8163-arm32/build_recovery_image.py \
  --source-boot /home/andy/workspace/echo-evidence/v184-stock32-parity/boot-v184-stock32-parity-stock.img \
  --stock-root /home/andy/workspace/echo-evidence/v184-stock32-parity/rootadb-ramdisk-verify \
  --busybox /home/andy/.local/var/pmbootstrap/chroot_rootfs_amazon-radar/usr/bin/busybox \
  --musl-loader /home/andy/.local/var/pmbootstrap/chroot_rootfs_amazon-radar/usr/lib/ld-musl-armhf.so.1 \
  --connectivity-stock-root /home/andy/workspace/echo-evidence/v181-stock-userspace-layer/stock-root \
  --wmt-config-helper /tmp/libreecho-mt8163-connectivity/wmt_configure \
  --wmt-responder /tmp/libreecho-mt8163-connectivity/wmt_responder \
  --wmt-bt-on /tmp/libreecho-mt8163-connectivity/wmt_bt_on \
  --zimage /tmp/libreecho-arm32-wifi.final88b8/arch/arm/boot/zImage \
  --expected-zimage-sha256 fa6717058fa25337dfbd63be52bafda18552ecd7361fe3df9a78c3715e3e6718 \
  --system-map /tmp/libreecho-arm32-wifi.final88b8/System.map \
  --expected-system-map-sha256 1700f451d7931269974153749a9ab860906ffd3a86fc28a2f67e0b257b16c9d2 \
  --dtb /tmp/giza-evt-stock-bus-clock-canonical.dtb \
  --expected-dtb-sha256 d5e8b62e14956fb6402c510bfbc784e2e82479daa3183c32cac1e7bc139e9f04 \
  --output /tmp/libreecho-arm32-connectivity-mt8163-v181-stock-v1.img
```

The staged payload is 4,188,667 uncompressed bytes.  Two independent builds
produced byte-identical outputs:

| Output | Size | SHA-256 |
| --- | ---: | --- |
| boot image | 16,777,216 | `f7c37f74aefe1772897992f20c45732aee629c7c07cfd0e9297dacc166e94ded` |
| recovery ramdisk | 4,117,953 | `a4d9a98e6b23399f9efa651fc2da97f5318cc9ef123ae692f4f65046083dd751` |

Verify it with an explicit bundle expectation:

```sh
python3 -B tools/mt8163-arm32/verify_recovery_image.py \
  --source-boot /home/andy/workspace/echo-evidence/v184-stock32-parity/boot-v184-stock32-parity-stock.img \
  --zimage /tmp/libreecho-arm32-wifi.final88b8/arch/arm/boot/zImage \
  --expected-zimage-sha256 fa6717058fa25337dfbd63be52bafda18552ecd7361fe3df9a78c3715e3e6718 \
  --system-map /tmp/libreecho-arm32-wifi.final88b8/System.map \
  --expected-system-map-sha256 1700f451d7931269974153749a9ab860906ffd3a86fc28a2f67e0b257b16c9d2 \
  --ramdisk /tmp/libreecho-arm32-connectivity-mt8163-v181-stock-v1.ramdisk.cpio.gz \
  --manifest /tmp/libreecho-arm32-connectivity-mt8163-v181-stock-v1.manifest.json \
  --boot-image /tmp/libreecho-arm32-connectivity-mt8163-v181-stock-v1.img \
  --expected-boot-sha256 f7c37f74aefe1772897992f20c45732aee629c7c07cfd0e9297dacc166e94ded \
  --expected-dtb-sha256 d5e8b62e14956fb6402c510bfbc784e2e82479daa3183c32cac1e7bc139e9f04 \
  --expected-connectivity-bundle mt8163-v181-stock-v1
```

The staged envelope remains disjoint: the compressed ramdisk occupies
`0x43478000-0x438655c1`, below the `0x44000000` limit and RAM console at
`0x44400000`.  Its manifest and verifier report:

```text
status=PREPARED_NOT_FLASHED
autostart=no
activation=manual-gates-only
```

No target action is automatic.  Follow the fresh-boot Gates 0 through 5 in
[connectivity/README.md](connectivity/README.md); never retry a failed
activation in the same boot.
