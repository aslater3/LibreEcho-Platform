#!/usr/bin/env bash
set -euo pipefail

AGENTD="${1:?usage: package_feature.sh <agentd> <curl> <ca-bundle> <curl-license> <ca-copyright> <payload> <manifest>}"
CURL="${2:?usage: package_feature.sh <agentd> <curl> <ca-bundle> <curl-license> <ca-copyright> <payload> <manifest>}"
CA_BUNDLE="${3:?usage: package_feature.sh <agentd> <curl> <ca-bundle> <curl-license> <ca-copyright> <payload> <manifest>}"
CURL_LICENSE="${4:?usage: package_feature.sh <agentd> <curl> <ca-bundle> <curl-license> <ca-copyright> <payload> <manifest>}"
CA_COPYRIGHT="${5:?usage: package_feature.sh <agentd> <curl> <ca-bundle> <curl-license> <ca-copyright> <payload> <manifest>}"
PAYLOAD="${6:?usage: package_feature.sh <agentd> <curl> <ca-bundle> <curl-license> <ca-copyright> <payload> <manifest>}"
MANIFEST="${7:?usage: package_feature.sh <agentd> <curl> <ca-bundle> <curl-license> <ca-copyright> <payload> <manifest>}"
PIPELINE_ROOT="$(cd -- "$(dirname -- "$0")/../../../../pipeline" && pwd -P)"
PACKAGER="$PIPELINE_ROOT/package_feature_payload.sh"
CA_SHA256=c0c940a0e30d859783f7f130868d8082e79936ff0b41a0b1098ac7f98909263b

for input in "$AGENTD" "$CURL" "$CA_BUNDLE" "$CURL_LICENSE" \
    "$CA_COPYRIGHT"; do
  [[ -f "$input" && ! -L "$input" ]] || {
    echo "ERROR: assistant input is missing or is a symlink: $input" >&2
    exit 1
  }
done
[[ -x "$PACKAGER" ]] || {
  echo "ERROR: feature packager is missing: $PACKAGER" >&2
  exit 1
}
[[ "$(sha256sum "$CA_BUNDLE" | awk '{print $1}')" == "$CA_SHA256" ]] || {
  echo "ERROR: CA bundle is not the reviewed 2026-06-01 bundle" >&2
  exit 1
}
for executable in "$AGENTD" "$CURL"; do
  file -b "$executable" | grep -Eq 'ELF 32-bit.*ARM.*statically linked' || {
    echo "ERROR: assistant executable is not static ARM32: $executable" >&2
    exit 1
  }
  if readelf -l "$executable" | grep -q 'Requesting program interpreter'; then
    echo "ERROR: assistant executable has a dynamic interpreter: $executable" >&2
    exit 1
  fi
done

root="$(mktemp -d /tmp/libreecho-assistant-feature.XXXXXX)"
trap 'rm -rf "$root"' EXIT
install -d "$root/usr/local/sbin" "$root/usr/local/libexec" \
  "$root/usr/local/share/libreecho" \
  "$root/usr/local/share/licenses/curl" \
  "$root/usr/local/share/licenses/ca-certificates"
install -m 0755 "$AGENTD" "$root/usr/local/sbin/libreecho-agentd"
install -m 0755 "$CURL" "$root/usr/local/libexec/libreecho-curl"
install -m 0644 "$CA_BUNDLE" \
  "$root/usr/local/share/libreecho/cacert.pem"
install -m 0644 "$CURL_LICENSE" \
  "$root/usr/local/share/licenses/curl/COPYING"
install -m 0644 "$CA_COPYRIGHT" \
  "$root/usr/local/share/licenses/ca-certificates/copyright"

"$PACKAGER" assistant "$root" "$PAYLOAD" "$MANIFEST"
