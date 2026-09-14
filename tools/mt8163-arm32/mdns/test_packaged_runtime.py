#!/usr/bin/env python3
"""Real ARMHF runtime smoke; Docker network none plus an isolated dummy link.

Requires an already-present container image, static BusyBox, qemu-arm-static,
and Docker access. This is local publication evidence, not LAN/hardware acceptance.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from verify_runtime import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--image', required=True, help='pre-existing local Docker image')
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    verify(args.runtime, args.manifest_sha256)
    for mutation in ('binary', 'loader', 'extra', 'manifest'):
        with tempfile.TemporaryDirectory(prefix='mdns-mutation-') as temp:
            candidate = Path(temp) / 'runtime'
            shutil.copytree(args.runtime, candidate)
            if mutation == 'binary':
                (candidate / 'root/usr/sbin/avahi-daemon').write_bytes(b'corrupt')
            elif mutation == 'loader':
                (candidate / 'root/lib/ld-linux-armhf.so.3').unlink()
            elif mutation == 'extra':
                (candidate / 'root/unexpected').write_text('unexpected')
            else:
                (candidate / 'manifest.json').write_text('{}')
            try:
                verify(candidate, args.manifest_sha256)
            except ValueError:
                print('Rejected runtime mutation:', mutation)
            else:
                raise SystemExit('runtime mutation accepted: ' + mutation)
    subprocess.run(['docker', 'image', 'inspect', args.image], check=True,
                   stdout=subprocess.DEVNULL, timeout=10)
    with tempfile.TemporaryDirectory(prefix='mdns-real-') as temp:
        root = Path(temp) / 'root'
        shutil.copytree(args.runtime / 'root', root)
        (root / 'bin').mkdir(exist_ok=True)
        (root / 'dev').mkdir(exist_ok=True)
        for source, destination in [('/bin/busybox', 'bin/busybox'),
                                    ('/usr/bin/qemu-arm-static', 'qemu'),
                                    (str(Path(__file__).with_suffix('.sh')), 'test.sh')]:
            shutil.copyfile(source, root / destination)
            (root / destination).chmod(0o755)
        command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
                   '--cap-add', 'NET_ADMIN', '--security-opt', 'no-new-privileges',
                   '--mount', f'type=bind,source={root},target=/runtime']
        for name in ('null', 'urandom', 'random'):
            (root / 'dev' / name).touch()
            command += ['--mount', f'type=bind,source=/dev/{name},target=/runtime/dev/{name},readonly']
        # The namespace can chown runtime directories; restore only the mutable
        # paths before returning them to the unprivileged temporary-dir owner.
        command += ['--entrypoint', '/runtime/bin/busybox', args.image, 'sh', '-c',
                    '/runtime/bin/busybox chroot /runtime /bin/busybox sh /test.sh; rc=$?; '
                    f'/runtime/bin/busybox chown -R {os.getuid()}:{os.getgid()} /runtime/run /runtime/var; exit "$rc"']
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        print(completed.stdout, end='')
        print(completed.stderr, end='')
        if completed.returncode:
            raise SystemExit(completed.returncode)
        if 'int32 2' not in completed.stdout or ';21000;' not in completed.stdout:
            raise SystemExit('runtime did not confirm server RUNNING and resolve the selected port')
        print('Packaged ARM runtime: private bus, Avahi RUNNING, local Wyoming port 21000: PASS')

if __name__ == '__main__':
    main()
