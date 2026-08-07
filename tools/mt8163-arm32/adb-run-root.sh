#!/usr/bin/env bash
# Run one script as root through the deliberate LibreEcho /tmp/runme runner.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: adb-run-root.sh SCRIPT [TIMEOUT_SECONDS]

Environment:
  ADB_SERIAL   target serial (must be supplied by the operator)
  ADB_BIN      adb executable (default: adb)

The development OS intentionally does not depend on an interactive adb shell.
This helper pushes a nonce-marked script to /tmp/runme, waits for /tmp/result,
prints the command output, and returns the remote script's exit status.
EOF
}

[[ ${1:-} == -h || ${1:-} == --help ]] && { usage; exit 0; }
[[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 2; }

script=$1
timeout=${2:-30}
serial=${ADB_SERIAL:-}
[[ -n "$serial" ]] || { echo "ERROR: ADB_SERIAL must be supplied" >&2; exit 2; }
adb_bin=${ADB_BIN:-adb}
[[ -f "$script" ]] || { echo "ERROR: script not found: $script" >&2; exit 2; }
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: timeout must be a positive integer" >&2; exit 2; }
command -v "$adb_bin" >/dev/null || { echo "ERROR: adb not found: $adb_bin" >&2; exit 1; }

state=$("$adb_bin" -s "$serial" get-state 2>/dev/null || true)
[[ "$state" == device ]] || { echo "ERROR: ADB device $serial is not ready (state=${state:-none})" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
nonce="LIBREECHO_RUNME_$(date +%s)_$$_$RANDOM"
wrapper="$work/runme"
result="$work/result"

{
  printf '#!/bin/busybox sh\n'
  printf 'printf "%s\\n" %q\n' "$nonce" "$nonce"
  printf 'set +e\n'
  printf 'trap '\''rc=$?; printf "LIBREECHO_RUNME_RC=%%s\\n" "$rc"'\'' EXIT\n'
  printf '# ---- user script begins ----\n'
  cat -- "$script"
  printf '\n# ---- user script ends ----\n'
} > "$wrapper"

"$adb_bin" -s "$serial" push "$wrapper" /tmp/runme >/dev/null

deadline=$((SECONDS + timeout))
while ((SECONDS < deadline)); do
  if "$adb_bin" -s "$serial" pull /tmp/result "$result" >/dev/null 2>&1 &&
     grep -Fxq "$nonce" "$result" &&
     grep -Eq '^LIBREECHO_RUNME_RC=[0-9]+$' "$result"; then
    rc_line=$(grep '^LIBREECHO_RUNME_RC=[0-9][0-9]*$' "$result" | tail -1 || true)
    [[ -n "$rc_line" ]] || {
      echo "ERROR: root script ran but did not publish an exit status (did it call exit?)" >&2
      sed "1{/^${nonce}$/d;}" "$result"
      exit 1
    }
    rc=${rc_line#LIBREECHO_RUNME_RC=}
    sed -e "1{/^${nonce}$/d;}" -e '/^LIBREECHO_RUNME_RC=[0-9][0-9]*$/d' "$result"
    exit "$rc"
  fi
  sleep 1
done

echo "ERROR: timed out after ${timeout}s waiting for root result" >&2
printf 'cancel\n' > "$work/cancel"
"$adb_bin" -s "$serial" push "$work/cancel" /tmp/runme.cancel >/dev/null 2>&1 || true
exit 124
