import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import preflight
import release_evidence


def make_repo(root):
    """Owned public source copy with real tools and tiny executed test cases."""
    snapshot = release_evidence.source_snapshot(ROOT)
    for item in snapshot['files']:
        if item['path'].startswith('tests/'):
            continue
        target = root / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / item['path'], target)
    (root / 'tests').mkdir()
    shutil.copyfile(ROOT / 'tests/test_renderer_config.cjs', root / 'tests/test_renderer_config.cjs')
    (root / 'tests/test_probe.py').write_text('import unittest\nclass Probe(unittest.TestCase):\n    def test_value(self): self.assertTrue(True)\n')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'bounded source fixture'], check=True)


def run_preflight(root, output, timeout=90, stage_timeout=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env.pop('GITHUB_SHA', None)
    env['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), str(Path(env.get('CASSQUERY_NODE') or shutil.which('node')).parent), env['PATH']))
    command = [sys.executable, str(root / 'tools/preflight.py')]
    if stage_timeout is not None:
        # Target the Python stage only: syntax checks legitimately exceed a tiny
        # shared budget as the application grows; this is not a syntax-timeout test.
        command = [sys.executable, '-c',
                   f'import sys;sys.path.insert(0,{str(root / "tools")!r});import preflight;'
                   f'original=preflight.run_python_stage;'
                   f'preflight.run_python_stage=lambda args,root,stream,timeout: '
                   f'original(args,root,stream,{stage_timeout});preflight.main()']
    return subprocess.run([*command, '--root', str(root), '--output', str(output)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=timeout, check=False)


class PreflightEvidenceTests(unittest.TestCase):
    def test_early_rejection_replaces_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'preflight.json'
            output.write_text('{"pass":true}')
            with mock.patch.object(sys, 'argv', ['preflight', '--root', str(ROOT), '--output', str(output), '--require-clean']), \
                 mock.patch.object(preflight, 'source_snapshot', return_value={'clean': False}), \
                 self.assertRaises(ValueError):
                preflight.main()
            self.assertFalse(json.loads(output.read_text())['pass'])

    def test_png_real_capture_and_corruption(self):
        spec = importlib.util.spec_from_file_location('png_reader', ROOT / 'tools/png_evidence.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        image = (ROOT / 'docs/viewer.png').read_bytes()
        self.assertEqual(module.dimensions(image), (1320, 900))
        for data in (image[:33], image[:-4], image[:100] + b'X' + image[101:]):
            with self.assertRaises(ValueError):
                module.dimensions(data)

    def test_real_tool_zero_unpinned_failure_skip_timeout_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'repo'; root.mkdir()
            make_repo(root)
            original_node = (root / 'tests/test_renderer_config.cjs').read_bytes()
            original_python = (root / 'tests/test_probe.py').read_text()
            cases = ('positive', 'zero-node', 'zero-python', 'ignored-nested', 'ignored-self-mutation', 'failure', 'skip', 'timeout')
            for case in cases:
                with self.subTest(case=case):
                    subprocess.run(['git', '-C', str(root), 'reset', '-q', '--mixed', 'HEAD'], check=True)
                    shutil.rmtree(root / 'tests'); (root / 'tests').mkdir()
                    (root / 'tests/test_renderer_config.cjs').write_bytes(original_node)
                    (root / 'tests/test_probe.py').write_text(original_python)
                    exclude = root / '.git/info/exclude'; exclude.write_text('')
                    if case == 'zero-node':
                        (root / 'tests/test_renderer_config.cjs').unlink()
                    elif case == 'zero-python':
                        (root / 'tests/test_probe.py').unlink()
                    elif case == 'ignored-nested':
                        nested = root / 'tests/nested'; nested.mkdir()
                        (nested / '__init__.py').write_text('')
                        (nested / 'test_hidden.py').write_text(original_python)
                        subprocess.run(['git', '-C', str(root), 'add', 'tests/nested/__init__.py'], check=True)
                        exclude.write_text('tests/nested/test_hidden.py\n')
                    elif case == 'ignored-self-mutation':
                        exclude.write_text('tests/test_ignored.py\n')
                        ignored = root / 'tests/test_ignored.py'
                        ignored.write_text('import unittest,pathlib\nclass Ignored(unittest.TestCase):\n    def test_mutate(self): pathlib.Path(__file__).write_text("changed")\n')
                    elif case in ('failure', 'skip', 'timeout'):
                        body = {'failure': 'self.fail("executed failure")', 'skip': 'self.skipTest("executed skip")',
                                'timeout': '__import__("time").sleep(30)'}[case]
                        (root / 'tests/test_probe.py').write_text('import unittest\nclass Probe(unittest.TestCase):\n    def test_value(self): ' + body + '\n')
                    output = base / case / 'preflight.json'
                    result = run_preflight(root, output, stage_timeout=2 if case == 'timeout' else None)
                    report = release_evidence.read_json(output)
                    if case == 'positive':
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue(report['pass'])
                        self.assertEqual([stage['name'] for stage in report['stages']], list(preflight.STAGES))
                        # The borrowed production Node file can gain cases; bind the total to executed evidence.
                        node_counts = preflight.node_counts((output.parent / 'node.log').read_text())
                        python_counts = release_evidence.read_json(output.parent / 'python-counts.json')
                        self.assertGreater(node_counts['tests'], 0)
                        self.assertEqual(python_counts['files'], ['tests/test_probe.py'])
                        self.assertEqual(python_counts['tests'], python_counts['collected'])
                        self.assertEqual(python_counts['tests'], 1)
                        self.assertEqual(report['tests'], node_counts['tests'] + python_counts['tests'])
                        preflight.validate_preflight(report, output.parent, pinned={item['path'] for item in release_evidence.source_snapshot(root)['files']})
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIs(report['pass'], False)
                        self.assertTrue(report['error'])
                        self.assertNotIn('brand-exports', [stage['name'] for stage in report['stages']])
                    if case == 'ignored-self-mutation':
                        self.assertNotEqual(ignored.read_text(), 'changed', 'unpinned source executed before rejection')
                    if case == 'failure':
                        self.assertEqual(report['failures'], 1)
                    if case == 'skip':
                        self.assertEqual(report['skipped'], 1)
                    if case == 'timeout':
                        stage = report['stages'][-1]
                        self.assertEqual(stage['name'], 'python')
                        self.assertIn('TimeoutExpired', stage['error'])
                        self.assertGreater(stage['duration_s'], 0)
                        self.assertTrue(stage['sha256'])

    def test_nested_collected_package_runs_in_real_python_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / 'repo'; root.mkdir(); make_repo(root)
            nested = root / 'tests/nested'; nested.mkdir()
            (nested / '__init__.py').write_text('')
            (nested / 'test_nested.py').write_text('import unittest\nclass Nested(unittest.TestCase):\n    def test_value(self): self.assertEqual(2,2)\n')
            subprocess.run(['git', '-C', str(root), 'add', 'tests/nested'], check=True)
            output = base / 'counts.json'
            result = subprocess.run([sys.executable, str(root / 'tools/python_test_stage.py'), str(root), str(output)],
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            counts = release_evidence.read_json(output)
            self.assertEqual(counts['tests'], counts['collected'])
            self.assertEqual(counts['tests'], 2)
            self.assertIn('tests/nested/test_nested.py', counts['files'])
            preflight.python_counts(result.stderr, counts)
