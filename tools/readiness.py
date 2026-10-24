#!/usr/bin/env python3
"""Verify explicit checklist inputs without creating tags or publishing releases."""
import argparse
import json
from pathlib import Path

from release_evidence import read_json, require, source_snapshot, verify_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('pre-push', 'pre-release'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--preflight', type=Path, default=Path('evidence/preflight.json'))
    parser.add_argument('--run-json', type=Path)
    parser.add_argument('--jobs-json', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    snapshot = source_snapshot(root)
    report = read_json(args.preflight)
    require(report.get('schema') == 1 and report.get('pass') is True and report.get('source_sha') == snapshot['source_sha']
            and report.get('version') == json.loads((root / 'package.json').read_text())['version']
            and report.get('source_fingerprint') == snapshot['fingerprint']
            and report.get('after_fingerprint') == snapshot['fingerprint'], 'preflight is missing, failed or stale')
    require(type(report.get('failures')) is int and report['failures'] == 0
            and type(report.get('skipped')) is int and report['skipped'] == 0
            and type(report.get('tests')) is int and report['tests'] > 0,
            'required source tests did not all execute')
    stages = report.get('stages', [])
    require(len(stages) == 5 and {stage.get('name') for stage in stages} == {'syntax', 'node', 'python', 'release-contract', 'brand-exports'},
            'preflight stage evidence incomplete')
    from release_evidence import digest
    for stage in stages:
        require(stage.get('exit') == 0 and stage.get('path') == stage['name'] + '.log', 'preflight stage not successful')
        path = args.preflight.parent / stage['path']
        require(path.is_file() and digest(path) == stage.get('sha256'), 'preflight log evidence missing or changed')
    require((root / 'docs/PRE_PUSH.md').is_file() and (root / 'docs/PRE_RELEASE.md').is_file(), 'checklists missing')
    if args.phase == 'pre-release':
        require(snapshot['clean'], 'pre-release source must be a clean commit')
        require(args.run_json and args.jobs_json, 'pre-release requires actual candidate run and job evidence')
        run = read_json(args.run_json)
        value = read_json(args.jobs_json)
        verify_run(run, value['jobs'] if isinstance(value, dict) else value, snapshot['source_sha'])
    print(json.dumps({'schema': 1, 'pass': True, 'phase': args.phase, 'source_sha': snapshot['source_sha'],
                      'source_fingerprint': snapshot['fingerprint'], 'tests': report['tests'],
                      'not_checked': ['independent human review', 'native/artifact distribution approval',
                                      'tag creation and release publication authorization']}, indent=2))


if __name__ == '__main__':
    main()
