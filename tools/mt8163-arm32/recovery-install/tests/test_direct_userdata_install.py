#!/usr/bin/env python3
"""Host-side tests for the direct-userdata (protocol v2) recovery helper.

Runs the *real* self-contained shell helper under a real POSIX shell with the
device simulated by isolated stub binaries and a private mount/sysfs tree: no
device, no adb, no fastboot, no /dev writes.

Fixture mode and production mode are deliberately different:

* ``Harness.run()`` runs an *instrumented copy* of the shipped helper. The
  harness injects overrides for exactly two probes (``node_is_block`` and
  ``dev_t_of``) because fixture partitions are regular files; every other
  check -- the mmcblk0pN name pattern, sysfs PARTNAME/DEVNAME equality,
  ancestor symlinks, geometry, GPT ranges -- is the shipped code.
* ``Harness.run_production()`` runs the shipped file byte-for-byte and proves
  the shipped helper refuses regular-file partition nodes.

Covers the fail-closed surface required for a direct-userdata install: explicit
protocol, strict bundle-manifest validation, receipt atomicity + invocation
binding, monotone transaction guard, exactly-once format, full dry-run
validation, mandatory transfer roles, signed-manifest cross-checks before the
first boot write, path confinement/symlink safety, free-space accounting, GPT
range safety, hardlink-only placement, and both targets.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
HELPER = REPO / "src" / "libreecho-direct-install.sh"
LEGACY = REPO / "src" / "META-INF" / "com" / "google" / "android" / "update-binary"
BOOT_BYTES = 32768 * 512
USERDATA_SECTORS = 2153472
OUT_OF_CONTRACT_SECTORS = 1000000
TRANSFER_OVERHEAD_BYTES = 16 * 1024 * 1024
TARGET = "radar_puffin"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def extract_function(source: str, name: str) -> str:
    for marker in (f"{name}()\n{{\n", f"{name}() {{"):
        if marker in source:
            break
    else:
        raise AssertionError(f"function not found: {name}")
    start = source.index(marker)
    depth = 0
    for offset, char in enumerate(source[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"unterminated function: {name}")


def tree_snapshot(root: Path) -> dict:
    """path -> sha256 for files, 'dir'/'symlink' markers, recursively."""
    snapshot = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            snapshot[str(path)] = "symlink"
        elif path.is_file():
            snapshot[str(path)] = sha256_file(path)
        else:
            snapshot[str(path)] = "dir"
    return snapshot


def stage_and_init(case, manifest: Path) -> None:
    """Run prepare -> initialize -> transfer on a fresh case fixture.

    The transaction guard binds the exact bundle, so a scenario that changes
    the bundle (or the signed manifest the bundle pins) is a *new* transaction
    and gets its own device - exactly as re-driving the install would.
    """
    for phase in ("prepare", "initialize", "transfer"):
        result = case.h.run(phase, manifest)
        assert result.returncode == 0, \
            (phase, result.stderr, result.stdout, case.h.receipt())


def assert_dry_equals_real(case, manifest: Path, prefix: str, *, label: str = "") -> None:
    """A dry-run and a real finalize must agree on every refusal, and the
    refused run must leave the boot slots and the userdata tree untouched."""
    boot_before = sha256_file(case.h.node("mmcblk0p10"))
    tree_before = tree_snapshot(case.h.data)
    dry = case.h.run("finalize", manifest, "--dry-run")
    assert dry.returncode != 0, (label, "dry-run unexpectedly succeeded")
    dry_error = case.h.receipt().get("error", "")
    assert dry_error.startswith(prefix), (label, dry_error)
    real = case.h.run("finalize", manifest)
    assert real.returncode != 0, (label, "real run unexpectedly succeeded")
    assert case.h.receipt().get("error") == dry_error, (label, dry_error)
    assert sha256_file(case.h.node("mmcblk0p10")) == boot_before, label
    assert tree_snapshot(case.h.data) == tree_before, label


INSTRUMENTATION = """\
# --- isolated test instrumentation (injected by the host test harness) ------
# The shipped helper refuses partition nodes that are not block devices and
# checks their dev_t against sysfs. These fixtures are regular files under a
# private root, so the harness overrides exactly those two probes; every other
# check stays the shipped one. The shipped file is also exercised unmodified
# by the production refusal test.
node_is_block() { return 0; }
dev_t_of() { cat "$SYS_BLOCK/$(basename "$1")/dev" 2>/dev/null; }
# --- end test instrumentation ------------------------------------------------
"""


def instrument_helper(source: str) -> str:
    marker = '\nmain "$@"\n'
    assert source.count(marker) == 1, "helper entry point not found exactly once"
    return source.replace(marker, "\n" + INSTRUMENTATION + '\nmain "$@"\n')


class Harness:
    """A private, fully stubbed device for one test."""

    def __init__(self, root: Path, *, target: str = TARGET):
        self.root = root
        self.target = target
        self.sbin = root / "sbin"
        self.sys = root / "sys" / "class" / "block"
        self.dev = root / "dev"
        self.byname = self.dev / "block" / "by-name"
        self.calls = root / "calls"
        self.mounts = root / "mounts"
        self.state = root / "cache" / "libreecho-direct"
        self.data = root / "data"
        self.incoming = self.data / "libreecho" / "incoming"
        self.bundle = root / "bundle"
        self.assets = root / "assets"
        self.df_free_kb = 10_000_000
        self.free_file = root / "free_kb"
        self.transfer_bytes = 0
        self.partitions = {}
        self.helper = root / "libreecho-direct-install.instrumented.sh"
        self.build()

    # -- construction ---------------------------------------------------------
    def build(self) -> None:
        for path in (self.sbin, self.sys, self.byname, self.calls, self.state,
                     self.data, self.bundle, self.assets):
            path.mkdir(parents=True, exist_ok=True)
        (self.sys / "mmcblk0").mkdir(parents=True, exist_ok=True)
        (self.sys / "mmcblk0" / "size").write_text("4194304\n")
        (self.sys / "mmcblk0" / "dev").write_text("179:0\n")
        (self.sys / "mmcblk0" / "uevent").write_text("DEVNAME=mmcblk0\n")
        self.free_file.write_text(str(self.df_free_kb))
        self._partition("mmcblk0p10", "boot_a", 32768)
        self._partition("mmcblk0p11", "boot_b", 32768)
        self._partition("mmcblk0p15", "cache", 1600000)
        self._partition("mmcblk0p16", "userdata", USERDATA_SECTORS)
        self._partition("mmcblk0p17", "misc", 2048)
        self._partition("mmcblk0p18", "system_a", 2000000)
        # userdata starts mounted, the way recovery presents it.
        self.mounts.write_text(
            f"{self.node('mmcblk0p16')} /data ext4 rw 0 0\n"
            f"{self.node('mmcblk0p16')} /sdcard ext4 rw 0 0\n")
        self._stubs()
        self.helper.write_text(instrument_helper(HELPER.read_text()))

    def node(self, part: str) -> Path:
        return self.dev / part

    def _partition(self, part: str, name: str, sectors: int) -> None:
        (self.sys / part).mkdir(parents=True, exist_ok=True)
        (self.sys / part / "size").write_text(f"{sectors}\n")
        (self.sys / part / "uevent").write_text(f"DEVNAME={part}\nPARTNAME={name}\n")
        index = 100 + len(self.partitions)
        (self.sys / part / "dev").write_text(f"179:{index}\n")
        (self.dev / part).write_bytes(b"\0" * 4096)
        link = self.byname / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(self.node(part))
        self.partitions[name] = (part, sectors)

    def _stub(self, name: str, body: str) -> None:
        path = self.sbin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _stubs(self) -> None:
        self._stub("sha256sum",
                   f'echo "$*" >> {self.calls}/sha256sum.calls\n'
                   'exec /usr/bin/sha256sum "$@"\n')
        self.rewrite_sgdisk()
        self._stub("mke2fs", f'echo "$@" >> {self.calls}/mke2fs\nexit 0\n')
        self._stub("mount",
                   f'echo "$@" >> {self.calls}/mount\n'
                   'second=""; last=""\n'
                   'for a in "$@"; do second="$last"; last="$a"; done\n'
                   f'echo "$second $last ext4 rw 0 0" >> {self.mounts}\n')
        self._stub("umount",
                   f'echo "$@" >> {self.calls}/umount\n'
                   f'awk -v m="$1" \'$2 != m\' {self.mounts} > {self.mounts}.tmp\n'
                   f'mv {self.mounts}.tmp {self.mounts}\n')
        self.set_serial("TESTSERIAL01")
        self._stub("df",
                   'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
                   f'echo "/dev/block/mmcblk0p16 1 1 $(cat {self.free_file}) 1% {self.data}"\n')
        self._stub("sync", "exit 0\n")

    def rewrite_sgdisk(self, *, guid: str = "12345678-1234-1234-1234-1234567890AB",
                       first: int = 2048, last: int = 2155519) -> None:
        self._stub(
            "sgdisk",
            f'echo "$@" >> {self.calls}/sgdisk\n'
            'case "$1" in\n'
            f'  --info=*) echo "First sector: {first} (at 1.0 MiB)"; '
            f'echo "Last sector: {last} (at 1.0 GiB)"; '
            f'echo "Partition unique GUID: {guid}"; '
            "echo \\\"Partition name: 'userdata'\\\" ;;\n"
            '  *) echo "The operation has completed successfully" ;;\n'
            'esac\n')

    def set_serial(self, serial: str) -> None:
        self._stub("getprop",
                   f'case "$1" in ro.serialno|ro.boot.serialno) echo "{serial}";; '
                   '*) echo "";; esac\n')

    # -- inputs ---------------------------------------------------------------
    def add_file(self, directory: Path, name: str, data: bytes | str) -> Path:
        path = directory / name
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def make_bundle_manifest(self, *, boot: Path, transfer: dict, staging: list,
                             extra: str = "", name: str = "bundle.manifest") -> Path:
        lines = [
            "schema=1",
            "protocol=2",
            f"release=0.14.0-test",
            f"device={self.target}",
            f"target={self.target}",
            "fastboot_products=" + ("RADAR" if self.target == "radar_puffin" else "BISCUIT"),
            "soc=mt8163",
            "image_profile=ota",
            "service_profile=production",
            f"userdata_sectors={USERDATA_SECTORS}",
            f"transfer_bytes_total={self.transfer_bytes}",
            f"boot_image={boot.name}",
            f"boot_image_sha256={sha256_file(boot)}",
        ]
        if "local-package" in transfer:
            package = transfer["local-package"]
            lines.append(f"local_package={package.name}:{sha256_file(package)}")
        for role, path in transfer.items():
            lines.append(f"transfer={role}:{path.name}:{sha256_file(path)}")
        for feature, payload, manifest in staging:
            lines.append(
                f"staging={feature}:{payload.name}:{sha256_file(payload)}"
                f":{manifest.name}:{sha256_file(manifest)}")
        if extra:
            lines.append(extra)
        path = self.bundle / name
        path.write_text("\n".join(lines) + "\n")
        return path

    # -- run ------------------------------------------------------------------
    def helper_env(self) -> dict:
        env = os.environ.copy()
        env.update({
            "LIBREECHO_SBIN": str(self.sbin),
            "LIBREECHO_SYS_BLOCK": str(self.sys),
            "LIBREECHO_BYNAME_DIR": str(self.byname),
            "LIBREECHO_MOUNTS_FILE": str(self.mounts),
            "LIBREECHO_DISK": str(self.node("mmcblk0")),
            "LIBREECHO_DATA_ROOT": str(self.data),
            "LIBREECHO_GETPROP": str(self.sbin / "getprop"),
            "LIBREECHO_DF": str(self.sbin / "df"),
        })
        return env

    def _argv(self, helper: Path, phase: str, manifest: Path, extra: tuple) -> list:
        args = [
            "/bin/sh", str(helper),
            "--protocol", "2",
            "--phase", phase,
            "--bundle-manifest", str(manifest),
            "--bundle-manifest-sha256", sha256_file(manifest),
            "--state-dir", str(self.state),
            "--incoming-dir", str(self.incoming),
            "--target", self.target,
            "--release", "0.14.0-test",
        ]
        args.extend(extra)
        return args

    def run(self, phase: str, manifest: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(self._argv(self.helper, phase, manifest, extra),
                              text=True, capture_output=True, env=self.helper_env())

    def run_production(self, phase: str, manifest: Path, *extra: str) -> subprocess.CompletedProcess:
        """Run the shipped helper unmodified: no test instrumentation at all."""
        return subprocess.run(self._argv(HELPER, phase, manifest, extra),
                              text=True, capture_output=True, env=self.helper_env())

    def run_raw(self, *args: str, helper: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", str(helper or self.helper), *args],
                              text=True, capture_output=True, env=self.helper_env())

    def receipt(self) -> dict:
        path = self.state / "receipt"
        if not path.is_file():
            return {}
        values = {}
        for line in path.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
        return values

    def guard(self) -> dict:
        path = self.state / "transaction.state"
        if not path.is_file():
            return {}
        values = {}
        for line in path.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
        return values

    def calls_to(self, name: str) -> list[str]:
        path = self.calls / name
        return path.read_text().splitlines() if path.is_file() else []

    def mutating_sgdisk(self) -> list[str]:
        """sgdisk invocations that WRITE the GPT; --info reads are allowed."""
        return [line for line in self.calls_to("sgdisk")
                if any(flag in line for flag in
                       ("--delete=", "--new=", "--typecode=", "--partition-guid=",
                        "--change-name="))]

    def hashes_of(self, path: Path) -> int:
        return sum(1 for line in self.calls_to("sha256sum.calls")
                   if line.strip() == str(path))


class BasicCase:
    """A small self-contained install fixture, rebuildable per subtest."""

    def __init__(self, work: Path, *, guid: str | None = None, serial: str | None = None):
        self.work = work
        self.h = Harness(work)
        if guid is not None:
            self.h.rewrite_sgdisk(guid=guid)
        if serial is not None:
            self.h.set_serial(serial)
        self.boot = self.h.add_file(self.h.assets, "boot.img",
                                    b"ANDROID!" + bytes(BOOT_BYTES - 8))
        self.payload = self.h.add_file(self.h.assets, "tts.payload.squashfs", b"payload")
        self.fmanifest = self.h.add_file(self.h.assets, "tts.manifest.json", b"{}")
        self.ota = self.h.add_file(
            self.h.assets, "manifest",
            f"board={TARGET}\nversion=1\nboot_sha256={sha256_file(self.boot)}\n"
            "feature_ids=tts\nfeature_tts_action=replace\nfeature_tts_asset=tts.squashfs\n"
            f"feature_tts_size={self.payload.stat().st_size}\n"
            f"feature_tts_sha256={sha256_file(self.payload)}\n"
            "feature_tts_manifest_asset=tts.manifest.json\n"
            f"feature_tts_manifest_size={self.fmanifest.stat().st_size}\n"
            f"feature_tts_manifest_sha256={sha256_file(self.fmanifest)}\n")
        self.sig = self.h.add_file(self.h.assets, "manifest.sig", b"sig")
        self.package = self.h.add_file(self.h.assets, "1.ota.tar", b"tar")
        self.h.transfer_bytes = (self.boot.stat().st_size + self.payload.stat().st_size
                                 + self.fmanifest.stat().st_size + self.ota.stat().st_size
                                 + self.sig.stat().st_size + self.package.stat().st_size)
        self.transfer = {"boot": self.boot, "ota-manifest": self.ota,
                         "ota-signature": self.sig, "local-package": self.package}
        self.staging = [("tts", self.payload, self.fmanifest)]

    def manifest(self, transfer: dict | None = None, staging: list | None = None,
                 boot: Path | None = None, name: str = "bundle.manifest") -> Path:
        return self.h.make_bundle_manifest(
            boot=boot or self.boot,
            transfer=self.transfer if transfer is None else transfer,
            staging=self.staging if staging is None else staging,
            name=name)

    def land(self, *, skip: tuple = ()) -> None:
        self.h.incoming.mkdir(parents=True, exist_ok=True)
        for path in (self.boot, self.payload, self.fmanifest, self.ota,
                     self.sig, self.package):
            if path.name in skip:
                continue
            (self.h.incoming / path.name).write_bytes(path.read_bytes())


class _InstallFixture(unittest.TestCase):
    """Shared standard install fixture (identical assets for both suites)."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="le-direct-"))
        self.h = Harness(self.work)
        # A boot image and one feature payload/manifest, plus signed manifest/sig.
        self.boot = self.h.add_file(self.h.assets, "boot.img", b"ANDROID!" + bytes(BOOT_BYTES - 8))
        self.payload = self.h.add_file(self.h.assets, "tts.payload.squashfs", b"tts-payload-bytes")
        self.fmanifest = self.h.add_file(self.h.assets, "tts.manifest.json", b'{"feature":"tts"}')
        self.otamanifest = self.h.add_file(
            self.h.assets, "manifest",
            f"board={TARGET}\nversion=0.14.0\n"
            f"boot_sha256={sha256_file(self.boot)}\nfeature_ids=tts\n"
            "feature_tts_action=replace\nfeature_tts_asset=tts.squashfs\n"
            f"feature_tts_size={self.payload.stat().st_size}\n"
            f"feature_tts_sha256={sha256_file(self.payload)}\n"
            "feature_tts_manifest_asset=tts.manifest.json\n"
            f"feature_tts_manifest_size={self.fmanifest.stat().st_size}\n"
            f"feature_tts_manifest_sha256={sha256_file(self.fmanifest)}\n")
        self.otasig = self.h.add_file(self.h.assets, "manifest.sig", b"signature")
        self.package = self._make_package()
        self.h.transfer_bytes = (self.boot.stat().st_size + self.payload.stat().st_size
                                 + self.fmanifest.stat().st_size
                                 + self.otamanifest.stat().st_size
                                 + self.otasig.stat().st_size
                                 + self.package.stat().st_size)
        self.transfer = {
            "boot": self.boot,
            "ota-manifest": self.otamanifest,
            "ota-signature": self.otasig,
            "local-package": self.package,
        }
        self.staging = [("tts", self.payload, self.fmanifest)]

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def _make_package(self) -> Path:
        path = self.h.assets / "0.14.0.ota.tar"
        with tarfile.open(path, "w") as archive:
            for name, data in (("manifest", self.otamanifest.read_bytes()),
                               ("manifest.sig", self.otasig.read_bytes()),
                               ("boot.img", self.boot.read_bytes())):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, __import__("io").BytesIO(data))
        return path

    def manifest(self, **kwargs) -> Path:
        return self.h.make_bundle_manifest(
            boot=kwargs.get("boot", self.boot),
            transfer=kwargs.get("transfer", self.transfer),
            staging=kwargs.get("staging", self.staging),
            extra=kwargs.get("extra", ""))

    def edit_manifest(self, old: str, new: str) -> Path:
        path = self.manifest()
        path.write_text(path.read_text().replace(old, new))
        return path

    def edit_signed(self, old: str, new: str) -> None:
        """Rewrite the *signed OTA manifest* (the upload), not the bundle pin."""
        self.otamanifest.write_text(self.otamanifest.read_text().replace(old, new))

    def _assert_dry_equals_real(self, manifest: Path, prefix: str, *, label: str = "") -> None:
        """A dry-run and a real finalize must agree on every refusal, and the
        refused run must leave the boot slots and the userdata tree untouched."""
        boot_before = sha256_file(self.h.node("mmcblk0p10"))
        tree_before = tree_snapshot(self.h.data)
        dry = self.h.run("finalize", manifest, "--dry-run")
        self.assertNotEqual(dry.returncode, 0, label)
        dry_error = self.h.receipt().get("error", "")
        self.assertTrue(dry_error.startswith(prefix), (label, dry_error))
        real = self.h.run("finalize", manifest)
        self.assertNotEqual(real.returncode, 0, label)
        self.assertEqual(self.h.receipt().get("error"), dry_error, label)
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), boot_before, label)
        self.assertEqual(tree_snapshot(self.h.data), tree_before, label)

    # -- helpers --------------------------------------------------------------
    def prepare(self, **kwargs):
        result = self.h.run("prepare", self.manifest(**kwargs))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        return result

    def initialize(self):
        result = self.h.run("initialize", self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        return result

    def transfer_phase(self):
        result = self.h.run("transfer", self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        return result

    def land_uploads(self, *, corrupt: str | None = None, truncate: str | None = None,
                     missing: str | None = None, symlink: str | None = None) -> None:
        self.h.incoming.mkdir(parents=True, exist_ok=True)
        files = {
            self.boot.name: self.boot.read_bytes(),
            self.payload.name: self.payload.read_bytes(),
            self.fmanifest.name: self.fmanifest.read_bytes(),
            self.otamanifest.name: self.otamanifest.read_bytes(),
            self.otasig.name: self.otasig.read_bytes(),
            self.package.name: self.package.read_bytes(),
        }
        for name, data in files.items():
            if name == missing:
                continue
            if name == corrupt:
                data = bytearray(data)
                data[0] ^= 0xFF
                data = bytes(data)
            if name == truncate:
                data = data[: max(1, len(data) // 2)]
            if name == symlink:
                target = self.h.incoming.parent / (name + ".real")
                target.write_bytes(data)
                (self.h.incoming / name).symlink_to(target)
                continue
            (self.h.incoming / name).write_bytes(data)


class DirectInstallTests(_InstallFixture):
    # -- protocol gate --------------------------------------------------------
    def test_missing_protocol_fails_closed(self) -> None:
        result = self.h.run_raw("--phase", "prepare", "--bundle-manifest",
                                str(self.manifest()), "--state-dir", str(self.h.state))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("result"), "failed")
        self.assertEqual(self.h.receipt().get("error"), "protocol-required")

    def test_unsupported_protocol_refused(self) -> None:
        result = self.h.run_raw("--protocol", "3", "--phase", "prepare",
                                "--bundle-manifest", str(self.manifest()),
                                "--state-dir", str(self.h.state))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("unsupported-protocol"))

    def test_unknown_argument_refused(self) -> None:
        result = self.h.run("prepare", self.manifest(), "--bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("unknown-argument"))

    def test_bundle_manifest_digest_mismatch_refused(self) -> None:
        manifest = self.manifest()
        result = self.h.run_raw(
            "--protocol", "2", "--phase", "prepare",
            "--bundle-manifest", str(manifest), "--bundle-manifest-sha256", "0" * 64,
            "--state-dir", str(self.h.state))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "bundle-manifest-digest-mismatch")

    # -- prepare --------------------------------------------------------------
    def test_prepare_noop_when_layout_in_contract(self) -> None:
        self.prepare()
        self.assertEqual(self.h.receipt().get("result"), "prepare-noop")
        self.assertEqual(self.h.receipt().get("reboot_required"), "0")
        self.assertEqual(self.h.mutating_sgdisk(), [])
        self.assertEqual(self.h.calls_to("mke2fs"), [])

    def test_prepare_reshapes_out_of_contract_layout(self) -> None:
        part, _ = self.h.partitions["userdata"]
        (self.h.sys / part / "size").write_text(f"{OUT_OF_CONTRACT_SECTORS}\n")
        self.prepare()
        self.assertEqual(self.h.receipt().get("result"), "prepare-ok")
        self.assertEqual(self.h.receipt().get("reboot_required"), "1")
        self.assertTrue(self.h.calls_to("sgdisk"), "sgdisk must run to reshape")
        self.assertEqual(self.h.calls_to("mke2fs"), [], "prepare must never format")
        self.assertEqual(self.h.guard().get("phase"), "prepare")
        self.assertEqual(self.h.guard().get("format_state"), "absent")

    # -- initialize -----------------------------------------------------------
    def test_initialize_formats_once_mounts_and_verifies(self) -> None:
        self.prepare()
        self.initialize()
        self.assertEqual(self.h.receipt().get("result"), "initialized")
        self.assertEqual(self.h.receipt().get("format_state"), "formatted")
        self.assertEqual(len(self.h.calls_to("mke2fs")), 1)
        self.assertEqual(self.h.guard().get("format_state"), "formatted")
        mounts = self.h.mounts.read_text()
        self.assertIn(f"{self.h.node('mmcblk0p16')} {self.h.data} ext4", mounts)

    def test_initialize_second_time_never_reformats(self) -> None:
        self.prepare()
        self.initialize()
        result = self.h.run("initialize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "already-initialized")
        self.assertEqual(len(self.h.calls_to("mke2fs")), 1, "must not reformat")

    def test_initialize_refuses_when_format_uncertain(self) -> None:
        self.prepare()
        guard = self.h.state / "transaction.state"
        guard.write_text(guard.read_text().replace("format_state=absent", "format_state=formatting"))
        result = self.h.run("initialize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "format-uncertain")
        self.assertEqual(self.h.calls_to("mke2fs"), [])

    def test_ramfs_at_data_is_refused(self) -> None:
        """A ramfs mounted at /data must not be mistaken for userdata."""
        self.prepare()
        self.initialize()
        self.h.mounts.write_text(f"tmpfs {self.h.data} tmpfs rw 0 0\n")
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("data-mounted-from-wrong-node"),
                        self.h.receipt())

    def test_initialize_refuses_stale_transaction(self) -> None:
        self.prepare()
        guard = self.h.state / "transaction.state"
        guard.write_text(guard.read_text().replace(
            "bundle_manifest_sha256=" + sha256_file(self.manifest()),
            "bundle_manifest_sha256=" + "0" * 64))
        result = self.h.run("initialize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "transaction-conflict:bundle_manifest_sha256")

    def test_aborted_prepare_still_allows_initialize(self) -> None:
        """prepare wrote its guard but nothing else; a reboot then initialize works."""
        self.prepare()
        self.assertIn(self.h.guard().get("format_state"), ("absent", None))
        # No format yet, guard phase=prepare -> initialize is the next legal phase.
        self.initialize()
        self.assertEqual(self.h.receipt().get("result"), "initialized")

    # -- transfer -------------------------------------------------------------
    def test_transfer_prepares_landing_zone(self) -> None:
        self.prepare()
        self.initialize()
        result = self.h.run("transfer", self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.h.receipt().get("result"), "transferred")
        self.assertTrue(self.h.incoming.is_dir())

    def test_transfer_refuses_without_space(self) -> None:
        self.prepare()
        self.initialize()
        self.h.free_file.write_text("1")
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "insufficient-space")

    # -- finalize -------------------------------------------------------------
    def test_finalize_writes_boots_and_links_features_never_formats(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads()
        result = self.h.run("finalize", self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        self.assertEqual(self.h.receipt().get("result"), "installed")
        wanted = sha256_file(self.boot)
        self.assertEqual(self.h.receipt().get("boot_a_sha256"), wanted)
        self.assertEqual(self.h.receipt().get("boot_b_sha256"), wanted)
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), wanted)
        self.assertEqual(sha256_file(self.h.node("mmcblk0p11")), wanted)
        self.assertEqual(len(self.h.calls_to("mke2fs")), 1, "finalize must not call mke2fs")
        # Feature is linked into staging, not copied (same inode as the upload).
        staged = (self.h.data / "libreecho" / "update" / "staging" / "features" / "tts")
        placed = staged / "tts.squashfs"
        self.assertTrue(placed.is_file())
        self.assertEqual(sha256_file(placed), sha256_file(self.payload))
        self.assertEqual(placed.stat().st_ino, (self.h.incoming / self.payload.name).stat().st_ino,
                         "payload must be hardlinked, not duplicated")
        signed = (self.h.data / "libreecho" / "update" / "staging" / "manifest")
        self.assertTrue(signed.is_file())
        self.assertEqual(signed.stat().st_ino,
                         (self.h.incoming / self.otamanifest.name).stat().st_ino)
        self.assertEqual(self.h.guard().get("phase"), "finalized")

    def test_finalize_refuses_corrupt_upload(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads(corrupt=self.payload.name)
        before = sha256_file(self.h.node("mmcblk0p10"))
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("missing-upload"))
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), before, "no boot write on refusal")
        self.assertEqual(len(self.h.calls_to("mke2fs")), 1)

    def test_finalize_refuses_truncated_upload(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads(truncate=self.boot.name)
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("missing-upload"))

    def test_finalize_refuses_missing_upload(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads(missing=self.otasig.name)
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("missing-upload"))

    def test_finalize_refuses_symlinked_upload(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads(symlink=self.payload.name)
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("missing-upload"))

    def test_finalize_refuses_same_size_wrong_partition(self) -> None:
        """Point boot_a's by-name link at boot_b's node (same 32768 sectors)."""
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads()
        link = self.h.byname / "boot_a"
        link.unlink()
        link.symlink_to(self.h.node("mmcblk0p11"))
        before = sha256_file(self.h.node("mmcblk0p11"))
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", ""))
        self.assertEqual(sha256_file(self.h.node("mmcblk0p11")), before, "must not write the wrong node")

    def test_finalize_twice_refused(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads()
        self.assertEqual(self.h.run("finalize", self.manifest()).returncode, 0)
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "already-finalized")

    def test_finalize_before_format_refused(self) -> None:
        self.prepare()
        self.land_uploads()
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("phase-order"))

    def test_finalize_dry_run_writes_nothing(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()
        self.land_uploads()
        before_a = sha256_file(self.h.node("mmcblk0p10"))
        result = self.h.run("finalize", self.manifest(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.h.receipt().get("result"), "dry-run-ok")
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), before_a)

    # -- duplicate digests ----------------------------------------------------
    def test_finalize_reuses_one_upload_for_duplicate_digests(self) -> None:
        """Two preserved features whose payloads are the same bytes -> one inode."""
        # Rewrite the signed manifest to declare two preserve features on one file.
        keep = self.payload.name
        otamanifest = self.h.assets / "manifest"
        otamanifest.write_text(
            f"board={TARGET}\nversion=0.14.0\n"
            f"boot_sha256={sha256_file(self.boot)}\nfeature_ids=tts,airplay2\n"
            "feature_tts_action=preserve\n"
            f"feature_tts_base_payload_sha256={sha256_file(self.payload)}\n"
            f"feature_tts_base_manifest_sha256={sha256_file(self.fmanifest)}\n"
            "feature_airplay2_action=preserve\n"
            f"feature_airplay2_base_payload_sha256={sha256_file(self.payload)}\n"
            f"feature_airplay2_base_manifest_sha256={sha256_file(self.fmanifest)}\n")
        staging = [("tts", self.payload, self.fmanifest),
                   ("airplay2", self.payload, self.fmanifest)]
        manifest = self.h.make_bundle_manifest(boot=self.boot, transfer=self.transfer,
                                               staging=staging)
        self.assertNotEqual(keep, "")
        for phase in ("prepare", "initialize", "transfer"):
            result = self.h.run(phase, manifest)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        self.land_uploads()
        result = self.h.run("finalize", manifest)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        a = self.h.data / "libreecho" / "features" / "tts" / "payload.squashfs"
        b = self.h.data / "libreecho" / "features" / "airplay2" / "payload.squashfs"
        self.assertEqual(sha256_file(a), sha256_file(self.payload))
        self.assertEqual(sha256_file(b), sha256_file(self.payload))
        self.assertEqual(a.stat().st_ino, b.stat().st_ino, "duplicate digest -> one inode")

    # -- structural invariants ------------------------------------------------
    def test_finalize_structurally_cannot_format(self) -> None:
        source = HELPER.read_text()
        finalize = extract_function(source, "phase_finalize")
        self.assertNotIn("format_userdata", finalize)
        self.assertNotIn("mke2fs", finalize)
        fmt = extract_function(source, "format_userdata")
        self.assertIn('[ "$PHASE" = initialize ]', fmt)

    # -- target binding -------------------------------------------------------
    def test_target_mismatch_refused(self) -> None:
        self.prepare()
        result = self.h.run_raw(
            "--protocol", "2", "--phase", "initialize",
            "--bundle-manifest", str(self.manifest()),
            "--bundle-manifest-sha256", sha256_file(self.manifest()),
            "--state-dir", str(self.h.state), "--target", "biscuit")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("target-mismatch"))


