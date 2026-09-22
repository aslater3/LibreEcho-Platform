#!/usr/bin/env python3
"""Build the LibreEcho initial-install bundle.

The bundle is a TWRP installer zip plus the payload files it installs:

    <out>/
        libreecho-install.zip        the TWRP installer
        bundle.manifest              shell-readable pin of every payload
        SHA256SUMS, bundle.json
        <payload files>              the OS image, feature payloads, install manifest

and it is pushed to /cache on the device - never to /sdcard, because in TWRP
/sdcard is /data, and /data is what gets formatted.

The release's own ``manifest.json`` is the authoritative contract: it names the
boot image and every feature with digests. This script reads it, checks it
against the files actually present, and translates it into the key=value
``bundle.manifest`` the installer consumes - the device side is a shell with no
JSON parser, so the translation happens here rather than there.

Payloads are not put inside the zip on purpose: the feature set is hundreds of
megabytes and TWRP's /tmp is a ramdisk.

Deterministic: the same inputs give byte-identical output, so a rebuilt bundle
can be compared rather than trusted. This is the single producer for local
builds and CI.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

SCHEMA = 1
ZIP_NAME = "libreecho-install.zip"
MANIFEST_NAME = "bundle.manifest"
INSTALL_MANIFEST_NAME = "manifest.json"
BOOT_SLOT_SECTORS = 32768  # 16 MiB: the boot slot size this family uses
# The device-side local-install path (`libreecho-update install <package>`)
# refuses a package larger than this: it stats the package before extracting.
# The initial-install tar is 239 MiB and can never pass, which is why the
# bundle ships the release's own OTA tar as the local-install package.
LOCAL_PACKAGE_MAX_BYTES = 33554432
LOCAL_PACKAGE_MEMBERS = ["manifest", "manifest.sig", "boot.img"]
# The install manifest is a contract, not a hint: an unknown key set means the
# OS and this builder disagree about what an install is.
REQUIRED_MANIFEST_KEYS = {
    "schema", "release", "board", "soc", "image_profile", "service_profile",
    "boot", "ota_public_key", "features", "amonet",
}
# Fixed timestamp and permissions everywhere: a release artifact should not
# change because a file was copied at a different time of day.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
ZIP_MODE = 0o644
ZIP_MODE_EXEC = 0o755


class BuildError(Exception):
    """A bundle that would be wrong, refused before it is written."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_assets(assets_dir: Path, work: Path) -> Path:
    """Return a directory holding every loose asset file.

    A release ships ``*-initial-install.tar`` (boot image, feature payloads and
    the install manifest) and ``*.ota.tar`` (the SIGNED manifest and its
    signature). Both are unpacked, the tars themselves are kept because one of
    them is the local-install package, and the directory's own loose files are
    merged in beside them.

    Merging matters for naming, not for content: a release also ships the
    version-named feature assets the SIGNED manifest references
    (``libreecho-radar-puffin-<version>-<feature>.payload.squashfs``), while the
    initial-install tar carries the same bytes under build-tag names. Only the
    merged directory can build a bundle that ships the names the manifest
    names. Tar members win a name collision: the tar is the release's own
    install bundle, and every digest is re-checked against the manifest anyway.
    """
    tars = sorted(assets_dir.glob("*initial-install.tar")) + \
        sorted(assets_dir.glob("*.ota.tar"))
    if not tars:
        return assets_dir
    unpacked = work / "release-assets"
    unpacked.mkdir(parents=True, exist_ok=True)
    for archive in tars:
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise BuildError(f"unsafe path in {archive.name}: {member.name}")
                if not member.isfile():
                    continue
                target = unpacked / Path(member.name).name
                source = bundle.extractfile(member)
                if source is None:
                    raise BuildError(f"unreadable member: {member.name}")
                with target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
    for loose in sorted(assets_dir.iterdir()):
        if not loose.is_file() or loose in tars:
            continue
        if (unpacked / loose.name).exists():
            continue
        shutil.copyfile(loose, unpacked / loose.name)
    # The OTA tar is the package the device's local-install path consumes, so it
    # has to survive unpacking as a file of its own.
    for archive in tars:
        shutil.copyfile(archive, unpacked / archive.name)
    return unpacked


