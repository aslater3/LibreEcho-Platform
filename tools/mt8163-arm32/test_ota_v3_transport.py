"""v3 discovery and TLS bootstrap cannot depend on a legacy feature tree."""
import subprocess
import test_ota_v3_manifest as grammar


class TransportTests(grammar.SignedFixture):
    def test_v3_discovery_uses_separate_pointer(self):
        source = (grammar.TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        self.assertIn('/release-pointer-v3.txt', source)
        self.assertNotIn('/release-pointer.txt', source)

    def test_https_bootstrap_authenticates_current_generation_without_legacy_writes(self):
        source = (grammar.TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        a = source.index('prepare_https_client()')
        b = source.index('\n}\n', a) + 3
        body = source[a:b]
        self.assertIn('GENERATION_TRANSACTION', body)
        self.assertNotIn(' verify ', body)
        engine = (grammar.TOOLS / 'initramfs/libreecho-generation-transaction').read_text()
        client = engine[engine.index('https_client()'):engine.index('provenance()')]
        self.assertIn('TARGET_MANIFEST', client)
        self.assertIn('feature_assistant_', client)
        self.assertNotIn('verify_generation', client)
        self.assertIn('current', body)
        self.assertNotIn('chmod', body)
        self.assertNotIn('manifest_payload_value', body)

    def test_legacy_state_alone_cannot_bootstrap_tls(self):
        source = (grammar.TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        a = source.index('prepare_https_client()')
        b = source.index('\n}\n', a) + 3
        control = self.root / 'update'
        control.mkdir()
        legacy = self.root / 'legacy'
        legacy.mkdir()
        (legacy / 'payload.squashfs').write_bytes(b'legacy')
        (legacy / 'manifest.json').write_text('{}')
        script = self.root / 'tls'
        script.write_text('BB=/bin/busybox\nROOT=' + str(control) + '\nASSISTANT_PAYLOAD=' + str(legacy / 'payload.squashfs') + '\nASSISTANT_MANIFEST=' + str(legacy / 'manifest.json') + '\ndie() { echo "ERROR:$1" >&2; exit 1; }\n' + source[a:b] + '\nprepare_https_client\n')
        result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('legacy_manifest_unsupported', result.stderr)
        self.assertEqual((legacy / 'payload.squashfs').read_bytes(), b'legacy')


if __name__ == '__main__':
    import unittest
    unittest.main()

FAKE_CURL = '#!/bin/sh\nout=; headers=; url=; range=;\nwhile [ "$#" -gt 0 ]; do\n  case "$1" in\n    --output) out=$2; shift 2 ;;\n    --stderr) shift 2 ;;\n    --dump-header) headers=$2; shift 2 ;;\n    --range) range=$2; shift 2 ;;\n    --write-out) shift 2 ;;\n    *) url=$1; shift ;;\n  esac\ndone\nprintf \'%s\\n\' "scenario=$SCENARIO range=$range" >> "$CURL_LOG"\nsrc=$SOURCE_PACKAGE\nsize=$(stat -c %s "$src") || exit 63\nheader_200() { if [ "$SCENARIO" = 200-nolength ]; then printf \'HTTP/1.1 200 OK\\r\\n\\r\\n\' > "$headers"; else printf \'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\n\\r\\n\' "$size" > "$headers"; fi; }\nif [ "$range" = 0-0 ]; then\n  case "$SCENARIO" in\n    206|redirect206) [ "$SCENARIO" = redirect206 ] && printf \'HTTP/1.1 302 Found\\r\\nLocation: https://redirect.invalid/ota\\r\\n\\r\\n\' > "$headers"; printf \'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 0-0/%s\\r\\nContent-Length: 1\\r\\n\\r\\n\' "$size" >> "$headers"; printf x > "$out"; exit 0 ;;\n    malformed206) printf \'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 1-0/%s\\r\\nContent-Length: 1\\r\\n\\r\\n\' "$size" > "$headers"; printf x > "$out"; exit 0 ;;\n    contradictory206) printf \'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 0-0/%s\\r\\nContent-Range: bytes 1-1/%s\\r\\nContent-Length: 1\\r\\n\\r\\n\' "$size" "$size" > "$headers"; printf x > "$out"; exit 0 ;;\n    416) printf \'HTTP/1.1 416 Range Not Satisfiable\\r\\nContent-Range: bytes */%s\\r\\n\\r\\n\' "$size" > "$headers"; exit 33 ;;\n    200-length|200-nolength|oversized-chunked|oversized-length) header_200; exit 0 ;;\n    interrupted|transport-tls) [ "$SCENARIO" = transport-tls ] && exit 60; printf \'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 0-0/%s\\r\\nContent-Length: 1\\r\\n\\r\\n\' "$size" > "$headers"; printf x > "$out"; exit 0 ;;\n  esac\nfi\nif [ -n "$range" ]; then\n  offset=${range%-}\n  case "$SCENARIO" in\n    206|redirect206) printf \'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes %s-%s/%s\\r\\nContent-Length: %s\\r\\n\\r\\n\' "$offset" "$((size - 1))" "$size" "$((size - offset))" > "$headers"; tail -c +"$((offset + 1))" "$src" > "$out"; exit 0 ;;\n    interrupted) printf \'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes %s-%s/%s\\r\\nContent-Length: %s\\r\\n\\r\\n\' "$offset" "$((size - 1))" "$size" "$((size - offset))" > "$headers"; dd if="$src" bs=1 skip="$offset" count=3 of="$out" 2>/dev/null; exit 28 ;;\n    resume-malformed) printf \'HTTP/1.1 206 Partial Content\\r\\nContent-Range: malformed\\r\\nContent-Length: 3\\r\\n\\r\\n\' > "$headers"; printf xxx > "$out"; exit 0 ;;\n    200-length|200-nolength) header_200; cat "$src" > "$out"; exit 0 ;;\n  esac\nfi\ncase "$SCENARIO" in\n  fresh-malformed) printf \'HTTP/1.1 200 OK\\r\\nContent-Length: 1\\r\\nContent-Length: 2\\r\\n\\r\\n\' > "$headers"; printf x > "$out"; exit 0 ;;\n  fresh-classification) printf \'HTTP/1.1 302 Found\\r\\nLocation: https://redirect.invalid/ota\\r\\n\\r\\n\' > "$headers"; printf x > "$out"; exit 0 ;;\n  fresh-transport-tls) exit 60 ;;\n  signal) printf \'HTTP/1.1 200 OK\\r\\nContent-Length: 33554432\\r\\n\\r\\n\' > "$headers"; i=0; while [ "$i" -lt 1000 ]; do head -c 65536 /dev/zero >> "$out" || exit 23; i=$((i + 1)); sleep 1; done; exit 0 ;;\n  oversized-chunked) printf \'HTTP/1.1 200 OK\\r\\n\\r\\n\' > "$headers"; head -c 33554433 /dev/zero > "$out"; exit $? ;;\n  oversized-length) printf \'HTTP/1.1 200 OK\\r\\nContent-Length: 33554433\\r\\n\\r\\n\' > "$headers"; head -c 33554433 /dev/zero > "$out"; exit $? ;;\n  *) header_200; cat "$src" > "$out"; exit 0 ;;\nesac\n'


import os
import shutil
import time
import test_ota_v3_integration as integration


class ControlTransportTests(grammar.SignedFixture):
    """Real transport, signed v3 control and updater; only HTTPS peer is fake."""
    script = integration.UpdaterTests.script
    inspect = integration.UpdaterTests.inspect

    def setUp(self):
        super().setUp()
        self.data = self.root / 'data'
        self.update = self.data / 'libreecho/update'
        self.stage = self.update / 'staging'
        self.stage.mkdir(parents=True)
        self.boot = b'ANDROID!' + bytes(16777216 - 8)
        self.text = grammar.manifest().replace('boot_sha256=' + 'a' * 64,
                    'boot_sha256=' + __import__('hashlib').sha256(self.boot).hexdigest())
        self.env['TARGET_MANIFEST'] = str(grammar.TOOLS / 'initramfs/libreecho-target-manifest')
        self.assertEqual(self.inspect(self.text).returncode, 0)
        updater = self.script('inspect_package "$2"')
        updater.chmod(0o755)
        self.raw = (self.root / 'package.tar').read_bytes()
        self.curl = self.root / 'fake-curl'
        self.curl.write_text(FAKE_CURL)
        self.curl.chmod(0o755)
        source = (grammar.TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        source = source[:source.rfind('case "${1:-}" in')]
        source = source.replace('ROOT=/data/libreecho/update', 'ROOT=' + str(self.update))
        source = source.replace('UPDATE=/usr/local/sbin/libreecho-update', 'UPDATE=' + str(updater))
        source = source.replace('CURL_STDERR=/run/libreecho/ota-curl.stderr', 'CURL_STDERR=' + str(self.update / 'curl.stderr'))
        source = source.replace('CURL_HEADERS=/run/libreecho/ota-curl.headers', 'CURL_HEADERS=' + str(self.update / 'curl.headers'))
        source += '\nCURL=' + str(self.curl) + '\nCA=/dev/null\nurl=https://fixture.invalid/v3\nchannel=stable\ncheck_status_write() { :; }\ndie() { echo "ERROR:$1" >&2; exit 1; }\nRUN_ROOT=' + str(self.root / 'run') + '\nmkdir -p "$INCOMING" "$RUN_ROOT/libreecho"\nfetch_lock\ndownload_and_inspect\n'
        self.fetcher = self.root / 'fetcher'
        self.fetcher.write_text(source)
        self.env.update(SOURCE_PACKAGE=str(self.root / 'package.tar'), CURL_LOG=str(self.root / 'curl.log'))

    def run_case(self, mode, partial=None, slow_reader=False):
        incoming = self.update / 'incoming'
        shutil.rmtree(incoming, ignore_errors=True)
        incoming.mkdir()
        if partial is not None:
            (incoming / 'github-update.ota.tar.part').write_bytes(partial)
        env = dict(self.env, SCENARIO=mode)
        script = self.fetcher
        if slow_reader:
            bb = self.root / 'slow-busybox'
            bb.write_text('#!/bin/sh\nif [ "$1" = head ]; then /bin/busybox "$@"; rc=$?; sleep 0.1; exit "$rc"; fi\nexec /bin/busybox "$@"\n')
            bb.chmod(0o755)
            script = self.root / 'slow-fetcher'
            script.write_text(self.fetcher.read_text().replace('BB=/bin/busybox', 'BB=' + str(bb)))
        return subprocess.run(['/bin/busybox', 'sh', str(script)], env=env, capture_output=True, text=True, timeout=30)

    def assert_clean(self):
        self.assertEqual(list(self.update.glob('.control-*')), [])
        self.assertFalse((self.update / 'curl.headers').exists())
        self.assertFalse((self.update / 'curl.stderr').exists())

    def test_completed_producer_does_not_kill_draining_reader(self):
        result = self.run_case('206', self.raw[:1000], slow_reader=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.update / 'incoming/github-update.ota.tar').read_bytes(), self.raw)
        self.assert_clean()

    def test_control_resume_response_matrix(self):
        for mode in ('206', 'redirect206', '200-length', '200-nolength', '416'):
            with self.subTest(mode=mode):
                result = self.run_case(mode, self.raw[:1000])
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual((self.update / 'incoming/github-update.ota.tar').read_bytes(), self.raw)
                self.assert_clean()

    def test_malformed_interrupted_and_oversized_control_never_promotes(self):
        for mode in ('malformed206', 'contradictory206', 'interrupted', 'transport-tls', 'oversized-chunked', 'oversized-length', 'resume-malformed'):
            with self.subTest(mode=mode):
                original = self.raw[:1000]
                result = self.run_case(mode, original)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.update / 'incoming/github-update.ota.tar.part').read_bytes(), original)
                self.assertFalse((self.update / 'incoming/github-update.ota.tar').exists())
                self.assert_clean()
        for mode in ('fresh-malformed', 'fresh-classification', 'fresh-transport-tls', 'oversized-chunked', 'oversized-length'):
            with self.subTest(mode=mode):
                result = self.run_case(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.update / 'incoming/github-update.ota.tar').exists())
                self.assert_clean()
