#!/usr/bin/env bash
set -euo pipefail

AIRPLAY_OUTPUT="${1:?usage: package_feature.sh <airplay-build-output> <ui-bundle> <payload> <manifest>}"
UI_BUNDLE="${2:?usage: package_feature.sh <airplay-build-output> <ui-bundle> <payload> <manifest>}"
PAYLOAD="${3:?usage: package_feature.sh <airplay-build-output> <ui-bundle> <payload> <manifest>}"
MANIFEST="${4:?usage: package_feature.sh <airplay-build-output> <ui-bundle> <payload> <manifest>}"
PIPELINE_ROOT="${LIBREECHO_PIPELINE_ROOT:?ERROR: set LIBREECHO_PIPELINE_ROOT explicitly}"
PACKAGER="$PIPELINE_ROOT/package_feature_payload.sh"

[[ -x "$PACKAGER" ]] || { echo "ERROR: feature packager is missing: $PACKAGER" >&2; exit 1; }
[[ -d "$AIRPLAY_OUTPUT" && -d "$AIRPLAY_OUTPUT/runtime" ]] || {
  echo "ERROR: AirPlay build output/runtime is missing" >&2
  exit 1
}
[[ -f "$UI_BUNDLE/etc/libreecho/airplay2.conf" ]] || {
  echo "ERROR: AirPlay UI configuration is missing from the UI bundle" >&2
  exit 1
}
LICENSE_ROOT="$AIRPLAY_OUTPUT/runtime/usr/local/share/licenses/libreecho-airplay"
[[ -f "$LICENSE_ROOT/COMPONENTS.tsv" ]] || {
  echo "ERROR: AirPlay component inventory is missing" >&2
  exit 1
}
for component in nqptp shairport-sync ffmpeg tinyalsa; do
  find "$LICENSE_ROOT/source/$component" -maxdepth 1 -type f -print -quit | grep -q . || {
    echo "ERROR: AirPlay source license closure is missing for $component" >&2
    exit 1
  }
done
find "$LICENSE_ROOT/debian" -mindepth 2 -maxdepth 2 -type f -name copyright \
  -print -quit | grep -q . || {
    echo "ERROR: AirPlay Debian copyright closure is missing" >&2
    exit 1
  }

root="$(mktemp -d /tmp/libreecho-airplay-feature.XXXXXX)"
trap 'rm -rf "$root"' EXIT
cp -a "$AIRPLAY_OUTPUT/runtime/." "$root/"
mkdir -p "$root/usr/local/sbin" "$root/etc/libreecho" \
  "$root/dev/shm" "$root/proc" "$root/sys" "$root/run" "$root/var" \
  "$root/etc/avahi" "$root/etc/dbus-1/system.d"
for binary in nqptp shairport-sync libreecho-airplay-audio \
  libreecho-audio-engine avahi-daemon dbus-daemon
do
  install -m 0755 "$AIRPLAY_OUTPUT/$binary" "$root/usr/local/sbin/$binary"
done
install -m 0644 "$UI_BUNDLE/etc/libreecho/airplay2.conf" \
  "$root/etc/libreecho/airplay2.conf"
printf '%s\n' '127.0.0.1 localhost libreecho libreecho-dev' > "$root/etc/hosts"
printf '%s\n' 'nameserver 127.0.0.1' > "$root/etc/resolv.conf"
printf '%s\n' \
  'root:x:0:0:root:/root:/bin/sh' \
  'messagebus:x:102:105::/nonexistent:/sbin/nologin' \
  'avahi:x:114:121:Avahi mDNS daemon,,,:/run/avahi-daemon:/sbin/nologin' \
  > "$root/etc/passwd"
printf '%s\n' \
  'root:x:0:' \
  'messagebus:x:105:' \
  'avahi:x:121:' \
  'netdev:x:106:' \
  > "$root/etc/group"

"$PACKAGER" airplay2 "$root" "$PAYLOAD" "$MANIFEST"
