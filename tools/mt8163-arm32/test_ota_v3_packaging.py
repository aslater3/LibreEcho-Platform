"""Every v3 helper must ship in the verified image, not just in source."""
import unittest
from pathlib import Path
import build_recovery_image as build
import verify_recovery_image as verify
from test_multi_target import RadarTreeDiffTests

TOOLS = Path(__file__).resolve().parent
HELPERS = ('libreecho-target-manifest', 'libreecho-generation', 'libreecho-generation-transaction', 'libreecho-config-migrate')


class PackagingTests(unittest.TestCase):
    def test_all_helpers_are_staged_verified_and_allowlisted(self):
        source = Path(build.__file__).read_text()
        for helper in HELPERS:
            with self.subTest(helper=helper):
                self.assertIn('"' + helper + '":', source)
                self.assertEqual(verify.OVERLAY_FILES[helper], 0o755)
                self.assertEqual(verify.OVERLAY_TARGETS[helper], 'usr/local/sbin/' + helper)
                self.assertIn('usr/local/sbin/' + helper, RadarTreeDiffTests.ALLOWED)




class BootHttpsTransportTests(unittest.TestCase):
    """Real SquashFS -> boot newc -> independent verifier closure tests.

    The tiny ELF is an ABI fixture, not an executable or ARM runtime evidence.
    """
    def setUp(self):
        import tempfile
        import struct
        import hashlib
        import json
        import subprocess
        from unittest.mock import patch
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.ca = b'-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n'
        self.ca_hash = hashlib.sha256(self.ca).hexdigest()
        self.curl = bytearray(52)
        self.curl[:7] = b'\x7fELF\x01\x01\x01'
        struct.pack_into('<H', self.curl, 18, 40)
        struct.pack_into('<I', self.curl, 36, 0x05000400)
        self.mapping = {
            'usr/bin/curl': 'usr/local/libexec/libreecho-curl',
            'etc/ssl/certs/ca-certificates.crt': 'usr/local/share/libreecho/cacert.pem',
        }
        for source in ('usr/local/share/licenses/curl/COPYING',
                       'usr/local/share/licenses/ca-certificates/copyright'):
            self.mapping[source] = source
        for name in ('THIRD_PARTY_NOTICES.txt', 'OpenSSL-copyright',
                     'glibc-copyright', 'gcc-runtime-copyright', 'LGPL-2.1.txt', 'GPL-3.0.txt'):
            source = 'usr/local/share/licenses/libreecho-assistant/' + name
            self.mapping[source] = source
        self.files = {}
        for target, source in self.mapping.items():
            data = bytes(self.curl) if target == 'usr/bin/curl' else (
                self.ca if target.startswith('etc/ssl/') else b'license fixture\n')
            path = self.source / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            mode = '0755' if target == 'usr/bin/curl' else '0644'
            path.chmod(int(mode, 8))
            self.files[source] = dict(sha256=hashlib.sha256(data).hexdigest(), size=len(data), mode=mode)
        self.payload = self.root / 'assistant.squashfs'
        subprocess.run(['mksquashfs', str(self.source), str(self.payload), '-noappend',
                        '-comp', 'lz4', '-processors', '1', '-quiet'], check=True,
                       capture_output=True, timeout=30)
        self.digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        self.document = dict(schema_version=1, feature_id='assistant', format='squashfs-lz4',
                             payload=dict(filename=self.payload.name, sha256=self.digest,
                                          size=self.payload.stat().st_size), files=self.files)
        self.input_manifest = self.root / 'assistant.manifest.json'
        self.input_manifest.write_text(json.dumps(self.document))
        self.manifest = {}
        self.qemu = self.root / 'qemu-arm'
        self.qemu.write_text('#!/bin/sh\nprintf "curl 8.21.0 (arm) libcurl/8.21.0 OpenSSL/3.0.13\\nProtocols: http https\\nFeatures: SSL\\n"\n')
        self.qemu.chmod(0o755)
        # Synthetic ABI fixtures use a capability-probe shim, not ARM runtime
        # evidence. The supplied-artifact test below executes real qemu/curl.
        import os
        patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH']).start()
        # Test trust-store fixture only; production pins remain mandatory.
        self.addCleanup(patch.stopall)
        patch('boot_https_transport.CA_SHA256', self.ca_hash).start()
        patch.object(verify, 'BOOT_HTTPS_CA_SHA256', self.ca_hash).start()

    def package(self):
        build.add_boot_https_transport(self.stage, self.payload, self.input_manifest, self.manifest)
        return verify.parse_newc(build.build_cpio(self.stage, 0))

    def test_boot_client_is_packaged_without_assistant_feature(self):
        entries = self.package()
        verify.validate_boot_https_transport(entries, self.manifest, self.digest)
        self.assertNotIn('assistant', self.manifest)
        self.assertEqual(entries['usr/bin/curl'].data, bytes(self.curl))
        self.assertEqual(entries['etc/ssl/certs/ca-certificates.crt'].data, self.ca)
        self.assertNotIn('usr/local/sbin/libreecho-agentd', entries)

    def test_verifier_rejects_missing_transport_and_untrusted_input(self):
        entries = self.package()
        for digest in (None, '0' * 64):
            with self.subTest(digest=digest), self.assertRaises(SystemExit):
                verify.validate_boot_https_transport(entries, self.manifest, digest)
        with self.assertRaises(SystemExit):
            verify.validate_boot_https_transport(entries, {}, self.digest)
        for name in self.mapping:
            changed = dict(entries)
            del changed[name]
            with self.subTest(name=name), self.assertRaises(SystemExit):
                verify.validate_boot_https_transport(changed, self.manifest, self.digest)

    def test_verifier_rejects_corruption_modes_and_abi_even_with_updated_hash(self):
        import dataclasses
        import hashlib
        entries = self.package()
        for name in self.mapping:
            changed = dict(entries)
            changed[name] = dataclasses.replace(changed[name], data=b'corrupt')
            with self.subTest(name=name), self.assertRaises(SystemExit):
                verify.validate_boot_https_transport(changed, self.manifest, self.digest)
        changed = dict(entries)
        changed['usr/bin/curl'] = dataclasses.replace(changed['usr/bin/curl'], mode=0o100644)
        with self.assertRaises(SystemExit):
            verify.validate_boot_https_transport(changed, self.manifest, self.digest)
        bad = bytes(self.curl[:18]) + b'\x3e\x00' + bytes(self.curl[20:])
        changed['usr/bin/curl'] = dataclasses.replace(entries['usr/bin/curl'], data=bad)
        record = self.manifest['boot_https_transport']['files']['usr/bin/curl']
        record['sha256'] = hashlib.sha256(bad).hexdigest()
        with self.assertRaises(SystemExit):
            verify.validate_boot_https_transport(changed, self.manifest, self.digest)

    def test_real_arm_client_survives_corrupt_build_input(self):
        """Optional supplied-artifact probe: no fake executable or feature mount.

        Runs ARM user emulation, not PID 1, namespaces or a physical device.
        """
        import os
        import subprocess
        import hashlib
        import tempfile
        import json
        from unittest.mock import patch
        payload = os.environ.get('LIBREECHO_BOOT_HTTPS_TEST_PAYLOAD')
        source_manifest = os.environ.get('LIBREECHO_BOOT_HTTPS_TEST_MANIFEST')
        qemu = os.environ.get('LIBREECHO_BOOT_HTTPS_TEST_QEMU')
        if not all((payload, source_manifest, qemu)):
            self.skipTest('supply reviewed assistant payload/manifest and qemu-arm for ARM execution')
        assert payload is not None and source_manifest is not None and qemu is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_payload = root / Path(payload).name
            input_payload.write_bytes(Path(payload).read_bytes())
            stage = root / 'boot-root'
            stage.mkdir()
            manifest = {}
            with patch('boot_https_transport.CA_SHA256',
                       'c0c940a0e30d859783f7f130868d8082e79936ff0b41a0b1098ac7f98909263b'), \
                 patch.object(verify, 'BOOT_HTTPS_CA_SHA256',
                              'c0c940a0e30d859783f7f130868d8082e79936ff0b41a0b1098ac7f98909263b'):
                build.add_boot_https_transport(stage, input_payload, Path(source_manifest), manifest, qemu)
                ramdisk = build.compress_ramdisk(build.build_cpio(stage, 0))
                entries = verify.parse_newc(verify.decompress_ramdisk(ramdisk))
                verify.validate_boot_https_transport(
                    entries, manifest, hashlib.sha256(input_payload.read_bytes()).hexdigest(),
                )
            # Corrupt the assistant input after packaging. The boot client must
            # run from archive-contained bytes and never consult that input.
            input_payload.write_bytes(b'corrupt assistant')
            client = root / 'boot-curl'
            client.write_bytes(entries['usr/bin/curl'].data)
            client.chmod(0o755)
            result = subprocess.run([qemu, str(client), '--version'],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('https', result.stdout)
            self.assertIn('SSL', result.stdout)
            self.assertNotIn('usr/local/sbin/libreecho-agentd', entries)
            # Execute the production selection function with the failing
            # generation transport boundary. Only host paths and the qemu
            # exec shim are substituted; transport selection is unmodified.
            wrapper = root / 'curl-via-qemu'
            wrapper.write_text('#!/bin/sh\nexec ' + qemu + ' ' + str(client) + ' "$@"\n')
            wrapper.chmod(0o755)
            ca = root / 'boot-ca'
            ca.write_bytes(entries['etc/ssl/certs/ca-certificates.crt'].data)
            update = root / 'update'
            update.mkdir()
            (update / 'current').write_text('corrupt-generation\n')
            raw = (TOOLS / 'initramfs/libreecho-update-fetch').read_text()
            function = raw[raw.index('prepare_https_client()'):raw.index('\n}', raw.index('prepare_https_client()')) + 2]
            function = function.replace('CURL=/usr/bin/curl', 'CURL=' + str(wrapper))
            function = function.replace('CA=/etc/ssl/certs/ca-certificates.crt', 'CA=' + str(ca))
            script = root / 'fallback.sh'
            script.write_text('die() { echo "$1" >&2; exit 1; }\n' + function + '\nprepare_https_client\n"$CURL" --version\n')
            env = dict(os.environ, ROOT=str(update), BB='/usr/bin/busybox',
                       CLIENT_ROOT=str(root / 'unavailable-assistant'),
                       GENERATION_TRANSACTION='/bin/false')
            result = subprocess.run(['sh', str(script)], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('https', result.stdout)

    def test_packager_rejects_client_without_https_tls_capability(self):
        self.qemu.write_text('#!/bin/sh\nprintf "curl 8.21.0 libcurl/8.21.0\\nProtocols: http\\nFeatures: Largefile\\n"\n')
        with self.assertRaisesRegex(SystemExit, 'HTTPS.*capabilit'):
            self.package()

    def test_packager_rejects_wrong_member_identity_and_ca_pin(self):
        import json
        self.document['files']['usr/local/libexec/libreecho-curl']['sha256'] = '0' * 64
        self.input_manifest.write_text(json.dumps(self.document))
        with self.assertRaises(SystemExit):
            self.package()
        self.document['files']['usr/local/libexec/libreecho-curl'] = self.files['usr/local/libexec/libreecho-curl'].copy()
        # Restore independently: files and document originally share the record.
        import hashlib
        self.document['files']['usr/local/libexec/libreecho-curl']['sha256'] = hashlib.sha256(bytes(self.curl)).hexdigest()
        self.input_manifest.write_text(json.dumps(self.document))
        from unittest.mock import patch
        with patch('boot_https_transport.CA_SHA256', '0' * 64), self.assertRaises(SystemExit):
            self.package()


if __name__ == '__main__':
    unittest.main()
