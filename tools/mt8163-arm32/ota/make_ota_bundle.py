#!/usr/bin/env python3
"""Create a deterministic signed LibreEcho OTA v1 tar."""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import tarfile
from pathlib import Path

from nacl.signing import SigningKey


BOOT_SIZE = 16 * 1024 * 1024
VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,95}\Z")


def read_signing_key(path: Path) -> SigningKey:
    text = path.read_text(encoding="ascii").strip()
    if len(text) != 64 or not re.fullmatch(r"[0-9a-f]{64}", text):
        raise SystemExit("ERROR: signing key must be 32 raw bytes as 64 lowercase hex characters")
    return SigningKey(bytes.fromhex(text))


def tar_record(archive: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    record = tarfile.TarInfo(name)
    record.size = len(data)
    record.mode = mode
    record.uid = record.gid = 0
    record.uname = record.gname = "root"
    record.mtime = 0
    archive.addfile(record, io.BytesIO(data))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--boot-image", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--service-profile", choices=("diagnostic", "production"),
                        default="diagnostic")
    parser.add_argument("--feature-policy", choices=("exclude", "preserve"),
                        default="preserve")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not VALUE_RE.fullmatch(args.version):
        raise SystemExit("ERROR: version contains unsupported characters")
    if args.output.exists():
        raise SystemExit(f"ERROR: refusing to overwrite {args.output}")
    boot = args.boot_image.read_bytes()
    if len(boot) != BOOT_SIZE or boot[:8] != b"ANDROID!":
        raise SystemExit("ERROR: boot image must be an exact 16 MiB Android boot image")

    digest = hashlib.sha256(boot).hexdigest()
    manifest = (
        "format=libreecho-ota-v1\n"
        "manifest_version=1\n"
        "board=radar_puffin\n"
        "soc=mt8163\n"
        "architecture=armv7\n"
        f"version={args.version}\n"
        "boot_filename=boot.img\n"
        f"boot_size={len(boot)}\n"
        f"boot_sha256={digest}\n"
        f"feature_policy={args.feature_policy}\n"
        "image_profile=ota\n"
        f"service_profile={args.service_profile}\n"
    ).encode("ascii")
    signing_key = read_signing_key(args.signing_key)
    public_key = args.public_key.read_text(encoding="ascii").strip()
    if public_key != signing_key.verify_key.encode().hex():
        raise SystemExit("ERROR: signing key does not match the embedded OTA public key")
    signature = signing_key.sign(manifest).signature.hex().encode("ascii") + b"\n"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w", format=tarfile.USTAR_FORMAT) as archive:
        tar_record(archive, "manifest", manifest, 0o644)
        tar_record(archive, "manifest.sig", signature, 0o644)
        tar_record(archive, "boot.img", boot, 0o644)
    print(f"ota_bundle={args.output.resolve()}")
    print(f"ota_version={args.version}")
    print(f"boot_sha256={digest}")
    print(f"bundle_sha256={hashlib.sha256(args.output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
