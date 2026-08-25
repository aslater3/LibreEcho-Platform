#!/bin/sh
# Verify mkdisk.sh: default image unchanged, BCB follows the profile, both
# scenarios land in userdata, and an unknown scenario is refused.
set -e
cd /work
fail=0

bcb() {  # $1=image -> 7 BCB bytes as hex
  p8=$(sgdisk -i 8 "$1" | awk '/First sector/{print $3}')
  dd if="$1" bs=1 skip=$((p8 * 512 + 512 + 0x160)) count=7 2>/dev/null | od -An -tx1 | tr -s ' '
}
lsdata() { # $1=image -> find output of /data
  s=$(sgdisk -i 16 "$1" | awk '/First sector/{print $3}')
  L=$(losetup -o $((s*512)) --sizelimit $((2137088*512)) -f --show "$1")
  m=$(mktemp -d); mount "$L" "$m"
  (cd "$m" && find . -maxdepth 3 -not -name 'lost+found' | sort)
  umount "$m"; losetup -d "$L"; rmdir "$m"
}

echo "== default (no arguments) =="
sh mkdisk.sh >/dev/null 2>&1
got=$(bcb emmc.img)
[ "$got" = " 00 41 42 42 01 8f 00" ] && echo "  BCB unchanged: $got" \
  || { echo "  FAIL default BCB: $got"; fail=1; }

cat > /tmp/prof.json <<'JSON'
{"schema_version":1,
 "system_update":{"current_slot":"b","rollback_available":true},
 "config_export":{"hostname":"libreecho-dev","volume":26,"wake_word":"Alexa"}}
JSON

echo "== --profile (slot b, rollback available) =="
sh mkdisk.sh --profile /tmp/prof.json >/dev/null 2>&1
got=$(bcb emmc.img)
[ "$got" = " 00 41 42 42 01 0f 8f" ] && echo "  BCB follows profile: $got" \
  || { echo "  FAIL profile BCB: $got (want 00 41 42 42 01 0f 8f)"; fail=1; }
lsdata emmc.img | grep -q './libreecho/config/config.json' \
  && echo "  config seeded into userdata" || { echo "  FAIL config not seeded"; fail=1; }

echo "== --scenario config-dir =="
sh mkdisk.sh --profile /tmp/prof.json --scenario config-dir >/dev/null 2>&1
s=$(sgdisk -i 16 emmc.img | awk '/First sector/{print $3}')
L=$(losetup -o $((s*512)) --sizelimit $((2137088*512)) -f --show emmc.img)
m=$(mktemp -d); mount "$L" "$m"
[ -d "$m/libreecho/config/led.json" ] && echo "  led.json is a DIRECTORY (the brick shape)" \
  || { echo "  FAIL config-dir not seeded"; fail=1; }
umount "$m"; losetup -d "$L"; rmdir "$m"

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
