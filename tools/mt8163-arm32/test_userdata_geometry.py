#!/usr/bin/env python3
"""Test the actual shell/C userdata contracts without touching devices.

Userdata is accepted by partition identity, a minimum size and (for any
non-legacy size) the LIBREECHO_DATA ext4 label -- LibreEcho #257 / Platform #249.
"""
import hashlib
import re
import shutil
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ('initramfs/libreecho-init', 'initramfs/libreecho-update', 'stage_feature_root.sh')
VALUES = ('2137088', '2153472', '2137087', '2137089', '2153471', '2153473', '',
          '0', '-2153472', '+2153472', '2153472junk', ' 2153472', '2153472 ',
          '02153472', '999999999999999999999999999999999999',
          # Stock Radar userdata kept by the pinned Kaeru chain (LibreEcho #257).
          '2588639', '2604000', '33554432', '33554433', '99999999', '100000000')
MIN_SECTORS = 2137088
MAX_SECTORS = 33554432
LEGACY = ('2137088', '2153472')


def size_wanted(value):
    """The floor rule: a canonical decimal within [MIN, MAX] sectors."""
    return (value.isdigit() and value.isascii() and not value.startswith('0')
            and len(value) <= 8 and MIN_SECTORS <= int(value) <= MAX_SECTORS)


def shell_functions(source):
    names = ('userdata_size_supported', 'userdata_size_legacy', 'userdata_label_ok', 'userdata_identity')
    parts = [re.search(r'\nUSERDATA_MIN_SECTORS=.*?\nUSERDATA_LABEL=[A-Z_]+\n', source, re.S)[0]]
    for name in names:
        match = re.search(r'\n' + name + r'\(\)\n\{\n.*?\n\}\n', source, re.S)
        if match is None:
            raise AssertionError('missing ' + name)
        parts.append(match[0])
    return ''.join(parts)


def shells():
    found = [['/bin/sh']]
    for candidate in ('/bin/busybox', '/usr/bin/busybox'):
        if Path(candidate).is_file():
            found.append([candidate, 'sh'])
            break
    return found


class UserdataShellGeometryTests(unittest.TestCase):
    def test_all_shell_guards_accept_floor_sizes(self):
        for relative in SCRIPTS:
            functions = shell_functions((ROOT / relative).read_text())
            for shell in shells():
                for value in VALUES:
                    result = subprocess.run(shell + ['-c', 'BB=; ' + functions + '\nuserdata_size_supported "$1"', 'test', value], capture_output=True)
                    self.assertEqual(result.returncode == 0, size_wanted(value), (relative, shell, value))
                    result = subprocess.run(shell + ['-c', 'BB=; ' + functions + '\nuserdata_size_legacy "$1"', 'test', value], capture_output=True)
                    self.assertEqual(result.returncode == 0, value in LEGACY, (relative, shell, value))

    def test_shell_helpers_are_identical(self):
        bodies = {relative: shell_functions((ROOT / relative).read_text()) for relative in SCRIPTS}
        self.assertEqual(len(set(bodies.values())), 1, list(bodies))

    def test_helpers_are_wired_into_identity_checks(self):
        expected = {
            'initramfs/libreecho-init': 'userdata_identity "$USERDATA" "$USERDATA_SYS"',
            'initramfs/libreecho-update': 'userdata_identity "$USERDATA_DEVICE" "$USERDATA_SYS" &&',
            'stage_feature_root.sh': 'userdata_identity "$DEVICE" "$SYS" || {',
        }
        for path, expression in expected.items():
            source = (ROOT / path).read_text()
            self.assertIn(expression, source)
            self.assertIn("PARTNAME=userdata", source)
            self.assertNotIn('mke2fs', source.split('userdata_identity()')[1].split('\n}\n')[0])
            self.assertNotRegex(source, r'\[ "\$\(\$BB cat "\$(?:USERDATA_)?SYS/size" 2>/dev/null\)" [!=]=? 2137088 \]')
            self.assertEqual(subprocess.run(['/bin/sh', '-n', str(ROOT / path)], capture_output=True).returncode, 0)
        init = (ROOT / 'initramfs/libreecho-init').read_text()
        for token in ('log userdata-size-unsupported', 'log userdata-not-libreecho', 'log userdata-identity-rejected'):
            self.assertIn(token, init)

    def test_init_hash_pins_match_modified_source(self):
        expected = hashlib.sha256((ROOT / 'initramfs/libreecho-init').read_bytes()).hexdigest()
        for name, constant in (('build_recovery_image.py', 'RECOVERY_INIT_SHA256'), ('verify_recovery_image.py', 'INIT_SHA256')):
            source = (ROOT / name).read_text()
            match = re.search(r'^' + constant + r' = "([0-9a-f]{64})"$', source, re.M)
            self.assertIsNotNone(match)
            self.assertEqual(match[1], expected)


