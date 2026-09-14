#!/usr/bin/env python3
"""Independently verify a shared runtime against its pinned manifest hash.

The checked-in runtime contract fixes the loader, libraries, executables,
configuration, accounts and license records that must be present.  Verification
fails closed when any contract category is missing or changed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import contract as mdns_contract  # noqa: E402  (local contract loader)

EXECUTABLES = {'usr/sbin/avahi-daemon', 'usr/bin/dbus-daemon',
               'usr/bin/dbus-send', 'usr/bin/avahi-browse'}
CONFIG_FILES = {'etc/avahi/avahi-daemon.conf', 'etc/dbus-1/system.conf'}
ACCOUNT_FILES = {'etc/passwd', 'etc/group'}

def _data_mode_ok(mode):
    """Data files must not be executable, world-writable, or world-writable-odd.

    New builds canonicalise to 0644; a previously built runtime may carry the
    group-writable 0664 mode the extraction tooling inherited from the source
    files, which is still non-executable and not world-writable.
    """
    return mode & 0o113 == 0

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _require_contract(actual, contract):
    """Fail closed on any missing loader/library/executable/config/license."""
    loader = contract['loader']
    if loader not in actual:
        raise ValueError('runtime loader missing: ' + loader)
    for name in sorted(mdns_contract.category_paths(contract, 'libraries')):
        if name not in actual:
            raise ValueError('runtime library missing: ' + name)
    for name in sorted(mdns_contract.category_paths(contract, 'executables')):
        if name not in actual:
            raise ValueError('runtime executable missing: ' + name)
    for name in sorted(mdns_contract.category_paths(contract, 'config')):
        if name not in actual:
            raise ValueError('runtime config missing: ' + name)
    for name in sorted(mdns_contract.category_paths(contract, 'licenses')):
        if name not in actual:
            raise ValueError('runtime license missing: ' + name)
    for name in sorted(mdns_contract.category_paths(contract, 'accounts')):
        if name not in actual:
            raise ValueError('runtime account record missing: ' + name)

def _require_contract_packages(directory, contract):
    packages_path = directory / 'packages.json'
    try:
        document = json.loads(packages_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError('runtime package inventory is unreadable') from exc
    if document.get('schema') != 'libreecho-mdns-packages/v1':
        raise ValueError('unsupported runtime package lock')
    names = {record.get('package') for record in document.get('packages', [])
             if isinstance(record, dict)}
    missing = sorted(set(contract['packages']) - names)
    if missing:
        raise ValueError('runtime package missing: ' + missing[0])

def verify(directory, expected_manifest, contract_path=None):
    manifest_path = directory / 'manifest.json'
    if not re.fullmatch('[0-9a-f]{64}', expected_manifest) or digest(manifest_path) != expected_manifest:
        raise ValueError('manifest identity mismatch')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 'libreecho-mdns-runtime/v1':
        raise ValueError('unsupported runtime manifest')
    if digest(directory / 'packages.json') != manifest['packages_sha256']:
        raise ValueError('package inventory mismatch')
    contract = mdns_contract.load(contract_path or mdns_contract.CONTRACT_PATH)
    _require_contract_packages(directory, contract)
    root = directory / 'root'
    files = manifest['files']
    _require_contract(set(files), contract)
    required = EXECUTABLES | CONFIG_FILES | ACCOUNT_FILES | {'lib/ld-linux-armhf.so.3'}
    required |= mdns_contract.required_paths(contract)
    if not required.issubset(files):
        missing = sorted(required - set(files))
        raise ValueError('required runtime files missing: ' + missing[0])
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('runtime contains a symlink')
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        if not path.is_file() or name not in files:
            raise ValueError('unexpected runtime entry: ' + name)
        actual.add(name)
        record = files[name]
        if digest(path) != record['sha256'] or path.stat().st_mode & 0o7777 != record['mode']:
            raise ValueError('runtime file changed: ' + name)
        executable_paths = EXECUTABLES | {mdns_contract.load()['loader']} | \
                mdns_contract.category_paths(contract, 'executables') | \
                mdns_contract.category_paths(contract, 'libraries') | \
                {name for name in files if name.startswith('usr/lib/') or name.startswith('lib/')}
        if name in executable_paths and record['mode'] != 0o755:
            raise ValueError('runtime executable mode invalid: ' + name)
        if name not in executable_paths and record['mode'] != 0o644:
            raise ValueError('runtime data mode invalid: ' + name)
        if name in CONFIG_FILES | mdns_contract.category_paths(contract, 'config') | \
                mdns_contract.category_paths(contract, 'licenses') | \
                mdns_contract.category_paths(contract, 'accounts') and not _data_mode_ok(record['mode']):
            raise ValueError('runtime data mode invalid: ' + name)
        if path.read_bytes()[:4] == b'\x7fELF':
            output = subprocess.run(['readelf', '-h', '-d', '-l', str(path)], check=True,
                                    capture_output=True, text=True, timeout=10).stdout
            if not re.search(r'Machine:\s+ARM\s*\n', output) or 'hard-float ABI' not in output:
                raise ValueError('unexpected runtime ABI')
            needed = re.findall(r'Shared library: \[([^]]+)\]', output)
            for library in needed:
                if Path(library).name != library or not any((root / d / library).is_file() for d in ('lib', 'usr/lib')):
                    raise ValueError('unresolved runtime dependency: ' + library)
            if name in mdns_contract.category_paths(contract, 'executables'):
                if contract['abi']['interpreter'] not in output:
                    raise ValueError('unexpected ELF interpreter: ' + name)
    if actual != set(files):
        raise ValueError('runtime inventory incomplete')
    _require_contract(actual, contract)
    for package in ('avahi-daemon', 'dbus-daemon', 'libc6'):
        if f'usr/share/licenses/libreecho-mdns/{package}/copyright' not in actual:
            raise ValueError('required copyright missing: ' + package)
    return {'files': len(actual), 'manifest_sha256': expected_manifest,
            'contract_schema': contract['schema'],
            'source_offer_verified': manifest.get('source_offer_verified') is True}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--contract', type=Path,
                        help='override the checked-in runtime contract')
    args = parser.parse_args()
    print(json.dumps(verify(args.runtime, args.manifest_sha256, args.contract), sort_keys=True))

if __name__ == '__main__':
    main()
