#!/usr/bin/env python3
"""Read-only, serial-bound hardware baseline and fail-closed qualification ledger.

This tool never installs, reboots, confirms a slot, changes configuration, records
microphone audio, or plays sound. Physical/OTA tests require separate evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REQUIRED = (
    'source-regressions', 'emulator-ota', 'artifact-verification',
    'hardware-baseline', 'setup-auth-persistence', 'voice-local',
    'voice-home-assistant', 'mdns-without-airplay', 'audio-playback-capture',
    'wake-barge-in', 'buttons-leds-clock', 'radio-airplay-bluetooth',
    'ota-upgrade-rollback', 'cold-boot-persistence', 'stability-soak',
)
PROBES = {
    'kernel': ['uname', '-r'],
    'boot-id': ['cat', '/proc/sys/kernel/random/boot_id'],
    'uptime': ['cat', '/proc/uptime'],
    'slot': ['cat', '/proc/cmdline'],
    'alsa': ['cat', '/proc/asound/cards'],
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_candidate(path):
    candidate = json.loads(Path(path).read_text())
    if set(candidate) != {'schema', 'sources', 'boot_sha256', 'kernel_release'}:
        raise ValueError('candidate must contain schema, sources, boot_sha256, kernel_release')
    if candidate['schema'] != 1 or set(candidate['sources']) != {'product', 'platform', 'linux', 'ui'}:
        raise ValueError('candidate needs all four source identities')
    if any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{40}', v) for v in candidate['sources'].values()):
        raise ValueError('source identities must be exact commit SHAs')
    if not isinstance(candidate['boot_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', candidate['boot_sha256']):
        raise ValueError('invalid boot hash')
    if not isinstance(candidate['kernel_release'], str) or not candidate['kernel_release'].strip():
        raise ValueError('exact kernel release is required')
    return candidate


def identity(candidate):
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def run(argv, timeout: float = 10):
    # Bounded outputs are enforced without keeping arbitrary command output in RAM.
    import tempfile
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(argv, stdout=out, stderr=err, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise ValueError('probe timed out')
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > 131072:
                    raise ValueError('probe exceeded output limit')
                time.sleep(0.05)
            if process.returncode:
                raise ValueError('probe failed (exit %s)' % process.returncode)
            if os.fstat(out.fileno()).st_size > 131072:
                raise ValueError('probe exceeded output limit')
            out.seek(0)
            return out.read(131073).decode('utf-8', errors='strict').strip()
        finally:
            if process.poll() is None:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def baseline(candidate, serial, sample_count=3, interval=5, runner=run):
    if not serial or serial.startswith('-') or any(c.isspace() for c in serial):
        raise ValueError('explicit single ADB serial required')
    if not 2 <= sample_count <= 120 or not 0 <= interval <= 60:
        raise ValueError('invalid bounded sample policy')
    report = {'schema': 1, 'candidate_id': identity(candidate), 'gate': 'hardware-baseline',
              'status': 'HOLD', 'samples': [], 'checks': [],
              'limitations': ['Kernel identity is not boot-image hash verification.',
                              'ALSA registration is not acoustic acceptance.',
                              'Short baseline sampling is not a stability soak.']}
    try:
        devices = runner(['adb', 'devices']).splitlines()
        matches = [line.split() for line in devices if line.split() and line.split()[0] == serial]
        if matches != [[serial, 'device']]:
            raise ValueError('selected ADB device absent, ambiguous, or unauthorized')
        previous = None
        boot_id = None
        for index in range(sample_count):
            sample = {}
            for name, command in PROBES.items():
                value = runner(['adb', '-s', serial, 'exec-out'] + command)
                if not value:
                    raise ValueError('empty ' + name + ' response')
                sample[name] = value
            if sample['kernel'] != candidate['kernel_release']:
                raise ValueError('kernel identity mismatch')
            if not re.fullmatch('[0-9a-f-]{36}', sample['boot-id']):
                raise ValueError('invalid boot identity')
            if boot_id is not None and sample['boot-id'] != boot_id:
                raise ValueError('unexpected reboot during baseline')
            boot_id = sample['boot-id']
            uptime = float(sample['uptime'].split()[0])
            if not uptime >= 0 or (previous is not None and uptime <= previous):
                raise ValueError('uptime did not advance')
            previous = uptime
            slots = re.findall(r'(?:^|\s)androidboot.slot_suffix=_([ab])(?:\s|$)', sample['slot'])
            if len(slots) != 1:
                raise ValueError('missing or ambiguous active slot')
            if 'no soundcards' in sample['alsa'].lower() or not re.search(r'^\s*\d+\s+\[', sample['alsa'], re.M):
                raise ValueError('ALSA card not registered')
            # Never store raw cmdline, serials, addresses, or arbitrary device logs.
            report['samples'].append({'kernel': sample['kernel'], 'uptime': uptime,
                                      'slot': slots[0], 'boot_id_sha256': hashlib.sha256(boot_id.encode()).hexdigest(),
                                      'alsa_registered': True})
            if index + 1 < sample_count:
                time.sleep(interval)
        report['status'] = 'PASS'
    except (ValueError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
        report['checks'].append({'status': 'HOLD', 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    return report


def aggregate(candidate, records):
    expected = identity(candidate)
    gates = {}
    for record in records:
        if record.get('schema') != 1 or record.get('candidate_id') != expected:
            raise ValueError('evidence schema or candidate mismatch')
        gate = record.get('gate')
        if gate not in REQUIRED or gate in gates:
            raise ValueError('unknown or duplicate gate')
        if record.get('status') not in ('PASS', 'FAIL', 'HOLD', 'SKIP'):
            raise ValueError('invalid evidence status')
        if gate != 'hardware-baseline':
            artifacts = record.get('artifacts', [])
            if not artifacts:
                raise ValueError('gate needs independently retained evidence artifacts')
            for artifact in artifacts:
                if digest(artifact['path']) != artifact['sha256']:
                    raise ValueError('evidence artifact hash mismatch')
        gates[gate] = record['status']
    statuses = {gate: gates.get(gate, 'HOLD') for gate in REQUIRED}
    return {'schema': 1, 'candidate_id': expected, 'gates': statuses,
            'status': 'PASS' if all(v == 'PASS' for v in statuses.values()) else 'HOLD'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['baseline', 'aggregate'])
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--serial')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--evidence', action='append', default=[])
    args = parser.parse_args()
    candidate = load_candidate(args.candidate)
    report = baseline(candidate, args.serial, args.samples, args.interval) if args.mode == 'baseline' else aggregate(candidate, [json.loads(Path(p).read_text()) for p in args.evidence])
    with open(args.output, 'x', encoding='utf-8') as out:
        os.chmod(args.output, 0o600)
        json.dump(report, out, indent=2)
        out.write('\n')
    print(report['status'])
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('HOLD: ' + str(exc), file=sys.stderr)
        sys.exit(2)
