#!/usr/bin/env python3
"""Build a boot-owned, isolated ARMHF discovery runtime from hash-locked debs.

No AirPlay source, feature payload, host library, or package installation is used.
Binary provenance is not a corresponding-source offer; release wiring must supply
that additional gate before publishing this component.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import contract as mdns_contract  # noqa: E402  (checked-in runtime contract)

DEFAULT_CONTRACT = mdns_contract.load()
EXECUTABLES = tuple(DEFAULT_CONTRACT['executables'])
REQUIRED_PACKAGES = set(DEFAULT_CONTRACT['packages'])

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def validate_lock(archives, document):
    if document.get('schema') != 'libreecho-mdns-packages/v1':
        raise ValueError('unsupported package lock')
    records = document.get('packages', [])
    if not records or not REQUIRED_PACKAGES.issubset({r['package'] for r in records}):
        raise ValueError('required discovery packages absent')
    names = set()
    for record in records:
        name = record['file']
        if Path(name).name != name or not name.endswith('.deb') or name in names:
            raise ValueError('invalid or duplicate archive path')
        names.add(name)
        path = archives / name
        if path.is_symlink() or not path.is_file() or sha(path) != record['sha256']:
            raise ValueError('archive hash mismatch: ' + name)
        if record['architecture'] not in ('armhf', 'all'):
            raise ValueError('incorrect package architecture')
    return records

def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60).stdout

def elf(path):
    header = run('readelf', '-h', str(path))
    if not re.search(r'Class:\s+ELF32', header) or not re.search(r'Machine:\s+ARM\s*\n', header):
        raise ValueError('not an ARM32 ELF: ' + str(path))
    if 'hard-float ABI' not in header:
        raise ValueError('not ARM hard-float ABI: ' + str(path))
    needed = re.findall(r'Shared library: \[([^]]+)\]', run('readelf', '-d', str(path)))
    for name in needed:
        if Path(name).name != name:
            raise ValueError('unsafe SONAME')
    return needed

def contract_check(root, contract):
    """Fail closed unless every contract category is present with a sane mode."""
    checks = (
        ('executable', contract['executables'], 0o755),
        ('library', contract['libraries'], 0o755),
        ('config', contract['config'], 0o644),
        ('license', contract['licenses'], 0o644),
        ('account record', contract['accounts'], 0o644),
    )
    for label, names, mode in checks:
        for name in names:
            target = root / name
            if not target.is_file() or target.is_symlink():
                raise ValueError('runtime contract missing ' + label + ': ' + name)
            actual = target.stat().st_mode & 0o777
            if actual != mode:
                raise ValueError('runtime contract mode mismatch for ' + name)
    loader = root / contract['loader']
    if not loader.is_file() or loader.is_symlink():
        raise ValueError('runtime contract missing loader: ' + contract['loader'])
    return contract

def build(archives, lock, output):
    document = json.loads(lock.read_text())
    records = validate_lock(archives, document)
    if output.exists() or output.is_symlink():
        raise ValueError('refusing to overwrite runtime output')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mdns-package-') as temp:
        sysroot = Path(temp) / 'sysroot'
        sysroot.mkdir()
        for record in records:
            run('dpkg-deb', '-x', str(archives / record['file']), str(sysroot))
        staged = Path(temp) / 'output'
        root = staged / 'root'
        copied = {}
        def copy(source, destination):
            resolved = source.resolve(strict=True)
            if not resolved.is_relative_to(sysroot.resolve()):
                raise ValueError('package symlink escapes sysroot')
            target = root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved, target)
            target.chmod(0o755 if destination in EXECUTABLES or destination.startswith('lib/') or destination.startswith('usr/lib/') else 0o644)
            copied[destination] = resolved
            return target
        pending = []
        for name in EXECUTABLES:
            target = copy(sysroot / name, name)
            program = run('readelf', '-l', str(target))
            if '[Requesting program interpreter: /lib/ld-linux-armhf.so.3]' not in program:
                raise ValueError('unexpected ELF interpreter')
            pending.extend(elf(target))
        libdirs = [sysroot / 'usr/lib/arm-linux-gnueabihf', sysroot / 'lib/arm-linux-gnueabihf']
        pending.append('ld-linux-armhf.so.3')
        seen = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            candidates = [d / name for d in libdirs if (d / name).is_file()]
            if not candidates:
                raise ValueError('missing runtime library: ' + name)
            destination = ('lib/' if name == 'ld-linux-armhf.so.3' else 'usr/lib/') + name
            pending.extend(elf(copy(candidates[0], destination)))
        # The distro's complete package notice inventory preserves transitive
        # records, including symlinked copyright files. No feature dependency.
        for path in sorted((sysroot / 'usr/share/doc').glob('*/copyright')):
            copy(path, 'usr/share/licenses/libreecho-mdns/' + path.parent.name + '/copyright')
        if not list((root / 'usr/share/licenses/libreecho-mdns').glob('*/copyright')):
            raise ValueError('missing distribution license inventory')
        for source, destination in [('avahi-daemon.conf', 'etc/avahi/avahi-daemon.conf'),
                                    ('dbus-system.conf', 'etc/dbus-1/system.conf')]:
            target = root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / source, target)
            target.chmod(0o644)
        for directory in ('etc/avahi/services', 'run/dbus', 'run/avahi-daemon', 'var/lib/dbus'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/false\navahi:x:84:84:Avahi:/:/bin/false\n')
        (root / 'etc/passwd').chmod(0o644)
        (root / 'etc/group').write_text('root:x:0:\navahi:x:84:\n')
        (root / 'etc/group').chmod(0o644)
        contract_check(root, DEFAULT_CONTRACT)
        (staged / 'packages.json').write_text(json.dumps(document, sort_keys=True, indent=2) + '\n')
        inventory = {p.relative_to(root).as_posix(): {'sha256': sha(p), 'size': p.stat().st_size, 'mode': p.stat().st_mode & 0o777}
                     for p in sorted(root.rglob('*')) if p.is_file()}
        manifest = {'schema': 'libreecho-mdns-runtime/v1', 'files': inventory,
                    'packages_sha256': sha(staged / 'packages.json'),
                    'source_offer_verified': False}
        (staged / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        shutil.copytree(staged, output)
    return manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archives', type=Path, required=True)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = build(args.archives, args.lock, args.output)
    print(json.dumps({'schema': manifest['schema'], 'files': len(manifest['files']),
                      'source_offer_verified': False}))

if __name__ == '__main__':
    main()
