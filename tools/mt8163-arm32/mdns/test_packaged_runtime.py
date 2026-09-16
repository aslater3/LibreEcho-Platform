#!/usr/bin/env python3
"""Run the production mDNS init wrapper with the real ARMHF runtime under QEMU.

The privileged disposable container supplies a private proc/network namespace and
dummy multicast link. The wrapper must create its own D-Bus identity and runtime
state; the fixture does not prepare those prerequisites for it. Requires an
already-present container image, static BusyBox, qemu-arm-static, and Docker.
This is production-startup evidence, not LAN/hardware acceptance.
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
    parser.add_argument('--init-wrapper', type=Path, required=True)
    parser.add_argument('--qemu', type=Path, default=Path('/usr/bin/qemu-arm-static'))
    args = parser.parse_args()
    verify(args.runtime, args.manifest_sha256)
    if not args.init_wrapper.is_file() or args.init_wrapper.is_symlink():
        raise SystemExit('production mDNS init wrapper is missing or unsafe')
    if not args.qemu.is_file() or args.qemu.is_symlink():
        raise SystemExit('qemu-arm-static is missing or unsafe')
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
        runtime_root = root / 'usr/local/lib/libreecho-mdns/root'
        shutil.copytree(args.runtime / 'root', runtime_root)
        (root / 'bin').mkdir(exist_ok=True)
        (runtime_root / 'bin').mkdir(exist_ok=True)
        shutil.copyfile('/bin/busybox', runtime_root / 'bin/busybox')
        (runtime_root / 'bin/busybox').chmod(0o755)
        (root / 'dev').mkdir(exist_ok=True)
        (root / 'proc').mkdir(exist_ok=True)
        (root / 'tmp').mkdir(exist_ok=True)
        (root / 'tmp').chmod(0o1777)
        (root / 'run').mkdir(exist_ok=True)
        (root / 'var/run').mkdir(parents=True, exist_ok=True)
        (root / 'etc/init.d').mkdir(parents=True, exist_ok=True)
        (root / 'etc/libreecho').mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.runtime / 'manifest.json',
                        root / 'etc/libreecho/mdns-runtime.json')
        for source, destination in [('/bin/busybox', 'bin/busybox'),
                                    (str(args.qemu),
                                     'usr/local/lib/libreecho-mdns/root/qemu'),
                                    (str(args.init_wrapper),
                                     'etc/init.d/libreecho-mdnsd.init'),
                                    (str(Path(__file__).with_suffix('.sh')), 'test.sh')]:
            shutil.copyfile(source, root / destination)
            (root / destination).chmod(0o755)
        # Keep the verified ARM executables at their production paths. Wrap only
        # the packaged dynamic loader so the production init must invoke it
        # explicitly; launching dbus-daemon directly must fail just as it does on
        # the musl target where /lib/ld-linux-armhf.so.3 is absent.
        target = runtime_root / 'lib/ld-linux-armhf.so.3'
        arm = target.with_name(target.name + '.arm')
        target.rename(arm)
        target.write_text(
            '#!/bin/busybox sh\n'
            'exec /qemu -L / /lib/ld-linux-armhf.so.3.arm "$@"\n'
        )
        target.chmod(0o755)
        command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
                   '--privileged',
                   '--mount', f'type=bind,source={root},target=/runtime']
        for name in ('null', 'urandom', 'random'):
            (root / 'dev' / name).touch()
            command += ['--mount', f'type=bind,source=/dev/{name},target=/runtime/dev/{name},readonly']
        # The namespace can chown runtime directories; restore only the mutable
        # paths before returning them to the unprivileged temporary-dir owner.
        command += ['--entrypoint', '/runtime/bin/busybox', args.image, 'sh', '-c',
                    '/runtime/bin/busybox mount -t proc proc /runtime/proc; '
                    '/runtime/bin/busybox chroot /runtime /bin/busybox sh /test.sh; rc=$?; '
                    '/runtime/bin/busybox umount /runtime/proc; '
                    f'/runtime/bin/busybox chown -R {os.getuid()}:{os.getgid()} '
                    '/runtime/run /runtime/var /runtime/usr/local/lib/libreecho-mdns/root; '
                    'exit "$rc"']
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
