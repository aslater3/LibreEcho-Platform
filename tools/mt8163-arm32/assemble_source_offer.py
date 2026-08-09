#!/usr/bin/env python3
"""Build a deterministic corresponding-source and relink-object offer."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _logical_path(value: str) -> str:
    logical = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or logical.is_absolute()
        or any(part in {"", ".", ".."} for part in logical.parts)
        or str(logical) != value
    ):
        raise ValueError(f"unsafe logical path: {value}")
    return value


def _regular(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"input is not a regular non-symlink file: {path}")
    return path


def _tarinfo(name: str, size: int, source_date_epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = source_date_epoch
    return info


def assemble(
    *,
    component: str,
    output: Path,
    source_files: list[tuple[Path, str]],
    relink_files: list[tuple[Path, str]],
    metadata: dict[str, str],
    source_date_epoch: int,
) -> dict[str, object]:
    if not COMPONENT.fullmatch(component):
        raise ValueError(f"invalid component identifier: {component}")
    if source_date_epoch < 0:
        raise ValueError("source date epoch must be non-negative")
    if output.is_symlink() or output.exists():
        raise ValueError(f"refusing to overwrite source offer: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    members: list[dict[str, object]] = []
    inputs: list[tuple[str, Path, str]] = []
    seen: set[str] = set()
    for kind, entries in (("source", source_files), ("relink-object", relink_files)):
        for path, raw_logical in entries:
            logical = _logical_path(raw_logical)
            if logical in seen:
                raise ValueError(f"duplicate logical path: {logical}")
            seen.add(logical)
            path = _regular(path)
            inputs.append((logical, path, kind))
            members.append(
                {
                    "kind": kind,
                    "path": logical,
                    "sha256": sha256(path),
                    "size": path.stat().st_size,
                }
            )
    members.sort(key=lambda item: str(item["path"]))
    inputs.sort(key=lambda item: item[0])
    manifest: dict[str, object] = {
        "schema_version": 1,
        "component": component,
        "source_date_epoch": source_date_epoch,
        "metadata": dict(sorted(metadata.items())),
        "members": members,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()

    with output.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9,
                           mtime=source_date_epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                archive.addfile(
                    _tarinfo("SOURCE-OFFER-MANIFEST.json", len(manifest_bytes), source_date_epoch),
                    io.BytesIO(manifest_bytes),
                )
                for logical, path, _kind in inputs:
                    with path.open("rb") as stream:
                        archive.addfile(
                            _tarinfo(logical, path.stat().st_size, source_date_epoch),
                            stream,
                        )
    return manifest


def _mapping(value: str) -> tuple[Path, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected LOGICAL=PATH")
    logical, raw_path = value.split("=", 1)
    try:
        _logical_path(logical)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return Path(raw_path), logical


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-file", action="append", default=[], type=_mapping,
                        metavar="LOGICAL=PATH")
    parser.add_argument("--relink-file", action="append", default=[], type=_mapping,
                        metavar="LOGICAL=PATH")
    parser.add_argument("--metadata", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--source-date-epoch", required=True, type=int)
    args = parser.parse_args()
    metadata: dict[str, str] = {}
    for value in args.metadata:
        if "=" not in value:
            parser.error("--metadata expects KEY=VALUE")
        key, item = value.split("=", 1)
        if not key or key in metadata:
            parser.error("metadata keys must be non-empty and unique")
        metadata[key] = item
    manifest = assemble(
        component=args.component,
        output=args.output,
        source_files=args.source_file,
        relink_files=args.relink_file,
        metadata=metadata,
        source_date_epoch=args.source_date_epoch,
    )
    sidecar = args.output.with_suffix(args.output.suffix + ".manifest.json")
    sidecar.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"source_offer={args.output}")
    print(f"source_offer_sha256={sha256(args.output)}")
    print(f"source_offer_manifest={sidecar}")
    print(
        f"source_offer_member_count="
        f"{len(args.source_file) + len(args.relink_file)}"
    )


if __name__ == "__main__":
    main()