class RecoveryV2BlockerTests(_InstallFixture):
    """One test group per recovery v2 safety/specification blocker."""

    def full_init(self) -> None:
        self.prepare()
        self.initialize()
        self.transfer_phase()

    # -- (1) reset is gone; guard is never deleted ---------------------------
    def test_reset_argument_is_removed_and_guard_is_never_deleted(self) -> None:
        source = HELPER.read_text()
        self.assertNotIn("--reset-transaction", source)
        self.assertNotIn("RESET_TRANSACTION", source)
        self.prepare()
        guard = self.h.state / "transaction.state"
        before = guard.read_bytes()
        for extra in (("--reset-transaction",), ("--reset-transaction", "--dry-run")):
            result = self.h.run("prepare", self.manifest(), *extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(self.h.receipt().get("error", "").startswith("unknown-argument"))
        self.assertEqual(guard.read_bytes(), before)

    # -- (2)+(5) receipts: atomic, bound, never stale ------------------------
    def test_receipt_binds_the_invocation(self) -> None:
        self.prepare()
        manifest_sha = sha256_file(self.manifest())
        receipt = self.h.receipt()
        self.assertEqual(receipt.get("protocol"), "2")
        self.assertEqual(receipt.get("phase"), "prepare")
        self.assertEqual(receipt.get("bundle_manifest_sha256"), manifest_sha)
        self.assertEqual(receipt.get("result"), "prepare-noop")
        expected = sha256_bytes("|".join([
            "2", "prepare", manifest_sha, receipt.get("device_digest", ""),
            TARGET, "0.14.0-test"]).encode())
        self.assertEqual(receipt.get("invocation_sha256"), expected)

    def test_stale_receipt_never_survives_a_new_run(self) -> None:
        self.prepare()
        receipt = self.h.state / "receipt"
        receipt.write_text("result=installed\nphase=finalize\nboot_a_sha256=deadbeef\n")
        result = self.h.run("transfer", self.manifest())  # refused: not initialized
        self.assertNotEqual(result.returncode, 0)
        now = self.h.receipt()
        self.assertEqual(now.get("result"), "failed")
        self.assertEqual(now.get("phase"), "transfer")
        self.assertNotEqual(now.get("result"), "installed")
        self.assertEqual(len(now.get("invocation_sha256", "")), 64)

    @unittest.skipIf(os.geteuid() == 0, "permission bits are bypassed as root")
    def test_unwritable_receipt_file_is_replaced_not_trusted(self) -> None:
        self.prepare()
        receipt = self.h.state / "receipt"
        os.chmod(receipt, 0o400)
        try:
            result = self.h.run("initialize", self.manifest())
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(self.h.receipt().get("result"), "initialized")
        finally:
            os.chmod(receipt, 0o644)

    @unittest.skipIf(os.geteuid() == 0, "permission bits are bypassed as root")
    def test_unwritable_state_dir_refuses_before_mutations(self) -> None:
        self.prepare()
        stale = self.h.receipt().get("result")
        os.chmod(self.h.state, 0o500)
        try:
            boot_before = sha256_file(self.h.node("mmcblk0p10"))
            result = self.h.run("finalize", self.manifest())
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr.strip(), "refusal must be visible on stderr")
            self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), boot_before)
            self.assertNotEqual(self.h.receipt().get("result"), stale,
                                "the truncated receipt must not read as the old success")
        finally:
            os.chmod(self.h.state, 0o700)

    # -- (3) dry-run performs the full validation ----------------------------
    def _fresh_cross_check(self, edit, prefix: str, *, label: str) -> None:
        """Run a signed-manifest refusal on its own fresh transaction.

        The guard binds the exact bundle, so a scenario that edits the signed
        manifest (which the bundle pins) is a new transaction: it gets its own
        device, exactly as re-driving the install would.
        """
        work = Path(tempfile.mkdtemp(prefix="le-fresh-"))
        try:
            case = BasicCase(work)
            edit(case)
            manifest = case.manifest()
            stage_and_init(case, manifest)
            case.land()
            assert_dry_equals_real(case, manifest, prefix, label=label)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_dry_run_matches_real_finalize_on_every_failure(self) -> None:
        self.full_init()
        # corruption / absence on the upload side: the bundle (and its guard)
        # is unchanged across these sub-cases.
        self.land_uploads(corrupt=self.payload.name)
        self._assert_dry_equals_real(self.manifest(), "missing-upload", label="corrupt payload")
        (self.h.incoming / self.package.name).unlink()
        self.land_uploads(missing=self.package.name)
        self._assert_dry_equals_real(self.manifest(), "missing-upload", label="missing package")
        # signed-versus-staging disagreement: a fresh transaction whose bundle
        # pins the edited signed manifest.
        self._fresh_cross_check(
            lambda c: c.ota.write_text(c.ota.read_text().replace(
                f"feature_tts_sha256={sha256_file(c.payload)}",
                "feature_tts_sha256=" + "0" * 64)),
            "signed-staging-digest", label="signed digest disagreement")
        # wrong-size boot: digest matches, geometry does not
        work = Path(tempfile.mkdtemp(prefix="le-dry-small-"))
        try:
            case = BasicCase(work)
            small = case.h.add_file(case.h.assets, "small-boot.img",
                                    b"ANDROID!" + bytes(4096 - 8))
            transfer = dict(case.transfer)
            transfer["boot"] = small
            manifest = case.h.make_bundle_manifest(boot=small, transfer=transfer,
                                                   staging=case.staging)
            stage_and_init(case, manifest)
            case.land()
            (case.h.incoming / small.name).write_bytes(small.read_bytes())
            boot_before = sha256_file(case.h.node("mmcblk0p10"))
            dry = case.h.run("finalize", manifest, "--dry-run")
            self.assertNotEqual(dry.returncode, 0)
            self.assertTrue(case.h.receipt().get("error", "").startswith("boot-size"),
                            case.h.receipt())
            self.assertEqual(sha256_file(case.h.node("mmcblk0p10")), boot_before)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_successful_dry_run_leaves_userdata_and_guard_untouched(self) -> None:
        self.full_init()
        self.land_uploads()
        tree_before = tree_snapshot(self.h.data)
        guard_before = (self.h.state / "transaction.state").read_bytes()
        result = self.h.run("finalize", self.manifest(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.h.receipt().get("result"), "dry-run-ok")
        self.assertEqual(tree_snapshot(self.h.data), tree_before)
        self.assertEqual((self.h.state / "transaction.state").read_bytes(), guard_before)
        self.assertEqual(len(self.h.calls_to("mke2fs")), 1)

    # -- (4) mandatory roles + strict manifest validation --------------------
    def test_every_transfer_role_is_mandatory(self) -> None:
        for missing in ("boot", "ota-manifest", "ota-signature", "local-package"):
            with self.subTest(missing=missing):
                work = Path(tempfile.mkdtemp(prefix="le-role-"))
                try:
                    case = BasicCase(work)
                    good = case.manifest()
                    trimmed = {role: path for role, path in case.transfer.items()
                               if role != missing}
                    bad = case.manifest(transfer=trimmed, name="bad.manifest")
                    self.assertEqual(case.h.run("prepare", good).returncode, 0)
                    init_bad = case.h.run("initialize", bad)
                    self.assertNotEqual(init_bad.returncode, 0, missing)
                    self.assertEqual(case.h.calls_to("mke2fs"), [], missing)
                    self.assertEqual(case.h.run("initialize", good).returncode, 0, missing)
                    case.land()
                    boot_before = sha256_file(case.h.node("mmcblk0p10"))
                    fin_bad = case.h.run("finalize", bad)
                    self.assertNotEqual(fin_bad.returncode, 0, missing)
                    self.assertEqual(sha256_file(case.h.node("mmcblk0p10")), boot_before, missing)
                    error = case.h.receipt().get("error", "")
                    self.assertTrue(error.startswith("manifest-missing"),
                                    (missing, error))
                finally:
                    shutil.rmtree(work, ignore_errors=True)

    def test_strict_manifest_validation(self) -> None:
        boot_sha = sha256_file(self.boot)
        package_pin = f"local_package={self.package.name}:{sha256_file(self.package)}"
        checks = [
            ("duplicate role",
             lambda: self.manifest(extra=f"transfer=boot:{self.boot.name}:{boot_sha}"),
             "manifest-duplicate-role:boot"),
            ("duplicate feature",
             lambda: self.manifest(extra=(
                 f"staging=tts:{self.payload.name}:{sha256_file(self.payload)}"
                 f":{self.fmanifest.name}:{sha256_file(self.fmanifest)}")),
             "manifest-duplicate-feature:tts"),
            ("malformed transfer line",
             lambda: self.manifest(extra="transfer=boot:onlyname"),
             "manifest-malformed:transfer"),
            ("unknown key",
             lambda: self.manifest(extra="evil_key=1"),
             "manifest-unknown-key:evil_key"),
            ("bad boot digest",
             lambda: self.edit_manifest(f"boot_image_sha256={boot_sha}", "boot_image_sha256=" + "z" * 64),
             "manifest-bad-digest:boot_image_sha256"),
            ("unsafe boot name",
             lambda: self.edit_manifest("boot_image=boot.img", "boot_image=../boot.img"),
             "manifest-bad-name:boot_image"),
            ("non-numeric transfer bytes",
             lambda: self.edit_manifest(f"transfer_bytes_total={self.h.transfer_bytes}",
                                        "transfer_bytes_total=lots"),
             "manifest-bad-size:transfer_bytes_total"),
            ("userdata out of contract",
             lambda: self.edit_manifest(f"userdata_sectors={USERDATA_SECTORS}",
                                        "userdata_sectors=123456"),
             "manifest-bad-size:userdata_sectors"),
            ("local package pin mismatch",
             lambda: self.edit_manifest(package_pin,
                                        f"local_package={self.package.name}:" + "0" * 64),
             "manifest-local-package-mismatch"),
            ("local package pin absent",
             lambda: self.edit_manifest(package_pin + "\n", ""),
             "manifest-missing-key:local_package"),
            ("empty value",
             lambda: self.edit_manifest("release=0.14.0-test", "release="),
             "manifest-empty-value:release"),
        ]
        for label, make, prefix in checks:
            with self.subTest(label=label):
                manifest = make()
                result = self.h.run("prepare", manifest)
                self.assertNotEqual(result.returncode, 0, label)
                error = self.h.receipt().get("error", "")
                self.assertTrue(error.startswith(prefix), (label, error))

    def test_non_hex_bundle_sha_argument_refused(self) -> None:
        manifest = self.manifest()
        result = self.h.run_raw(
            "--protocol", "2", "--phase", "prepare", "--bundle-manifest", str(manifest),
            "--bundle-manifest-sha256", "Z" * 64, "--state-dir", str(self.h.state))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "bundle-manifest-sha256-invalid")

    # -- (6) monotone guard / no uncertain retries ---------------------------
    def test_prepare_and_transfer_cannot_regress_finalized(self) -> None:
        self.full_init()
        self.land_uploads()
        self.assertEqual(self.h.run("finalize", self.manifest()).returncode, 0)
        guard = self.h.state / "transaction.state"
        guard_before = guard.read_bytes()
        boot_before = sha256_file(self.h.node("mmcblk0p10"))
        expectations = {
            "prepare": "already-finalized",
            "transfer": "already-finalized",
            "finalize": "already-finalized",
            "initialize": "already-initialized",
        }
        for phase, token in expectations.items():
            with self.subTest(phase=phase):
                result = self.h.run(phase, self.manifest())
                self.assertNotEqual(result.returncode, 0, phase)
                self.assertEqual(self.h.receipt().get("error"), token, phase)
        self.assertEqual(guard.read_bytes(), guard_before)
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), boot_before)

    def test_finalizing_guard_refuses_repeats_even_dry_run(self) -> None:
        self.full_init()
        guard = self.h.state / "transaction.state"
        self.assertIn("phase=transfer\n", guard.read_text())
        guard.write_text(guard.read_text().replace("phase=transfer", "phase=finalizing"))
        self.land_uploads()
        boot_before = sha256_file(self.h.node("mmcblk0p10"))
        for phase, extra in (("finalize", ()), ("finalize", ("--dry-run",)),
                             ("prepare", ()), ("transfer", ())):
            with self.subTest(phase=phase, extra=extra):
                result = self.h.run(phase, self.manifest(), *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.h.receipt().get("error"), "finalize-uncertain")
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), boot_before)

    def test_formatting_guard_blocks_prepare_and_initialize(self) -> None:
        self.prepare()
        guard = self.h.state / "transaction.state"
        guard.write_text(guard.read_text().replace("format_state=absent", "format_state=formatting"))
        for phase in ("prepare", "initialize"):
            with self.subTest(phase=phase):
                result = self.h.run(phase, self.manifest())
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.h.receipt().get("error"), "format-uncertain")
        self.assertEqual(self.h.calls_to("mke2fs"), [])

    # -- (7) finalize requires a completed transfer --------------------------
    def test_finalize_requires_transfer_completed(self) -> None:
        self.prepare()
        self.initialize()
        self.land_uploads()
        boot_before = sha256_file(self.h.node("mmcblk0p10"))
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("phase-order"))
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), boot_before)
        self.assertEqual(self.h.run("transfer", self.manifest()).returncode, 0)
        self.assertEqual(self.h.run("finalize", self.manifest()).returncode, 0)
        self.assertEqual(self.h.receipt().get("result"), "installed")

    # -- (8)+(9) signed-manifest cross-checks before the first boot write ----
    def test_signed_vs_staging_cross_checks(self) -> None:
        checks = [
            ("digest disagreement", "feature_tts_sha256", "0" * 64,
             "signed-staging-digest-mismatch:tts"),
            ("payload size disagreement", "feature_tts_size", "999",
             "signed-payload-size:tts"),
            ("manifest size disagreement", "feature_tts_manifest_size", "999",
             "signed-manifest-size:tts"),
            ("boot digest disagreement", "boot_sha256", "0" * 64,
             "signed-manifest-boot-mismatch"),
            ("board mismatch", "board", "biscuit",
             "signed-manifest-board-mismatch"),
        ]
        for label, key, value, prefix in checks:
            with self.subTest(label=label):
                def edit(case):
                    lines = case.ota.read_text().splitlines()
                    self.assertEqual(sum(line.startswith(key + "=") for line in lines), 1)
                    case.ota.write_text("\n".join(
                        key + "=" + value if line.startswith(key + "=") else line
                        for line in lines) + "\n")
                self._fresh_cross_check(edit, prefix, label=label)

    def test_signed_feature_set_must_match_staging_exactly(self) -> None:
        for ids, token in (("tts,airplay2", "signed-manifest-feature-mismatch"),
                           ("tts,tts", "duplicate-signed-feature")):
            with self.subTest(ids=ids):
                self._fresh_cross_check(
                    lambda c: c.ota.write_text(c.ota.read_text().replace(
                        "feature_ids=tts\n", f"feature_ids={ids}\n")), token, label=ids)
        def extra_staging(case):
            case.staging.append(("airplay2", case.payload, case.fmanifest))
        self._fresh_cross_check(extra_staging, "signed-manifest-feature-mismatch:airplay2",
                                label="unsigned staging entry")

    def test_unsupported_feature_actions_refused(self) -> None:
        for action in ("runtime", "delete"):
            with self.subTest(action=action):
                self._fresh_cross_check(
                    lambda c: c.ota.write_text(c.ota.read_text().replace(
                        "feature_tts_action=replace", f"feature_tts_action={action}")),
                    f"unsupported-feature-action:tts:{action}", label=action)

    def test_signed_manifest_must_have_exactly_one_feature_ids_line(self) -> None:
        for replacement in ("", "feature_ids=tts\nfeature_ids=tts\n"):
            with self.subTest(replacement=replacement):
                self._fresh_cross_check(
                    lambda c: c.ota.write_text(c.ota.read_text().replace(
                        "feature_ids=tts\n", replacement)),
                    "signed-manifest-feature-ids", label="feature_ids singleton")

    # -- (10) path confinement / symlink ancestors ---------------------------
    def test_symlinked_ancestors_and_feature_paths_refused(self) -> None:
        self.full_init()
        self.land_uploads()
        outside = self.work / "outside"
        outside.mkdir()
        update = self.h.data / "libreecho" / "update"
        shutil.rmtree(update)
        update.symlink_to(outside)
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("path-symlink"),
                        self.h.receipt())
        self.assertFalse((outside / "staging" / "manifest").exists(),
                         "must not write through a symlinked ancestor")

    def test_incoming_symlink_refused(self) -> None:
        self.full_init()
        self.land_uploads()
        shutil.rmtree(self.h.incoming)
        elsewhere = self.work / "elsewhere"
        elsewhere.mkdir()
        self.h.incoming.symlink_to(elsewhere)
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((elsewhere / self.payload.name).exists())

    def test_feature_live_dir_symlink_refused(self) -> None:
        self.full_init()
        self.land_uploads()
        features = self.h.data / "libreecho" / "features"
        outside = self.work / "outside-features"
        outside.mkdir()
        (features / "tts").symlink_to(outside)
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((outside / "payload.squashfs").exists())

    def test_custom_cli_paths_outside_the_contract_refused(self) -> None:
        manifest = self.manifest()
        common = ["--protocol", "2", "--phase", "prepare",
                  "--bundle-manifest", str(manifest),
                  "--bundle-manifest-sha256", sha256_file(manifest),
                  "--target", TARGET, "--release", "0.14.0-test"]
        evil_state = self.work / "evil-state"
        result = self.h.run_raw(*common, "--state-dir", str(evil_state),
                                "--incoming-dir", str(self.h.incoming))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(evil_state.exists())
        elsewhere = self.work / "elsewhere-incoming"
        result = self.h.run_raw(*common, "--state-dir", str(self.h.state),
                                "--incoming-dir", str(elsewhere))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(elsewhere.exists())
        escaped = self.h.state / ".." / "libreecho-direct"
        result = self.h.run_raw(*common, "--state-dir", str(escaped),
                                "--incoming-dir", str(self.h.incoming))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())

    def test_symlinked_state_dir_refused(self) -> None:
        real = self.work / "real-state"
        real.mkdir()
        link = self.work / "cache" / "linked-direct"
        link.symlink_to(real)
        manifest = self.manifest()
        result = self.h.run_raw(
            "--protocol", "2", "--phase", "prepare", "--bundle-manifest", str(manifest),
            "--bundle-manifest-sha256", sha256_file(manifest),
            "--state-dir", str(link), "--incoming-dir", str(self.h.incoming))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((real / "receipt").exists())

    # -- (11) production block-node identity ---------------------------------
    def test_production_helper_refuses_regular_file_partitions(self) -> None:
        result = self.h.run_production("prepare", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "partition-identity:userdata")
        self.assertEqual(self.h.calls_to("sgdisk"), [])
        self.assertEqual(self.h.calls_to("mke2fs"), [])

    def test_block_node_identity_checks_major_minor_and_pattern(self) -> None:
        source = HELPER.read_text()
        parts = [extract_function(source, name)
                 for name in ("dev_t_of", "node_is_block", "block_node_identity")]
        real = None
        for candidate in ("/dev/sda", "/dev/vda", "/dev/nvme0n1", "/dev/loop0",
                          "/dev/loop1", "/dev/loop2", "/dev/mmcblk0"):
            path = Path(candidate)
            if path.exists() and stat.S_ISBLK(os.stat(path).st_mode):
                real = path
                break
        if real is None:
            self.skipTest("no host block device available for the dev_t probe")
        st = os.stat(real)
        sysdir = self.work / "sysref"
        (sysdir / "mmcblk0p99").mkdir(parents=True)
        (sysdir / "mmcblk0p99" / "uevent").write_text("DEVNAME=mmcblk0p99\nPARTNAME=userdata\n")
        (sysdir / "mmcblk0p99" / "dev").write_text(f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}\n")
        regular = self.work / "regular-node"
        regular.write_bytes(b"\0")
        harness = self.work / "identity.sh"
        harness.write_text(
            "#!/bin/sh\nset -u\n"
            f"SYS_BLOCK={sysdir}\n"
            "probe() { if block_node_identity \"$1\" \"$2\"; then echo \"ACCEPT:$3\"; "
            "else echo \"REJECT:$3\"; fi; }\n"
            + "\n".join(parts) + "\n"
            f"probe mmcblk0p99 /dev/null devnull\n"
            f"probe mmcblk0p99 {regular} regular-with-swapped-dev\n"
            f"probe mmcblk0p99 {real} real-block-matching\n"
            f"probe sda1 {real} wrong-parent-pattern\n")
        os.chmod(harness, os.stat(harness).st_mode | stat.S_IEXEC)
        result = subprocess.run(["/bin/sh", str(harness)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [
            "REJECT:devnull",
            "REJECT:regular-with-swapped-dev",
            "ACCEPT:real-block-matching",
            "REJECT:wrong-parent-pattern",
        ], result.stdout + result.stderr)
        # Now make sysfs disagree about the device: the real node must be refused.
        (sysdir / "mmcblk0p99" / "dev").write_text("1:2\n")
        result = subprocess.run(["/bin/sh", str(harness)], text=True, capture_output=True)
        self.assertEqual(result.stdout.split(), [
            "REJECT:devnull",
            "REJECT:regular-with-swapped-dev",
            "REJECT:real-block-matching",
            "REJECT:wrong-parent-pattern",
        ], result.stdout + result.stderr)

    def test_boot_slot_geometry_checked(self) -> None:
        part, _ = self.h.partitions["boot_a"]
        (self.h.sys / part / "size").write_text("100\n")
        result = self.h.run("prepare", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("boot-slot-size:boot_a"),
                        self.h.receipt())

    # -- (12) device identity + storage hygiene ------------------------------
    def test_serial_missing_refused(self) -> None:
        self.h.set_serial("")
        result = self.h.run("prepare", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "serial-missing")
        self.assertEqual(self.h.guard(), {})

    def test_device_digest_binds_serial_and_userdata_guid(self) -> None:
        digests = {}
        variations = {
            "base": ("11111111-1111-1111-1111-111111111111", "SERIAL-ONE"),
            "other-guid": ("22222222-2222-2222-2222-222222222222", "SERIAL-ONE"),
            "other-serial": ("11111111-1111-1111-1111-111111111111", "SERIAL-TWO"),
        }
        for label, (guid, serial) in variations.items():
            work = Path(tempfile.mkdtemp(prefix="le-digest-"))
            try:
                case = BasicCase(work, guid=guid, serial=serial)
                result = case.h.run("prepare", case.manifest())
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                digest = case.h.receipt().get("device_digest", "")
                self.assertEqual(len(digest), 64, label)
                digests[label] = digest
            finally:
                shutil.rmtree(work, ignore_errors=True)
        self.assertEqual(len(set(digests.values())), 3,
                         f"digest must bind serial + userdata GUID: {digests}")

    def test_transfer_requires_rw_ext4_real_userdata_mount(self) -> None:
        self.full_init()
        node = self.h.node("mmcblk0p16")
        mounts = self.h.mounts.read_text()
        self.h.mounts.write_text(mounts.replace(f"{node} {self.h.data} ext4 rw",
                                                f"{node} {self.h.data} ext4 ro"))
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("data-mount-not-rw"),
                        self.h.receipt())
        self.h.mounts.write_text(mounts.replace(f"{node} {self.h.data} ext4 rw",
                                                f"{node} {self.h.data} f2fs rw"))
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("data-mount-fstype"),
                        self.h.receipt())

    def test_layout_fingerprint_enforced_on_every_later_phase(self) -> None:
        self.full_init()
        part, _ = self.h.partitions["userdata"]
        size_file = self.h.sys / part / "size"
        size_file.write_text(f"{OUT_OF_CONTRACT_SECTORS}\n")
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "userdata-layout-mismatch")
        size_file.write_text(f"{USERDATA_SECTORS}\n")
        self.assertEqual(self.h.run("transfer", self.manifest()).returncode, 0)
        self.land_uploads()
        size_file.write_text(f"{OUT_OF_CONTRACT_SECTORS}\n")
        boot_before = sha256_file(self.h.node("mmcblk0p10"))
        result = self.h.run("finalize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "userdata-layout-mismatch")
        self.assertEqual(sha256_file(self.h.node("mmcblk0p10")), boot_before)

    def test_df_gate_is_numeric_and_accounts_for_overhead(self) -> None:
        self.full_init()
        need_bytes = self.h.transfer_bytes + TRANSFER_OVERHEAD_BYTES
        # A free-space figure that covers the payloads but not the overhead is refused.
        self.h.free_file.write_text(str(need_bytes // 1024 - 1))
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "insufficient-space")
        # Non-numeric output is refused, not parsed as zero.
        self.h.free_file.write_text("12x")
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "free-space-unknown")
        # A df that has no row for the mount is refused.
        (self.h.sbin / "df").write_text(
            "#!/bin/sh\necho \"Filesystem 1024-blocks Used Available Capacity Mounted on\"\n")
        (self.h.sbin / "df").chmod((self.h.sbin / "df").stat().st_mode | stat.S_IEXEC)
        result = self.h.run("transfer", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "free-space-unknown")

    def test_df_gate_accepts_real_figures_under_32_bit_mksh(self) -> None:
        """TWRP runs the helper under mksh, whose `[` compares integers in
        32 bits. A 2^52 sanity bound made every real df figure fail with
        free-space-unknown on hardware while dash passed the same test."""
        mksh = shutil.which("mksh")
        if mksh is None:
            self.skipTest("mksh is not installed")
        self.full_init()
        self.h.free_file.write_text("1025664")      # observed Biscuit userdata
        argv = self.h._argv(self.h.helper, "transfer", self.manifest(), ())
        argv[0] = mksh
        result = subprocess.run(argv, text=True, capture_output=True, env=self.h.helper_env())
        self.assertEqual(result.returncode, 0, result.stderr + str(self.h.receipt()))
        self.assertEqual(self.h.receipt().get("result"), "transferred")
        # An absurdly long figure is still refused as a parse artefact.
        self.h.free_file.write_text("1" * 10)
        result = subprocess.run(argv, text=True, capture_output=True, env=self.h.helper_env())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "free-space-unknown")

    def test_reshape_range_checks_and_readback(self) -> None:
        part, _ = self.h.partitions["userdata"]
        size_file = self.h.sys / part / "size"
        size_file.write_text(f"{OUT_OF_CONTRACT_SECTORS}\n")
        # Disk too small for the target geometry: refused before any sgdisk write.
        (self.h.sys / "mmcblk0" / "size").write_text("1000\n")
        result = self.h.run("prepare", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "reshape-range")
        self.assertEqual(self.h.mutating_sgdisk(), [])
        # First sector refusing GPT area.
        (self.h.sys / "mmcblk0" / "size").write_text("4194304\n")
        self.h.rewrite_sgdisk(first=10)
        result = self.h.run("prepare", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "reshape-range")
        self.assertEqual(self.h.mutating_sgdisk(), [])
        # Readback must match the requested geometry.
        self.h.rewrite_sgdisk(last=999)
        result = self.h.run("prepare", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "userdata-last-sector-mismatch")
        self.assertTrue(self.h.mutating_sgdisk())

    def test_guard_write_failure_fails_closed_before_writes(self) -> None:
        part, _ = self.h.partitions["userdata"]
        (self.h.sys / part / "size").write_text(f"{OUT_OF_CONTRACT_SECTORS}\n")
        (self.h.state / "transaction.state").mkdir()
        result = self.h.run("prepare", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.h.receipt().get("error", "").startswith("guard-"),
                        self.h.receipt())
        self.assertEqual(self.h.mutating_sgdisk(), [], "no GPT write after a failed guard")

    def test_no_unrelated_userdata_is_deleted(self) -> None:
        self.full_init()
        keep = self.h.data / "local" / "keep.txt"
        keep.parent.mkdir(parents=True)
        keep.write_text("user data that recovery must not delete")
        media = self.h.data / "media" / "picture.jpg"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"jpeg")
        (self.h.data / "tmp").mkdir()
        # TWRP recreates /data/media/0 after formatting; an all-empty tree
        # must go or the first boot's data contract blocks every service.
        (self.h.data / "test" / "0" / "Android").mkdir(parents=True)
        self.land_uploads()
        result = self.h.run("finalize", self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        self.assertEqual(keep.read_text(), "user data that recovery must not delete")
        self.assertTrue(media.is_file())
        self.assertFalse((self.h.data / "tmp").exists(),
                         "empty recovery scratch dirs are removed with rmdir")
        self.assertFalse((self.h.data / "test").exists(),
                         "nested empty recovery scratch trees are removed")
        helper_tidy = extract_function(HELPER.read_text(), "tidy_userdata_root")
        self.assertNotIn("rm -rf", helper_tidy)
        self.assertIn("rmdir", helper_tidy)
        legacy_tidy = extract_function(LEGACY.read_text(), "tidy_userdata_root")
        self.assertNotIn("rm -rf", legacy_tidy)
        self.assertIn("rmdir", legacy_tidy)

    def test_placement_is_hardlink_only_and_hashes_uploads_once(self) -> None:
        source = HELPER.read_text()
        place = extract_function(source, "place_linked")
        self.assertIn("ln ", place)
        self.assertNotIn("mv ", place)
        self.assertNotIn("cp ", place)
        self.full_init()
        self.land_uploads()
        result = self.h.run("finalize", self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + str(self.h.receipt()))
        incoming_payload = self.h.incoming / self.payload.name
        placed = (self.h.data / "libreecho" / "update" / "staging" / "features"
                  / "tts" / "tts.squashfs")
        self.assertEqual(placed.stat().st_ino, incoming_payload.stat().st_ino)
        self.assertLessEqual(self.h.hashes_of(incoming_payload), 2,
                             "uploads must be hashed once into an index, not once per lookup")

    def test_reset_cannot_be_used_to_retry_an_uncertain_format(self) -> None:
        self.prepare()
        guard = self.h.state / "transaction.state"
        guard.write_text(guard.read_text().replace("format_state=absent", "format_state=formatting"))
        reset = self.h.run("prepare", self.manifest(), "--reset-transaction")
        self.assertNotEqual(reset.returncode, 0)
        result = self.h.run("initialize", self.manifest())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.receipt().get("error"), "format-uncertain")
        self.assertEqual(self.h.calls_to("mke2fs"), [])


