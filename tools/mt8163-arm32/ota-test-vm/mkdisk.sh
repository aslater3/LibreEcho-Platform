# Build a fresh 2 GiB emulated MTK eMMC (GPT layout + valid BCB) for the OTA test VM.
# Run in a PRIVILEGED linux/amd64 container with this dir mounted at /work:
#   docker run --rm -i --privileged --platform linux/amd64 -v "$PWD":/work debian:bookworm-slim bash -s < mkdisk.sh
set -e
# Optional seeding.  With no arguments the image is byte-identical to before.
#
#   --profile FILE   a captured device profile; its system_update block decides
#                    which slot the BCB marks current and whether the other is
#                    bootable, and its config_export block is written into
#                    /data/libreecho/config inside userdata.
#   --scenario NAME  seed userdata into a shape known to break a real device:
#                      config-dir        a DIRECTORY where a config FILE belongs,
#                                        which halts every service on next boot
#                      stray-data-file   an unallowlisted file under /data, which
#                                        has left both A/B slots unbootable
#
# Both scenarios reproduce failures that have each bricked hardware. Having them
# in QEMU is the point of this script.
PROFILE=""; SCENARIO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)  PROFILE=$2; shift 2 ;;
    --scenario) SCENARIO=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$SCENARIO" in
  ""|config-dir|stray-data-file) ;;
  *) echo "unknown scenario: $SCENARIO (want config-dir or stray-data-file)" >&2; exit 2 ;;
esac
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
# Seed /data inside userdata (p16). Mounting needs the privileged container the
# header already requires.
USTART=$(sgdisk -i 16 $IMG | awk '/First sector/{print $3}')
LOOP=$(losetup -o $((USTART*512)) --sizelimit $((2137088*512)) -f --show $IMG)
MNT=$(mktemp -d); mount "$LOOP" "$MNT"
mkdir -p "$MNT/libreecho/config"
if [ -n "$PROFILE" ]; then
  sed -n 's/.*"config_export"[[:space:]]*:[[:space:]]*\({.*}\).*/\1/p' "$PROFILE" \
    > "$MNT/libreecho/config/config.json" || true
  [ -s "$MNT/libreecho/config/config.json" ] || echo '{}' > "$MNT/libreecho/config/config.json"
  echo "userdata seeded from $PROFILE"
fi
case "$SCENARIO" in
  config-dir)
    # A directory where led.json should be a file. On a real device this halts
    # every service on the NEXT boot, and rollback does not help because /data
    # is shared between slots.
    mkdir -p "$MNT/libreecho/config/led.json"
    echo "scenario: config-dir seeded" ;;
  stray-data-file)
    # An unallowlisted file directly under /data. This has left both A/B slots
    # unbootable on real hardware.
    echo unexpected > "$MNT/unexpected-file"
    echo "scenario: stray-data-file seeded" ;;
esac
sync; umount "$MNT"; losetup -d "$LOOP"; rmdir "$MNT"
# valid initial BCB in misc (p8): [00 ABB 01 slot_a=0x8f(pri15,success) slot_b=0x00]
P8=$(sgdisk -i 8 $IMG | awk '/First sector/{print $3}')
OFF=$((P8*512 + 512 + 0x160))
SLOT=a; ROLLBACK=false
if [ -n "$PROFILE" ]; then
  S=$(sed -n 's/.*"current_slot"[[:space:]]*:[[:space:]]*"\([ab]\)".*/\1/p' "$PROFILE" | head -1)
  [ -n "$S" ] && SLOT=$S
  grep -q '"rollback_available"[[:space:]]*:[[:space:]]*true' "$PROFILE" && ROLLBACK=true
fi
# 143 = 0x8f, priority 15 + successful; 15 = 0x0f, bootable but not yet proven;
# 0 = unbootable. Octal escapes below, because POSIX printf has no \xNN.
OTHER=0; [ "$ROLLBACK" = true ] && OTHER=15
if [ "$SLOT" = a ]; then A=143; B=$OTHER; else A=$OTHER; B=143; fi
printf "$(printf '\\%03o' 0 65 66 66 1 "$A" "$B")" | dd of=$IMG bs=1 seek=$OFF conv=notrunc 2>/dev/null
echo "BCB written at offset $OFF (p8 sector $P8): current slot $SLOT, rollback $ROLLBACK"
sgdisk -p $IMG | grep -E "^\s+16\b"
ls -la $IMG | awk '{print $5" bytes"}'
