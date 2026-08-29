#!/usr/bin/env bash
set -euo pipefail

script="$(cd -- "$(dirname -- "$0")" && pwd -P)/package_feature.sh"
root="$(mktemp -d /tmp/libreecho-tts-contract.XXXXXX)"
trap 'rm -rf "$root"' EXIT

mkdir -p "$root/input/espeak-ng-data" "$root/licenses" "$root/bin"
printf 'phoneme\n' >"$root/input/espeak-ng-data/phontab"
printf 'index\n' >"$root/input/espeak-ng-data/phonindex"
printf 'daemon\n' >"$root/bin/ttsd"
printf 'model\n' >"$root/northern.onnx"
printf 'model\n' >"$root/female.onnx"
printf 'tokens\n' >"$root/tokens.txt"
printf 'license\n' >"$root/licenses/NOTICE"

mkdir -p "$root/pipeline"
cat >"$root/pipeline/package_feature_payload.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cp -a "$2" "$3.root"
printf '{}\n' >"$4"
touch "$3"
EOF
chmod 755 "$root/pipeline/package_feature_payload.sh"

TTS_PACKAGE_CONTRACT_TEST=1 LIBREECHO_PIPELINE_ROOT="$root/pipeline" \
  "$script" "$root/bin/ttsd" "$root/northern.onnx" "$root/female.onnx" \
  "$root/tokens.txt" "$root/input" "$root/tts.squashfs" "$root/tts.manifest.json"

for voice in northern-male southern-female; do
  data="$root/tts.squashfs.root/usr/local/share/libreecho/tts/models/$voice/espeak-ng-data"
  test -f "$data/phontab"
  test -f "$data/phonindex"
  test ! -e "$data/espeak-ng-data/phontab"
done

rm -rf "$root/linked-input" "$root/linked-output" "$root/linked-manifest.json"
mkdir -p "$root/linked-input"
ln -s "$root/input/espeak-ng-data" "$root/linked-input/espeak-ng-data"
if TTS_PACKAGE_CONTRACT_TEST=1 LIBREECHO_PIPELINE_ROOT="$root/pipeline" \
  "$script" "$root/bin/ttsd" "$root/northern.onnx" "$root/female.onnx" \
  "$root/tokens.txt" "$root/linked-input" "$root/linked-output" \
  "$root/linked-manifest.json"; then
  echo 'ERROR: nested eSpeak symlink was accepted' >&2
  exit 1
fi

echo 'TTS eSpeak data packaging behavior: ok'