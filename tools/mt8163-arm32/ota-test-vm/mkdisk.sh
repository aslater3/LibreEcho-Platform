# Build a fresh 2 GiB emulated MTK eMMC (GPT layout + valid BCB) for the OTA test VM.
# Run in a PRIVILEGED linux/amd64 container with this dir mounted at /work:
#   docker run --rm -i --privileged --platform linux/amd64 -v "$PWD":/work debian:bookworm-slim bash -s < mkdisk.sh
set -e
apt-get update -qq && apt-get install -y -qq gdisk e2fsprogs >/dev/null 2>&1
IMG=/work/emmc.img
rm -f $IMG; truncate -s 2048M $IMG
sgdisk -a 1 \
  -n 7:2048:+20480 -c 7:expdb -n 8:0:+1025 -c 8:misc -n 9:0:+32768 -c 9:persist \
  -n 10:0:+32768 -c 10:boot_a_x -n 11:0:+32768 -c 11:boot_b_x \
  -n 17:0:+225280 -c 17:boot_a -n 18:0:+225280 -c 18:boot_b \
  -n 16:0:+2137088 -c 16:userdata $IMG >/dev/null
# format userdata (p16) via offset loop
START=$(sgdisk -i 16 $IMG | awk '/First sector/{print $3}')
mke2fs -F -q -t ext4 -L userdata -E offset=$((START*512)) $IMG $((2137088/8*4)) 2>&1 | head -2 || \
  { L=$(losetup -o $((START*512)) --sizelimit $((2137088*512)) -f --show $IMG); mke2fs -F -q -t ext4 -L userdata $L; losetup -d $L; }
echo "userdata formatted"
# valid initial BCB in misc (p8): [00 ABB 01 slot_a=0x8f(pri15,success) slot_b=0x00]
P8=$(sgdisk -i 8 $IMG | awk '/First sector/{print $3}')
OFF=$((P8*512 + 512 + 0x160))
printf '\x00\x41\x42\x42\x01\x8f\x00' | dd of=$IMG bs=1 seek=$OFF conv=notrunc 2>/dev/null
echo "BCB written at offset $OFF (p8 sector $P8)"
sgdisk -p $IMG | grep -E "^\s+16\b"
ls -la $IMG | awk '{print $5" bytes"}'
