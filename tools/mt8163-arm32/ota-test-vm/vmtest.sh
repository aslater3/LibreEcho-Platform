#!/bin/sh
# Verify mkdisk.sh: the default image is an empty userdata + canonical BCB, a
# profile seeds web-config.json and a genuinely bootable rollback slot, both
# scenarios land in userdata, a malformed profile is refused, and an unknown
# scenario is refused. Run in the same privileged linux/amd64 container the
# mkdisk.sh header describes, with this dir mounted at /work.
set -e
cd /work
fail=0

bcb() {  # $1=image -> 7 BCB bytes as space-separated hex
  p8=$(sgdisk -i 8 "$1" | awk '/First sector/{print $3}')
  dd if="$1" bs=1 skip=$((p8 * 512 + 512 + 0x160)) count=7 2>/dev/null | od -An -tx1 | tr -s ' '
}
slot_byte() { # $1=image $2=a|b -> decimal value of that slot's metadata byte
  p8=$(sgdisk -i 8 "$1" | awk '/First sector/{print $3}')
  idx=5; [ "$2" = b ] && idx=6
  dd if="$1" bs=1 skip=$((p8 * 512 + 512 + 0x160 + idx)) count=1 2>/dev/null | od -An -tu1 | tr -d ' '
}
bootable() { # $1=metadata byte value -> "yes" if selected_slot() would boot it
  v=$1; pri=$((v & 0x0f)); tries=$(((v >> 4) & 0x07)); succ=$((v >> 7))
  { [ "$succ" -eq 1 ] || [ "$tries" -gt 0 ]; } && echo yes || echo no
}
lsdata() { # $1=image -> find output of /data (relative, sorted, minus lost+found)
  s=$(sgdisk -i 16 "$1" | awk '/First sector/{print $3}')
  L=$(losetup -o $((s*512)) --sizelimit $((2137088*512)) -f --show "$1")
  m=$(mktemp -d); mount "$L" "$m"
  (cd "$m" && find . -not -name 'lost+found' | sort)
  umount "$m"; losetup -d "$L"; rmdir "$m"
}
catdata() { # $1=image $2=relpath -> file contents from userdata
  s=$(sgdisk -i 16 "$1" | awk '/First sector/{print $3}')
  L=$(losetup -o $((s*512)) --sizelimit $((2137088*512)) -f --show "$1")
  m=$(mktemp -d); mount "$L" "$m"
  cat "$m/$2" 2>/dev/null; rc=$?
  umount "$m"; losetup -d "$L"; rmdir "$m"; return $rc
}

echo "== default (no arguments): empty userdata + canonical BCB =="
sh mkdisk.sh >/dev/null 2>&1
got=$(bcb emmc.img)
[ "$got" = " 00 41 42 42 01 8f 00" ] && echo "  BCB unchanged: $got" \
  || { echo "  FAIL default BCB: $got"; fail=1; }
# The compatibility guarantee: with no seeding the filesystem is empty.
tree=$(lsdata emmc.img)
[ "$tree" = "." ] && echo "  userdata empty, as before" \
  || { echo "  FAIL default userdata not empty:"; echo "$tree"; fail=1; }

# A real captured profile is pretty-printed multi-line JSON, not one line; the
# parser must handle that (a brace-on-each-line sed would extract nothing).
cat > /tmp/prof.json <<'JSON'
{
  "schema_version": 1,
  "system_update": {
    "current_slot": "b",
    "rollback_available": true
  },
  "config_export": {
    "hostname": "libreecho-dev",
    "volume": 26,
    "wake_word": "Alexa"
  }
}
JSON

echo "== --profile (slot b, bootable rollback) =="
sh mkdisk.sh --profile /tmp/prof.json >/dev/null 2>&1
got=$(bcb emmc.img)
[ "$got" = " 00 41 42 42 01 8e 8f" ] && echo "  BCB follows profile: $got" \
  || { echo "  FAIL profile BCB: $got (want 00 41 42 42 01 8e 8f)"; fail=1; }
# Decode the inactive slot rather than trusting the raw byte: it must be
# bootable (a rollback the bootloader would actually select), not merely
# high-priority. 0x8e = priority 14, successful.
other=$(slot_byte emmc.img a)
[ "$(bootable "$other")" = yes ] \
  && echo "  rollback slot a decodes bootable (byte=$other)" \
  || { echo "  FAIL rollback slot a not bootable (byte=$other)"; fail=1; }
# config_export must reach web-config.json -- the file the platform reads --
# with its contents intact, not a placeholder {}.
lsdata emmc.img | grep -q './libreecho/config/web-config.json' \
  && echo "  web-config.json seeded" || { echo "  FAIL web-config.json not seeded"; fail=1; }
wc=$(catdata emmc.img libreecho/config/web-config.json || true)
echo "$wc" | grep -q '"wake_word": "Alexa"' && echo "$wc" | grep -q '"hostname": "libreecho-dev"' \
  && echo "  web-config.json carries the captured settings" \
  || { echo "  FAIL web-config.json contents wrong:"; echo "$wc"; fail=1; }

echo "== --profile with malformed config_export is refused =="
printf '{"schema_version":1,"config_export":"not-an-object"}\n' > /tmp/bad.json
if sh mkdisk.sh --profile /tmp/bad.json >/dev/null 2>&1; then
  echo "  FAIL malformed profile accepted"; fail=1
else
  echo "  refused, as required"
fi

echo "== --scenario config-dir =="
sh mkdisk.sh --profile /tmp/prof.json --scenario config-dir >/dev/null 2>&1
lsdata emmc.img | grep -q './libreecho/config/led.json$' && {
  s=$(sgdisk -i 16 emmc.img | awk '/First sector/{print $3}')
  L=$(losetup -o $((s*512)) --sizelimit $((2137088*512)) -f --show emmc.img)
  m=$(mktemp -d); mount "$L" "$m"
  [ -d "$m/libreecho/config/led.json" ] && echo "  led.json is a DIRECTORY (the brick shape)" \
    || { echo "  FAIL led.json not a directory"; fail=1; }
  umount "$m"; losetup -d "$L"; rmdir "$m"
} || { echo "  FAIL config-dir not seeded"; fail=1; }

echo "== --scenario stray-data-file =="
sh mkdisk.sh --profile /tmp/prof.json --scenario stray-data-file >/dev/null 2>&1
lsdata emmc.img | grep -q './unexpected-file' \
  && echo "  stray file present at /data root" || { echo "  FAIL stray file absent"; fail=1; }

echo "== unknown scenario refused =="
if sh mkdisk.sh --scenario nonesuch >/dev/null 2>&1; then
  echo "  FAIL unknown scenario accepted"; fail=1
else
  echo "  refused, as required"
fi

echo
[ "$fail" -eq 0 ] && echo "ALL MKDISK CHECKS PASSED" || echo "MKDISK CHECKS FAILED"
exit $fail
