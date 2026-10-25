#!/usr/bin/env python3
"""Run exhaustive source-bound unittest collection in exactly two isolated processes."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import unittest

WORKERS = 2
WAIT_TIMEOUT = 880  # Includes discovery/startup; leaves cleanup inside the outer900s cap.


def python_sources(root, pinned):
    """Check the small tests tree before unittest can import package/test source."""
    from release_evidence import require
    base = root / 'tests'
    require(base.is_dir() and not base.is_symlink(), 'Python tests directory missing or symlinked')
    sources, entries = [], 0
    for directory, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != '__pycache__')
        entries += len(dirs) + len(files)
        require(entries <= 10000, 'Python test discovery exceeds10000entries')
        require(all(not (Path(directory) / name).is_symlink() for name in dirs), 'Python test directory symlink')
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            file = Path(directory) / name
            relative = file.relative_to(root).as_posix()
            require(not file.is_symlink() and relative in pinned, f'Unpinned Python test source: {relative}')
            sources.append(relative)
    require(any(Path(name).name.startswith('test_') for name in sources), 'Python test collection is empty')
    return sorted(sources)


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def partition(groups):
    """Deterministic count balance; keep the two heavy admission modules apart."""
    remaining = dict(groups)
    shards, sizes = [{}, {}], [0, 0]
    for index, module in enumerate(('test_release_publish', 'test_release_evidence')):
        if module in remaining:
            shards[index][module] = remaining.pop(module)
            sizes[index] += len(shards[index][module])
    for module, ids in sorted(remaining.items(), key=lambda item: (-len(item[1]), item[0])):
        index = min(range(WORKERS), key=lambda index: (sizes[index], index))
        shards[index][module] = ids
        sizes[index] += len(ids)
    return [{'modules': sorted(shard), 'ids': sorted(test for ids in shard.values() for test in ids)} for shard in shards]


def atomic_json(path, value):
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.active = None

    def startTest(self, test):
        super().startTest(test)
        self.active = {'id': test.id(), 'status': 'incomplete', 'started': time.monotonic()}
        self.records.append(self.active)

    def stopTest(self, test):
        self.active['duration_s'] = time.monotonic() - self.active.pop('started')
        super().stopTest(test)
        self.active = None

    def addSuccess(self, test):
        super().addSuccess(test); self.active['status'] = 'success'

    def addFailure(self, test, err):
        super().addFailure(test, err)
        if self.active is not None: self.active['status'] = 'failure'

    def addError(self, test, err):
        super().addError(test, err)
        if self.active is not None: self.active['status'] = 'error'

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if self.active is not None: self.active['status'] = 'skipped'

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err); self.active['status'] = 'expected failure'

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test); self.active['status'] = 'unexpected success'

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None: self.active['status'] = 'failure'


def read_protocol(path):
    from release_evidence import require
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 16 * 1024 * 1024,
            'Python worker protocol is missing, redirected or oversized')
    with path.open('rb') as file:
        data = file.read(16 * 1024 * 1024 + 1)
    require(len(data) <= 16 * 1024 * 1024, 'Python worker protocol exceeds limit')
    return json.loads(data)


def validate_manifest(manifest):
    from release_evidence import require
    require(isinstance(manifest, dict) and set(manifest) == {'schema', 'root', 'source_fingerprint', 'files', 'expected_ids', 'shards'}
            and type(manifest['schema']) is int and manifest['schema'] == 1
            and isinstance(manifest['root'], str) and Path(manifest['root']).is_absolute()
            and isinstance(manifest['source_fingerprint'], str) and re.fullmatch(r'[a-f0-9]{64}', manifest['source_fingerprint']), 'invalid Python manifest identity')
    for key in ('files', 'expected_ids'):
        values = manifest[key]
        require(isinstance(values, list) and values and all(isinstance(value, str) and value for value in values)
                and values == sorted(set(values)), 'invalid Python manifest collection')
    shards = manifest['shards']
    require(isinstance(shards, list) and len(shards) == WORKERS and all(isinstance(shard, dict)
            and set(shard) == {'modules', 'ids'} and all(isinstance(shard[key], list)
            and all(isinstance(item, str) and item for item in shard[key])
            and shard[key] == sorted(set(shard[key])) for key in ('modules', 'ids')) for shard in shards), 'invalid Python shard manifest')
    ids = [test for shard in shards for test in shard['ids']]
    modules = [module for shard in shards for module in shard['modules']]
    require(sorted(ids) == manifest['expected_ids'] and len(ids) == len(set(ids))
            and len(modules) == len(set(modules)), 'Python manifest shards overlap or omit discovery')


def worker(root, output, manifest_path, index):
    from release_evidence import require, source_snapshot
    manifest = read_protocol(manifest_path)
    validate_manifest(manifest)
    require(manifest['root'] == str(root), 'invalid worker root')
    expected = manifest['shards'][index]
    before = source_snapshot(root)
    require(before['fingerprint'] == manifest['source_fingerprint'], 'source changed before Python shard')
    sources = python_sources(root, {item['path'] for item in before['files']})
    require(sources == manifest['files'], 'Python shard source collection changed')
    allowed = {Path(name).relative_to('tests').with_suffix('').as_posix().replace('/', '.') for name in sources}
    require(all(module in allowed for module in expected['modules']), 'Python shard module is not pinned test source')
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(expected['modules'])
    actual = sorted(test.id() for test in cases(suite))
    require(not loader.errors and actual == expected['ids'] and len(actual) == len(set(actual)), 'Python shard discovery differs from parent')
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    after = source_snapshot(root)
    details = {'failures': len(result.failures), 'errors': len(result.errors),
               'unexpected successes': len(result.unexpectedSuccesses), 'skipped': len(result.skipped),
               'expected failures': len(result.expectedFailures)}
    report = {'schema': 1, 'index': index, 'pid': os.getpid(), 'source_fingerprint': before['fingerprint'],
              'after_fingerprint': after['fingerprint'], 'assigned_ids': expected['ids'], 'records': result.records,
              'tests': result.testsRun, 'collected': len(actual), 'details': details,
              'failures': details['failures'] + details['errors'] + details['unexpected successes'],
              'skipped': details['skipped'] + details['expected failures']}
    atomic_json(output, report)
    return 0 if result.wasSuccessful() and report['tests'] == len(actual) and report['skipped'] == 0 and before['fingerprint'] == after['fingerprint'] else 1


def aggregate(manifest, reports):
    from release_evidence import require
    validate_manifest(manifest)
    require(len(reports) == WORKERS, 'missing Python shard report')
    ids, totals = [], {'tests': 0, 'failures': 0, 'skipped': 0}
    details = {key: 0 for key in ('failures', 'errors', 'unexpected successes', 'skipped', 'expected failures')}
    pids = []
    for index, report in enumerate(reports):
        require(type(report.get('schema')) is int and report['schema'] == 1 and type(report.get('index')) is int
                and report['index'] == index and type(report.get('pid')) is int and report['pid'] > 0, 'invalid Python shard identity')
        pids.append(report['pid'])
        require(report.get('source_fingerprint') == report.get('after_fingerprint') == manifest['source_fingerprint'], 'Python shard source changed')
        expected = manifest['shards'][index]['ids']
        require(report.get('assigned_ids') == expected and type(report.get('collected')) is int and report['collected'] == len(expected), 'Python shard assignment changed')
        records = report.get('records')
        require(isinstance(records, list) and all(isinstance(record, dict) and isinstance(record.get('id'), str)
                and record.get('status') in ('success', 'failure', 'error', 'skipped', 'expected failure', 'unexpected success')
                and type(record.get('duration_s')) in (int, float) and math.isfinite(record['duration_s'])
                and record['duration_s'] >= 0 for record in records), 'incomplete Python test outcomes')
        executed = [record['id'] for record in records]
        require(sorted(executed) == expected and len(executed) == len(set(executed)), 'missing/duplicate/foreign Python execution')
        require(all(type(report.get(key)) is int and report[key] >= 0 for key in totals)
                and report['tests'] == len(executed), 'invalid Python shard counts')
        values = report.get('details', {})
        require(set(values) == set(details) and all(type(value) is int and value >= 0 for value in values.values())
                and report['failures'] == sum(values[key] for key in ('failures', 'errors', 'unexpected successes'))
                and report['skipped'] == values['skipped'] + values['expected failures']
                and report['failures'] >= sum(record['status'] in ('failure', 'error', 'unexpected success') for record in records)
                and report['skipped'] >= sum(record['status'] in ('skipped', 'expected failure') for record in records),
                'Python shard outcomes contradict counts')
        for key in totals: totals[key] += report[key]
        for key in details: details[key] += values[key]
        ids.extend(executed)
    require(len(set(pids)) == WORKERS and sorted(ids) == manifest['expected_ids'] and len(ids) == len(set(ids)), 'Python shards overlap or omit parent discovery')
    return totals, details, sorted(ids)


def stop_workers(processes):
    for process in processes:
        if process.poll() is None: process.terminate()
    for process in processes:
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=2)


def parent(root, output):
    from release_evidence import require, source_snapshot
    started = time.monotonic()
    output.unlink(missing_ok=True)
    before = source_snapshot(root)
    sources = python_sources(root, {item['path'] for item in before['files']})
    loader = unittest.TestLoader()
    suite = loader.discover(str(root / 'tests'), pattern='test_*.py', top_level_dir=str(root / 'tests'))
    groups = {}
    for test in cases(suite): groups.setdefault(test.__class__.__module__, []).append(test.id())
    ids = sorted(test for values in groups.values() for test in values)
    require(not loader.errors and ids and len(ids) == len(set(ids)), 'empty/error/duplicate Python discovery')
    manifest = {'schema': 1, 'root': str(root), 'source_fingerprint': before['fingerprint'],
                'files': sources, 'expected_ids': ids, 'shards': partition(groups)}
    folder = output.parent / 'python-shards'; folder.mkdir(exist_ok=True)
    manifest_path = folder / 'manifest.json'; atomic_json(manifest_path, manifest)
    processes, streams = [], []
    def interrupted(_signal, _frame): raise KeyboardInterrupt('Python stage cancelled')
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        for index in range(WORKERS):
            counts = folder / f'worker-{index}.json'; counts.unlink(missing_ok=True)
            stream = (folder / f'worker-{index}.log').open('w'); streams.append(stream)
            command = [sys.executable, str(Path(__file__).resolve()), str(root), str(counts), '--worker', str(index), '--manifest', str(manifest_path)]
            # Inherit the preflight-owned group; never detach workers from cleanup.
            processes.append(subprocess.Popen(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT))
        deadline = started + WAIT_TIMEOUT
        while any(process.poll() is None for process in processes):
            require(time.monotonic() < deadline, 'Python shard absolute deadline exceeded')
            require(all(process.returncode is None or process.returncode in (0, 1) for process in processes), 'Python shard terminated unexpectedly')
            time.sleep(.02)
        for stream in streams: stream.close()
        reports = [read_protocol(folder / f'worker-{index}.json') for index in range(WORKERS)]
        require(all(type(report.get('pid')) is int and report['pid'] == process.pid
                    for report, process in zip(reports, processes)), 'Python report PID differs from owned worker')
        totals, details, executed = aggregate(manifest, reports)
        after = source_snapshot(root)
        require(after['fingerprint'] == before['fingerprint'], 'source changed during Python execution')
        counts = {'schema': 1, **totals, 'collected': len(ids), 'files': sources, 'expected_ids': ids, 'test_ids': executed,
                  'source_fingerprint': before['fingerprint'], 'after_fingerprint': after['fingerprint'], 'workers': []}
        for index, process in enumerate(processes):
            log = folder / f'worker-{index}.log'; report_file = folder / f'worker-{index}.json'
            counts['workers'].append({'index': index, 'pid': process.pid, 'exit': process.returncode,
                                     'counts': {'path': report_file.relative_to(output.parent).as_posix(), 'sha256': hashlib.sha256(report_file.read_bytes()).hexdigest()},
                                     'log': {'path': log.relative_to(output.parent).as_posix(), 'sha256': hashlib.sha256(log.read_bytes()).hexdigest()}})
            for line in log.read_text().splitlines(): print(f'[worker {index}] {line}', file=sys.stderr)
        print(f'Ran {totals["tests"]} tests in {time.monotonic()-started:.3f}s', file=sys.stderr)
        summary = ', '.join(f'{key}={value}' for key, value in details.items() if value)
        print(('FAILED' if totals['failures'] else 'OK') + (f' ({summary})' if summary else ''), file=sys.stderr)
        atomic_json(output, counts)
        return 0 if all(process.returncode == 0 for process in processes) and totals['failures'] == totals['skipped'] == 0 else 1
    finally:
        stop_workers(processes)
        for stream in streams: stream.close()
        signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path); parser.add_argument('output', type=Path)
    parser.add_argument('--worker', type=int, choices=range(WORKERS), help=argparse.SUPPRESS)
    parser.add_argument('--manifest', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(); root = args.root.resolve()
    sys.path[:0] = [str(root / 'tools'), str(root / 'tests')]
    if args.worker is not None:
        if args.manifest is None:
            parser.error('--manifest required for worker')
        raise SystemExit(worker(root, args.output, args.manifest, args.worker))
    raise SystemExit(parent(root, args.output))


if __name__ == '__main__':
    main()
