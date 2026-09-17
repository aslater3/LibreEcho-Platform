#!/usr/bin/env python3
"""Behavioral tests of the real shell dev discovery resolver (mock transport)."""
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'initramfs/libreecho-update-fetch'
TAG = 'radar-puffin-build-' + 'a'*7 + '-' + 'b'*16 + '-' + 'c'*16
SHA256 = 'd'*64
BUSYBOX = shutil.which('busybox') or '/bin/busybox'


def source_region(start: str, stop: str) -> str:
    """Return the real shell source from `start` up to `stop`."""
    text = SOURCE.read_text()
    begin = text.index(start)
    return text[begin:text.index(stop, begin)]


def field_map(record: str) -> dict:
    return dict(line.split('=', 1) for line in record.splitlines() if '=' in line)


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


RECORD_STUBS = '''\
BB="$TEST_BB"
ROOT="$TEST_ROOT"
CHECK_STATUS="$ROOT/check-status"
CHANNEL_FILE="$ROOT/automatic-updates"
CONFIG="$ROOT/ota-source.conf"
PROFILE="$ROOT/image-profile"
CURL_DIAGNOSTIC_MAX=160
channel_value() { "$BB" sed -n 's/^channel=//p' "$CHANNEL_FILE" 2>/dev/null; }
config_value() { "$BB" sed -n "s/^$1=//p" "$CONFIG" 2>/dev/null; }
'''

# The real record writer, its sanitising helpers, the real failure writer, and
# the real channel-change record block, extracted from the shipped helper.
RECORD_REGION_MARKERS = ('sanitize_status_value()\n', '\nautomatic_updates_enabled()')
FAILURE_REGION_MARKERS = ('die()\n', '\nrequire_environment()')
CANDIDATE_REGION_MARKERS = ('check_status_write_candidate()\n', '\nwatch_updates()')
CHANNEL_REGION_MARKERS = ('set_channel()\n', '\nmanifest_payload_value()')


def region(markers: tuple) -> str:
    """Extract a real shell region, failing loudly if the helper no longer has it."""
    text = SOURCE.read_text()
    for marker in markers:
        if marker not in text:
            raise AssertionError(f'libreecho-update-fetch no longer defines {marker!r}')
    return source_region(*markers)

ORCHESTRATION_STUBS = '''\
BB="$TEST_BB"
ROOT="$TEST_ROOT"
DATA_ROOT="$TEST_ROOT/data"
PROC_ROOT="$TEST_ROOT/proc"
RUN_ROOT="$TEST_ROOT/run"
CHECK_STATUS="$ROOT/check-status"
CHANNEL_FILE="$ROOT/automatic-updates"
CONFIG="$ROOT/ota-source.conf"
CURL_DIAGNOSTIC_MAX=160
PACKAGE="$ROOT/incoming/github-update.ota.tar"
FEATURE_STAGE="$ROOT/staging/features"
UPDATE=/bin/true
channel="$TEST_CHANNEL"
require_environment() { :; }
fetch_lock() { :; }
install_lock() { :; }
install_unlock() { :; }
seed_channel() { :; }
validate_source() { :; }
prepare_https_client() { :; }
resolve_dev_release() {
    [ "$channel" = dev ] || return 0
    DEV_RELEASE_TAG="$TEST_TAG"
    DEV_OTA_SHA256="$TEST_SHA"
}
download_and_inspect() { printf '%s\\n' "$TEST_VERSION"; }
download_feature_assets() { :; }
quarantine_file() { :; }
channel_value() { "$BB" sed -n 's/^channel=//p' "$CHANNEL_FILE" 2>/dev/null; }
config_value() { "$BB" sed -n "s/^$1=//p" "$CONFIG" 2>/dev/null; }
check_value_from_file() { "$BB" sed -n "s/^$2=//p" "$1" 2>/dev/null; }
record_channel() { "$BB" sed -n 's/^update_channel=//p' "$1" 2>/dev/null; }
candidate_matches_record() { return "$TEST_MATCH"; }
'''