@unittest.skipUnless(shutil.which('mke2fs') and shutil.which('busybox'), 'needs mke2fs and busybox')
class UserdataFilesystemIdentityTests(unittest.TestCase):
    """Run the shipped userdata_identity() against real ext4 superblocks.

    A regular file stands in for the block device (the -b test is shimmed); the
    sysfs directory is a fixture holding uevent and size exactly as the kernel
    exposes them.  BusyBox provides dd/od/tr/grep/cat as on the device."""

    def run_identity(self, root, size, label=None, fs='ext4', partname='userdata', relative=SCRIPTS[0]):
        image = root / 'image'
        image.unlink(missing_ok=True)
        with open(image, 'wb') as handle:
            handle.truncate(16 * 1024 * 1024)
        if fs:
            argv = ['mke2fs', '-q', '-F', '-t', fs]
            if label is not None:
                argv += ['-L', label]
            subprocess.run(argv + [str(image)], check=True, capture_output=True)
        sysfs = root / 'sys'
        sysfs.mkdir(exist_ok=True)
        (sysfs / 'uevent').write_text('MAJOR=179\nMINOR=16\nDEVNAME=mmcblk0p16\nDEVTYPE=partition\nPARTN=16\nPARTNAME=%s\n' % partname)
        (sysfs / 'size').write_text(size + '\n')
        functions = shell_functions((ROOT / relative).read_text())
        # Test-only shim: a regular file stands in for /dev/mmcblk0p16.
        script = 'BB=%s\n' % shutil.which('busybox') + functions.replace('[ -b "$1" ]', '[ -f "$1" ]') + '\nuserdata_identity "$1" "$2"'
        result = subprocess.run([shutil.which('busybox'), 'sh', '-c', script, 'test', str(image), str(sysfs)], capture_output=True, text=True)
        return result.returncode

    def test_identity_matrix(self):
        cases = (
            # size, label, fs, partname, expected status
            ('2588639', 'LIBREECHO_DATA', 'ext4', 'userdata', 0),   # issue #257 stock Radar
            ('2604000', 'LIBREECHO_DATA', 'ext4', 'userdata', 0),   # stock Biscuit-sized
            ('2137088', None, 'ext4', 'userdata', 0),                # legacy exact size, label not required
            ('2153472', 'userdata', 'ext4', 'userdata', 0),          # legacy recovery-installer label
            ('2588639', None, 'ext4', 'userdata', 3),                # unlabelled ext4
            ('2588639', 'userdata', 'ext4', 'userdata', 3),          # foreign label
            ('2588639', 'LIBREECHO_DATAX', 'ext4', 'userdata', 3),   # label prefix must not match
            ('2588639', 'LIBREECHO_DAT', 'ext4', 'userdata', 3),     # truncated label
            ('2588639', 'LIBREECHO_DATA', None, 'userdata', 3),      # no filesystem at all
            ('2588639', 'LIBREECHO_DATA', 'ext4', 'cache', 1),       # wrong partition
            ('2137087', 'LIBREECHO_DATA', 'ext4', 'userdata', 2),    # below floor
            ('33554433', 'LIBREECHO_DATA', 'ext4', 'userdata', 2),   # above ceiling
            ('02588639', 'LIBREECHO_DATA', 'ext4', 'userdata', 2),   # non-canonical decimal
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in SCRIPTS:
                for size, label, fs, partname, status in cases:
                    with self.subTest(script=relative, size=size, label=label, fs=fs, partname=partname):
                        self.assertEqual(self.run_identity(root, size, label, fs, partname, relative), status)


@unittest.skipUnless(shutil.which('cc'), 'C contract test requires a host C compiler')
class UserdataBootctlGeometryTests(unittest.TestCase):
    # Both supported layouts declare a slot store at the same device node and the
    # same reviewed 32768 sectors. The pinned set drops the Amonet wrapper
    # partitions, so a wrapper size must never be accepted for a pinned store.
    LAYOUTS = {
        'partitions_amonet': {
            'boot_a_x': 32768,
            'boot_b_x': 32768,
            'boot_a': 225280,
            'boot_b': 225280,
            'misc': 1025,
            'persist': 32768,
            'userdata': 2137088,
        },
        'partitions_pinned': {
            'boot_a': 32768,
            'boot_b': 32768,
            'misc': 1025,
            'persist': 32768,
            'userdata': 2137088,
        },
    }

    def compile_layout_harness(self, source, table_name, directory):
        contract = re.search(r'struct partition_contract \{.*?\n\};', source, re.S)[0]
        table = re.search(
            r'static const struct partition_contract ' + table_name + r'\[\] = \{.*?\n\};',
            source, re.S,
        )[0]
        helper = re.search(r'#define USERDATA_MAX_SECTORS.*?static int partition_sectors_match\(.*?\n\}', source, re.S)[0]
        code = '#include <stdio.h>\n#include <string.h>\n' + contract + '\n' + table + '\n' + helper + (
            r'''
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    for (size_t i = 0; i < sizeof(TABLE)/sizeof(TABLE[0]); ++i)
        printf("%s:%lu:%d\n", TABLE[i].name, TABLE[i].sectors,
               partition_sectors_match(&TABLE[i], argv[1]));
    struct partition_contract wrong = {"/dev/mmcblk0p99", "unused", "userdata", 2137088};
    if (partition_sectors_match(&wrong, "2153472")) return 3;
    if (partition_sectors_match(&wrong, "2588639")) return 4;
    struct partition_contract persist = {"/dev/mmcblk0p9", "unused", "persist", 32768};
    if (partition_sectors_match(&persist, "32769")) return 5;
    return 0;
}
'''
        ).replace('TABLE', table_name)
        compiled_path = directory / ('%s.c' % table_name)
        binary = directory / table_name
        compiled_path.write_text(code)
        compiled = subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror', str(compiled_path), '-o', str(binary)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        return binary

    def test_compiled_partition_contract(self):
        source = (ROOT / 'ota/libreecho_bootctl.c').read_text()
        self.assertIn('!partition_sectors_match(contract, text)', source)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for table_name, expected in self.LAYOUTS.items():
                with self.subTest(layout=table_name):
                    binary = self.compile_layout_harness(source, table_name, root)
                    observed = {}
                    for value in VALUES + ('1025', '32768', '225280'):
                        result = subprocess.run([str(binary), value], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        for line in result.stdout.splitlines():
                            name, standard, accepted = line.split(':')
                            observed[name] = standard
                            wanted = value == standard or (name == 'userdata' and size_wanted(value))
                            self.assertEqual(accepted == '1', wanted, (table_name, name, value))
                    self.assertEqual(
                        observed, {name: str(sectors) for name, sectors in expected.items()}, table_name
                    )


if __name__ == '__main__':
    if '--require-compiler' in sys.argv:
        sys.argv.remove('--require-compiler')
        if not shutil.which('cc'):
            raise SystemExit('C contract regression requires a host C compiler')
    unittest.main()
