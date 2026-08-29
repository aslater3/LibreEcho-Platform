#!/usr/bin/env bash
set -euo pipefail

script="$(cd -- "$(dirname -- "$0")" && pwd -P)/package_feature.sh"
grep -q 'ESPEAK_DATA/espeak-ng-data/phontab' "$script"
grep -q 'ESPEAK_DATA/phontab' "$script"
grep -q 'ESPEAK_DATA/phontab.*phonindex' "$script"
grep -q 'packaged eSpeak data is incomplete' "$script"
echo 'TTS eSpeak data-root contract: ok'