def read_ota_manifest(path: Path) -> dict[str, str]:
    """Parse the SIGNED OTA manifest into a mapping.

    This is the contract the feature transaction commits against, so the bundle
    takes its asset names and digests from here rather than from the
    install-manifest tar, which names the same bytes differently.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError as error:
        raise BuildError(f"unreadable OTA manifest: {error}") from error
    for line in text.splitlines():
        if not line:
            continue
        key, _, value = line.partition("=")
        if not key or not value:
            raise BuildError(f"malformed OTA manifest line: {line}")
        if key in values:
            raise BuildError(f"duplicate OTA manifest key: {key}")
        values[key] = value
    return values


def _asset_by_digest(directory: Path, digest: str) -> Path | None:
    """First file in ``directory`` whose sha256 is ``digest``, or None."""
    for candidate in sorted(directory.iterdir()):
        if candidate.is_file() and sha256_file(candidate) == digest:
            return candidate
    return None


def read_install_manifest(path: Path) -> dict:
    """Parse the release's install manifest."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise BuildError(f"unreadable install manifest: {error}") from error
    if not isinstance(data, dict):
        raise BuildError("install manifest is not an object")
    missing = REQUIRED_MANIFEST_KEYS - set(data)
    extra = set(data) - REQUIRED_MANIFEST_KEYS
    if missing or extra:
        raise BuildError(
            f"install manifest keys differ (missing={sorted(missing)}, extra={sorted(extra)})")
    return data


def _checked_asset(record: object, directory: Path, what: str) -> Path:
    """Resolve one {name, sha256, size} record and verify the file matches it."""
    if not isinstance(record, dict) or set(record) != {"name", "sha256", "size"}:
        raise BuildError(f"{what} record is malformed")
    path = directory / str(record["name"])
    if not path.is_file():
        raise BuildError(f"{what} is missing: {record['name']}")
    size = path.stat().st_size
    if size != record["size"]:
        raise BuildError(
            f"{what} {record['name']} is {size} bytes, the manifest says {record['size']}")
    if sha256_file(path) != record["sha256"]:
        raise BuildError(f"{what} {record['name']} does not match its manifest digest")
    return path


def _resolve_feature_file(
    assets: Path,
    declared_name: str,
    declared_digest: str,
    install_record: object,
    what: str,
    seen: set[str],
) -> tuple[Path, str]:
    """Resolve one feature file to (source path, name it ships under).

    The SIGNED manifest decides the name when it declares one, because that is
    the name the feature transaction stages under on the device; the
    install-manifest record is the fallback for features the signed manifest
    names nothing for (``preserve``, whose assets live in the live tree under
    fixed names). Bytes are always bound by digest, and the two manifests are
    required to agree, so a disagreement is refused rather than resolved.
    """
    if not declared_name:
        if declared_digest:
            raise BuildError(f"{what} is pinned by digest but not named by the OTA manifest")
        path = _checked_asset(install_record, assets, what)
        if path.name in seen:
            raise BuildError(f"asset listed twice in the manifests: {path.name}")
        seen.add(path.name)
        return path, path.name
    if not declared_digest:
        raise BuildError(f"{what} is named by the OTA manifest but not pinned by digest")
    recorded = _checked_asset(install_record, assets, what)
    if recorded.name == declared_name:
        if sha256_file(recorded) != declared_digest:
            raise BuildError(f"{what} digest disagrees with the OTA manifest")
        seen.add(declared_name)
        return recorded, declared_name
    # The two manifests describe the same bytes under different names: the
    # install tar ships build-tag names, the signed manifest version-named ones.
    # Ship the published file when the release carries it, otherwise the same
    # bytes under the name the manifest references.
    if sha256_file(recorded) != declared_digest:
        raise BuildError(
            f"{what} differs between the install manifest and the OTA manifest")
    published = assets / declared_name
    source = published if published.is_file() and \
        sha256_file(published) == declared_digest else _asset_by_digest(assets, declared_digest)
    if source is None:
        raise BuildError(f"{what} has no file matching the OTA manifest digest")
    if declared_name in seen:
        raise BuildError(f"asset listed twice in the manifests: {declared_name}")
    seen.add(declared_name)
    return source, declared_name


