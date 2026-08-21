# LibreEcho initial-install QEMU/eMMC transaction VM

This fixture validates the **storage and boot transaction** around the MT8163
BROM/Amonet boundary without touching hardware.

It starts with the physical BISCUIT GPT captured from the real-device Amonet
log, runs a deterministic mocked BROM/Amonet installation, transforms the GPT
to the LK logical view, applies redirected fastboot writes to a sparse raw eMMC
image, and verifies the resulting payloads and boot contract.

## What it covers

- physical GPT validation (`kb`, `dkb`, `lk_a`, `tee1`, `lk_b`, `tee2`, `expdb`,
  `misc`, `persist`, `boot_a`, `boot_b`, `recovery`, `system_a`, `system_b`,
  `cache`, `userdata`);
- Amonet wrapper transformation:
  - physical `boot_a`/`boot_b` become logical `boot_a_x`/`boot_b_x`;
  - wrapper entries become logical `boot_a`/`boot_b` at the observed sectors;
- disk-backed 4 GiB sparse eMMC image;
- real ext4 userdata filesystem metadata;
- fastboot writes to both redirected payload slots;
- exact 16 MiB Android-v0/`bootopt` image validation;
- complete `_x` payload readback and SHA-256 verification;
- transaction state/progress evidence.

The real BROM USB wire protocol, Download Agent framing, RPMB authentication,
and MT8163 silicon are intentionally outside this host fixture's evidence
boundary.

## Run the host transaction test

```sh
cd tools/mt8163-arm32/initial-install-vm
python3 -m unittest -v test_initial_install_vm.py
python3 initial_install_vm.py --workdir /tmp/libreecho-initial-install
```

The runner prints a JSON result and leaves:

```text
/tmp/libreecho-initial-install/emmc.img
/tmp/libreecho-initial-install/transaction.json
/tmp/libreecho-initial-install/boot.img
```

Use `--boot <verified-16MiB-boot.img>` to test a real release image instead of
the generated contract image. No release or hardware pointer is modified.

## Driving the released installer

The Build installer has an explicit `--emulator-root` mode. Point it at a copy
of `emulator_tool.py`, provide the matching ARM QEMU kernel/initramfs, and keep
`--slots both` to exercise both redirected writes:

```sh
python3 libreecho-radar-puffin-v0.13.7-installer.py one-shot \\
  --release-dir <release-dir> --release-tag radar-puffin-v0.13.7 \\
  --emulator-root <this-directory> \\
  --emulator-kernel <ota-test-vm>/vmlinuz \\
  --emulator-initramfs <ota-test-vm>/initramfs.cpio.gz \\
  --no-open-browser --slots both
```

This preserves the installer state machine and command sequence. The emulator
provides command-compatible BROM, fastboot, and ADB adapters, and writes
`uart.log` with the GPT/LK/fastboot markers captured from the real device.

## Optional QEMU boot

The existing `ota-test-vm` build produces a matching ARM kernel and initramfs.
After building those inputs, pass them explicitly:

```sh
python3 initial_install_vm.py \
  --workdir /tmp/libreecho-initial-install \
  --qemu-kernel ../ota-test-vm/vmlinuz \
  --qemu-initramfs ../ota-test-vm/initramfs.cpio.gz
```

QEMU is then launched against the transaction's `emmc.img` using virtio block
storage. The generated guest verifies Linux startup, GPT names for both
redirected payload partitions, and complete payload SHA-256 readback before
powering off. Userdata filesystem creation and geometry are verified on the
host transaction path; the QEMU boot probe does not claim a guest userdata mount
because the existing OTA initramfs's ext4 module set is a separate contract.

## Evidence boundary

A passing run means:

```text
physical GPT: PASS
mocked BROM/Amonet transaction: PASS
logical GPT transformation: PASS
fastboot redirected writes: PASS
payload readback: PASS
boot image contract: PASS
QEMU boot: PASS only when explicitly run and returned zero
```

It does not prove physical BROM compatibility or hardware acceptance.