class BothTargetsTests(unittest.TestCase):
    def test_helper_binds_both_targets(self) -> None:
        for target, product in (("radar_puffin", "RADAR"), ("biscuit", "BISCUIT")):
            with self.subTest(target=target):
                work = Path(tempfile.mkdtemp(prefix="le-direct-t-"))
                try:
                    h = Harness(work, target=target)
                    boot = h.add_file(h.assets, "boot.img",
                                      b"ANDROID!" + bytes(BOOT_BYTES - 8))
                    payload = h.add_file(h.assets, "tts.payload.squashfs", b"p")
                    fman = h.add_file(h.assets, "tts.manifest.json", b"{}")
                    ota = h.add_file(h.assets, "manifest",
                                     f"board={target}\nversion=1\n"
                                     f"boot_sha256={sha256_file(boot)}\nfeature_ids=tts\n")
                    sig = h.add_file(h.assets, "manifest.sig", b"s")
                    package = h.add_file(h.assets, "1.ota.tar", b"t")
                    h.transfer_bytes = 1
                    manifest = h.make_bundle_manifest(
                        boot=boot, transfer={"boot": boot, "ota-manifest": ota,
                                             "ota-signature": sig, "local-package": package},
                        staging=[("tts", payload, fman)])
                    result = h.run("prepare", manifest)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    self.assertEqual(h.receipt().get("target"), target)
                    self.assertEqual(h.receipt().get("result"), "prepare-noop")
                    self.assertIn(product, (h.bundle / "bundle.manifest").read_text())
                finally:
                    shutil.rmtree(work, ignore_errors=True)


