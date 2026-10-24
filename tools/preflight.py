#!/usr/bin/env python3
"""Run complete source preflight with source-bound, log-bound evidence."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from release_evidence import digest, require, source_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('evidence/preflight.json'))
    parser.add_argument('--require-clean', action='store_true')
    parser.add_argument('--source-before', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'schema': 1, 'pass': False, 'error': 'preflight not completed'}) + '\n')
    before = source_snapshot(root)
    if args.require_clean:
        require(before['clean'], 'preflight requires clean committed source')
        require(os.environ.get('GITHUB_SHA', before['source_sha']) == before['source_sha'], 'preflight source differs from CI SHA')
    if args.source_before:
        recorded = json.loads(args.source_before.read_text())
        require(recorded['fingerprint'] == before['fingerprint'], 'source changed since initial snapshot')
    output.parent.mkdir(parents=True, exist_ok=True)
    tests = failures = skipped = 0
    stages = []
    started = time.time()
    pass_all = False
    try:
        for name, command in (
            ('syntax', ['npm', 'run', 'check']),
            ('node', ['node', '--test', '--test-reporter=tap', *[str(file.relative_to(root)) for file in sorted((root / 'tests').glob('*.cjs'))]]),
        ):
            log = output.parent / f'{name}.log'
            with log.open('w') as stream:
                result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, timeout=300)
            stages.append({'name': name, 'exit': result.returncode, 'path': log.name, 'sha256': digest(log)})
            if name == 'node':
                import re
                text = log.read_text()
                def count(key):
                    matches = re.findall(r'^# ' + key + r' (\d+)$', text, re.MULTILINE)
                    require(len(matches) == 1, f'missing Node {key} summary')
                    return int(matches[0])
                tests += count('tests')
                failures += count('fail') + count('cancelled')
                skipped += count('skipped') + count('todo')
            require(result.returncode == 0, f'{name} stage failed; see {log}')
        sys.path.insert(0, str(root / 'tests'))
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_*.py', top_level_dir=str(root / 'tests'))
        log = output.parent / 'python.log'
        with log.open('w') as stream:
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        tests += result.testsRun
        failures += len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
        skipped += len(result.skipped) + len(result.expectedFailures)
        stages.append({'name': 'python', 'exit': 0 if result.wasSuccessful() else 1, 'path': log.name, 'sha256': digest(log)})
        require(result.wasSuccessful(), f'Python tests failed; see {log}')
        for name, command in (
            ('release-contract', [sys.executable, 'tools/check_release.py']),
            ('brand-exports', [sys.executable, 'tools/make_brand_assets.py', '--check']),
        ):
            log = output.parent / f'{name}.log'
            with log.open('w') as stream:
                result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, timeout=300)
            stages.append({'name': name, 'exit': result.returncode, 'path': log.name, 'sha256': digest(log)})
            require(result.returncode == 0, f'{name} stage failed; see {log}')
        require(tests > 0 and failures == 0 and skipped == 0, 'preflight contains failed, skipped, cancelled or unexecuted tests')
        pass_all = True
    finally:
        after = source_snapshot(root)
        pass_all = pass_all and after['fingerprint'] == before['fingerprint'] and (not args.require_clean or after['clean'])
        report = {'schema': 1, 'pass': pass_all, 'source_sha': before['source_sha'],
                  'source_fingerprint': before['fingerprint'], 'after_fingerprint': after['fingerprint'],
                  'tests': tests, 'failures': failures, 'skipped': skipped,
                  'version': json.loads((root / 'package.json').read_text())['version'],
                  'architecture': os.environ.get('CASSQUERY_ARCH', 'arm64' if sys.platform == 'darwin' and os.uname().machine == 'arm64' else 'x64'),
                  'started_at': started, 'completed_at': time.time(), 'stages': stages,
                  'clean': before['clean'] and after['clean']}
        output.write_text(json.dumps(report, indent=2) + '\n')
    require(pass_all, 'source changed during preflight')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
