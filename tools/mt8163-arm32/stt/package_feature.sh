#!/usr/bin/env bash
set -euo pipefail

STTD="${1:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
ENCODER="${2:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
DECODER="${3:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
JOINER="${4:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
TOKENS="${5:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
MODEL_LICENSE="${6:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
PAYLOAD="${7:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
MANIFEST="${8:?usage: package_feature.sh <sttd> <encoder> <decoder> <joiner> <tokens> <model-license> <payload> <manifest>}"
PIPELINE_ROOT="${LIBREECHO_PIPELINE_ROOT:?ERROR: set LIBREECHO_PIPELINE_ROOT explicitly}"
PACKAGER="$PIPELINE_ROOT/package_feature_payload.sh"

for input in "$STTD" "$ENCODER" "$DECODER" "$JOINER" "$TOKENS" \
    "$MODEL_LICENSE"; do
  [[ -f "$input" && ! -L "$input" ]] || {
    echo "ERROR: STT input is missing or is a symlink: $input" >&2
    exit 1
  }
done
[[ -x "$PACKAGER" ]] || {
  echo "ERROR: feature packager is missing: $PACKAGER" >&2
  exit 1
}
[[ "$(sha256sum "$ENCODER" | awk '{print $1}')" == \
   3810755ce7c3ab26b42a8bcf39d191308fa27fb0f53358823ba46141d03b7eb3 ]] || {
  echo "ERROR: STT encoder is not the reviewed English int8 model" >&2
  exit 1
}
[[ "$(sha256sum "$DECODER" | awk '{print $1}')" == \
   21e2a2acd961b3ac72f55be2f10f1a285e1b0b0ba010d7c0b6eab141411b163c ]] || {
  echo "ERROR: STT decoder is not the reviewed English int8 model" >&2
  exit 1
}
[[ "$(sha256sum "$JOINER" | awk '{print $1}')" == \
   e085d73b593cf9b0707f370dbd656d58327d3fe36d80d849202ef81df02cb01e ]] || {
  echo "ERROR: STT joiner is not the reviewed English int8 model" >&2
  exit 1
}
[[ "$(sha256sum "$TOKENS" | awk '{print $1}')" == \
   49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb ]] || {
  echo "ERROR: STT token table is not the reviewed English table" >&2
  exit 1
}
[[ "$(sha256sum "$MODEL_LICENSE" | awk '{print $1}')" == \
   505f6b0e8a39f066a0794c4fb0b5689533d3bcd9d1dc5e5f47ccffeef1af9877 ]] || {
  echo "ERROR: STT model license notice changed" >&2
  exit 1
}
file -b "$STTD" | grep -Eq 'ELF 32-bit.*ARM.*statically linked' || {
  echo "ERROR: STT daemon is not a static ARM32 executable" >&2
  exit 1
}
if readelf -l "$STTD" | grep -q 'Requesting program interpreter'; then
  echo "ERROR: STT daemon has a dynamic interpreter" >&2
  exit 1
fi

root="$(mktemp -d /tmp/libreecho-stt-feature.XXXXXX)"
trap 'rm -rf "$root"' EXIT
model_root="$root/usr/local/share/libreecho/stt"
license_root="$root/usr/local/share/licenses/libreecho-stt-model"
install -d "$root/usr/local/sbin" "$model_root" "$license_root"
install -m 0755 "$STTD" "$root/usr/local/sbin/libreecho-sttd"
install -m 0644 "$ENCODER" \
  "$model_root/encoder-epoch-99-avg-1.int8.onnx"
install -m 0644 "$DECODER" \
  "$model_root/decoder-epoch-99-avg-1.int8.onnx"
install -m 0644 "$JOINER" \
  "$model_root/joiner-epoch-99-avg-1.int8.onnx"
install -m 0644 "$TOKENS" "$model_root/tokens.txt"
install -m 0644 "$MODEL_LICENSE" "$license_root/MODEL-LICENSE.md"

"$PACKAGER" stt "$root" "$PAYLOAD" "$MANIFEST"