class LegacyGuardTests(unittest.TestCase):
    DATES = None

    def _run_guard(self, mounts_text: str, bundle_dir: str):
        source = LEGACY.read_text()
        self.assertIn("bundle_source_is_userdata", source,
                      "legacy update-binary must carry the userdata-source guard")
        function = extract_function(source, "bundle_source_is_userdata")
        work = Path(tempfile.mkdtemp(prefix="le-legacy-"))
        try:
            mnt = work / "mnt"
            mounts = work / "mounts"
            mounts.write_text(mounts_text)
            harness = work / "t.sh"
            harness.write_text(
                "#!/bin/sh\nset -u\n"
                "partition_node() { case \"$1\" in userdata) echo /dev/block/mmcblk0p16;; esac; }\n"
                'part_node() { echo "$1"; }\n'
                f"{function}\n"
                f'if bundle_source_is_userdata "{bundle_dir}"; then echo USERDATA; else echo OTHER; fi\n')
            env = os.environ.copy()
            env["LIBREECHO_MOUNTS_FILE"] = str(mounts)
            return subprocess.run(["/bin/sh", str(harness)], text=True,
                                  capture_output=True, env=env)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_legacy_refuses_bundle_sourced_from_userdata(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="le-legacy-"))
        try:
            mnt = work / "mnt"
            data_mnt = mnt / "data"
            cache_mnt = mnt / "cache"
            (data_mnt / "bundle").mkdir(parents=True)
            (cache_mnt / "bundle").mkdir(parents=True)
            mounts = work / "mounts"
            mounts.write_text(
                f"/dev/block/mmcblk0p16 {data_mnt} ext4 rw 0 0\n"
                f"/dev/block/mmcblk0p15 {cache_mnt} ext4 rw 0 0\n")
            source = LEGACY.read_text()
            self.assertIn("bundle_source_is_userdata", source,
                          "legacy update-binary must carry the userdata-source guard")
            function = extract_function(source, "bundle_source_is_userdata")
            harness = work / "t.sh"
            harness.write_text(
                "#!/bin/sh\nset -u\n"
                "partition_node() { case \"$1\" in userdata) echo /dev/block/mmcblk0p16;; esac; }\n"
                'part_node() { echo "$1"; }\n'
                f"{function}\n"
                f'if bundle_source_is_userdata "{data_mnt}/bundle"; then echo USERDATA; else echo OTHER; fi\n'
                f'if bundle_source_is_userdata "{cache_mnt}/bundle"; then echo USERDATA; else echo OTHER; fi\n')
            env = os.environ.copy()
            env["LIBREECHO_MOUNTS_FILE"] = str(mounts)
            result = subprocess.run(["/bin/sh", str(harness)], text=True,
                                    capture_output=True, env=env)
            self.assertEqual(result.stdout.split(), ["USERDATA", "OTHER"], result.stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_legacy_fails_closed_on_unresolvable_mount_sources(self) -> None:
        """A device-mapper/overlay source cannot be proven distinct -> refuse."""
        work = Path(tempfile.mkdtemp(prefix="le-dm-"))
        try:
            mnt = work / "mnt"
            (mnt / "data" / "bundle").mkdir(parents=True)
            result = self._run_guard(
                f"/dev/block/dm-0 {mnt / 'data'} ext4 rw 0 0\n",
                str(mnt / "data" / "bundle"))
            self.assertEqual(result.stdout.split(), ["USERDATA"], result.stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_legacy_rejects_lexical_data_and_sdcard_aliases(self) -> None:
        for alias in ("/data/bundle", "/sdcard/bundle"):
            with self.subTest(alias=alias):
                result = self._run_guard("", alias)
                self.assertEqual(result.stdout.split(), ["USERDATA"], result.stderr)


class LegacyTidyTests(unittest.TestCase):
    def test_legacy_tidy_removes_recovery_nested_empty_dirs(self) -> None:
        # TWRP recreates /data/media/0 (and friends) after a format; a plain
        # rmdir of /data/media then fails and the first boot's data contract
        # blocks every service. Empty trees go; anything holding a file stays.
        source = LEGACY.read_text()
        function = extract_function(source, "tidy_userdata_root")
        start = source.index("prune_empty_dirs() (")
        prune = source[start:source.index("\n)\n", start) + 2]
        work = Path(tempfile.mkdtemp(prefix="le-tidy-nested-"))
        try:
            data = work / "data"
            (data / "media" / "0" / "Android" / "obb").mkdir(parents=True)
            (data / "media" / "0" / ".hidden").mkdir()
            (data / "libreecho").mkdir()
            keep = data / "local" / "a" / "keep.txt"
            keep.parent.mkdir(parents=True)
            keep.write_text("keep")
            harness = work / "t.sh"
            harness.write_text(
                "#!/bin/sh\nset -u\nDRY_RUN=0\n"
                'ui_print() { printf "%s\\n" "$*"; }\n'
                "sync() { :; }\n"
                f"{prune}\n{function}\ntidy_userdata_root\n")
            env = os.environ.copy()
            env["LIBREECHO_DATA_ROOT"] = str(data)
            result = subprocess.run(["/bin/sh", str(harness)], text=True,
                                    capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((data / "media").exists(), result.stdout)
            self.assertEqual(keep.read_text(), "keep")
            self.assertTrue((data / "libreecho").is_dir())
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_legacy_tidy_never_deletes_user_files(self) -> None:
        source = LEGACY.read_text()
        function = extract_function(source, "tidy_userdata_root")
        work = Path(tempfile.mkdtemp(prefix="le-tidy-"))
        try:
            data = work / "data"
            keep = data / "local" / "keep.txt"
            keep.parent.mkdir(parents=True)
            keep.write_text("keep")
            media = data / "media" / "pic.jpg"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"jpg")
            (data / "tmp").mkdir()
            harness = work / "t.sh"
            harness.write_text(
                "#!/bin/sh\nset -u\n"
                "DRY_RUN=0\n"
                'ui_print() { printf "%s\\n" "$*"; }\n'
                'log_line() { printf "%s\\n" "$*"; }\n'
                "sync() { :; }\n"
                + source[source.index("prune_empty_dirs() ("):
                         source.index("\n)\n", source.index("prune_empty_dirs() (")) + 2] + "\n"
                f"{function}\n"
                "tidy_userdata_root\n")
            env = os.environ.copy()
            env["LIBREECHO_DATA_ROOT"] = str(data)
            result = subprocess.run(["/bin/sh", str(harness)], text=True,
                                    capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(keep.read_text(), "keep")
            self.assertTrue(media.is_file())
            self.assertNotIn("rm -rf", function)
            self.assertIn("rmdir", function)
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