def _checked_local_package(package: Path, boot_image: Path) -> Path:
    """Verify the OTA tar is a package the device's local-install path accepts.

    ``libreecho-update install`` stats the package first and refuses anything
    over 32 MiB, then requires the tar to hold exactly the signed manifest, its
    signature and the boot image. A bundle whose package cannot pass is a bundle
    that strands a freshly formatted device, so the check happens here.
    """
    size = package.stat().st_size
    if size > LOCAL_PACKAGE_MAX_BYTES:
        raise BuildError(
            f"local-install package {package.name} is {size} bytes; the device "
            f"refuses more than {LOCAL_PACKAGE_MAX_BYTES}")
    with tarfile.open(package) as archive:
        members = [m.name for m in archive.getmembers()]
        if members != LOCAL_PACKAGE_MEMBERS:
            raise BuildError(
                f"local-install package {package.name} holds {members}, "
                f"expected {LOCAL_PACKAGE_MEMBERS}")
        member = archive.extractfile("boot.img")
        if member is None:
            raise BuildError(f"local-install package {package.name} has no boot image")
        digest = hashlib.sha256(member.read()).hexdigest()
    if digest != sha256_file(boot_image):
        raise BuildError(
            "the local-install package carries a different boot image than the one being shipped")
    return package


def discover(assets: Path) -> dict:
    """Classify the assets, driven by the install manifest.

    The manifest is checked against the files on disk rather than believed: a
    bundle built from a manifest describing different bytes is the one failure
    that reaches a device, so it is refused here.
    """
    manifest_path = assets / INSTALL_MANIFEST_NAME
    if not manifest_path.is_file():
        raise BuildError(f"{INSTALL_MANIFEST_NAME} not found in {assets}")
    manifest = read_install_manifest(manifest_path)

    boot_image = _checked_asset(manifest["boot"], assets, "boot image")
    expected = BOOT_SLOT_SECTORS * 512
    if boot_image.stat().st_size != expected:
        raise BuildError(f"boot image is {boot_image.stat().st_size} bytes, expected {expected}")
    ota_key = _checked_asset(manifest["ota_public_key"], assets, "OTA public key")

    # The feature transaction commits against a SIGNED manifest, so the bundle
    # must carry it and its detached signature. Without them the installer fails
    # closed on the device, after the format step, which is the worst place to
    # discover a packaging mistake.
    ota_manifest = assets / "manifest"
    ota_signature = assets / "manifest.sig"
    if not ota_manifest.is_file() or not ota_signature.is_file():
        raise BuildError(f"the OTA manifest is required: manifest + manifest.sig in {assets}")
    ota = read_ota_manifest(ota_manifest)

    declared_boot = ota.get("boot_sha256")
    if declared_boot and declared_boot != sha256_file(boot_image):
        raise BuildError(
            "the OTA manifest describes a different boot image than the one being shipped")

    features = []
    seen = {boot_image.name}
    records = manifest["features"]
    if not isinstance(records, list) or not records:
        raise BuildError("install manifest lists no features")
    for record in records:
        if not isinstance(record, dict) or set(record) != {"name", "payload", "manifest"}:
            raise BuildError("feature record is malformed")
        name = record["name"]
        if not isinstance(name, str) or not name or "/" in name:
            raise BuildError(f"unsafe feature name: {name!r}")
        for entry in (record["payload"], record["manifest"]):
            filename = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(filename, str) or not filename:
                raise BuildError(f"feature {name} has a malformed asset record")
        payload, payload_name = _resolve_feature_file(
            assets, ota.get(f"feature_{name}_asset", ""),
            ota.get(f"feature_{name}_sha256", ""),
            record["payload"], f"{name} payload", seen)
        feature_manifest, manifest_name = _resolve_feature_file(
            assets, ota.get(f"feature_{name}_manifest_asset", ""),
            ota.get(f"feature_{name}_manifest_sha256", ""),
            record["manifest"], f"{name} manifest", seen)
        features.append({
            "name": name,
            "payload": payload,
            "payload_name": payload_name,
            "manifest": feature_manifest,
            "manifest_name": manifest_name,
        })

    # The package the device-side local install consumes. A release always
    # publishes an OTA tar; if one is present it must be installable.
    local_package = None
    candidates = sorted(assets.glob("*.ota.tar"))
    if candidates:
        preferred = f"libreecho-{manifest['release']}.ota.tar"
        chosen = next((c for c in candidates if c.name == preferred), candidates[0])
        local_package = _checked_local_package(chosen, boot_image)

    return {
        "install_manifest": manifest_path,
        "manifest_data": manifest,
        "boot_image": boot_image,
        "ota_key": ota_key,
        "features": features,
        "ota_manifest": ota_manifest,
        "ota_signature": ota_signature,
        "ota_values": ota,
        "ota_release": ota.get("version", ""),
        "local_package": local_package,
    }


