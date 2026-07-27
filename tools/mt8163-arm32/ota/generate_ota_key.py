#!/usr/bin/env python3
"""Generate a local Ed25519 OTA signing key and its distributable public key."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from nacl.signing import SigningKey


def write_new(path: Path, data: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    args = parser.parse_args()
    args.private_key.parent.mkdir(parents=True, exist_ok=True)
    args.public_key.parent.mkdir(parents=True, exist_ok=True)
    key = SigningKey.generate()
    write_new(args.private_key, key.encode().hex() + "\n", 0o600)
    write_new(args.public_key, key.verify_key.encode().hex() + "\n", 0o644)
    print(f"private_key={args.private_key.resolve()}")
    print(f"public_key={args.public_key.resolve()}")


if __name__ == "__main__":
    main()
