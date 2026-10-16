"""Harness safety/negative tests; fixtures are not hardware evidence."""
import json
from pathlib import Path
import tempfile
import unittest
import qualification as q

CANDIDATE = {'schema': 1, 'sources': {k: 'a' * 40 for k in ('product', 'platform', 'linux', 'ui')},
             'boot_sha256': 'b' * 64, 'kernel_release': 'fixture-kernel'}


class QualificationTests(unittest.TestCase):
    def runner(self, fault=None):
        calls = []
        ticks = [0]
        def execute(argv):
            calls.append(argv)
            if argv == ['adb', 'devices']:
                return 'List of devices attached\nfixture device' if fault != 'absent' else 'List of devices attached'
            self.assertEqual(argv[:4], ['adb', '-s', 'fixture', 'exec-out'])
            self.assertIn(argv[4:], list(q.PROBES.values()))
            name = next(k for k, v in q.PROBES.items() if v == argv[4:])
            if fault == 'transport':
                raise ValueError('probe failed')
            if name == 'uptime':
                ticks[0] += 1
                return str(ticks[0]) + ' 0'
            return {'kernel': 'wrong' if fault == 'kernel' else 'fixture-kernel',
                    'boot-id': ('b' if fault == 'reboot' and ticks[0] else 'a') * 8 + '-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    'slot': 'none' if fault == 'slot' else 'androidboot.slot_suffix=_b',
                    'alsa': '--- no soundcards ---' if fault == 'alsa' else ' 0 [fixture]: card'}[name]
        return execute, calls

    def test_baseline_and_negative_transport_gates(self):
        for fault in [None, 'absent', 'transport', 'kernel', 'reboot', 'slot', 'alsa']:
            runner, calls = self.runner(fault)
            report = q.baseline(CANDIDATE, 'fixture', 2, 0, runner)
            self.assertEqual(report['status'], 'PASS' if fault is None else 'HOLD', (fault, report))
            self.assertNotIn('androidboot', json.dumps(report))
            if fault == 'transport':
                self.assertEqual(len(calls), 2, 'stop after first transport failure')

    def test_missing_skipped_and_foreign_evidence_never_pass(self):
        self.assertEqual(q.aggregate(CANDIDATE, [])['status'], 'HOLD')
        record = {'schema': 1, 'candidate_id': q.identity(CANDIDATE), 'gate': 'hardware-baseline', 'status': 'SKIP'}
        self.assertEqual(q.aggregate(CANDIDATE, [record])['status'], 'HOLD')
        with self.assertRaises(ValueError):
            q.aggregate(CANDIDATE, [record, record])
        record['candidate_id'] = 'other'
        with self.assertRaises(ValueError):
            q.aggregate(CANDIDATE, [record])

    def test_candidate_and_artifact_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / 'candidate.json'
            candidate.write_text(json.dumps(CANDIDATE))
            self.assertEqual(q.load_candidate(candidate), CANDIDATE)
            bad = dict(CANDIDATE, sources={'product': 'main'})
            candidate.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                q.load_candidate(candidate)
            artifact = root / 'log'
            artifact.write_text('fixture evidence')
            record = {'schema': 1, 'candidate_id': q.identity(CANDIDATE), 'gate': 'source-regressions',
                      'status': 'PASS', 'artifacts': [{'path': str(artifact), 'sha256': q.digest(artifact)}]}
            self.assertEqual(q.aggregate(CANDIDATE, [record])['status'], 'HOLD')
            artifact.write_text('modified')
            with self.assertRaises(ValueError):
                q.aggregate(CANDIDATE, [record])

    def test_probe_timeout_and_output_limits(self):
        import sys
        with self.assertRaisesRegex(ValueError, 'timed out'):
            q.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.1)
        with self.assertRaisesRegex(ValueError, 'output limit'):
            q.run([sys.executable, '-c', 'print("x" * 200000)'])


if __name__ == '__main__':
    unittest.main()
