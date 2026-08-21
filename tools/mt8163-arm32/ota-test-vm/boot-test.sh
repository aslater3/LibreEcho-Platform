#!/bin/sh
# Boot the OTA test VM on the host (needs qemu-system-arm). Writes ./boot-test.log.
# Usage: ./boot-test.sh   (run mkdisk.sh + build-initramfs.sh first)
cd "$(dirname "$0")"
rm -f boot-test.log
qemu-system-arm -M virt -m 512 -cpu cortex-a15 \
  -kernel vmlinuz -initrd initramfs.cpio.gz \
  -append "console=ttyAMA0 rdinit=/init" \
  -drive file=emmc.img,format=raw,id=hd0,if=none \
  -device virtio-blk-device,drive=hd0 \
  -nographic -no-reboot > boot-test.log 2>&1 &
QPID=$!
( sleep 220; kill $QPID 2>/dev/null ) &
GUARD=$!
wait $QPID 2>/dev/null
qemu_rc=$?
kill $GUARD 2>/dev/null || true
wait $GUARD 2>/dev/null || true
echo "qemu exited rc=$qemu_rc"
exit "$qemu_rc"
