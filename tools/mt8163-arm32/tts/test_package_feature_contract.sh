#!/usr/bin/env bash
set -euo pipefail

script="$(cd -- "$(dirname -- "$0")" && pwd -P)/package_feature.sh"
root="$(mktemp -d /tmp/libreecho-tts-contract.XXXXXX)"
trap 'rm -rf "$root"' EXIT

payload="$root/tts.squashfs"
payload_url="https://github.com/aslater3/LibreEcho/releases/download/"
payload_url+="radar-puffin-build-70bcb92-8ef37f6bfbdc8cab-177f49b75ac9ce88/"
payload_url+="libreecho-radar-puffin-build-70bcb92-8ef37f6bfbdc8cab-177f49b75ac9ce88-tts.squashfs"
curl -fsSL --retry 3 -o "$payload" "$payload_url"
printf '%s  %s\n' \
  53033508bd7e70048a2b89d214de93cbcbf9901753ef211af84077c39f051160 \
  "$payload" | sha256sum -c -
unsquashfs -quiet -d "$root/tree" "$payload"

input="$root/input"
ttsd="$root/tree/usr/local/sbin/libreecho-ttsd"
northern="$root/tree/usr/local/share/libreecho/tts/models/northern-male/model.onnx"
female="$root/tree/usr/local/share/libreecho/tts/models/southern-female/model.onnx"
tokens="$root/tree/usr/local/share/libreecho/tts/models/northern-male/tokens.txt"
mkdir -p "$input"
cp -a /usr/lib/x86_64-linux-gnu/espeak-ng-data "$input/espeak-ng-data"
test -f "$input/espeak-ng-data/phontab"
test -f "$input/espeak-ng-data/phonindex"
test -x "$ttsd"
test -f "$northern"
test -f "$female"
test -f "$tokens"

mkdir -p "$root/pipeline"
cat >"$root/pipeline/package_feature_payload.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cp -a "$2" "$3.root"
printf '{}\n' >"$4"
touch "$3"
EOF
chmod 755 "$root/pipeline/package_feature_payload.sh"

LIBREECHO_PIPELINE_ROOT="$root/pipeline" \
  "$script" "$ttsd" "$northern" "$female" "$tokens" "$input" \
  "$root/tts.squashfs.out" "$root/tts.manifest.json"

for voice in northern-male southern-female; do
  data="$root/tts.squashfs.out.root/usr/local/share/libreecho/tts/models/$voice/espeak-ng-data"
  test -f "$data/phontab"
  test -f "$data/phonindex"
  test ! -e "$data/espeak-ng-data/phontab"
done

rm -rf "$root/linked-input" "$root/linked-output" "$root/linked-manifest.json"
mkdir -p "$root/linked-input"
ln -s "$input/espeak-ng-data" "$root/linked-input/espeak-ng-data"
if LIBREECHO_PIPELINE_ROOT="$root/pipeline" \
  "$script" "$ttsd" "$northern" "$female" "$tokens" "$root/linked-input" \
  "$root/linked-output" "$root/linked-manifest.json"; then
  echo 'ERROR: nested eSpeak symlink was accepted' >&2
  exit 1
fi

echo 'TTS eSpeak data packaging behavior: ok'