#!/usr/bin/env python3
"""Multi-target contracts on public host fixtures; never touches real devices."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / 'ota'))
sys.path.insert(0, str(TOOLS / 'recovery-install'))
from test_ota_v2_implementation import manifest, feature
import feature_manifest
import build_install_bundle as bundle
import build_recovery_image as image
import verify_recovery_image as verifier

INSTALLER = TOOLS / 'recovery-install/src/META-INF/com/google/android/update-binary'


def function(source, name):
    start = source.index(name + '()')
    end = source.index('\n}', start) + 2
    return source[start:end] + '\n'


def run(command, **kwargs):
    return subprocess.run(command, text=True, capture_output=True, timeout=45, **kwargs)


class TargetCLITests(unittest.TestCase):
    def test_all_product_tools_advertise_target_and_descriptor(self):
        for tool in ('build_recovery_image.py', 'verify_recovery_image.py',
                     'ota/make_ota_bundle.py', 'recovery-install/build_install_bundle.py'):
            with self.subTest(tool=tool):
                result = run([sys.executable, str(TOOLS / tool), '--help'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--target ', result.stdout)
                self.assertIn('--target-descriptor-sha256', result.stdout)

    def test_cli_unknown_target_fails_before_inputs(self):
        result = run([sys.executable, str(TOOLS / 'recovery-install/build_install_bundle.py'),
                      '--assets', '/nonexistent', '--out', '/nonexistent', '--target', 'unknown'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown target', result.stderr)

    def test_signed_v2_biscuit_roundtrip_and_asset_identity(self):
        data = manifest([feature('assistant', 'replace')])
        data['board'] = 'biscuit'
        for record in data['features']:
            for field in ('asset', 'manifest_asset'):
                if field in record:
                    record[field] = record[field].replace('radar-puffin', 'biscuit')
        raw = feature_manifest.serialize_manifest(data)
        self.assertEqual(feature_manifest.parse_manifest(raw), data)
        bad = copy.deepcopy(data)
        bad['features'][-1]['asset'] = bad['features'][-1]['asset'].replace('biscuit', 'radar-puffin')
        with self.assertRaises(ValueError):
            feature_manifest.serialize_manifest(bad)

    def test_signed_v2_unknown_target_refused(self):
        data = manifest([])
        data['board'] = 'unknown'
        with self.assertRaises(ValueError):
            feature_manifest.serialize_manifest(data)

    def test_dtb_registry_uses_existing_semantics_for_both_targets(self):
        import libreecho_platform_targets
        for target in ('radar_puffin', 'biscuit'):
            self.assertEqual(libreecho_platform_targets.get_target(target)['dtb_verifier'], 'radar_puffin')
        with self.assertRaises(ValueError):
            libreecho_platform_targets.get_target('unknown')

    def test_real_target_dtb_cli_preserves_radar_validation_for_both_targets(self):
        from test_verify_radar_puffin_dtb import VALID_DTS
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, dtb = root / 'input.dts', root / 'input.dtb'
            for valid in (True, False):
                source.write_text(VALID_DTS if valid else VALID_DTS.replace('clock-frequency = <9600000>', 'clock-frequency = <24000000>'))
                compiled = run(['dtc', '-I', 'dts', '-O', 'dtb', '-o', str(dtb), str(source)])
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                for target in ('radar_puffin', 'biscuit'):
                    result = run([sys.executable, str(TOOLS / 'verify_target_dtb.py'), '--target', target, '--dtb', str(dtb)])
                    self.assertEqual(result.returncode == 0, valid, result.stdout + result.stderr)

    def test_image_verifier_requires_matching_identity_and_descriptor(self):
        import stat
        import libreecho_platform_targets
        for target in ('radar_puffin', 'biscuit'):
            metadata = {'board': target, 'ota': {'board': target}, 'target_descriptor_sha256': 'a' * 64}
            entries = {'etc/libreecho/target': verifier.Entry('etc/libreecho/target', stat.S_IFREG | 0o644, 0, 0, 0,
                        libreecho_platform_targets.identity_bytes(target, 'a' * 64))}
            verifier.validate_target_identity(entries, metadata, target, 'a' * 64)
            for other in ('unknown', 'radar_puffin' if target == 'biscuit' else 'biscuit'):
                with self.assertRaises((SystemExit, ValueError)):
                    verifier.validate_target_identity(entries, metadata, other, 'a' * 64)
            with self.assertRaises(SystemExit):
                verifier.validate_target_identity(entries, metadata, target, 'b' * 64)
            with self.assertRaises(SystemExit):
                verifier.validate_target_identity({}, metadata, target, 'a' * 64)

    def test_legacy_mirror_pin_refuses_unrelated_source_changes(self):
        raw = (TOOLS / 'initramfs/libreecho-init').read_bytes()
        self.assertEqual(image.sha256(image.legacy_init_mirror(raw)),
                         'f845a7b2070e5960f71cd777a5fdf47e7408598f5dc8564185d13feae0fb8d55')
        with self.assertRaises(SystemExit):
            image.legacy_init_mirror(raw + b'# unrelated change\n')

    def test_image_target_identity_and_radar_props(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / 'default.prop').write_bytes((TOOLS / 'initramfs/default.prop').read_bytes())
            original = (stage / 'default.prop').read_bytes()
            metadata = {}
            image.add_target_identity(stage, metadata, 'radar_puffin', 'a' * 64)
            self.assertEqual((stage / 'default.prop').read_bytes(), original)
            self.assertEqual((stage / 'etc/libreecho/target').read_text(),
                             'target_id=radar_puffin\nrelease_slug=radar-puffin\nhw_profile=radar_puffin@1\ndescriptor_sha256=' + 'a' * 64 + '\n')
            self.assertEqual(metadata['board'], 'radar_puffin')
            image.add_target_identity(stage, metadata, 'biscuit')
            self.assertIn('ro.product.device=biscuit', (stage / 'default.prop').read_text())
            self.assertIn('hw_profile=biscuit@0', (stage / 'etc/libreecho/target').read_text())
            self.assertEqual(metadata['board'], 'biscuit')

    def test_descriptor_digest_format_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            for invalid in ('A' * 64, 'a' * 63, 'a' * 64 + '\n'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    image.add_target_identity(Path(tmp), {}, 'radar_puffin', invalid)


class RuntimeTargetTests(unittest.TestCase):
    def check(self, target, board, *, consumer='libreecho-update', v2=False, mutate=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_file = root / 'target'
            if target is not None:
                target_file.write_text('target_id=' + target + '\n')
            staging = root / 'staging'; staging.mkdir()
            if v2:
                data = manifest([feature('assistant', 'replace')])
                raw = feature_manifest.serialize_manifest(data).decode().replace('board=radar_puffin', 'board=' + board)
                if board == 'biscuit': raw = raw.replace('libreecho-radar-puffin-', 'libreecho-biscuit-')
            else:
                raw = ('format=libreecho-ota-v1\nmanifest_version=1\nboard=' + board +
                       '\nsoc=mt8163\narchitecture=armv7\nversion=0.14.0\nboot_filename=boot.img\nboot_size=16777216\nboot_sha256=' + 'a' * 64 +
                       '\nfeature_policy=exclude\nimage_profile=ota\nservice_profile=diagnostic\nupdate_channel=dev\n')
            if mutate: raw = mutate(raw)
            (staging / 'manifest').write_text(raw)
            source = (TOOLS / 'initramfs' / consumer).read_text()
            if consumer == 'libreecho-update':
                source = source.split('\ncase "${1:-}" in', 1)[0]
                invocation = 'verify_manifest'
            else:
                # Only pure manifest validation plus real immutable target loader;
                # cryptography is covered by the maintained transaction suite.
                names = ['value', 'valid_hash', 'valid_uint', 'valid_token', 'valid_commit', 'valid_asset', 'key_allowed', 'check_manifest']
                if 'load_image_target()' in source: names.insert(0, 'load_image_target')
                source = '\n'.join(function(source, name) for name in names)
                source = 'BB=/bin/busybox\n' + source
                invocation = 'check_manifest "$STAGING/manifest"'
            source = source.replace('/etc/libreecho/target', str(target_file))
            harness = root / 'check.sh'
            harness.write_text(source + '\n' +
                f'STAGING={shlex.quote(str(staging))}\n' +
                'channel_value() { echo dev; }\n'
                'die() { echo "ERROR:$1" >&2; exit 1; }\n'
                'fail() { echo "ERROR:$1" >&2; exit 1; }\n'
                'verify_signed_manifest() { :; }\n' +
                invocation + '\n')
            return run(['/bin/busybox', 'sh', str(harness)])

    def test_missing_identity_accepts_radar_v1_and_logs(self):
        result = self.check(None, 'radar_puffin')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('target-file=missing fallback=radar_puffin', result.stderr)

    def test_same_target_v1_acceptance(self):
        for target in ('radar_puffin', 'biscuit'):
            result = self.check(target, target)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_target_v1_refused(self):
        for target, board in (('radar_puffin', 'biscuit'), ('biscuit', 'radar_puffin'), (None, 'biscuit')):
            result = self.check(target, board)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('manifest_board', result.stderr)

    def test_same_and_cross_target_v2_updater_and_transaction(self):
        for consumer in ('libreecho-update', 'libreecho-feature-transaction'):
            for target in ('radar_puffin', 'biscuit'):
                with self.subTest(consumer=consumer, target=target):
                    result = self.check(target, target, consumer=consumer, v2=True,
                                        mutate=lambda raw: raw.replace('update_channel=stable', 'update_channel=dev'))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    other = 'biscuit' if target == 'radar_puffin' else 'radar_puffin'
                    self.assertNotEqual(self.check(target, other, consumer=consumer, v2=True).returncode, 0)

    def test_unknown_or_duplicate_runtime_identity_fails_closed(self):
        for target in ('unknown', '', 'radar_puffin\ntarget_id=biscuit'):
            self.assertNotEqual(self.check(target, 'radar_puffin').returncode, 0)

    def test_radar_existing_manifest_rejections_unchanged(self):
        mutations = [lambda raw: raw.replace('soc=mt8163', 'soc=wrong'),
                     lambda raw: raw.replace('boot_size=16777216', 'boot_size=1'),
                     lambda raw: raw + 'board=radar_puffin\n',
                     lambda raw: raw.replace('update_channel=dev', 'update_channel=stable'),
                     lambda raw: raw + 'unknown=value\n']
        for mutation in mutations:
            self.assertNotEqual(self.check(None, 'radar_puffin', mutate=mutation).returncode, 0)

    def test_runtime_ignores_hostile_target_environment(self):
        with unittest.mock.patch.dict(os.environ, {'LIBREECHO_TARGET': 'biscuit'}):
            self.assertEqual(self.check(None, 'radar_puffin').returncode, 0)
            self.assertNotEqual(self.check(None, 'biscuit').returncode, 0)

    def test_missing_v2_identity_accepts_radar_and_logs_for_both_consumers(self):
        for consumer in ('libreecho-update', 'libreecho-feature-transaction'):
            result = self.check(None, 'radar_puffin', consumer=consumer, v2=True,
                                mutate=lambda raw: raw.replace('update_channel=stable', 'update_channel=dev'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('target-file=missing fallback=radar_puffin', result.stderr)

    def test_signed_cross_target_ota_refused_without_slot_or_bcb_writes(self):
        import test_ota_v2_implementation as fixtures
        for target, board in [('radar_puffin', 'biscuit'), ('biscuit', 'radar_puffin')]:
            fixture = fixtures.RuntimeHarnessTests()
            fixture.setUp()
            try:
                fixture.manifest['board'] = board
                if board == 'biscuit':
                    for record in fixture.manifest['features']:
                        for field in ('asset', 'manifest_asset'):
                            if field in record: record[field] = record[field].replace('radar-puffin', 'biscuit')
                fixture.package.write_bytes(feature_manifest.build_control_tar(fixture.manifest, fixture.boot, fixtures.KEY))
                identity = fixture.root / 'target'
                identity.write_text('target_id=' + target + '\n')
                transaction = fixtures.transaction_fixture(fixture.root, fixture.env)
                updater = fixtures.updater_fixture(fixture.root, fixture.env, transaction)
                updater.write_text(updater.read_text().replace('/etc/libreecho/target', str(identity)))
                before = [(fixture.parts / f'boot_{slot}').read_bytes() for slot in ('a', 'b')]
                result = run(['/bin/busybox', 'sh', str(updater), 'install', str(fixture.package)], env=fixture.env)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn('ERROR:v2_board', result.stderr)
                self.assertEqual(before, [(fixture.parts / f'boot_{slot}').read_bytes() for slot in ('a', 'b')])
                self.assertFalse(fixture.bootctl_log.exists())
            finally:
                fixture.tearDown()

    def test_first_install_marker_target_matching(self):
        source = (TOOLS / 'initramfs/libreecho-init').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / 'target'; marker = root / 'marker'
            body = function(source, 'load_image_target') + function(source, 'first_install_marker_matches')
            body = body.replace('/etc/libreecho/target', str(target))
            for identity, board, accepted in [(None, 'radar_puffin', True), ('biscuit', 'biscuit', True),
                                             ('biscuit', 'radar_puffin', False), ('unknown', 'radar_puffin', False)]:
                if identity is not None: target.write_text('target_id=' + identity + '\n')
                elif target.exists(): target.unlink()
                marker.write_text('schema=1\nmode=first-install\nboard=' + board + '\n')
                script = root / 'marker.sh'
                script.write_text('BB=/bin/busybox\nlog() { echo "$*" >&2; }\n' + body +
                                  '\nFIRST_INSTALL_MARKER=' + shlex.quote(str(marker)) + '\nfirst_install_marker_matches\n')
                result = run(['/bin/busybox', 'sh', str(script)])
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if identity is None: self.assertIn('fallback=radar_puffin', result.stderr)


class TargetBundleSurfaceTests(unittest.TestCase):
    def assets(self, root, target):
        from nacl.signing import SigningKey
        assets = root / 'assets'; assets.mkdir()
        boot = assets / 'boot.img'; boot.write_bytes(b'ANDROID!' + bytes(16777216 - 8))
        key = SigningKey(bytes(range(32)))
        public = assets / 'ota-public-key.hex'; public.write_text(key.verify_key.encode().hex() + '\n')
        records = []
        installs = []
        slug = 'biscuit' if target == 'biscuit' else 'radar-puffin'
        def asset(path):
            return {'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size}
        for fid in ('airplay2', 'tts', 'wakeword', 'stt', 'assistant'):
            record = feature(fid, 'replace')
            prefix = f'libreecho-{slug}-0.13.11-{fid}'
            payload = assets / (prefix + '.payload.squashfs'); payload.write_text('payload-' + fid)
            metadata = assets / (prefix + '.manifest.json'); metadata.write_text(json.dumps({'feature': fid}))
            record.update({'asset': payload.name, 'size': payload.stat().st_size, 'sha256': asset(payload)['sha256'],
                           'manifest_asset': metadata.name, 'manifest_size': metadata.stat().st_size, 'manifest_sha256': asset(metadata)['sha256']})
            records.append(record)
            installs.append({'name': fid, 'payload': asset(payload), 'manifest': asset(metadata)})
        signed = manifest(records)
        signed['board'] = target
        signed['boot_sha256'] = asset(boot)['sha256']
        raw = feature_manifest.serialize_manifest(signed)
        signature = feature_manifest.sign_manifest(signed, key)
        key.verify_key.verify(raw, bytes.fromhex(signature.decode().strip()))
        (assets / 'manifest').write_bytes(raw)
        (assets / 'manifest.sig').write_bytes(signature)
        install = {'schema': 1, 'release': 'radar-puffin-v0.14.0', 'board': target, 'soc': 'mt8163',
                   'image_profile': 'ota', 'service_profile': 'production', 'boot': asset(boot),
                   'ota_public_key': asset(public), 'features': installs, 'amonet': {'commit': '0' * 40}}
        (assets / 'manifest.json').write_text(json.dumps(install))
        return assets, signed

    def test_real_recovery_cli_names_identity_and_radar_byte_aliases(self):
        for target in ('radar_puffin', 'biscuit'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); assets, signed = self.assets(root, target); out = root / 'out'
                result = run([sys.executable, str(TOOLS / 'recovery-install/build_install_bundle.py'),
                              '--assets', str(assets), '--out', str(out), '--target', target])
                self.assertEqual(result.returncode, 0, result.stderr)
                slug = 'biscuit' if target == 'biscuit' else 'radar-puffin'
                text = (out / f'libreecho-{slug}-bundle.manifest').read_text()
                self.assertIn('target=' + target, text)
                self.assertIn('fastboot_products=' + ('BISCUIT' if target == 'biscuit' else 'RADAR'), text)
                self.assertTrue((out / f'libreecho-{slug}-install.zip').is_file())
                sums = out / f'libreecho-radar-puffin-v0.14.0-{slug}-TWRPINSTALL-SHA256SUMS'
                for line in sums.read_text().splitlines():
                    digest, name = line.split('  ', 1)
                    self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)
                self.assertEqual(bundle.check_bundle(out, target), [])
                if target == 'radar_puffin':
                    for name, alias in [('libreecho-radar-puffin-install.zip', 'libreecho-install.zip'),
                                        ('libreecho-radar-puffin-bundle.manifest', 'bundle.manifest')]:
                        self.assertEqual((out / name).read_bytes(), (out / alias).read_bytes())
                else:
                    self.assertFalse((out / 'libreecho-install.zip').exists())
                    self.assertFalse((out / 'bundle.manifest').exists())

    def test_recovery_cli_environment_and_flag_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); assets, signed = self.assets(root, 'biscuit')
            env = os.environ | {'LIBREECHO_TARGET': 'biscuit'}
            base = [sys.executable, str(TOOLS / 'recovery-install/build_install_bundle.py'), '--assets', str(assets)]
            result = run(base + ['--out', str(root / 'biscuit')], env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run(base + ['--out', str(root / 'radar'), '--target', 'radar_puffin'], env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('install manifest board', result.stderr)
            result = run(base + ['--out', str(root / 'unknown')], env=os.environ | {'LIBREECHO_TARGET': 'bad'})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unknown target', result.stderr)

    def test_recovery_builder_rejects_cross_target_signed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); assets, signed = self.assets(root, 'radar_puffin')
            (assets / 'manifest').write_text((assets / 'manifest').read_text().replace('board=radar_puffin', 'board=biscuit'))
            with self.assertRaisesRegex(bundle.BuildError, 'signed OTA manifest board'):
                bundle.assemble(assets, root / 'out', TOOLS / 'recovery-install/src', '', 2153472)

    def test_ota_signer_cli_targets_and_cross_target_rejection(self):
        import tarfile
        from nacl.signing import SigningKey
        for target in ('radar_puffin', 'biscuit'):
            for version in ('v1', 'v2'):
                with self.subTest(target=target, version=version), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); assets, signed = self.assets(root, target)
                    boot = assets / 'boot.img'
                    build = root / 'build.json'
                    build.write_text(json.dumps({'board': target, 'image_profile': 'ota', 'service_profile': 'diagnostic',
                                                 'feature_policy': 'exclude', 'update_channel': 'dev',
                                                 'output': {'size': boot.stat().st_size, 'sha256': signed['boot_sha256']}}))
                    secret = root / 'test-key.hex'; secret.write_text(bytes(range(32)).hex())
                    plan = root / 'plan.json'; plan.write_text(json.dumps({'board': target, 'features': signed['features']}))
                    output = root / 'update.ota.tar'
                    command = [sys.executable, str(TOOLS / 'ota/make_ota_bundle.py'), '--format', version,
                               '--boot-image', str(boot), '--build-manifest', str(build), '--version', '0.14.0',
                               '--signing-key', str(secret), '--public-key', str(assets / 'ota-public-key.hex'),
                               '--service-profile', 'diagnostic', '--feature-policy', 'exclude', '--update-channel', 'dev',
                               '--output', str(output), '--feature-plan', str(plan)]
                    result = run(command, env=os.environ | {'LIBREECHO_TARGET': target})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    with tarfile.open(output) as archive:
                        raw = archive.extractfile('manifest').read()
                        signature = archive.extractfile('manifest.sig').read()
                    SigningKey(bytes(range(32))).verify_key.verify(raw, bytes.fromhex(signature.decode().strip()))
                    self.assertIn(('board=' + target + '\n').encode(), raw)
                    output.unlink()
                    other = 'radar_puffin' if target == 'biscuit' else 'biscuit'
                    result = run(command + ['--target', other], env=os.environ | {'LIBREECHO_TARGET': target})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('target does not match build manifest board', result.stderr)
                    self.assertFalse(output.exists())


class BootctlPartnameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.work.name) / 'bootctl-partnames'
        result = run(['cc', '-Wall', '-Wextra', '-Werror', '-o', str(cls.binary),
                      str(TOOLS / 'ota/test_bootctl_partnames.c')])
        if result.returncode: raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def classify(self, names, wrappers=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = {8: ('misc', 1025), 9: ('persist', 32768),
                       10: (names[0], 32768), 11: (names[1], 32768), 16: ('userdata', 2153472)}
            if wrappers: records.update({17: ('boot_a', 225280), 18: ('boot_b', 225280)})
            for index, (name, size) in records.items():
                directory = root / f'mmcblk0p{index}'; directory.mkdir()
                (directory / 'size').write_text(str(size) + '\n')
                (directory / 'uevent').write_text('DEVTYPE=partition\nPARTNAME=' + name + '\n')
            return run([str(self.binary), str(root)])

    def test_exact_plain_and_wrapper_layouts(self):
        self.assertIn('boot_layout=pinned', self.classify(('boot_a', 'boot_b')).stdout)
        self.assertIn('boot_layout=amonet', self.classify(('boot_a_x', 'boot_b_x'), True).stdout)

    def test_missing_wrappers_never_fall_through_to_plain_prefix(self):
        self.assertNotEqual(self.classify(('boot_a_x', 'boot_b_x')).returncode, 0)

    def test_mixed_layout_never_classifies_as_plain(self):
        for names in [('boot_a_x', 'boot_b'), ('boot_a', 'boot_b_x'),
                      ('boot_a_bad', 'boot_b'), ('boot_a', 'boot_b_extra'),
                      ('boot_a\nNOTPARTNAME=boot_b', 'boot_b')]:
            with self.subTest(names=names):
                if names[-1] == 'boot_b' and names[0] == 'boot_a\nNOTPARTNAME=boot_b':
                    # Exact boot_a is valid even with an unrelated extra field.
                    self.assertEqual(self.classify(names).returncode, 0)
                else:
                    self.assertNotEqual(self.classify(names).returncode, 0)


class RadarTreeDiffTests(unittest.TestCase):
    """Actual staging functions, immutable release base, host-only binary inputs.

    Not an ARM/full image build: the ELF checks are stubbed for host binaries,
    while file writes, BusyBox applets, manifests and the complete tree are real.
    """
    BASE = '2715c573c15f982555b6b48264d75d468cf3af08'
    ALLOWED = {'etc/libreecho/target', 'usr/local/sbin/libreecho-update',
               'usr/local/sbin/libreecho-feature-transaction', 'init',
               'usr/local/sbin/libreecho-bootctl'}

    @staticmethod
    def tree(stage):
        import stat
        result = {}
        for path in sorted(stage.rglob('*')):
            name = path.relative_to(stage).as_posix()
            if path.is_symlink():
                result[name] = {'kind': 'symlink', 'target': os.readlink(path)}
            elif path.is_file():
                result[name] = {'kind': 'file', 'mode': stat.S_IMODE(path.stat().st_mode),
                                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            elif path.is_dir():
                result[name] = {'kind': 'directory', 'mode': stat.S_IMODE(path.stat().st_mode)}
        return result

    def stage(self, module, tools, root, bootctl, *, target=None):
        stage = root / 'stage'; stage.mkdir(parents=True)
        emulator = root / 'host-busybox-app-list'
        emulator.write_text('#!/bin/sh\n/bin/busybox --list | while read -r name; do [ \"$name\" = busybox ] || printf \"%s\\n\" \"$name\"; done\n'); emulator.chmod(0o755)
        loader = root / 'loader'; loader.write_bytes(b'public host loader fixture')
        verify = root / 'verifier'; verify.write_bytes(b'public host verifier fixture')
        public = root / 'key'; public.write_text('ab' * 32 + '\n')
        metadata = {}
        module.add_overlay(stage, tools / 'initramfs', Path('/bin/busybox'), loader,
                           module.sha256(Path('/bin/busybox').read_bytes()),
                           module.sha256(loader.read_bytes()), str(emulator), metadata)
        with mock.patch.object(module, 'require_elf_contract', return_value={'host_fixture': True}):
            args = () if target is None else (target,)
            module.add_ota_tools(stage, bootctl, verify, public, 'ota', 'production', 'exclude', 'dev', metadata, *args)
        return stage, metadata

    def test_complete_radar_staged_tree_excludes_only_amended_paths(self):
        import importlib.util
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'base.tar'
            result = run(['git', 'archive', '--format=tar', '-o', str(archive), self.BASE, 'tools/mt8163-arm32'], cwd=TOOLS.parent.parent)
            self.assertEqual(result.returncode, 0, result.stderr)
            baseline = root / 'base'; baseline.mkdir()
            with tarfile.open(archive) as tar:
                tar.extractall(baseline, filter='data')
            base_tools = baseline / 'tools/mt8163-arm32'
            spec = importlib.util.spec_from_file_location('radar_release_base', base_tools / 'build_recovery_image.py')
            assert spec is not None and spec.loader is not None
            old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
            binaries = []
            for name, tools in [('old', base_tools), ('new', TOOLS)]:
                binary = root / (name + '-bootctl')
                result = run(['cc', '-Wall', '-Wextra', '-Werror', '-o', str(binary), str(tools / 'ota/libreecho_bootctl.c')])
                self.assertEqual(result.returncode, 0, result.stderr)
                binaries.append(binary)
            # Actual before/after recovery builds from the same signed Radar
            # public fixture, including every feature file and ZIP member.
            spec_bundle = importlib.util.spec_from_file_location('radar_bundle_base', base_tools / 'recovery-install/build_install_bundle.py')
            assert spec_bundle is not None and spec_bundle.loader is not None
            old_bundle = importlib.util.module_from_spec(spec_bundle); spec_bundle.loader.exec_module(old_bundle)
            assets, _ = TargetBundleSurfaceTests().assets(root, 'radar_puffin')
            prior_bundle, next_bundle = root / 'before-bundle', root / 'after-bundle'
            old_bundle.assemble(assets, prior_bundle, base_tools / 'recovery-install/src', '', 2153472)
            bundle.assemble(assets, next_bundle, TOOLS / 'recovery-install/src', '', 2153472)
            self.assertEqual((prior_bundle / 'bundle.manifest').read_text(),
                             '\n'.join(line for line in (next_bundle / 'bundle.manifest').read_text().splitlines()
                                       if not line.startswith(('target=', 'fastboot_products='))) + '\n')
            import zipfile
            with zipfile.ZipFile(prior_bundle / 'libreecho-install.zip') as prior, zipfile.ZipFile(next_bundle / 'libreecho-install.zip') as next_zip:
                self.assertEqual(prior.namelist(), next_zip.namelist())
                for name in prior.namelist():
                    self.assertEqual(prior.getinfo(name).external_attr, next_zip.getinfo(name).external_attr)
                    if name != 'META-INF/com/google/android/update-binary':
                        self.assertEqual(prior.read(name), next_zip.read(name), name)
            for path in prior_bundle.iterdir():
                if path.name not in ('libreecho-install.zip', 'bundle.manifest'):
                    self.assertEqual(path.read_bytes(), (next_bundle / path.name).read_bytes(), path.name)
            before, before_metadata = self.stage(old, base_tools, root / 'old', binaries[0])
            after, after_metadata = self.stage(image, TOOLS, root / 'new', binaries[1], target='radar_puffin')
            self.assertEqual(after_metadata['ota']['board'], 'radar_puffin')
            self.assertEqual((after / 'etc/libreecho/first-install-confirm').read_bytes(),
                             b'schema=1\nmode=first-install\nboard=radar_puffin\n')
            left, right = self.tree(before), self.tree(after)
            changes = {name for name in left.keys() | right.keys() if left.get(name) != right.get(name)}
            self.assertEqual(changes - self.ALLOWED, set(), f'unenumerated Radar paths: {sorted(changes - self.ALLOWED)}')
            self.assertEqual((before / 'default.prop').read_bytes(), (after / 'default.prop').read_bytes())
            # Every unchanged build function and overlay input is also pinned to
            # the base, covering unexercised kernel/envelope/audio additions.
            import ast
            def functions(path):
                raw = path.read_text()
                return {n.name: ast.get_source_segment(raw, n) for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef)}
            old_functions, new_functions = functions(base_tools / 'build_recovery_image.py'), functions(TOOLS / 'build_recovery_image.py')
            for name in old_functions.keys() & new_functions.keys() - {'main', 'add_overlay', 'add_ota_tools'}:
                self.assertEqual(old_functions[name], new_functions[name], name)
            summary = {'base': self.BASE, 'evidence_class': 'unit_staging_not_ARM_image',
                       'allowed_exclusions': sorted(self.ALLOWED), 'changed_paths': sorted(changes),
                       'unchanged_entries': sum(left.get(k) == right.get(k) for k in left.keys() | right.keys()),
                       'before': left, 'after': right, 'unexpected_changes': [],
                       'kernel_dtb_envelope_packaging_functions_identical': True,
                       'radar_bundle_features_identical': True,
                       'radar_bundle_manifest_only_adds_target_and_fastboot_products': True,
                       'radar_zip_only_changed_member': 'META-INF/com/google/android/update-binary'}
            evidence = os.environ.get('LIBREECHO_PARITY_EVIDENCE')
            if evidence: Path(evidence).write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')

    def test_biscuit_staging_binds_first_install_and_ota_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootctl = root / 'bootctl'; bootctl.write_bytes(b'host bootctl fixture')
            stage, metadata = self.stage(image, TOOLS, root / 'biscuit', bootctl, target='biscuit')
            self.assertEqual(metadata['ota']['board'], 'biscuit')
            self.assertEqual(metadata['board'], 'biscuit')
            self.assertIn(b'board=biscuit\n', (stage / 'etc/libreecho/first-install-confirm').read_bytes())
            self.assertIn(b'ro.product.device=biscuit\n', (stage / 'default.prop').read_bytes())


class RecoveryPreflightTests(unittest.TestCase):
    def harness(self, root, *, product='', override=False, missing=None, action='preserve', signed_board='radar_puffin', target='radar_puffin', qualified=False, cmdline_value='', props=None, all_features=False):
        assets = root / 'bundle'; assets.mkdir()
        boot = assets / 'boot.img'; boot.write_bytes(b'ANDROID!' + bytes(16777216 - 8))
        payload = assets / 'tts.payload.squashfs'; payload.write_bytes(b'payload')
        fm = assets / 'tts.manifest.json'; fm.write_bytes(b'{"feature":"tts"}')
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        lines = ['board=' + signed_board, 'feature_ids=tts', 'feature_tts_action=' + action]
        if action == 'preserve':
            lines += ['feature_tts_base_payload_sha256=' + digest(payload), 'feature_tts_base_manifest_sha256=' + digest(fm)]
        else:
            lines += ['feature_tts_asset=tts.payload.squashfs', 'feature_tts_sha256=' + digest(payload),
                      'feature_tts_size=' + str(payload.stat().st_size),
                      'feature_tts_manifest_asset=tts.manifest.json', 'feature_tts_manifest_sha256=' + digest(fm),
                      'feature_tts_manifest_size=' + str(fm.stat().st_size)]
        if all_features:
            lines[1] = 'feature_ids=airplay2,tts,wakeword,stt,assistant'
            for fid in ('airplay2', 'wakeword', 'stt', 'assistant'):
                fp = assets / (fid + '.payload.squashfs'); fp.write_text('payload-' + fid)
                fm_extra = assets / (fid + '.manifest.json'); fm_extra.write_text('{"feature":"' + fid + '"}')
                lines.append('feature_' + fid + '_action=' + action)
                if action == 'preserve':
                    lines += ['feature_' + fid + '_base_payload_sha256=' + digest(fp),
                              'feature_' + fid + '_base_manifest_sha256=' + digest(fm_extra)]
                else:
                    lines += ['feature_' + fid + '_asset=' + fp.name, 'feature_' + fid + '_sha256=' + digest(fp),
                              'feature_' + fid + '_size=' + str(fp.stat().st_size),
                              'feature_' + fid + '_manifest_asset=' + fm_extra.name,
                              'feature_' + fid + '_manifest_sha256=' + digest(fm_extra),
                              'feature_' + fid + '_manifest_size=' + str(fm_extra.stat().st_size)]
            if missing == 'late-payload': fp.unlink()
            if missing == 'late-manifest': fm_extra.unlink()
            if missing == 'late-corrupt-manifest': fm_extra.write_text('corrupt')
        ota = assets / 'manifest'; ota.write_text('\n'.join(lines) + '\n')
        sig = assets / 'manifest.sig'; sig.write_text('signature')
        slug = 'biscuit' if target == 'biscuit' else 'radar-puffin'
        manifest_name = f'libreecho-{slug}-bundle.manifest' if qualified else 'bundle.manifest'
        zip_name = f'libreecho-{slug}-install.zip' if qualified else 'install.zip'
        (assets / manifest_name).write_text('target=' + target + '\ndevice=' + target + '\nfastboot_products=' + ('BISCUIT' if target == 'biscuit' else 'RADAR') + '\nboot_image=boot.img\n' +
            ''.join('payload=' + p.name + ':' + digest(p) + '\n' for p in (boot, ota, sig)))
        if missing == 'payload': payload.unlink()
        if missing == 'manifest': fm.unlink()
        if missing == 'corrupt-manifest': fm.write_text('changed')
        if missing == 'corrupt-payload': payload.write_text('changed')
        slot_a = root / 'slot-a'; slot_b = root / 'slot-b'
        slot_a.write_text('previous-a'); slot_b.write_text('previous-b')
        cmdline = root / 'cmdline'; cmdline.write_text(cmdline_value)
        flag = root / 'override'
        if override: flag.touch()
        source = INSTALLER.read_text().rsplit('\nmain "$@"', 1)[0]
        source = source.replace('/cache/libreecho-allow-cross-target', str(flag)).replace('/proc/cmdline', str(cmdline)).replace('/etc/libreecho/target', str(root / 'target'))
        source = source.replace('/cache/libreecho-install-receipt', str(root / 'receipt')).replace('/cache/libreecho-install.log', str(root / 'log')).replace('/cache/libreecho-install-dry-run', str(root / 'dry'))
        source = source.replace('/data/libreecho', str(root / 'data/libreecho')).replace('/tmp/dd.out', str(root / 'dd.out'))
        script = root / 'install.sh'
        script.write_text(source + '\n' +
            'ui_print() { printf "%s\\n" "$*" >> "$LOG"; }\n'
            'sha256_of() { sha256sum "$1" | cut -d" " -f1; }\n'
            'check_device() { :; }\nfingerprint_ok() { return 0; }\n'
            'format_userdata() { echo formatted >> "$LOG"; }\nensure_userdata_mounted() { :; }\ntidy_userdata_root() { :; }\n'
            'partition_sectors() { echo 2153472; }\n'
            f'partition_node() {{ case "$1" in boot_a) echo {shlex.quote(str(slot_a))};; boot_b) echo {shlex.quote(str(slot_b))};; userdata) echo fixture;; esac; }}\n' +
            'getprop() { case "$1" in ' + ''.join(shlex.quote(k) + ') printf "%s\\n" ' + shlex.quote(v) + ';;' for k, v in (props or {'ro.boot.product': product}).items()) + ' esac; }\n' +
            f'main 3 1 {shlex.quote(str(assets / zip_name))}\n')
        result = run(['sh', str(script)])
        return result, slot_a.read_bytes(), slot_b.read_bytes(), (root / 'receipt').read_text(), (root / 'log').read_text()

    def test_all_five_features_preflight_before_either_slot_or_format(self):
        for action in ('preserve', 'replace'):
            for missing in ('late-payload', 'late-manifest', 'late-corrupt-manifest', None):
                with self.subTest(action=action, missing=missing), tempfile.TemporaryDirectory() as tmp:
                    result, a, b, receipt, log = self.harness(Path(tmp), action=action, missing=missing, all_features=True)
                    self.assertEqual(result.returncode == 0, missing is None, result.stderr + log)
                    if missing is not None:
                        self.assertEqual(a, b'previous-a')
                        self.assertEqual(b, b'previous-b')
                        self.assertNotIn('formatted', log)
                    else:
                        self.assertIn('feature_preflight=verified', receipt)
                        for fid in ('airplay2', 'tts', 'wakeword', 'stt', 'assistant'):
                            self.assertLess(log.index(fid + ': signed payload and manifest preflight verified'), log.index('formatted'))

    def test_qualified_recovery_zip_uses_own_manifest(self):
        for target, product in [('radar_puffin', 'RADAR'), ('biscuit', 'BISCUIT')]:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                result, a, b, receipt, log = self.harness(Path(tmp), target=target, signed_board=target, product=product, qualified=True)
                self.assertEqual(result.returncode, 0, result.stderr + log)
                self.assertIn('target=' + target, receipt)
                self.assertIn('target_check=match', receipt)
                self.assertEqual(a, b)
                self.assertTrue(a.startswith(b'ANDROID!'))

    def test_cmdline_and_all_getprop_sources_are_logged_and_checked(self):
        for props, cmdline, accepted in [({'ro.product.device': 'RADAR'}, '', True),
                ({'ro.build.product': 'BISCUIT'}, '', False),
                ({'ro.boot.product': 'RADAR', 'ro.build.product': 'BISCUIT'}, '', False),
                ({}, 'androidboot.product=RADAR androidboot.hardware=mt8163', True),
                ({}, 'androidboot.device=BISCUIT', False)]:
            with self.subTest(props=props, cmdline=cmdline), tempfile.TemporaryDirectory() as tmp:
                result, a, b, receipt, log = self.harness(Path(tmp), props=props, cmdline_value=cmdline)
                self.assertEqual(result.returncode == 0, accepted, result.stderr + log)
                for key, value in props.items():
                    self.assertIn('target-product-source=' + key + ' value=' + value, log)
                if not accepted:
                    self.assertEqual(a, b'previous-a')
                    self.assertEqual(b, b'previous-b')

    def test_missing_or_corrupt_feature_fails_before_any_slot_or_format_write(self):
        for missing in ('payload', 'manifest', 'corrupt-manifest', 'corrupt-payload'):
            for action in ('preserve', 'replace'):
                with self.subTest(missing=missing, action=action), tempfile.TemporaryDirectory() as tmp:
                    result, a, b, receipt, log = self.harness(Path(tmp), missing=missing, action=action)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(a, b'previous-a', log)
                    self.assertEqual(b, b'previous-b', log)
                    self.assertNotIn('formatted', log)
                    self.assertIn('result=failed', receipt)

    def test_recovery_match_override_unknown_and_mismatch(self):
        for product, override, check, accepted in [('RADAR', False, 'match', True), ('BISCUIT', False, '', False),
                                                  ('BISCUIT', True, 'override', True), ('', False, 'unknown', True)]:
            with self.subTest(product=product, override=override), tempfile.TemporaryDirectory() as tmp:
                result, a, b, receipt, log = self.harness(Path(tmp), product=product, override=override)
                self.assertEqual(result.returncode == 0, accepted, result.stderr + log)
                self.assertIn('target=radar_puffin', receipt)
                if accepted:
                    self.assertIn('target_check=' + check, receipt)
                    self.assertEqual(a, b)
                    self.assertTrue(a.startswith(b'ANDROID!'))
                else:
                    self.assertEqual(a, b'previous-a')
                    self.assertEqual(b, b'previous-b')
                if override: self.assertIn('OVERRIDE', log)
                if not product: self.assertIn('target-check=unknown', log)

    def test_recovery_cross_target_manifest_fails_before_writes_even_with_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, a, b, receipt, log = self.harness(Path(tmp), product='BISCUIT', override=True, signed_board='biscuit')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(a, b'previous-a')
            self.assertEqual(b, b'previous-b')


if __name__ == '__main__':
    unittest.main(verbosity=2)
