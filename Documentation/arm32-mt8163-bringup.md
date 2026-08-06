# MT8163 ARM32 bring-up status

This document records what has been demonstrated on the target and what is
still required.  In particular, it must not be read as a claim that `wlan0`
works yet.

## Verified kernel entry and `setup_arch()`

The deployed ARM32 image produced this uninterrupted UART marker suffix:

```
HIPVTQUMBWSLDopfmeysbgrucixzAERT
```

The markers before the lower-case section cover decompression, ARM head code,
processor lookup, FDT validation, initial page tables, MMU enable, execution at
the linked virtual address, BSS clearing, entry to `start_kernel()`, and its
first C setup.  The lower-case section brackets the major `setup_arch()`
operations.  Reaching `AERT`, followed by normal printk output, demonstrates
that `setup_arch()` returned and early IRQ/time/console setup continued.

The same boot printed all of the following:

* Linux 3.18.140 running as ARMv7 on physical CPU 0;
* machine model `MT8163` selected from the supplied FDT;
* the expected RAM and reserved-memory layout;
* PSCI v0.1 selected from DT;
* the architected timer and MediaTek GPT initialized; and
* `ttyMT0` and the RAM console enabled.

This moves the known failure boundary beyond decompression, MMU entry, FDT
selection, paging, memblock, and `setup_arch()`.  The initramfs hand-off was
also valid in this test (`0x44000000` through `0x443afe02`, with gzip magic
`1f 8b 08 00`).

## MTEE AArch32 ABI correction

The next reproducible failure was not a generic ARM32 entry problem.  MTEE
logged `tz_client_init: TZ Failed -1`, after which `mtee_probe()` deliberately
called `BUG()`.

Cross-checking the generated AArch32 call sequence with the working firmware
ABI identified the cause.  The old AArch32 path put the system session handle
`0xffff1234` in `r0`; ATF therefore interpreted it as the SMC function ID and
returned `SMC_UNK` (`0xffffffff`).  MT8163 must use the modern MTEE convention:

```
r0 = 0x32000008              /* SMC_MTEE_SERVICE_CALL */
r1 = handle
r2 = operation
r3 = argument/type word
r4 = argument pointer/value
r5 = REE service buffer
smc #0
```

The ARM32 implementation must bind these values to `r0` through `r5`, stop the
REE-service loop on `SMC_UNK`, and propagate that result.  MTEE probe failure is
also made nonfatal: the partially registered character device is cleaned up
and probe returns `-ENODEV` instead of panicking the kernel.  This keeps an
optional secure service from preventing recovery and further driver bring-up.

## Branch baseline

The current work is preserved on `agent/arm32-v97-wlan`.  The reviewed kernel
input is commit `88b8cf66f34d7cc52317e4bed6ac71caf43b0204`; it descends from
`golden-v97` and retains the later CONSYS, BTIF, WLAN HIF,
firmware-download, and STP/WMT diagnostic work.  The successful
`HIPVTQUMBWSLDopfmeysbgrucixzAERT` trace above demonstrates the ARM32 entry
fix.  The newer connectivity candidate built from `88b8cf66` is separately
marked `PREPARED_NOT_FLASHED`, so its presence is not evidence that BT or
`wlan0` works.

## Recovery userspace contract

The v97 recovery environment is the behavioral reference for fast iteration.
The ARM32 image should preserve this contract:

1. Provide ARM-EABI BusyBox and a root-capable ARM-EABI `adbd`.
2. Mount the minimal `/proc`, `/sys`, and `/dev` filesystems and create the
   required MMC, WMT, and USB device nodes.  Keep `CONFIG_DEVTMPFS` enabled
   and mount devtmpfs from initramfs so dynamically allocated devices such as
   BTIF are not lost.
3. Write `FASTBOOT_PLEASE` to `expdb` (`/dev/mmcblk0p7` in the v97 layout) as
   early as possible, before optional hardware probes.  A failed boot can then
   return to fastboot for another image.
4. Mount FunctionFS, wait for USB to settle, start `adbd`, then enable the USB
   gadget.  Keep the settle delays that made v97 reliable.
5. Retain the `/tmp/runme` root command loop for rapid experiments.
6. Keep PID 1 alive even when an optional service or device probe fails.

The exact v97 ramdisk cannot be installed verbatim in an ARM32 boot image.  Its
BusyBox and `adbd` are AArch64 ELF binaries (and BusyBox expects
`/sbin/linker64`), so an ARM32 kernel would reject them.  Reuse the audited init
flow, firmware, configuration, and recovery semantics, but replace executable
userspace with ARM-EABI builds.

Reference artifacts are identified by SHA-256:

| Artifact | SHA-256 |
| --- | --- |
| `pmos-boot-v97-ffs-adb.img` | `7d487a5cbdc6ba6acaf1da40233f379fd4cf37830c98530b95001760d33f2ace` |
| `v97-ffs-adb-ramdisk.cpio.gz` | `f4275de87b3b1c685ac7e2b9c992af1516b455c78f646d93352c61919458c54f` |
| `v97-ffs-adb-ramdisk-wifi.cpio.gz` | `d40a0b6d693c876ecd6f807dd7c7e1af58b4095933f3b29e537a85037ced6439` |

These hashes identify reference inputs; they do not make the AArch64 ramdisks
suitable for the ARM32 image.

The larger recovery ramdisk must not be loaded at `0x44000000`: the DT-reserved
RAM console begins at `0x44400000`. The v97 ramdisk address `0x43478000` leaves
the audited ARM32 recovery archive above the ATF reservation ending at
`0x4302ffff`; it may use the free range through (but not including)
`0x44400000`. Recheck this bound from the final compressed size for every
packaged image.

## Device-tree contract for CONSYS/WLAN

The active ARM32 configuration uses `CONSYS_PWR_ON_OFF_API_AVAILABLE=1`, so
the driver takes the generic-power-domain path and maps CONSYS resources 0
through 2.  It does not map resource 3/SPM.  The pinned candidate therefore
retains the stock three-resource tuple intentionally; the fourth SPM resource
in the source DTS belongs to the alternative non-genpd path.

Resource 2 remains exactly `0x10001000/0x1000`.  Commit `b45d49d8` corrects
the active driver's AXI offsets to `0x220/0x228`, while EMI remapping uses
resource 2 plus `0x320` (`0x10001320`).  The only DTB additions are
`clocks = <5 3>` and `clock-names = "bus"` for `CLK_INFRA_PMIC_CONN`.

The reproducible raw DTB is 51,652 bytes, below LK's 64 KiB appended-FDT
limit, with SHA-256
`0da56af63b5835a825e6524f1c7ca28635ee020eb77e68f7425753ba5a3ae156`.
Its exact resource tuple, clock mutation, and boot envelope are checked by
the [Wi-Fi DTB contract](../tools/mt8163-arm32/WIFI_DTB.md).

## Stock radio parity corrections

The exact stock ARM32 kernel configuration contains
`CONFIG_ARCH_MTK_PROJECT="radar"`, both MediaTek Wi-Fi antenna options, and
does not contain WFD or Douglas thermal support.  The ARM32 candidate now
matches those choices.  This is relevant before `wlan0`: the antenna switch
option enables the WMT initialization that writes `0x81060010` and emits the
manual-antenna/coexistence feature encoded by the pinned `WMT_SOC.cfg`.
The source was the wrapped stock kernel with SHA-256
`1d32f61407cbec12f2dc491f0d5fd1e28ccb37a4551111b30a494018862f8195`;
its extracted configuration hashes to
`245f08be567589cc1df0159d05cb3874e05f2d924a04af24fed2bb70147cd9dd`.

Decompiling the exact stock ARM32 `wmt_launcher` also corrected the ROM-patch
registration contract.  Bytes 24 through 27 are the route word; the high and
low nibbles of byte 24 encode patch count and sequence, and stock clears that
byte before submitting the address.  The resulting records are sequence 2 at
`00:00:06:00` for `_1_0`, and sequence 1 at `00:00:0e:f0` for `_1_1`.
Earlier compatibility tooling read at byte 23 and assigned filename-order
sequences, so its post-START/pre-READY result did not test stock-equivalent
patch routing and cannot rule that routing out as the blocker.  The launcher
used for this machine-code check is the staged stock binary with SHA-256
`1f34425d727ea64524c9edaeac5e6b295df7a6054703dcc79b164021560252e5`.

## Milestone order

1. Reproduce v97 recovery behavior with ARM-EABI BusyBox and ADB.
2. Follow the manual connectivity Gates 0 through 5, advancing from a passive
   fresh boot through configure-only, BT-only, stock-runtime A/B, and one
   bounded Wi-Fi activation.
3. Load the matching WLAN firmware, complete WMT/STP/HIF bring-up, and create a
   usable `wlan0` interface.
4. Optionally add ADB plus RNDIS/USB networking without weakening recovery.
5. Integrate `LibreEcho-UI` only after the network interface is stable.

Each phase should keep the early `FASTBOOT_PLEASE` path and be preserved as a
known-good artifact before proceeding to the next one.

The authoritative build and gate details are in
[`tools/mt8163-arm32/README.md`](../tools/mt8163-arm32/README.md) and
[`tools/mt8163-arm32/connectivity/README.md`](../tools/mt8163-arm32/connectivity/README.md).
