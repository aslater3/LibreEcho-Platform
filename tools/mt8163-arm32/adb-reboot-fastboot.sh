#!/usr/bin/env bash
# Request fastboot from the running LibreEcho development OS via /tmp/runme.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: adb-reboot-fastboot.sh [TIMEOUT_SECONDS]

Environment:
  ADB_SERIAL    target serial (must be supplied by the operator)
  ADB_BIN       adb executable (default: adb)
  FASTBOOT_BIN  fastboot executable (default: fastboot)

The target script verifies Linux partition 7 is the 20,480-sector expdb
partition, writes and reads back FASTBOOT_PLEASE, syncs, then force-reboots.
EOF
}

[[ ${1:-} == -h || ${1:-} == --help ]] && { usage; exit 0; }
[[ $# -le 1 ]] || { usage >&2; exit 2; }
timeout=${1:-60}
serial=${ADB_SERIAL:-}
[[ -n "$serial" ]] || { echo "ERROR: ADB_SERIAL must be supplied" >&2; exit 2; }
adb_bin=${ADB_BIN:-adb}
fastboot_bin=${FASTBOOT_BIN:-fastboot}
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: timeout must be a positive integer" >&2; exit 2; }
command -v "$adb_bin" >/dev/null || { echo "ERROR: adb not found: $adb_bin" >&2; exit 1; }
command -v "$fastboot_bin" >/dev/null || { echo "ERROR: fastboot not found: $fastboot_bin" >&2; exit 1; }

state=$("$adb_bin" -s "$serial" get-state 2>/dev/null || true)
[[ "$state" == device ]] || { echo "ERROR: ADB device $serial is not ready (state=${state:-none})" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
printf 'fastboot\n' > "$work/reboot.request"
"$adb_bin" -s "$serial" push "$work/reboot.request" /tmp/reboot.request.new >/dev/null
"$adb_bin" -s "$serial" shell 'mv /tmp/reboot.request.new /tmp/reboot.request'

deadline=$((SECONDS + timeout))
while ((SECONDS < deadline)); do
  if "$fastboot_bin" -s "$serial" devices 2>/dev/null | awk '$2 == "fastboot" {found=1} END {exit !found}'; then
    "$fastboot_bin" -s "$serial" devices -l
    exit 0
  fi
  sleep 1
done

echo "ERROR: fastboot did not appear within ${timeout}s" >&2
printf '%s\n' "Current ADB state:" >&2
"$adb_bin" -s "$serial" devices -l >&2 || true
exit 124
