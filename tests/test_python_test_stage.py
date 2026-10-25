"""Exactly-once isolated Python shards and owned-group cleanup controls."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import preflight
import python_test_stage as stage


def protocol():
    ids = ['test_a.A.test_one', 'test_b.B.test_two']
    manifest = {'schema': 1, 'root': '/protocol-only', 'source_fingerprint': 'a' * 64,
                'files': ['tests/test_a.py', 'tests/test_b.py'], 'expected_ids': ids,
                'shards': [{'modules': ['test_a'], 'ids': ids[:1]}, {'modules': ['test_b'], 'ids': ids[1:]}]}
    reports = [{'schema': 1, 'index': index, 'pid': index + 100,
                'source_fingerprint': 'a' * 64, 'after_fingerprint': 'a' * 64,
                'assigned_ids': shard['ids'], 'records': [{'id': shard['ids'][0], 'status': 'success', 'duration_s': .1}],
                'tests': 1, 'collected': 1, 'failures': 0, 'skipped': 0,
                'details': {'failures': 0, 'errors': 0, 'unexpected successes': 0, 'skipped': 0, 'expected failures': 0}}
               for index, shard in enumerate(manifest['shards'])]
    return manifest, reports


def alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False


class ShardProtocolTests(unittest.TestCase):
    def test_heavy_modules_are_separate_and_all_ids_execute_once(self):
        groups = {'test_release_publish': ['p' + str(i) for i in range(52)],
                  'test_release_evidence': ['e' + str(i) for i in range(21)],
                  'test_worker': ['w' + str(i) for i in range(57)], 'test_other': ['o' + str(i) for i in range(40)]}
        first = stage.partition(groups); second = stage.partition(dict(reversed(list(groups.items()))))
        self.assertEqual(first, second)
        self.assertIn('test_release_publish', first[0]['modules'])
        self.assertIn('test_release_evidence', first[1]['modules'])
        self.assertEqual(sum(len(shard['ids']) for shard in first), 170)
        self.assertEqual(len({item for shard in first for item in shard['ids']}), 170)

    def test_missing_duplicate_foreign_and_truncated_worker_records_fail(self):
        manifest, reports = protocol()
        stage.aggregate(manifest, reports)
        for mutation in (lambda r: r.pop(), lambda r: r[0]['records'].clear(),
                         lambda r: r[1]['records'][0].update(id='test_a.A.test_one'),
                         lambda r: r[0]['records'].append(copy.deepcopy(r[0]['records'][0])),
                         lambda r: r[0].pop('details'), lambda r: r[1].update(pid=100),
                         lambda r: r[1].update(index=True)):
            changed = copy.deepcopy(reports); mutation(changed)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                stage.aggregate(manifest, changed)

    def test_outcomes_cannot_be_forged_green_or_nonfinite(self):
        manifest, reports = protocol()
        for status in ('failure', 'error', 'skipped', 'expected failure', 'unexpected success'):
            changed = copy.deepcopy(reports); changed[0]['records'][0]['status'] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                stage.aggregate(manifest, changed)
        for value in (float('nan'), float('inf'), True, -.1):
            changed = copy.deepcopy(reports); changed[0]['records'][0]['duration_s'] = value
            with self.assertRaises(ValueError): stage.aggregate(manifest, changed)
        changed = copy.deepcopy(reports); changed[0]['records'][0]['status'] = 'failure'
        changed[0]['details']['failures'] = changed[0]['failures'] = 3
        totals, _, _ = stage.aggregate(manifest, changed)
        self.assertEqual(totals['failures'], 3)  # Multiple failing subtests are permitted, never hidden.

    def test_source_and_manifest_collection_must_match(self):
        manifest, reports = protocol()
        changed = copy.deepcopy(reports); changed[1]['after_fingerprint'] = 'b' * 64
        with self.assertRaises(ValueError): stage.aggregate(manifest, changed)
        for mutation in (lambda m: m.update(schema=True), lambda m: m['expected_ids'].pop(),
                         lambda m: m['shards'][1]['modules'].append('test_a')):
            changed = copy.deepcopy(manifest); mutation(changed)
            with self.assertRaises(ValueError): stage.aggregate(changed, reports)

    def test_legacy_summary_is_single_and_parallel_ids_are_validated(self):
        counts = {'schema': 1, 'tests': 2, 'collected': 2, 'failures': 0, 'skipped': 0,
                  'files': ['tests/test_a.py', 'tests/test_b.py'], 'expected_ids': ['a', 'b'], 'test_ids': ['a', 'b'],
                  'source_fingerprint': 'a' * 64, 'after_fingerprint': 'a' * 64}
        text = '[worker 0] Ran 1 test in 0.1s\n[worker 0] OK\n[worker 1] Ran 1 test in 0.1s\n[worker 1] OK\nRan 2 tests in 0.2s\nOK\n'
        self.assertEqual(preflight.python_counts(text, counts)['tests'], 2)
        counts['test_ids'] = ['a', 'a']
        with self.assertRaises(ValueError): preflight.python_counts(text, counts)


class IsolatedWorkerTests(unittest.TestCase):
    def fixture(self, root):
        from test_preflight import make_repo
        root.mkdir(); make_repo(root)

    def run_stage(self, root, output):
        return subprocess.run([sys.executable, str(root / 'tools/python_test_stage.py'), str(root), str(output)],
                              cwd=root, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=20)

    def test_two_processes_overlap_and_every_discovered_id_runs_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve(); root = base / 'repo'; self.fixture(root)
            for name, other in (('a', 'b'), ('b', 'a')):
                source = ('import unittest,os,time,json\nfrom pathlib import Path\n'
                          f'F=Path({str(base)!r})\nclass Case(unittest.TestCase):\n'
                          '    def test_value(self):\n'
                          f'        (F/{(name+".json")!r}).write_text(json.dumps({{"pid":os.getpid()}}))\n'
                          '        end=time.monotonic()+5\n'
                          f'        while not (F/{(other+".json")!r}).exists() and time.monotonic()<end: time.sleep(.01)\n'
                          f'        self.assertTrue((F/{(other+".json")!r}).exists())\n')
                (root / f'tests/test_{name}.py').write_text(source)
            (root / 'tests/test_probe.py').unlink()
            subprocess.run(['git', '-C', str(root), 'add', '--all', 'tests'], check=True)
            result = self.run_stage(root, base / 'counts.json')
            self.assertEqual(result.returncode, 0, result.stderr)
            counts = json.loads((base / 'counts.json').read_text())
            self.assertEqual(counts['tests'], 2); self.assertEqual(counts['expected_ids'], counts['test_ids'])
            self.assertEqual(len({worker['pid'] for worker in counts['workers']}), 2)
            self.assertNotEqual(json.loads((base / 'a.json').read_text())['pid'], json.loads((base / 'b.json').read_text())['pid'])
            preflight.python_counts(result.stderr, counts)

    def test_worker_crash_leaves_no_qualified_counts_and_reaps_other_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve(); root = base / 'repo'; self.fixture(root)
            (root / 'tests/test_probe.py').write_text('import unittest,os,time\nclass Crash(unittest.TestCase):\n    def test_value(self): time.sleep(.3);os._exit(17)\n')
            (root / 'tests/test_sleeper.py').write_text('import unittest,os,time\nfrom pathlib import Path\nclass Sleep(unittest.TestCase):\n    def test_value(self):\n        Path(' + repr(str(base / 'sleep.pid')) + ').write_text(str(os.getpid()))\n        time.sleep(30)\n')
            result = self.run_stage(root, base / 'counts.json')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((base / 'counts.json').exists())
            if (base / 'sleep.pid').exists(): self.assertFalse(alive(int((base / 'sleep.pid').read_text())))


class OwnedGroupTests(unittest.TestCase):
    def test_timeout_and_failure_reap_only_the_owned_process_group(self):
        for outcome in ('timeout', 'failure'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); file = root / 'pids.json'
                program = ('import subprocess,sys,time,json,os\n'
                           'children=[subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]) for _ in range(2)]\n'
                           f'open({str(file)!r},"w").write(json.dumps([os.getpid()]+[p.pid for p in children]))\n'
                           + ('time.sleep(30)\n' if outcome == 'timeout' else 'sys.exit(1)\n'))
                foreign = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
                self.addCleanup(lambda process=foreign: (process.terminate(), process.wait()) if process.poll() is None else None)
                with (root / 'log').open('w') as stream:
                    if outcome == 'timeout':
                        with self.assertRaises(subprocess.TimeoutExpired):
                            preflight.run_python_stage([sys.executable, '-c', program], root, stream, 2)
                    else:
                        self.assertEqual(preflight.run_python_stage([sys.executable, '-c', program], root, stream, 2).returncode, 1)
                pids = json.loads(file.read_text()); deadline = time.monotonic() + 2
                while any(alive(pid) for pid in pids) and time.monotonic() < deadline: time.sleep(.02)
                self.assertFalse(any(alive(pid) for pid in pids), pids)
                self.assertTrue(alive(os.getpid()), 'cleanup must not kill the caller group')
                self.assertTrue(alive(foreign.pid), 'cleanup must not kill an unrelated control process')

    def test_cancelled_supervisor_reaps_its_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); file = root / 'pids.json'
            inner = ('import subprocess,sys,time,json,os\n'
                     'c=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\n'
                     f'open({str(file)!r},"w").write(json.dumps([os.getpid(),c.pid]))\n'
                     'time.sleep(30)\n')
            script = f'import sys;sys.path.insert(0,{str(ROOT / "tools")!r});import preflight;f=open({str(root / "log")!r},"w");preflight.run_python_stage([sys.executable,"-c",{inner!r}],{str(root)!r},f,10)'
            process = subprocess.Popen([sys.executable, '-c', script], stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 3
                while not file.exists() and time.monotonic() < deadline: time.sleep(.02)
                self.assertTrue(file.exists()); pids = json.loads(file.read_text())
                process.send_signal(signal.SIGTERM); process.wait(timeout=4)
                deadline = time.monotonic() + 2
                while any(alive(pid) for pid in pids) and time.monotonic() < deadline: time.sleep(.02)
                self.assertFalse(any(alive(pid) for pid in pids))
            finally:
                if process.poll() is None: process.kill(); process.wait()


if __name__ == '__main__': unittest.main()