def render_manifest(roles: dict, userdata_sectors: int) -> str:
    """Render bundle.manifest.

    Deliberately ``key=value`` rather than JSON: the reader is mksh inside TWRP,
    with no jq and no python, and a human debugging on the device has to read it
    too. Feature lines carry five fields because the device needs to know which
    feature directory each pair belongs in.
    """
    data = roles["manifest_data"]
    boot = roles["boot_image"]
    lines = [
        f"schema={SCHEMA}",
        f"release={data['release']}",
        f"device={data['board']}",
        f"soc={data['soc']}",
        f"image_profile={data['image_profile']}",
        f"service_profile={data['service_profile']}",
        f"userdata_sectors={userdata_sectors}",
        # payload=  verified before use, then written
        # staging=  <feature>:<payload>:<sha>:<manifest>:<sha>
        f"install_manifest={roles['install_manifest'].name}:{sha256_file(roles['install_manifest'])}",
        f"boot_image={boot.name}",
        f"boot_image_sha256={sha256_file(boot)}",
        f"payload={boot.name}:{sha256_file(boot)}",
        f"payload={roles['ota_key'].name}:{sha256_file(roles['ota_key'])}",
        # The signed manifest and its signature: the feature transaction's own
        # input, staged onto the device by name, so they are pinned here too.
        f"payload={roles['ota_manifest'].name}:{sha256_file(roles['ota_manifest'])}",
        f"payload={roles['ota_signature'].name}:{sha256_file(roles['ota_signature'])}",
    ]
    for feature in roles["features"]:
        payload = feature["payload"]
        manifest = feature["manifest"]
        lines.append(
            f"staging={feature['name']}:{feature['payload_name']}:{sha256_file(payload)}"
            f":{feature['manifest_name']}:{sha256_file(manifest)}")
    # The package the local-install path on the device consumes. It is what
    # turns a staged tree into a prepared feature transaction, and its name is
    # pinned here so the installer can prove it before copying it to /data.
    if roles["local_package"] is not None:
        lines.append(
            f"local_package={roles['local_package'].name}:{sha256_file(roles['local_package'])}")
    return "\n".join(lines) + "\n"


def build_zip(src: Path, out_zip: Path) -> None:
    """Zip the installer sources deterministically."""
    members = [p for p in sorted(src.rglob("*")) if p.is_file()]
    if not members:
        raise BuildError(f"installer source is empty: {src}")
    entry = src / "META-INF" / "com" / "google" / "android" / "update-binary"
    if not entry.is_file():
        raise BuildError("installer source is missing META-INF/com/google/android/update-binary")
    if not entry.stat().st_mode & 0o111:
        raise BuildError("update-binary is not executable; TWRP requires it to be")
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in members:
            rel = path.relative_to(src).as_posix()
            info = zipfile.ZipInfo(rel, date_time=ZIP_DATE)
            info.external_attr = (ZIP_MODE_EXEC if "update-binary" in rel else ZIP_MODE) << 16
            archive.writestr(info, path.read_bytes())