class UpdateCheckIdentityTests(unittest.TestCase):
    """A completed check records the identity of the candidate it resolved."""

    def run_check(self, body, seed=None, extra_env=None, extra_source=''):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'automatic-updates').write_text('channel=dev\n')
            (root/'image-profile').write_text('ota\n')
            (root/'data').mkdir()
            (root/'proc').mkdir()
            (root/'proc/mounts').write_text(f'none {tmp}/data ext4 rw 0 0\n')
            if seed is not None:
                (root/'check-status').write_text(seed)
            script = (RECORD_STUBS + region(RECORD_REGION_MARKERS)
                      + region(FAILURE_REGION_MARKERS) + extra_source + body)
            env = os.environ | dict(TEST_ROOT=tmp, TEST_BB=BUSYBOX) | (extra_env or {})
            result = subprocess.run(['sh', '-c', script], env=env, text=True, capture_output=True)
            record = (root/'check-status').read_text() if (root/'check-status').exists() else ''
            return result, record

    def test_terminal_status_records_the_resolved_candidate_identity(self):
        result, record = self.run_check(
            'check_status_write update-available 0.14.0 "" true "" "" "" "$TEST_TAG" "$TEST_SHA"\n',
            seed='schema=1\nstatus=checking\n',
            extra_env=dict(TEST_TAG=TAG, TEST_SHA=SHA256),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = field_map(record)
        self.assertEqual(values['status'], 'update-available')
        self.assertEqual(values['latest_version'], '0.14.0')
        self.assertEqual(values.get('resolved_release_tag'), TAG)
        self.assertEqual(values.get('ota_sha256'), SHA256)

    def test_status_without_an_identity_clears_the_previous_one(self):
        seed = (f'schema=1\nstatus=update-available\nlatest_version=0.14.0\n'
                f'resolved_release_tag={TAG}\nota_sha256={SHA256}\n')
        for body, status in (('check_status_write checking "" "" unknown\n', 'checking'),
                             ('check_status_write downloading "" "" true\n', 'downloading')):
            with self.subTest(status=status):
                result, record = self.run_check(body, seed=seed)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = field_map(record)
                self.assertEqual(values['status'], status)
                self.assertEqual(values.get('resolved_release_tag'), '')
                self.assertEqual(values.get('ota_sha256'), '')

    def test_failed_check_clears_the_previous_identity(self):
        seed = (f'schema=1\nstatus=update-available\nlatest_version=0.14.0\n'
                f'resolved_release_tag={TAG}\nota_sha256={SHA256}\n')
        result, record = self.run_check('die download_dns false 6 "Could not resolve host" ""\n', seed=seed)
        self.assertEqual(result.returncode, 1, result.stdout)
        values = field_map(record)
        self.assertEqual(values['status'], 'error')
        self.assertEqual(values['error'], 'download_dns')
        self.assertEqual(values['error_exit'], '6')
        self.assertEqual(values['source_reachable'], 'false')
        self.assertEqual(values.get('resolved_release_tag'), '')
        self.assertEqual(values.get('ota_sha256'), '')

    def test_malformed_or_oversized_identity_is_recorded_as_absent(self):
        bad_tags = (TAG.upper(), 'radar-puffin-v0.14.0', TAG + 'x', TAG + 'f'*200,
                    'radar-puffin-build-' + 'a'*8 + '-' + 'b'*16 + '-' + 'c'*16,
                    'https://github.com/aslater3/LibreEcho/releases/download/' + TAG, '')
        for bad in bad_tags:
            with self.subTest(tag=bad[:40]):
                result, record = self.run_check(
                    'check_status_write update-available 0.14.0 "" true "" "" "" "$TEST_TAG" "$TEST_SHA"\n',
                    extra_env=dict(TEST_TAG=bad, TEST_SHA=SHA256))
                self.assertEqual(result.returncode, 0, result.stderr)
                values = field_map(record)
                self.assertEqual(values.get('resolved_release_tag'), '')
                self.assertEqual(values.get('ota_sha256'), SHA256)
        bad_hashes = (SHA256[:63], SHA256 + 'd', SHA256.upper(), 'z'*64, SHA256 + 'e'*200, '')
        for bad in bad_hashes:
            with self.subTest(digest=bad[:40]):
                result, record = self.run_check(
                    'check_status_write update-available 0.14.0 "" true "" "" "" "$TEST_TAG" "$TEST_SHA"\n',
                    extra_env=dict(TEST_TAG=TAG, TEST_SHA=bad))
                self.assertEqual(result.returncode, 0, result.stderr)
                values = field_map(record)
                self.assertEqual(values.get('resolved_release_tag'), TAG)
                self.assertEqual(values.get('ota_sha256'), '')

    def test_recorded_identity_stays_within_the_bounds_the_ui_reads(self):
        result, record = self.run_check(
            'check_status_write update-available 0.14.0 "" true "" "" "" "$TEST_TAG" "$TEST_SHA"\n',
            extra_env=dict(TEST_TAG=TAG, TEST_SHA=SHA256))
        self.assertEqual(result.returncode, 0, result.stderr)
        values = field_map(record)
        self.assertEqual(values.get('resolved_release_tag'), TAG)
        self.assertEqual(len(values.get('resolved_release_tag') or ''), 60)
        self.assertEqual(len(values.get('ota_sha256') or ''), 64)

    def test_channel_change_clears_the_previous_identity(self):
        seed = (f'schema=1\nstatus=update-available\nlatest_version=0.14.0\n'
                f'resolved_release_tag={TAG}\nota_sha256={SHA256}\n')
        extra = ('DATA_ROOT="$TEST_ROOT/data"\n'
                 'PROC_ROOT="$TEST_ROOT/proc"\n'
                 'write_channel() { :; }\n'
                 'fetch_lock() { :; }\n'
                 'install_lock() { :; }\n'
                 'cleanup_locks() { :; }\n')
        result, record = self.run_check('set_channel stable\n', seed=seed,
                                        extra_source=region(CHANNEL_REGION_MARKERS) + extra)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        values = field_map(record)
        self.assertEqual(values['status'], 'not-checked')
        self.assertEqual(values['channel'], 'stable')
        self.assertIn('resolved_release_tag', values)
        self.assertEqual(values.get('resolved_release_tag'), '')
        self.assertIn('ota_sha256', values)
        self.assertEqual(values.get('ota_sha256'), '')


class UpdateCheckWiringTests(unittest.TestCase):
    """The production check paths carry the resolved identity into the record."""

    def run_check_or_install(self, channel='dev', action='check', package=b'libreecho-ota\n',
                             installed=None, matches='1', tag=TAG, sha=None, version='0.14.0'):
        digest = sha or hashlib.sha256(package).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'automatic-updates').write_text(f'channel={channel}\n')
            (root/'incoming').mkdir()
            (root/'incoming/github-update.ota.tar').write_bytes(package)
            (root/'staging').mkdir()
            (root/'staging/manifest').write_text('update_channel=dev\n')
            if installed is not None:
                (root/'installed').write_text(installed)
            script = (ORCHESTRATION_STUBS + region(RECORD_REGION_MARKERS)
                      + region(FAILURE_REGION_MARKERS) + region(CANDIDATE_REGION_MARKERS)
                      + f'check_or_install {action}\nrc=$?\nprintf "RC=%s\\n" "$rc"\nexit "$rc"\n')
            env = os.environ | dict(TEST_ROOT=tmp, TEST_BB=BUSYBOX, TEST_CHANNEL=channel,
                                    TEST_TAG=tag, TEST_SHA=digest, TEST_VERSION=version,
                                    TEST_MATCH=matches)
            result = subprocess.run(['sh', '-c', script], env=env, text=True, capture_output=True)
            record = (root/'check-status').read_text() if (root/'check-status').exists() else ''
            return result, record

    def test_available_update_records_the_dev_resolved_identity(self):
        result, record = self.run_check_or_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        values = field_map(record)
        self.assertEqual(values['status'], 'update-available')
        self.assertEqual(values['channel'], 'dev')
        self.assertEqual(values.get('resolved_release_tag'), TAG)
        self.assertEqual(values.get('ota_sha256'), hashlib.sha256(b'libreecho-ota\n').hexdigest())

    def test_up_to_date_records_the_resolved_identity(self):
        result, record = self.run_check_or_install(
            installed='version=0.14.0\nupdate_channel=dev\n', matches='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        values = field_map(record)
        self.assertEqual(values['status'], 'up-to-date')
        self.assertEqual(values.get('resolved_release_tag'), TAG)
        self.assertEqual(values.get('ota_sha256'), hashlib.sha256(b'libreecho-ota\n').hexdigest())

    def test_package_that_fails_the_resolved_digest_records_no_identity(self):
        result, record = self.run_check_or_install(sha=SHA256)
        self.assertEqual(result.returncode, 1, result.stdout)
        values = field_map(record)
        self.assertEqual(values['status'], 'error')
        self.assertEqual(values['error'], 'dev_control_identity')
        self.assertEqual(values.get('resolved_release_tag'), '')
        self.assertEqual(values.get('ota_sha256'), '')

    def test_stable_candidate_records_no_identity(self):
        result, record = self.run_check_or_install(channel='stable')
        self.assertEqual(result.returncode, 0, result.stderr)
        values = field_map(record)
        self.assertEqual(values['status'], 'update-available')
        self.assertEqual(values['channel'], 'stable')
        self.assertEqual(values.get('resolved_release_tag'), '')
        self.assertEqual(values.get('ota_sha256'), '')


if __name__ == '__main__':
    unittest.main()
