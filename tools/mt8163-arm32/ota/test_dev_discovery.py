#!/usr/bin/env python3
"""Behavioral tests of the real shell dev discovery resolver (mock transport)."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'initramfs/libreecho-update-fetch'
TAG = 'radar-puffin-build-' + 'a'*7 + '-' + 'b'*16 + '-' + 'c'*16


class DevDiscoveryTests(unittest.TestCase):
    def run_resolver(self, data, channel='dev', status='200'):
        text = SOURCE.read_text()
        function = text.split('resolve_dev_release()\n',1)[1].split('\nset_channel()',1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'fixture').write_bytes(data)
            script='''
BB="$TEST_BB"
ROOT="$TEST_ROOT"
CURL_HEADERS="$ROOT/headers"
CURL_STDERR="$ROOT/stderr"
channel="$TEST_CHANNEL"
url=stable-unchanged
bounded_curl() {
    cp "$ROOT/fixture" "$1"
    STREAM_SIZE=$(wc -c < "$1")
    STREAM_CURL_RC=0
}
downloader_stderr() { :; }
parse_response_headers() { RESPONSE_HTTP_CODE="$TEST_STATUS"; RESPONSE_RANGE_KIND=none; }
die() { printf 'ERROR:%s\\n' "$1"; exit 1; }
curl_failure() { die transport; }
resolve_dev_release()
'''+function+'''
resolve_dev_release || exit 1
printf '%s\\n' "$url" "${DEV_RELEASE_TAG:-}" "${DEV_OTA_SHA256:-}"
'''
            env=os.environ | dict(TEST_ROOT=tmp,TEST_CHANNEL=channel,TEST_STATUS=status,TEST_BB=shutil.which('busybox') or '/bin/busybox')
            return subprocess.run(['sh','-c',script],env=env,text=True,capture_output=True)

    def test_resolves_single_immutable_release(self):
        result=self.run_resolver((TAG+'\n'+'d'*64+'\n').encode())
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertIn('/download/'+TAG+'/libreecho-'+TAG+'.ota.tar',result.stdout)

    def test_stable_does_not_fetch_pointer(self):
        result=self.run_resolver(b'invalid','stable')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('stable-unchanged',result.stdout)

    def test_rejects_wrong_tags(self):
        for tag in ('latest','radar-puffin-v0.13.11','../other',TAG+'?x'):
            result=self.run_resolver((tag+'\n'+'d'*64+'\n').encode())
            self.assertNotEqual(result.returncode,0,tag)

    def test_rejects_bad_hash_extra_data_and_oversize(self):
        for data in ((TAG+'\n'+'z'*64+'\n').encode(),(TAG+'\n'+'d'*64+'\nextra\n').encode(),b'x'*257):
            self.assertNotEqual(self.run_resolver(data).returncode,0)

    def test_dev_candidate_identity_includes_boot_and_v2_manifest(self):
        import hashlib
        source = SOURCE.read_text()
        function = source.split('candidate_matches_record()\n', 1)[1].split('\ncheck_or_install()', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'staging').mkdir()
            manifest = 'format=libreecho-ota-v2\nboot_sha256=' + 'a'*64 + '\n'
            (root/'staging/manifest').write_text(manifest)
            digest = hashlib.sha256(manifest.encode()).hexdigest()
            script = '''
BB="$TEST_BB"
ROOT="$TEST_ROOT"
channel="$TEST_CHANNEL"
check_value_from_file() { "$BB" sed -n "s/^$2=//p" "$1"; }
candidate_matches_record()
''' + function + '\ncandidate_matches_record "$ROOT/installed"\n'
            for channel, boot, identity, expected in [
                ('dev', 'a'*64, digest, 0),
                ('dev', 'b'*64, digest, 0),
                ('dev', 'a'*64, 'c'*64, 1),
                ('dev', 'a'*64, '', 1),
                ('stable', 'b'*64, '', 0),
            ]:
                (root/'installed').write_text('boot_sha256='+boot+'\nmanifest_sha256='+identity+'\n')
                env = os.environ | dict(TEST_ROOT=tmp, TEST_CHANNEL=channel, TEST_BB=shutil.which('busybox') or '/bin/busybox')
                result = subprocess.run(['sh','-c',script], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_rejects_http_failure(self):
        self.assertNotEqual(self.run_resolver((TAG+'\n'+'d'*64+'\n').encode(),status='404').returncode,0)


if __name__ == '__main__':
    unittest.main()
