#!/usr/bin/env bash
set -euo pipefail

TTSD="${1:?usage: package_feature.sh <ttsd> <alan-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
ALAN_MODEL="${2:?usage: package_feature.sh <ttsd> <alan-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
FEMALE_MODEL="${3:?usage: package_feature.sh <ttsd> <alan-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
TOKENS="${4:?usage: package_feature.sh <ttsd> <alan-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
ESPEAK_DATA="${5:?usage: package_feature.sh <ttsd> <alan-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
PAYLOAD="${6:?usage: package_feature.sh <ttsd> <alan-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
MANIFEST="${7:?usage: package_feature.sh <ttsd> <alan-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
PIPELINE_ROOT="${LIBREECHO_PIPELINE_ROOT:?ERROR: set LIBREECHO_PIPELINE_ROOT explicitly}"
PACKAGER="$PIPELINE_ROOT/package_feature_payload.sh"

for input in "$TTSD" "$ALAN_MODEL" "$FEMALE_MODEL" "$TOKENS"; do
  [[ -f "$input" && ! -L "$input" ]] || {
    echo "ERROR: TTS input is missing or is a symlink: $input" >&2
    exit 1
  }
done
[[ -d "$ESPEAK_DATA" && ! -L "$ESPEAK_DATA" ]] || {
  echo "ERROR: eSpeak data is missing or is a symlink: $ESPEAK_DATA" >&2
  exit 1
}
[[ -x "$PACKAGER" ]] || {
  echo "ERROR: feature packager is missing: $PACKAGER" >&2
  exit 1
}

alan_sha=$(sha256sum "$ALAN_MODEL" | awk '{print $1}')
female_sha=$(sha256sum "$FEMALE_MODEL" | awk '{print $1}')
tokens_sha=$(sha256sum "$TOKENS" | awk '{print $1}')
[[ "$alan_sha" == 1e49226821b889e41ee3ebc189df6f24914057a9d3481c7b7291c611033a049e ]] || {
  echo "ERROR: Alan model hash is not the reviewed optimized model" >&2
  exit 1
}
[[ "$female_sha" == cf7f487689da2ec115cb5e9b5fb5ff4450f24e0c45565e0b72dd1eb4ed4caf65 ]] || {
  echo "ERROR: Southern English female model hash is not the reviewed optimized model" >&2
  exit 1
}
[[ "$tokens_sha" == 42d1a69ed2b91a51928a711aa228ed9f3dc021c6d359a3e9c4f37eb1d20f80bd ]] || {
  echo "ERROR: VITS token table hash is not the reviewed English table" >&2
  exit 1
}
file -b "$TTSD" | grep -Eq 'ELF 32-bit.*ARM.*statically linked' || {
  echo "ERROR: TTS daemon is not a static ARM32 executable" >&2
  exit 1
}
if readelf -l "$TTSD" | grep -q 'Requesting program interpreter'; then
  echo "ERROR: TTS daemon has a dynamic interpreter" >&2
  exit 1
fi

root="$(mktemp -d /tmp/libreecho-tts-feature.XXXXXX)"
trap 'rm -rf "$root"' EXIT
install -d "$root/usr/local/sbin"
install -m 0755 "$TTSD" "$root/usr/local/sbin/libreecho-ttsd"

for voice in alan southern-female; do
  model_dir="$root/usr/local/share/libreecho/tts/models/$voice"
  install -d "$model_dir"
  install -m 0644 "$TOKENS" "$model_dir/tokens.txt"
  cp -a "$ESPEAK_DATA" "$model_dir/espeak-ng-data"
done
install -m 0644 "$ALAN_MODEL" \
  "$root/usr/local/share/libreecho/tts/models/alan/model.onnx"
install -m 0644 "$FEMALE_MODEL" \
  "$root/usr/local/share/libreecho/tts/models/southern-female/model.onnx"

"$PACKAGER" tts "$root" "$PAYLOAD" "$MANIFEST"
