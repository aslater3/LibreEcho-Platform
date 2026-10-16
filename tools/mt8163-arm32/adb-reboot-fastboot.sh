#!/usr/bin/env bash
# Restart a running LibreEcho device into Kaeru fastboot over ADB.
#
# The device performs the only flash write: libreecho-bootctl proves the Kaeru
# payload in expdb (read-only) and writes Kaeru's one-shot misc command
# "bootonce-bootloader".  expdb is never written (Platform #195).  From fastboot
# a plain `fastboot reboot` returns to LibreEcho.
set -euo pipefail

ADB_BIN=${ADB_BIN:-adb}
serial=${1:-}
[[ -n "$serial" ]] || { echo "usage: $0 <adb-serial>" >&2; exit 2; }

"$ADB_BIN" -s "$serial" shell 'printf fastboot > /tmp/reboot.request'
echo "Requested restart to fastboot on $serial; wait for: fastboot devices"
