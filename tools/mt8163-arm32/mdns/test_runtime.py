"""Validate the shared mDNS runtime contract and its fail-closed coverage.

The negative tests build a synthetic runtime closure and remove one input per
contract category (loader, library, executable, config, license) to prove the
builder/verifier pair refuses an incomplete runtime.  If a real locally built
runtime is supplied through LIBREECHO_MDNS_RUNTIME and
LIBREECHO_MDNS_RUNTIME_MANIFEST_SHA, the same verifier is exercised against it;
no private path is recorded in this repository.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import contract as mdns_contract  # noqa: E402

BUILDER = HERE / 'build_runtime.py'
VERIFIER = HERE / 'verify_runtime.py'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    return sha256(Path(path).read_bytes())


def build_fixture(directory, omit=()):
    """Materialise a complete synthetic runtime closure, minus ``omit``."""
    contract = mdns_contract.load()
    root = Path(directory) / 'root'
    modes = {}
    for category, mode in (('executables', 0o755), ('libraries', 0o755),
                           ('config', 0o644), ('accounts', 0o644),
                           ('licenses', 0o644)):
        for name in contract[category]:
            modes[name] = mode
    modes[contract['loader']] = 0o755
    files = {}
    for name, mode in modes.items():
        if name in omit:
            continue
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'synthetic-runtime-fixture:' + name.encode())
        target.chmod(mode)
        files[name] = {'sha256': digest(target), 'size': target.stat().st_size, 'mode': mode}
    packages = {'schema': 'libreecho-mdns-packages/v1', 'packages': [
        {'package': name, 'file': name + '.deb', 'sha256': '0' * 64,
         'architecture': 'armhf', 'version': '0'}
        for name in contract['packages']
        if name not in omit
    ]}
    packages_path = Path(directory) / 'packages.json'
    packages_path.write_text(json.dumps(packages, sort_keys=True, indent=2) + '\n')
    manifest = {
        'schema': 'libreecho-mdns-runtime/v1',
        'files': files,
        'packages_sha256': digest(packages_path),
        'source_offer_verified': False,
    }
    manifest_path = Path(directory) / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    return directory, digest(manifest_path)


class ContractTests(unittest.TestCase):
    def test_contract_file_is_checked_in_and_complete(self):
        document = mdns_contract.load()
        self.assertEqual(document['schema'], 'libreecho-mdns-runtime-contract/v1')
        for category in mdns_contract.CATEGORIES:
            self.assertTrue(document[category], category)
        self.assertEqual(document['abi']['class'], 'ELF32')
        self.assertEqual(document['abi']['machine'], 'ARM')
        self.assertEqual(document['abi']['interpreter'], '/lib/ld-linux-armhf.so.3')
        self.assertEqual(document['loader'], 'lib/ld-linux-armhf.so.3')
        self.assertIn('avahi-daemon', document['packages'])
        runtime_root = '/' + document['image_runtime_root']
        self.assertEqual(document['runtime_dirs']['state_root'], runtime_root)
        self.assertEqual(document['runtime_dirs']['bus'], runtime_root + '/run/dbus')
        self.assertNotEqual(document['image_marker'], document['init_wrapper'])

    def test_builder_and_verifier_share_the_contract(self):
        builder = load_module('mdns_builder_contract', BUILDER)
        self.assertEqual(set(builder.EXECUTABLES), set(mdns_contract.load()['executables']))
        self.assertEqual(set(builder.REQUIRED_PACKAGES), set(mdns_contract.load()['packages']))

    def test_invalid_contracts_fail_closed(self):
        base = mdns_contract.load()
        for mutate in (
            lambda document: document.update(schema='bogus'),
            lambda document: document.pop('loader'),
            lambda document: document.update(executables=[]),
            lambda document: document.update(libraries=[]),
            lambda document: document.update(config=[]),
            lambda document: document.update(licenses=[]),
            lambda document: document.update(packages=[]),
            lambda document: document.update(loader='/lib/ld-linux-armhf.so.3'),
        ):
            candidate = json.loads(json.dumps(base))
            mutate(candidate)
            with self.assertRaises(ValueError):
                mdns_contract.validate(candidate)

    def test_missing_packages_fail_closed(self):
        self.assertTrue(BUILDER.is_file(), 'independent runtime builder missing')
        module = load_module('mdns_runtime_builder', BUILDER)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                module.validate_lock(
                    Path(directory),
                    {'schema': 'libreecho-mdns-packages/v1', 'packages': []},
                )

    def test_contract_check_rejects_each_missing_category(self):
        module = load_module('mdns_runtime_builder_check', BUILDER)
        contract = mdns_contract.load()
        with tempfile.TemporaryDirectory() as directory:
            _, _ = build_fixture(directory)
            module.contract_check(Path(directory) / 'root', contract)
        for category in ('executables', 'libraries', 'config', 'licenses'):
            with tempfile.TemporaryDirectory() as directory:
                _, _ = build_fixture(directory, omit=(contract[category][0],))
                with self.assertRaises(ValueError):
                    module.contract_check(Path(directory) / 'root', contract)
        with tempfile.TemporaryDirectory() as directory:
            _, _ = build_fixture(directory, omit=(contract['loader'],))
            with self.assertRaises(ValueError):
                module.contract_check(Path(directory) / 'root', contract)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.verifier = load_module('mdns_runtime_verifier', VERIFIER)

    def test_complete_runtime_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, manifest_sha = build_fixture(directory)
            record = self.verifier.verify(Path(runtime), manifest_sha)
            self.assertEqual(record['manifest_sha256'], manifest_sha)
            self.assertEqual(record['contract_schema'], mdns_contract.load()['schema'])

    def test_rejects_group_writable_data_file(self):
        module = self.verifier
        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = build_fixture(directory)
            path = Path(runtime) / 'root' / 'etc/avahi/avahi-daemon.conf'
            path.chmod(0o664)
            with self.assertRaisesRegex(ValueError, 'runtime (file changed|data mode invalid)'):
                module.verify(Path(runtime), digest(Path(runtime) / 'manifest.json'))

    def test_missing_runtime_inputs_fail_closed_per_category(self):
        contract = mdns_contract.load()
        cases = (
            ('loader', 'runtime loader missing', (contract['loader'],)),
            ('library', 'runtime library missing', (contract['libraries'][0],)),
            ('executable', 'runtime executable missing', (contract['executables'][0],)),
            ('config', 'runtime config missing', (contract['config'][0],)),
            ('license', 'runtime license missing', (contract['licenses'][0],)),
        )
        for label, message, omit in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as directory:
                    runtime, manifest_sha = build_fixture(directory, omit=omit)
                    with self.assertRaisesRegex(ValueError, message):
                        self.verifier.verify(Path(runtime), manifest_sha)

    def test_missing_required_package_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, manifest_sha = build_fixture(directory, omit=('libc6',))
            with self.assertRaisesRegex(ValueError, 'runtime package missing'):
                self.verifier.verify(Path(runtime), manifest_sha)

    def test_mutation_and_identity_negatives_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, manifest_sha = build_fixture(directory)
            (Path(runtime) / 'root/unexpected').write_text('unexpected')
            with self.assertRaises(ValueError):
                self.verifier.verify(Path(runtime), manifest_sha)
        with tempfile.TemporaryDirectory() as directory:
            runtime, manifest_sha = build_fixture(directory)
            (Path(runtime) / 'root/usr/sbin/avahi-daemon').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):
                self.verifier.verify(Path(runtime), manifest_sha)
        with tempfile.TemporaryDirectory() as directory:
            runtime, manifest_sha = build_fixture(directory)
            (Path(runtime) / 'manifest.json').write_text('{}')
            with self.assertRaises(ValueError):
                self.verifier.verify(Path(runtime), manifest_sha)
        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = build_fixture(directory)
            with self.assertRaises(ValueError):
                self.verifier.verify(Path(runtime), 'not-a-manifest-hash')

    def test_mutated_data_modes_are_rejected(self):
        for name, mode in (('root/etc/dbus-1/system.conf', 0o666),
                           ('root/etc/avahi/avahi-daemon.conf', 0o755),
                           ('root/usr/share/licenses/libreecho-mdns/libc6/copyright', 0o606)):
            with self.subTest(name=name, mode=oct(mode)):
                with tempfile.TemporaryDirectory() as directory:
                    runtime, manifest_sha = build_fixture(directory)
                    target = Path(runtime) / name
                    target.chmod(mode)
                    manifest = json.loads((Path(runtime) / 'manifest.json').read_text())
                    manifest['files'][name[len('root/'):]]['mode'] = mode & 0o777
                    (Path(runtime) / 'manifest.json').write_text(
                        json.dumps(manifest, sort_keys=True, indent=2) + '\n')
                    with self.assertRaises(ValueError):
                        self.verifier.verify(Path(runtime), digest(Path(runtime) / 'manifest.json'))
        with tempfile.TemporaryDirectory() as directory:
            runtime, manifest_sha = build_fixture(directory)
            (Path(runtime) / 'root/usr/sbin/avahi-daemon').chmod(0o644)
            manifest = json.loads((Path(runtime) / 'manifest.json').read_text())
            manifest['files']['usr/sbin/avahi-daemon']['mode'] = 0o644
            (Path(runtime) / 'manifest.json').write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + '\n')
            with self.assertRaises(ValueError):
                self.verifier.verify(
                    Path(runtime), digest(Path(runtime) / 'manifest.json'))

    def test_real_local_runtime_is_accepted_when_supplied(self):
        runtime = os.environ.get('LIBREECHO_MDNS_RUNTIME')
        manifest_sha = os.environ.get('LIBREECHO_MDNS_RUNTIME_MANIFEST_SHA')
        if not runtime or not manifest_sha:
            self.skipTest('set LIBREECHO_MDNS_RUNTIME and '
                          'LIBREECHO_MDNS_RUNTIME_MANIFEST_SHA for the local runtime')
        record = self.verifier.verify(Path(runtime), manifest_sha)
        self.assertEqual(record['manifest_sha256'], manifest_sha)
        self.assertGreater(record['files'], 0)


if __name__ == '__main__':
    unittest.main()
