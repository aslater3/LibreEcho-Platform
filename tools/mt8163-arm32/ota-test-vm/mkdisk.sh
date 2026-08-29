# Build a fresh 2 GiB emulated MTK eMMC (GPT layout + valid BCB) for the OTA test VM.
# Run in a PRIVILEGED linux/amd64 container with this dir mounted at /work:
#   docker run --rm -i --privileged --platform linux/amd64 -v "$PWD":/work debian:bookworm-slim bash -s < mkdisk.sh
set -e
# Optional seeding.  With no arguments the image is byte-identical to before:
# an empty userdata filesystem and the canonical slot-a BCB.
#
#   --profile FILE   a captured device profile; its system_update block decides
#                    which slot the BCB marks current and whether the other is
#                    a genuinely bootable rollback, and its config_export block
#                    is written to /data/libreecho/config/web-config.json inside
#                    userdata -- the path libreecho-init and the web service read.
#   --scenario NAME  seed userdata into a shape known to break a real device:
#                      config-dir        a DIRECTORY where a config FILE belongs,
#                                        which halts every service on next boot
#                      stray-data-file   an unallowlisted file under /data, which
#                                        has left both A/B slots unbootable
#
# Both scenarios reproduce failures that have each bricked hardware, and the OTA
# test VM's /init runs the production data-contract cleanup against the seeded
# userdata and asserts the rejection -- so these are exercised, not just present.
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
# python3 is only needed to parse a --profile structurally; install it always so
# the container is deterministic regardless of arguments.
apt-get update -qq && apt-get install -y -qq gdisk e2fsprogs python3 >/dev/null 2>&1
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

# Seed userdata (p16) ONLY when seeding was requested. With no --profile and no
# --scenario the freshly formatted filesystem is left untouched, so the default
# image stays byte-for-byte the empty userdata the compatibility guarantee
# documents -- adding /libreecho/config unconditionally would have broken that.
if [ -n "$PROFILE$SCENARIO" ]; then
  USTART=$(sgdisk -i 16 $IMG | awk '/First sector/{print $3}')
  LOOP=$(losetup -o $((USTART*512)) --sizelimit $((2137088*512)) -f --show $IMG)
  MNT=$(mktemp -d); mount "$LOOP" "$MNT"
  if [ -n "$PROFILE" ]; then
    mkdir -p "$MNT/libreecho/config"
    # Parse config_export structurally and write it to web-config.json, the file
    # libreecho-init and the web service actually read. A profile whose
    # config_export is missing or malformed is a hard error -- silently seeding
    # {} would let a broken capture masquerade as a realistic device.
    python3 - "$PROFILE" "$MNT/libreecho/config/web-config.json" <<'PY'
import json, sys
profile_path, out_path = sys.argv[1], sys.argv[2]
try:
    profile = json.load(open(profile_path))
except (OSError, ValueError) as exc:
    sys.exit("profile is not valid JSON: %s" % exc)
export = profile.get("config_export")
if not isinstance(export, dict):
    sys.exit("profile has no object-valued config_export block")
with open(out_path, "w") as f:
    json.dump(export, f, indent=2, sort_keys=True)
    f.write("\n")
PY
    echo "userdata seeded from $PROFILE -> config/web-config.json"
  fi
  case "$SCENARIO" in
    config-dir)
      # A directory where led.json (a config *file*) belongs. The production
      # data-contract cleanup treats a directory under config/ as a hard
      # failure and halts every service on the NEXT boot; rollback does not
      # help because /data is shared between slots.
      mkdir -p "$MNT/libreecho/config/led.json"
      echo "scenario: config-dir seeded" ;;
    stray-data-file)
      # An unallowlisted file directly under /data. This has left both A/B
      # slots unbootable on real hardware.
      echo unexpected > "$MNT/unexpected-file"
      echo "scenario: stray-data-file seeded" ;;
  esac
  sync; umount "$MNT"; losetup -d "$LOOP"; rmdir "$MNT"
fi

# valid initial BCB in misc (p8): [00 ABB 01 slot_a slot_b]
P8=$(sgdisk -i 8 $IMG | awk '/First sector/{print $3}')
OFF=$((P8*512 + 512 + 0x160))
SLOT=a; ROLLBACK=false
if [ -n "$PROFILE" ]; then
  S=$(sed -n 's/.*"current_slot"[[:space:]]*:[[:space:]]*"\([ab]\)".*/\1/p' "$PROFILE" | head -1)
  [ -n "$S" ] && SLOT=$S
  grep -q '"rollback_available"[[:space:]]*:[[:space:]]*true' "$PROFILE" && ROLLBACK=true
fi
# Slot metadata byte = priority(0-15) | tries<<4 | success<<7.
#   143 = 0x8f: priority 15, successful        -> the confirmed current slot
#   142 = 0x8e: priority 14, successful        -> a genuinely bootable rollback
#     0 = 0x00: priority 0, no tries, no success -> not bootable
# The rollback slot MUST carry a success or tries bit: libreecho_bootctl's
# selected_slot() only treats a slot as bootable when one of those is set, so
# the old 0x0f (priority 15, tries 0, success 0) decoded as unbootable and
# contradicted rollback_available. 0x8e matches the confirmed-successful
# priority-14 state activate() leaves the outgoing slot in. Octal escapes
# below, because POSIX printf has no \xNN.
OTHER=0; [ "$ROLLBACK" = true ] && OTHER=142
if [ "$SLOT" = a ]; then A=143; B=$OTHER; else A=$OTHER; B=143; fi
printf "$(printf '\\%03o' 0 65 66 66 1 "$A" "$B")" | dd of=$IMG bs=1 seek=$OFF conv=notrunc 2>/dev/null
echo "BCB written at offset $OFF (p8 sector $P8): current slot $SLOT, rollback $ROLLBACK"
sgdisk -p $IMG | grep -E "^\s+16\b"
ls -la $IMG | awk '{print $5" bytes"}'
