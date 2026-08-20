#!/usr/bin/env bash
# Build the LibreEcho emulation container from a release boot.img.
#
#   ./build.sh <boot.img> [assistant.squashfs]
#
# The optional assistant.squashfs (unsquashfs required) adds libreecho-agentd so
# the voice assistant service is available in the emulation.
set -euo pipefail
cd "$(dirname "$0")"

BOOT=${1:?usage: build.sh <boot.img> [assistant.squashfs]}
FEATURE=${2:-}
IMAGE=${IMAGE:-libreecho-emu:latest}

rm -rf rootfs && mkdir -p rootfs
python3 unpack-bootimg.py "$BOOT" rootfs

if [[ -n "$FEATURE" ]]; then
  command -v unsquashfs >/dev/null || { echo "unsquashfs required for the assistant feature" >&2; exit 1; }
  unsquashfs -f -d rootfs "$FEATURE"
  echo "assistant feature merged (agentd available)"
fi

docker build --platform linux/arm/v7 -t "$IMAGE" .
echo
echo "Built $IMAGE. Run it with:"
echo "  docker run -d --name libreecho-emu --platform linux/arm/v7 --memory 485m -p 8080:8080 $IMAGE"
echo "For the simulated-hardware (mock) backend, override the entrypoint:"
echo "  docker run -d --platform linux/arm/v7 -p 8080:8080 --entrypoint /bin/busybox $IMAGE sh /entrypoint-mock.sh"
