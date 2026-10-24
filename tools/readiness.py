#!/usr/bin/env python3
"""Verify explicit checklist inputs without creating tags or publishing releases."""
import argparse
import json
from pathlib import Path

from preflight import validate_preflight
from release_evidence import read_json, require, source_snapshot, verify_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('pre-push', 'pre-release'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--preflight', type=Path, default=Path('evidence/preflight.json'))
    parser.add_argument('--run-json', type=Path)
    parser.add_argument('--jobs-json', type=Path)
    parser.add_argument('--integration-run-json', type=Path)
    parser.add_argument('--integration-jobs-json', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    snapshot = source_snapshot(root)
    report = read_json(args.preflight)
    require(report.get('source_sha') == snapshot['source_sha']
            and report.get('version') == read_json(root / 'package.json')['version']
            and report.get('source_fingerprint') == snapshot['fingerprint']
            and report.get('after_fingerprint') == snapshot['fingerprint'], 'preflight is missing, failed or stale')
    validate_preflight(report, args.preflight.parent, pinned={item['path'] for item in snapshot['files']})
    require((root / 'docs/PRE_PUSH.md').is_file() and (root / 'docs/PRE_RELEASE.md').is_file(), 'checklists missing')
    if args.phase == 'pre-release':
        require(snapshot['clean'], 'pre-release source must be a clean commit')
        require(args.run_json and args.jobs_json and args.integration_run_json and args.integration_jobs_json,
                'pre-release requires actual candidate and source integration run/job evidence')
        from release_evidence import verify_integration_run
        verify_run(read_json(args.run_json), read_json(args.jobs_json), snapshot['source_sha'])
        verify_integration_run(read_json(args.integration_run_json), read_json(args.integration_jobs_json), snapshot['source_sha'])
    print(json.dumps({'schema': 1, 'pass': True, 'phase': args.phase, 'source_sha': snapshot['source_sha'],
                      'source_fingerprint': snapshot['fingerprint'], 'tests': report['tests'],
                      'not_checked': ['independent human review', 'native/artifact distribution approval',
                                      'tag creation and release publication authorization']}, indent=2))


if __name__ == '__main__':
    main()
