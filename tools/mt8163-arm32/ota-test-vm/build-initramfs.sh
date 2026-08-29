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
R=/work/initramfs; rm -rf $R; mkdir -p $R/{bin,sbin,proc,sys,dev,data,tmp,tools,mods,fakesys}
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
cp /work/stage/tools/libreecho-data-cleanup $R/usr/local/sbin/ 2>/dev/null
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
DC=/usr/local/sbin/libreecho-data-cleanup
run_expect(){
  name=$1; expected_rc=$2; expected_text=$3; shift 3
  out=/tmp/ota-$name.log
  set +e
  "$@" >$out 2>&1
  rc=$?
  set -e
  assert_rc "$name" "$rc" "$expected_rc" "$out"
  assert_output $out "$expected_text"
  $B cat $out
}
assert_rc(){
  name=$1; actual=$2; expected=$3; file=$4
  [ "$actual" -eq "$expected" ] || { $B cat "$file"; echo "ASSERT:$name:rc=$actual expected=$expected"; exit 1; }
}
assert_output(){
  file=$1; expected_text=$2
  $B grep -q "$expected_text" "$file" || { $B cat "$file"; echo "ASSERT:missing=$expected_text"; exit 1; }
}
assert_bcb(){
  selected=$1; success=$2
  status=$($BC status) || { echo "ASSERT:bcb:status"; exit 1; }
  echo "$status" | $B grep -q "^selected_slot=$selected$" || { echo "$status"; exit 1; }
  echo "$status" | $B grep -q "^slot_${selected}_success=$success$" || { echo "$status"; exit 1; }
}
assert_bcb_success(){
  slot=$1; success=$2
  status=$($BC status) || { echo "ASSERT:bcb-success:status"; exit 1; }
  echo "$status" | $B grep -q "^slot_${slot}_success=$success$" || { echo "$status"; exit 1; }
}
reset_bcb(){
  $B printf '\000ABB\001\217\000' >/tmp/fresh-bcb
  $B dd if=/tmp/fresh-bcb of=/dev/mmcblk0p8 bs=1 seek=864 conv=notrunc 2>/dev/null || exit 1
  assert_bcb a 1
}
echo "===PHASE0 capabilities (expect: allow-unsigned)==="
run_expect phase0-capabilities 0 allow-unsigned $UPD capabilities
run_expect phase0-unknown 2 Usage: $UPD bogus
echo "===PHASE0_END==="
# ---- PHASE_DATA_CONTRACT: run the production data-contract cleanup against the
# seeded userdata and assert its verdict. mkdisk.sh's --scenario shapes have
# each bricked real hardware; here the real validator must reject them, and a
# clean userdata must pass. The expectation is derived independently from the
# on-disk shape so the assertion cannot rubber-stamp the tool.
echo "===PHASE_DATA_CONTRACT (real cleanup over seeded /data)==="
expect_ok=1
[ -d /data/libreecho/config/led.json ] && expect_ok=0
for e in /data/* ; do
  [ -e "$e" ] || continue
  case "${e##*/}" in libreecho|lost+found) ;; *) expect_ok=0 ;; esac
done
set +e
$DC >/tmp/data-contract.log 2>&1
dc_rc=$?
set -e
$B cat /tmp/data-contract.log
if [ "$expect_ok" = 1 ]; then
  assert_rc data-contract "$dc_rc" 0 /tmp/data-contract.log
  assert_output /tmp/data-contract.log DATA_CLEANUP_OK
  echo "  clean userdata accepted"
else
  [ "$dc_rc" -ne 0 ] || { echo "ASSERT:data-contract:brick was accepted rc=$dc_rc"; exit 1; }
  assert_output /tmp/data-contract.log DATA_CLEANUP_CONTRACT_FAILED
  echo "  brick shape rejected by the real contract (rc=$dc_rc)"
  echo "===PHASE_DATA_CONTRACT_END==="
  echo "scenario image: brick reproduced and rejected; OTA phases skipped"
  $B poweroff -f
fi
echo "===PHASE_DATA_CONTRACT_END==="
# ---- PHASE_PROFILE: if mkdisk.sh seeded a captured profile (a non-default BCB
# -- a current slot other than a, or a bootable inactive slot), exercise an
# install against that real state BEFORE reset_bcb throws it away. This is the
# only phase that covers an install from slot b or with a rollback available;
# the reset-based phases below always start from the canonical slot-a fixture.
seeded=$($BC status)
seeded_slot=$(echo "$seeded" | $B sed -n 's/^selected_slot=//p')
other_slot=b; [ "$seeded_slot" = b ] && other_slot=a
other_succ=$(echo "$seeded" | $B sed -n "s/^slot_${other_slot}_success=//p")
other_tries=$(echo "$seeded" | $B sed -n "s/^slot_${other_slot}_tries=//p")
if [ "$seeded_slot" != a ] || [ "${other_succ:-0}" = 1 ] || [ "${other_tries:-0}" -gt 0 ]; then
  echo "===PHASE_PROFILE install from captured slot=$seeded_slot (rollback slot=$other_slot)==="
  run_expect phase-profile 0 UPDATE_READY $UPD install /tools/package.tar
  # The install must target the inactive slot and leave the slot it came from as
  # a confirmed-successful rollback (activate()'s priority-14 state).
  assert_bcb "$other_slot" 0
  assert_bcb_success "$seeded_slot" 1
  echo "  installed into $other_slot; $seeded_slot preserved as bootable rollback"
  echo "===PHASE_PROFILE_END==="
  $B rm -rf /data/libreecho/update 2>/dev/null
fi
$B rm -rf /data/libreecho/update 2>/dev/null
reset_bcb
echo "===PHASE1 signed package.tar (expect ota_manifest_signature=PASS + UPDATE_READY)==="
run_expect phase1 0 UPDATE_READY $UPD install /tools/package.tar
assert_output /tmp/ota-phase1.log ota_manifest_signature=PASS
assert_bcb b 0
echo "===PHASE1_END==="
$B rm -rf /data/libreecho/update 2>/dev/null
reset_bcb
echo "===PHASE2 unsigned.tar, NO flag (expect package_signature reject)==="
run_expect phase2 1 ERROR:package_signature $UPD install /tools/unsigned.tar
assert_bcb a 1
echo "===PHASE2_END==="
$B rm -rf /data/libreecho/update 2>/dev/null
reset_bcb
echo "===PHASE3 unsigned.tar, --allow-unsigned (expect skipped + UPDATE_READY)==="
run_expect phase3 0 UPDATE_READY $UPD install --allow-unsigned /tools/unsigned.tar
assert_bcb b 0
echo "===PHASE3_END==="
$B poweroff -f
INIT
chmod +x $R/init $R/tools/* $R/sbin/* 2>/dev/null || true
( cd $R && $R/bin/busybox find . | $R/bin/busybox cpio -o -H newc 2>/dev/null | gzip > /work/initramfs.cpio.gz )
echo "initramfs: $(ls -la /work/initramfs.cpio.gz | awk '{print $5}') bytes"
