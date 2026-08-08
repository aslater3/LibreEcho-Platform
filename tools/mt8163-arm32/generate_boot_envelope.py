#!/usr/bin/env python3
"""Generate the redistributable Radar Puffin Android-v0 boot envelope.

The output contains only reviewed board/header constants and zero-filled
capacity. It never reads or copies a stock boot image.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path

ANDROID_MAGIC = b"ANDROID!"
IMAGE_SIZE = 0x1000000
PAGE_SIZE = 0x800
KERNEL_ADDR = 0x40008000
RAMDISK_ADDR = 0x43478000
SECOND_ADDR = 0x40F00000
TAGS_ADDR = 0x48000000
BOOTOPT = b"bootopt=64S3,32N2,32N2"


def generate() -> bytes:
    image = bytearray(IMAGE_SIZE)
    image[:8] = ANDROID_MAGIC
    struct.pack_into(
        "<10I",
        image,
        8,
        0, KERNEL_ADDR, 0, RAMDISK_ADDR,
        0, SECOND_ADDR, TAGS_ADDR, PAGE_SIZE, 0, 0,
    )
    image[64:64 + len(BOOTOPT)] = BOOTOPT
    return bytes(image)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"ERROR: refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    data = generate()
    output.write_bytes(data)
    print(f"boot_envelope={output}")
    print(f"boot_envelope_sha256={hashlib.sha256(data).hexdigest()}")
    print(f"boot_envelope_size={len(data)}")


if __name__ == "__main__":
    main()