def assemble(assets_dir: Path, out_dir: Path, src_dir: Path, release: str,
             userdata_sectors: int) -> dict:
    """Build the bundle. Returns a summary dict."""
    if not src_dir.is_dir():
        raise BuildError(f"installer source directory not found: {src_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        source = _extract_assets(assets_dir, Path(work))
        roles = discover(source)
        # --release is a cross-check against the manifest, not the source of it:
        # a bundle whose stated release disagrees with its own manifest is
        # exactly the mismatch that should never reach a device.
        if release and release != roles["manifest_data"]["release"]:
            raise BuildError(
                f"--release {release} does not match the install manifest's "
                f"{roles['manifest_data']['release']}")
        render_manifest(roles, userdata_sectors)  # render once so faults surface early
        (out_dir / MANIFEST_NAME).write_text(render_manifest(roles, userdata_sectors))
        copied = []
        shippable: list[tuple[Path, str]] = [
            (roles["install_manifest"], roles["install_manifest"].name),
            (roles["boot_image"], roles["boot_image"].name),
            (roles["ota_key"], roles["ota_key"].name),
            (roles["ota_manifest"], roles["ota_manifest"].name),
            (roles["ota_signature"], roles["ota_signature"].name),
        ]
        for feature in roles["features"]:
            shippable.append((feature["payload"], feature["payload_name"]))
            shippable.append((feature["manifest"], feature["manifest_name"]))
        if roles["local_package"] is not None:
            shippable.append((roles["local_package"], roles["local_package"].name))
        for path, name in shippable:
            shutil.copyfile(path, out_dir / name)
            copied.append(name)
    zip_path = out_dir / ZIP_NAME
    build_zip(src_dir, zip_path)

    problems = check_bundle(out_dir)
    if problems:
        raise BuildError("bundle failed self-check: " + "; ".join(problems))
    return {
        "release": roles["manifest_data"]["release"],
        "device": roles["manifest_data"]["board"],
        "image_profile": roles["manifest_data"]["image_profile"],
        "features": [f["name"] for f in roles["features"]],
        "zip": zip_path.name,
        "zip_sha256": sha256_file(zip_path),
        "zip_size": zip_path.stat().st_size,
        "payload_files": sorted(copied),
        "local_package": roles["local_package"].name if roles["local_package"] else "",
        "manifest_sha256": sha256_file(out_dir / MANIFEST_NAME),
    }


def _declared_assets(line: str) -> list[tuple[str, str]]:
    """Every (filename, digest) pair a manifest line pins."""
    key, _, value = line.partition("=")
    parts = value.split(":")
    if key in ("payload", "install_manifest", "local_package") and len(parts) == 2:
        return [(parts[0], parts[1])]
    if key == "staging" and len(parts) == 5:
        return [(parts[1], parts[2]), (parts[3], parts[4])]
    if key in ("payload", "staging", "install_manifest", "local_package"):
        raise BuildError(f"malformed manifest line: {line}")
    return []


def check_bundle(out_dir: Path) -> list[str]:
    """Re-verify a bundle from its own manifest. Returns a list of problems."""
    problems: list[str] = []
    manifest = out_dir / MANIFEST_NAME
    if not manifest.is_file():
        return [f"{MANIFEST_NAME} missing"]
    if not (out_dir / ZIP_NAME).is_file():
        problems.append(f"{ZIP_NAME} missing")
    declared: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        if not line or "=" not in line:
            continue
        for name, digest in _declared_assets(line):
            declared[name] = digest
    if not declared:
        problems.append("manifest declares no payloads")
    for name, digest in sorted(declared.items()):
        path = out_dir / name
        if not path.is_file():
            problems.append(f"{name} declared but absent")
            continue
        if sha256_file(path) != digest:
            problems.append(f"{name} digest mismatch")
    exempt = {MANIFEST_NAME, ZIP_NAME, "SHA256SUMS", "bundle.json"}
    for extra in sorted(out_dir.iterdir()):
        if extra.is_file() and extra.name not in declared and extra.name not in exempt:
            problems.append(f"{extra.name} present but not declared")
    return problems


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Build the LibreEcho initial-install bundle (TWRP installer zip + payloads)")
    parser.add_argument("--assets", type=Path, required=True,
                        help="release assets dir, or a dir containing *initial-install.tar")
    parser.add_argument("--out", type=Path, required=True, help="bundle output directory")
    parser.add_argument("--src", type=Path, default=here / "src",
                        help="installer sources to zip (default: ./src)")
    parser.add_argument("--release", default="",
                        help="expected release identity; must match the install manifest")
    parser.add_argument("--userdata-sectors", type=int, default=2153472,
                        help="userdata size the OS contract accepts")
    parser.add_argument("--check", action="store_true",
                        help="only verify an existing bundle in --out")
    args = parser.parse_args(argv)

    if args.check:
        problems = check_bundle(args.out)
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        print("bundle: OK" if not problems else f"bundle: {len(problems)} problem(s)")
        return 1 if problems else 0

    try:
        summary = assemble(args.assets, args.out, args.src, args.release,
                           args.userdata_sectors)
    except BuildError as error:
        print(f"refusing to build: {error}", file=sys.stderr)
        return 1

    sums = args.out / "SHA256SUMS"
    with sums.open("w") as handle:
        for path in sorted(args.out.iterdir()):
            if path.is_file() and path.name not in ("SHA256SUMS", "bundle.json"):
                handle.write(f"{sha256_file(path)}  {path.name}\n")
    (args.out / "bundle.json").write_text(json.dumps(
        {**summary, "userdata_sectors": args.userdata_sectors,
         "schema": SCHEMA, "files": sorted(p.name for p in args.out.iterdir() if p.is_file())},
        indent=2, sort_keys=True) + "\n")

    print(f"release      {summary['release']}")
    print(f"device       {summary['device']}  (image profile {summary['image_profile']})")
    print(f"features     {', '.join(summary['features'])}")
    print(f"zip          {summary['zip']}  ({summary['zip_size']} bytes)")
    print(f"zip sha256   {summary['zip_sha256']}")
    print(f"payloads     {len(summary['payload_files'])} files")
    print(f"bundle       {args.out}  (self-check passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
