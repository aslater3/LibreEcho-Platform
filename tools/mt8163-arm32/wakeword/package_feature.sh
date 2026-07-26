#!/usr/bin/env bash
set -euo pipefail

WAKED="${1:?usage: package_feature.sh <waked> <mel-model> <embedding-model> <wake-model> <payload> <manifest>}"
MEL_MODEL="${2:?usage: package_feature.sh <waked> <mel-model> <embedding-model> <wake-model> <payload> <manifest>}"
EMBEDDING_MODEL="${3:?usage: package_feature.sh <waked> <mel-model> <embedding-model> <wake-model> <payload> <manifest>}"
WAKE_MODEL="${4:?usage: package_feature.sh <waked> <mel-model> <embedding-model> <wake-model> <payload> <manifest>}"
PAYLOAD="${5:?usage: package_feature.sh <waked> <mel-model> <embedding-model> <wake-model> <payload> <manifest>}"
MANIFEST="${6:?usage: package_feature.sh <waked> <mel-model> <embedding-model> <wake-model> <payload> <manifest>}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PIPELINE_ROOT="$(cd -- "$SCRIPT_DIR/../../../../pipeline" && pwd -P)"
PACKAGER="$PIPELINE_ROOT/package_feature_payload.sh"
LICENSE_NOTICE="$SCRIPT_DIR/MODEL-LICENSE.txt"

for input in "$WAKED" "$MEL_MODEL" "$EMBEDDING_MODEL" "$WAKE_MODEL" \
    "$LICENSE_NOTICE"; do
  [[ -f "$input" && ! -L "$input" ]] || {
    echo "ERROR: wakeword input is missing or is a symlink: $input" >&2
    exit 1
  }
done
[[ -x "$PACKAGER" ]] || {
  echo "ERROR: feature packager is missing: $PACKAGER" >&2
  exit 1
}

[[ "$(sha256sum "$MEL_MODEL" | awk '{print $1}')" == \
   ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f ]] || {
  echo "ERROR: mel model hash is not the reviewed openWakeWord model" >&2
  exit 1
}
[[ "$(sha256sum "$EMBEDDING_MODEL" | awk '{print $1}')" == \
   70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f ]] || {
  echo "ERROR: embedding model hash is not the reviewed openWakeWord model" >&2
  exit 1
}
[[ "$(sha256sum "$WAKE_MODEL" | awk '{print $1}')" == \
   6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e ]] || {
  echo "ERROR: Alexa model hash is not the reviewed development model" >&2
  exit 1
}
file -b "$WAKED" | grep -Eq 'ELF 32-bit.*ARM.*statically linked' || {
  echo "ERROR: wakeword daemon is not a static ARM32 executable" >&2
  exit 1
}
if readelf -l "$WAKED" | grep -q 'Requesting program interpreter'; then
  echo "ERROR: wakeword daemon has a dynamic interpreter" >&2
  exit 1
fi

root="$(mktemp -d /tmp/libreecho-wakeword-feature.XXXXXX)"
trap 'rm -rf "$root"' EXIT
model_root="$root/usr/local/share/libreecho/openwakeword"
license_root="$root/usr/local/share/licenses/libreecho-openwakeword"
install -d "$root/usr/local/sbin" "$model_root" "$license_root"
install -m 0755 "$WAKED" "$root/usr/local/sbin/libreecho-waked"
install -m 0644 "$MEL_MODEL" "$model_root/melspectrogram.onnx"
install -m 0644 "$EMBEDDING_MODEL" "$model_root/embedding_model.onnx"
install -m 0644 "$WAKE_MODEL" "$model_root/alexa_v0.1.onnx"
install -m 0644 "$LICENSE_NOTICE" "$license_root/MODEL-LICENSE.txt"

"$PACKAGER" wakeword "$root" "$PAYLOAD" "$MANIFEST"
