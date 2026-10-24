#!/usr/bin/env python3
"""Run complete source preflight with source-bound, log-bound evidence."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from release_evidence import digest, read_json, relative_file, require, source_snapshot

STAGES = ('syntax', 'node', 'python', 'release-contract', 'brand-exports')
STAGE_TIMEOUT = 900  # Fixed absolute cap with headroom for measured slower native CI hosts.


def node_counts(text):
    require(len(re.findall(r'^TAP version 13$', text, re.MULTILINE)) == 1, 'missing/duplicate Node TAP header')
    values = {}
    for key in ('tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'):
        matches = re.findall(r'^# ' + key + r' (\d+)$', text, re.MULTILINE)
        require(len(matches) == 1, f'missing/duplicate Node {key} summary')
        values[key] = int(matches[0])
    require(values['tests'] == sum(values[key] for key in ('pass', 'fail', 'cancelled', 'skipped', 'todo')), 'Node outcome counts contradict total')
    return {'tests': values['tests'], 'failures': values['fail'] + values['cancelled'], 'skipped': values['skipped'] + values['todo']}


def python_counts(text, counts):
    require(type(counts.get('schema')) is int and counts['schema'] == 1, 'invalid Python counts schema')
    require(all(type(counts.get(key)) is int and counts[key] >= 0 for key in ('tests', 'failures', 'skipped', 'collected'))
            and counts['tests'] == counts['collected'], 'Python counts are invalid or collection did not all execute')
    summaries = re.findall(r'^Ran (\d+) tests? in ([0-9]+(?:\.[0-9]+)?)s$', text, re.MULTILINE)
    require(len(summaries) == 1 and int(summaries[0][0]) == counts['tests'] and math.isfinite(float(summaries[0][1])), 'Python log count missing, duplicate or contradictory')
    endings = re.findall(r'^(OK|FAILED)(?: \(([^()]*)\))?$', text, re.MULTILINE)
    require(len(endings) == 1, 'missing/duplicate Python outcome summary')
    outcome, details = endings[0]
    values = {}
    for item in details.split(', ') if details else []:
        match = re.fullmatch(r'(failures|errors|skipped|expected failures|unexpected successes)=(\d+)', item)
        require(match and match[1] not in values, 'invalid/duplicate Python outcome count')
        values[match[1]] = int(match[2])
    failures = sum(values.get(key, 0) for key in ('failures', 'errors', 'unexpected successes'))
    skipped = sum(values.get(key, 0) for key in ('skipped', 'expected failures'))
    require(failures == counts['failures'] and skipped == counts['skipped'] and (outcome == 'FAILED') == (failures > 0), 'Python log outcomes contradict structured counts')
    files = counts.get('files')
    require(isinstance(files, list) and files and all(isinstance(name, str) and name.startswith('tests/') and name.endswith('.py')
            and '..' not in Path(name).parts for name in files) and files == sorted(set(files)), 'invalid Python source collection')
    return {key: counts[key] for key in ('tests', 'failures', 'skipped')}


def positive_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate_preflight(report, directory, pinned=None):
    """Validate physical stage evidence; pinned is an optional set of source paths."""
    require(type(report.get('schema')) is int and report['schema'] == 1 and report.get('pass') is True, 'preflight failed or invalid schema')
    start, end = report.get('started_at'), report.get('completed_at')
    require(positive_time(start) and positive_time(end) and end > start, 'invalid preflight checkpoint times')
    stages = report.get('stages')
    require(isinstance(stages, list) and all(isinstance(stage, dict) for stage in stages)
            and [stage.get('name') for stage in stages] == list(STAGES), 'preflight stage evidence incomplete or reordered')
    totals = {'tests': 0, 'failures': 0, 'skipped': 0}
    previous = start
    for stage in stages:
        name = stage['name']
        begin, finish, duration = stage.get('started_at'), stage.get('completed_at'), stage.get('duration_s')
        require(positive_time(begin) and positive_time(finish) and positive_time(duration)
                and previous <= begin < finish <= end and math.isclose(finish - begin, duration, rel_tol=1e-6, abs_tol=1e-6), f'invalid {name} checkpoint times')
        previous = finish
        require(type(stage.get('exit')) is int and stage['exit'] == 0 and not stage.get('error')
                and stage.get('path') == name + '.log', f'{name} stage not successful')
        log = relative_file(directory, stage['path'])
        require(digest(log) == stage.get('sha256'), f'{name} log evidence missing or changed')
        if name == 'node':
            actual = node_counts(log.read_text())
            files = stage.get('files')
            require(isinstance(files, list) and files and all(isinstance(file, str) and Path(file).parent.as_posix() == 'tests'
                    and file.endswith('.cjs') for file in files) and files == sorted(set(files)), 'invalid Node source collection')
            if pinned is not None:
                require(set(files) == {file for file in pinned if Path(file).parent.as_posix() == 'tests' and file.endswith('.cjs')}, 'Node collection differs from pinned source')
        elif name == 'python':
            record = stage.get('counts', {})
            require(isinstance(record, dict) and record.get('path') == 'python-counts.json', 'missing Python counts evidence')
            file = relative_file(directory, record['path'])
            require(digest(file) == record.get('sha256'), 'Python counts evidence changed')
            counts = read_json(file)
            actual = python_counts(log.read_text(), counts)
            if pinned is not None:
                expected = {file for file in pinned if file.startswith('tests/') and file.endswith('.py') and '__pycache__' not in Path(file).parts}
                require(set(counts['files']) == expected, 'Python collection differs from pinned source')
        else:
            continue
        require(actual['tests'] > 0 and actual['failures'] == actual['skipped'] == 0, f'{name} tests failed, skipped or unexecuted')
        for key, value in actual.items():
            require(type(stage.get(key)) is int and stage[key] == value, f'{name} count contradicts log evidence')
            totals[key] += value
    require(all(type(report.get(key)) is int and report[key] == value for key, value in totals.items()), 'preflight totals contradict executed stages')
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('evidence/preflight.json'))
    parser.add_argument('--require-clean', action='store_true')
    parser.add_argument('--source-before', type=Path)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'pass': False, 'error': 'preflight not completed', 'started_at': time.time(),
              'stages': [], 'tests': 0, 'failures': 0, 'skipped': 0}
    output.write_text(json.dumps(report) + '\n')

    def run_stage(name, command):
        log = output.parent / (name + '.log')
        stage = {'name': name, 'exit': None, 'path': log.name, 'started_at': time.time()}
        report['stages'].append(stage)
        try:
            with log.open('w') as stream:
                result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, timeout=STAGE_TIMEOUT)
            stage['exit'] = result.returncode
        except BaseException as error:
            stage['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            stage['completed_at'] = time.time()
            stage['duration_s'] = stage['completed_at'] - stage['started_at']
            if log.is_file():
                stage['sha256'] = digest(log)
        return stage

    def test_outcome(stage, counts):
        stage.update(counts)
        for key, value in counts.items():
            report[key] += value
        require(stage['exit'] == 0 and counts['tests'] > 0 and counts['failures'] == counts['skipped'] == 0,
                f'{stage["name"]} tests failed, skipped or unexecuted; see {stage["path"]}')

    try:
        before = source_snapshot(root)
        if args.require_clean:
            require(before['clean'], 'preflight requires clean committed source')
            require(os.environ.get('GITHUB_SHA', before['source_sha']) == before['source_sha'], 'preflight source differs from CI SHA')
        report.update(source_sha=before['source_sha'], source_fingerprint=before['fingerprint'], clean=before['clean'],
                      version=read_json(root / 'package.json')['version'],
                      architecture=os.environ.get('CASSQUERY_ARCH', 'arm64' if sys.platform == 'darwin' and os.uname().machine == 'arm64' else 'x64'))
        if args.source_before:
            require(read_json(args.source_before)['fingerprint'] == before['fingerprint'], 'source changed since initial snapshot')
        pinned = {item['path'] for item in before['files']}
        node_files = sorted(file.relative_to(root).as_posix() for file in (root / 'tests').glob('*.cjs'))
        require(node_files and all(file in pinned and not (root / file).is_symlink() for file in node_files), 'Node collection is empty or includes unpinned test files')
        from python_test_stage import python_sources
        python_sources(root, pinned)
        stage = run_stage('syntax', ['npm', 'run', 'check'])
        require(stage['exit'] == 0, 'syntax stage failed; see syntax.log')
        stage = run_stage('node', ['node', '--test', '--test-reporter=tap', *node_files])
        stage['files'] = node_files
        test_outcome(stage, node_counts((output.parent / 'node.log').read_text()))
        counts_file = output.parent / 'python-counts.json'
        counts_file.unlink(missing_ok=True)
        stage = run_stage('python', [sys.executable, 'tools/python_test_stage.py', str(root), str(counts_file)])
        require(counts_file.is_file(), f'Python stage did not produce complete counts (exit {stage["exit"]}); see python.log')
        stage['counts'] = {'path': counts_file.name, 'sha256': digest(counts_file)}
        test_outcome(stage, python_counts((output.parent / 'python.log').read_text(), read_json(counts_file)))
        for name, command in (('release-contract', [sys.executable, 'tools/check_release.py']),
                              ('brand-exports', [sys.executable, 'tools/make_brand_assets.py', '--check'])):
            stage = run_stage(name, command)
            require(stage['exit'] == 0, f'{name} stage failed; see {name}.log')
        after = source_snapshot(root)
        report.update(after_fingerprint=after['fingerprint'], clean=before['clean'] and after['clean'])
        require(after['fingerprint'] == before['fingerprint'] and (not args.require_clean or after['clean']), 'source changed during preflight')
        report['completed_at'] = time.time()
        report['pass'] = True
        report.pop('error', None)
        validate_preflight(report, output.parent, pinned=pinned)
    except BaseException as error:
        report['pass'] = False
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if 'source_sha' in report and 'after_fingerprint' not in report:
            try:
                after = source_snapshot(root)
                report['after_fingerprint'] = after['fingerprint']
                report['clean'] = report['clean'] and after['clean']
            except Exception as error:
                report['source_snapshot_error'] = f'{type(error).__name__}: {error}'
                report['pass'] = False
        report.setdefault('completed_at', time.time())
        output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
