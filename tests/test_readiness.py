import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import readiness
import release_evidence


class ReadinessTests(unittest.TestCase):
    def test_incomplete_or_changed_stage_logs_cannot_be_ready(self):
        snapshot = {'source_sha': 'a' * 40, 'fingerprint': 'b' * 64, 'clean': False}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'preflight.json'
            report = {'schema': 1, 'pass': True, 'source_sha': snapshot['source_sha'], 'version': json.loads((ROOT / 'package.json').read_text())['version'],
                      'source_fingerprint': snapshot['fingerprint'], 'after_fingerprint': snapshot['fingerprint'],
                      'failures': 0, 'skipped': 0, 'tests': 1, 'stages': []}
            with mock.patch.object(sys, 'argv', ['readiness', 'pre-push', '--root', str(ROOT), '--preflight', str(output)]), \
                 mock.patch.object(readiness, 'source_snapshot', return_value=snapshot):
                output.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    readiness.main()
                for name in ('syntax', 'node', 'python', 'release-contract', 'brand-exports'):
                    log = root / (name + '.log')
                    log.write_text('executed')
                    report['stages'].append({'name': name, 'exit': 0, 'path': log.name, 'sha256': release_evidence.digest(log)})
                output.write_text(json.dumps(report))
                readiness.main()
                for field, bad in [('source_sha', 'c' * 40), ('tests', True), ('version', '9.0.0'), ('schema', 2)]:
                    changed = {**report, field: bad}
                    output.write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        readiness.main()
                output.write_text(json.dumps(report))
                (root / 'node.log').write_text('changed')
                with self.assertRaises(ValueError):
                    readiness.main()
