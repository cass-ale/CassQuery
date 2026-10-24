"""Smoke schema regressions: OS/signing/process operations are deliberately mocked.

Run after placing in tests/: python -m unittest discover -s tests -p test_macos_smoke.py
Set CASSQUERY_REVIEW_ROOT only when running from external review scratch.
No real app, keychain, preferences, or process is modified.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import random
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

ROOT = Path(os.environ.get('CASSQUERY_REVIEW_ROOT', Path(__file__).resolve().parents[1]))
SOURCE = ROOT / 'tools/macos_smoke.py'
VERSION = json.loads((ROOT / 'package.json').read_text())['version']
spec = importlib.util.spec_from_file_location('review_macos_smoke', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CHECKS = ['certified demo', 'gate pass', 'assembled step', 'exploded step', 'reset view',
          'save updates rendered model', 'camera preserved on rebuild',
          'failed gate restores certified model', 'no renderer diagnostics']


class SmokeEvidenceTests(unittest.TestCase):
    def exercise(self, case):
        with tempfile.TemporaryDirectory(prefix='cq-schema-review-') as temporary:
            root = Path(temporary)
            app = root / 'Fake.app'
            binary = app / 'Contents/MacOS/CassQuery'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'placeholder: native checks are mocked')
            with (app / 'Contents/Info.plist').open('wb') as stream:
                plistlib.dump({'CFBundleIdentifier': 'com.cassquery.desktop', 'CFBundleShortVersionString': VERSION}, stream)
            output = root / 'output'
            output.mkdir()
            images = [output / 'assembled.png', output / 'apart.png']
            pixels = random.Random(42).randbytes(200 * 200 * 3)

            def save_images():
                for image in images:
                    Image.frombytes('RGB', (200, 200), pixels).save(image)

            # The stale-files control succeeds only if the harness fails to remove these.
            save_images()
            class FakeProcess:
                pid = 12345
                def __init__(self, _command, **kwargs):
                    env = kwargs['env']
                    if case != 'stale_images':
                        save_images()
                    report = {'pass': True, 'challenge': env['CASSQUERY_SMOKE_CHALLENGE'],
                              'app_pid': self.pid, 'worker_pid': 54321,
                              'checks': CHECKS, 'screenshots': [str(image) for image in images]}
                    if case == 'missing_challenge':
                        del report['challenge']
                    elif case == 'stale_challenge':
                        report['challenge'] = 'prior-run'
                    elif case == 'wrong_pid':
                        report['app_pid'] += 1
                    elif case == 'missing_worker':
                        del report['worker_pid']
                    elif case == 'empty_screenshots':
                        report['screenshots'] = []
                    elif case == 'duplicate_screenshots':
                        report['screenshots'] = [str(images[0]), str(images[0])]
                    elif case == 'non_png':
                        images[0].write_bytes(b'not a PNG' * 300)
                    elif case == 'small_png':
                        Image.frombytes('RGB', (50, 50), random.Random(1).randbytes(50*50*3)).save(images[0])
                    elif case == 'missing_check':
                        report['checks'] = CHECKS[:-1]
                    (output / 'renderer.json').write_text(json.dumps(report))
                def wait(self, timeout=None):
                    return 0

            def dead_pid(_pid, _signal):
                raise ProcessLookupError
            with patch.object(sys, 'argv', [str(SOURCE), '--app', str(app), '--output', str(output)]), \
                 patch.object(module.subprocess, 'check_output', return_value=platform.machine()), \
                 patch.object(module.subprocess, 'run'), \
                 patch.object(module.subprocess, 'Popen', FakeProcess), \
                 patch.object(module.os, 'kill', dead_pid), \
                 contextlib.redirect_stdout(io.StringIO()):
                module.main()

    def test_fresh_complete_real_png_evidence_passes(self):
        self.exercise('good')

    def test_missing_or_stale_or_incomplete_evidence_fails(self):
        for case in ('missing_challenge', 'stale_challenge', 'wrong_pid', 'missing_worker',
                     'empty_screenshots', 'duplicate_screenshots', 'stale_images',
                     'non_png', 'small_png', 'missing_check'):
            with self.subTest(case=case), self.assertRaises((RuntimeError, AssertionError, KeyError, ValueError, OSError)):
                self.exercise(case)


if __name__ == '__main__':
    unittest.main()
