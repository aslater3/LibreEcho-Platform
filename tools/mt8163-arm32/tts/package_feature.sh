#!/usr/bin/env bash
set -euo pipefail

TTSD="${1:?usage: package_feature.sh <ttsd> <northern-male-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
NORTHERN_MALE_MODEL="${2:?usage: package_feature.sh <ttsd> <northern-male-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
FEMALE_MODEL="${3:?usage: package_feature.sh <ttsd> <northern-male-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
TOKENS="${4:?usage: package_feature.sh <ttsd> <northern-male-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
ESPEAK_DATA="${5:?usage: package_feature.sh <ttsd> <northern-male-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
PAYLOAD="${6:?usage: package_feature.sh <ttsd> <northern-male-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
MANIFEST="${7:?usage: package_feature.sh <ttsd> <northern-male-model> <female-model> <tokens> <espeak-data> <payload> <manifest>}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
COMMON_LICENSE_DIR="$(dirname "$SCRIPT_DIR")/third-party-licenses"
PIPELINE_ROOT="${LIBREECHO_PIPELINE_ROOT:?ERROR: set LIBREECHO_PIPELINE_ROOT explicitly}"
PACKAGER="$PIPELINE_ROOT/package_feature_payload.sh"
THIRD_PARTY_NOTICES="$SCRIPT_DIR/THIRD_PARTY_NOTICES.md"
NORTHERN_MODEL_CARD="$SCRIPT_DIR/NORTHERN-MALE-MODEL-CARD.md"
SOUTHERN_MODEL_CARD="$SCRIPT_DIR/SOUTHERN-FEMALE-MODEL-CARD.md"
MODEL_LICENSE="$SCRIPT_DIR/CC-BY-SA-4.0.txt"

for input in "$TTSD" "$NORTHERN_MALE_MODEL" "$FEMALE_MODEL" "$TOKENS" \
    "$THIRD_PARTY_NOTICES" "$NORTHERN_MODEL_CARD" "$SOUTHERN_MODEL_CARD" \
    "$MODEL_LICENSE"; do
  [[ -f "$input" && ! -L "$input" ]] || {
    echo "ERROR: TTS input is missing or is a symlink: $input" >&2
    exit 1
  }
done
[[ -d "$COMMON_LICENSE_DIR" && ! -L "$COMMON_LICENSE_DIR" ]] || {
  echo "ERROR: common speech runtime license bundle is missing" >&2
  exit 1
}
[[ -d "$ESPEAK_DATA" && ! -L "$ESPEAK_DATA" ]] || {
  echo "ERROR: eSpeak data is missing or is a symlink: $ESPEAK_DATA" >&2
  exit 1
}
if [[ -d "$ESPEAK_DATA/espeak-ng-data" &&
      -f "$ESPEAK_DATA/espeak-ng-data/phontab" ]]; then
  ESPEAK_DATA="$ESPEAK_DATA/espeak-ng-data"
fi
[[ -f "$ESPEAK_DATA/phontab" && -f "$ESPEAK_DATA/phonindex" ]] || {
  echo "ERROR: eSpeak data root is missing phontab/phonindex: $ESPEAK_DATA" >&2
  exit 1
}
[[ -x "$PACKAGER" ]] || {
  echo "ERROR: feature packager is missing: $PACKAGER" >&2
  exit 1
}

northern_male_sha=$(sha256sum "$NORTHERN_MALE_MODEL" | awk '{print $1}')
female_sha=$(sha256sum "$FEMALE_MODEL" | awk '{print $1}')
tokens_sha=$(sha256sum "$TOKENS" | awk '{print $1}')
[[ "$northern_male_sha" == 786158f6507d49981889ece1803d8296adfcd34da847eb7e4ef69688ee148119 ]] || {
  echo "ERROR: Northern English male model hash is not the reviewed metadata-only derivative" >&2
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

for voice in northern-male southern-female; do
  model_dir="$root/usr/local/share/libreecho/tts/models/$voice"
  install -d "$model_dir"
  install -m 0644 "$TOKENS" "$model_dir/tokens.txt"
  cp -a "$ESPEAK_DATA" "$model_dir/espeak-ng-data"
  [[ -f "$model_dir/espeak-ng-data/phontab" &&
     -f "$model_dir/espeak-ng-data/phonindex" ]] || {
    echo "ERROR: packaged eSpeak data is incomplete for $voice" >&2
    exit 1
  }
done
install -m 0644 "$NORTHERN_MALE_MODEL" \
  "$root/usr/local/share/libreecho/tts/models/northern-male/model.onnx"
install -m 0644 "$FEMALE_MODEL" \
  "$root/usr/local/share/libreecho/tts/models/southern-female/model.onnx"

license_root="$root/usr/local/share/licenses/libreecho-tts"
install -d "$license_root"
install -m 0644 "$THIRD_PARTY_NOTICES" "$license_root/THIRD_PARTY_NOTICES.md"
install -m 0644 "$NORTHERN_MODEL_CARD" "$license_root/NORTHERN-MALE-MODEL-CARD.md"
install -m 0644 "$SOUTHERN_MODEL_CARD" "$license_root/SOUTHERN-FEMALE-MODEL-CARD.md"
install -m 0644 "$MODEL_LICENSE" "$license_root/CC-BY-SA-4.0.txt"
runtime_license_root="$license_root/runtime"
install -d "$runtime_license_root"
for input in "$COMMON_LICENSE_DIR"/*; do
  [[ -f "$input" && ! -L "$input" ]] || {
    echo "ERROR: unsafe common runtime license input: $input" >&2
    exit 1
  }
  install -m 0644 "$input" "$runtime_license_root/$(basename "$input")"
done

"$PACKAGER" tts "$root" "$PAYLOAD" "$MANIFEST"
