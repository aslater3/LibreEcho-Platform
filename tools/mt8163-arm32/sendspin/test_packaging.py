#!/usr/bin/env python3
"""airplay2 packaging of the Sendspin player (airplay/package_feature.sh).

Runs the real packager against a minimal AirPlay build output and a stub
payload step that captures the staged root, so the exact tree that would be
hashed into the feature manifest can be inspected. No squashfs, no device.
"""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PACKAGER = HERE.parent / 'airplay' / 'package_feature.sh'
COMPONENTS = ['sdk', 'arduinojson', 'ixwebsocket', 'micro-flac', 'micro-ogg-demuxer', 'noise-c']
STUB = '''#!/bin/sh
# package_feature_payload.sh <feature> <root> <payload> <manifest>: keep the root.
cp -a "$2" "$3.root"
: > "$3"; : > "$4"
'''


class SendspinPackaging(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='le-ss-pkg-'))
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.tmp)])
        airplay = self.tmp / 'airplay'
        lic = airplay / 'runtime/usr/local/share/licenses/libreecho-airplay'
        for component in ['nqptp', 'shairport-sync', 'ffmpeg', 'tinyalsa']:
            (lic / 'source' / component).mkdir(parents=True)
            (lic / 'source' / component / 'COPYING').write_text(component)
        (lic / 'debian/libc6').mkdir(parents=True)
        (lic / 'debian/libc6/copyright').write_text('libc6')
        (lic / 'COMPONENTS.tsv').write_text('x\n')
        for binary in ['nqptp', 'shairport-sync', 'libreecho-airplay-audio',
                       'libreecho-audio-engine', 'avahi-daemon', 'dbus-daemon']:
            (airplay / binary).write_text(binary)
        ui = self.tmp / 'ui/etc/libreecho'
        ui.mkdir(parents=True)
        (ui / 'airplay2.conf').write_text('conf')
        pipeline = self.tmp / 'pipeline'
        pipeline.mkdir()
        (pipeline / 'package_feature_payload.sh').write_text(STUB)
        (pipeline / 'package_feature_payload.sh').chmod(0o755)
        self.airplay, self.ui, self.pipeline = airplay, self.tmp / 'ui', pipeline

    def player(self, missing=None, link=False):
        out = self.tmp / 'sendspin'
        lic = out / 'licenses/libreecho-sendspin'
        lic.mkdir(parents=True)
        if link:
            (self.tmp / 'real').write_text('elf')
            (out / 'libreecho-sendspind').symlink_to(self.tmp / 'real')
        else:
            (out / 'libreecho-sendspind').write_text('elf')
        for name in ['COMPONENTS.tsv', 'glibc-copyright', 'gcc-runtime-copyright']:
            if name != missing:
                (lic / name).write_text(name)
        for component in COMPONENTS:
            if component != missing:
                (lic / component).mkdir()
                (lic / component / 'LICENSE').write_text(component)
        return out

    def package(self, sendspin=None):
        env = dict(os.environ, LIBREECHO_PIPELINE_ROOT=str(self.pipeline))
        env.pop('LIBREECHO_AIRPLAY_SENDSPIN', None)
        if sendspin is not None:
            env['LIBREECHO_AIRPLAY_SENDSPIN'] = str(sendspin)
        payload = self.tmp / 'airplay2.squashfs'
        proc = subprocess.run([str(PACKAGER), str(self.airplay), str(self.ui), str(payload),
                               str(self.tmp / 'airplay2.manifest.json')],
                              env=env, capture_output=True, text=True, timeout=60)
        return proc, Path(str(payload) + '.root')

    def test_without_player_is_unchanged(self):
        proc, root = self.package()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((root / 'usr/local/sbin/libreecho-audio-engine').is_file())
        self.assertFalse((root / 'usr/local/sbin/libreecho-sendspind').exists())
        self.assertFalse((root / 'usr/local/share/licenses/libreecho-sendspin').exists())

    def test_player_and_license_closure_installed(self):
        proc, root = self.package(self.player())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        binary = root / 'usr/local/sbin/libreecho-sendspind'
        self.assertTrue(binary.is_file() and not binary.is_symlink())
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)
        lic = root / 'usr/local/share/licenses/libreecho-sendspin'
        for component in COMPONENTS:
            self.assertTrue((lic / component / 'LICENSE').is_file(), component)
        for name in ['COMPONENTS.tsv', 'glibc-copyright', 'gcc-runtime-copyright']:
            self.assertTrue((lic / name).is_file(), name)
        # The AirPlay notices are untouched.
        self.assertTrue((root / 'usr/local/share/licenses/libreecho-airplay/COMPONENTS.tsv').is_file())

    def test_incomplete_player_output_refused(self):
        for missing in ['COMPONENTS.tsv', 'glibc-copyright', 'gcc-runtime-copyright', 'noise-c', 'sdk']:
            with self.subTest(missing=missing):
                self.setUp()
                proc, root = self.package(self.player(missing=missing))
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(root.exists(), 'payload staged despite a missing license')
        self.setUp()
        proc, _ = self.package(self.tmp / 'nowhere')
        self.assertNotEqual(proc.returncode, 0)
        self.setUp()
        proc, _ = self.package(self.player(link=True))
        self.assertNotEqual(proc.returncode, 0, 'symlinked player accepted')


if __name__ == '__main__':
    unittest.main()
