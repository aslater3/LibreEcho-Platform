# Build a matching armmp kernel (exported as ./vmlinuz) + an initramfs that drives
# the OTA-install self-test. Populate ./stage/ first (see README). Run in a
# linux/arm/v7 container with this dir mounted at /work:
#   docker run --rm -i --platform linux/arm/v7 -v "$PWD":/work debian:bookworm-slim bash -s < build-initramfs.sh
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq linux-image-armmp busybox-static xz-utils cpio e2fsprogs >/dev/null 2>&1
KVER=$(ls /lib/modules | head -1); MODS=/lib/modules/$KVER
# Export the matching kernel so vmlinuz and the initramfs modules are the same
# version (a stale vmlinuz makes every virtio/ext4 module fail vermagic).
cp /boot/vmlinuz-$KVER /work/vmlinuz
echo "exported kernel: $KVER"
R=/work/initramfs; rm -rf $R; mkdir -p $R/{bin,sbin,proc,sys,dev,data,tools,mods,fakesys}
cp /bin/busybox $R/bin/busybox
# static-ish e2fs tools for on-boot format (dynamic; add libs)
cp /sbin/mke2fs $R/sbin/ 2>/dev/null; cp /sbin/mkfs.ext4 $R/sbin/ 2>/dev/null || true
mkdir -p $R/lib/arm-linux-gnueabihf $R/lib
for lib in $(ldd /sbin/mke2fs 2>/dev/null | grep -oE '/lib[^ ]*\.so[^ ]*'); do cp "$lib" $R/$lib 2>/dev/null || true; done
cp /lib/ld-linux-armhf.so.3 $R/lib/ 2>/dev/null || true
cp /work/stage/tools/* $R/tools/ 2>/dev/null || true
cp /work/stage/package.tar $R/tools/package.tar
cp /work/stage/unsigned.tar $R/tools/unsigned.tar 2>/dev/null || true
cp /work/stage/ota-public-key.hex $R/tools/ 2>/dev/null || true
mkdir -p $R/usr/local/sbin $R/usr/local/libexec $R/etc/libreecho
cp /work/stage/tools/libreecho-update $R/usr/local/sbin/ 2>/dev/null
cp /work/stage/tools/libreecho-bootctl $R/usr/local/sbin/ 2>/dev/null
cp /work/stage/tools/libreecho-update-verify $R/usr/local/libexec/ 2>/dev/null
cp /work/stage/ota-public-key.hex $R/etc/libreecho/ota-public-key.hex
printf 'dev\n' > $R/etc/libreecho/update-channel
chmod +x $R/usr/local/sbin/* $R/usr/local/libexec/* 2>/dev/null || true
# ext4 modules (virtio_blk is built-in on armmp virt)
for m in virtio virtio_ring virtio_mmio virtio_pci virtio_blk crc16 crc32c_generic mbcache jbd2 ext4; do
  f=$(find $MODS -name "$m.ko*" | head -1); [ -n "$f" ] || continue
  b=$(basename "$f"); o=${b%.xz}; case "$f" in *.xz) xz -dc "$f">$R/mods/$o;; *) cp "$f" $R/mods/$o;; esac; echo "$o">>$R/mods/loadorder
done
cat > $R/init <<'INIT'
#!/bin/busybox sh
export PATH=/bin:/sbin:/tools
B=busybox
$B mount -t proc proc /proc; $B mount -t sysfs sys /sys; $B mount -t devtmpfs dev /dev 2>/dev/null || $B mdev -s
for m in $($B cat /mods/loadorder 2>/dev/null); do $B insmod /mods/$m 2>/dev/null; done
$B sleep 2; $B mdev -s 2>/dev/null
$B mkdir -p /fakesys
for e in /sys/class/block/*; do n=$($B basename $e); $B cp -a "$e" /fakesys/$n 2>/dev/null || $B ln -s "$($B readlink -f $e)" /fakesys/$n 2>/dev/null; done
mp(){ [ -b /dev/vda$1 ] && $B ln -sf /dev/vda$1 /dev/mmcblk0p$1; $B mkdir -p /fakesys/mmcblk0p$1; printf 'PARTNAME=%s\n' "$2" > /fakesys/mmcblk0p$1/uevent; printf '%s\n' "$3" > /fakesys/mmcblk0p$1/size; }
mp 7 expdb 20480; mp 8 misc 1025; mp 9 persist 32768; mp 10 boot_a_x 32768; mp 11 boot_b_x 32768; mp 16 userdata 2137088; mp 17 boot_a 225280; mp 18 boot_b 225280
$B mount --bind /fakesys /sys/class/block
$B mount -t ext4 /dev/mmcblk0p16 /data 2>/dev/null
UPD=/usr/local/sbin/libreecho-update
BC=/usr/local/sbin/libreecho-bootctl
echo "===PHASE0 capabilities (expect: allow-unsigned)==="
$UPD capabilities 2>&1
$UPD bogus 2>&1 | $B head -1; echo "bogus_rc=$?"
echo "===PHASE0_END==="
$B rm -rf /data/libreecho/update 2>/dev/null
echo "===PHASE1 signed package.tar (expect ota_manifest_signature=PASS + UPDATE_READY)==="
$UPD install /tools/package.tar 2>&1 | $B tail -6
echo "BCB:"; $BC status 2>&1 | $B tail -4
echo "===PHASE1_END==="
$B rm -rf /data/libreecho/update 2>/dev/null
echo "===PHASE2 unsigned.tar, NO flag (expect package_signature reject)==="
$UPD install /tools/unsigned.tar 2>&1 | $B tail -3
echo "===PHASE2_END==="
$B rm -rf /data/libreecho/update 2>/dev/null
echo "===PHASE3 unsigned.tar, --allow-unsigned (expect skipped + UPDATE_READY)==="
$UPD install --allow-unsigned /tools/unsigned.tar 2>&1 | $B tail -5
echo "BCB:"; $BC status 2>&1 | $B tail -4
echo "===PHASE3_END==="
$B poweroff -f
INIT
chmod +x $R/init $R/tools/* $R/sbin/* 2>/dev/null || true
( cd $R && $R/bin/busybox find . | $R/bin/busybox cpio -o -H newc 2>/dev/null | gzip > /work/initramfs.cpio.gz )
echo "initramfs: $(ls -la /work/initramfs.cpio.gz | awk '{print $5}') bytes"
