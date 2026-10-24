"""Native evidence boundary regressions; no app/keychain/process operations."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / 'package.json').read_text())['version']
spec = importlib.util.spec_from_file_location('assurance_macos', ROOT / 'tools/macos_smoke.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SmokeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='cq-schema-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.app = self.root / 'CassQuery.app'
        self.resources = self.app / 'Contents/Resources'
        self.started = {'wall_time_ms': 1000, 'monotonic_ms': 1000}
        self.finished = {'wall_time_ms': 5000, 'monotonic_ms': 5000}
        names = ['renderer-start', 'certified-ready', 'production-controls', 'native-matrix',
                 'save-certified', 'failed-gate-retained', 'renderer-complete', 'worker-shutdown']
        checkpoints = [{'name': name, 'wall_time_ms': 1200 + i * 200, 'monotonic_ms': 1200 + i * 200} for i, name in enumerate(names)]
        images, matrix = [], []
        for theme in ('dark', 'light'):
            for width in (450, 700, 1000, 1320):
                for state in ('assembled', 'apart'):
                    file = self.output / f'{theme}-{width}-{state}.png'
                    Image.new('RGB', (width, 700), (30 if theme == 'dark' else 200, 40, 80 if state == 'assembled' else 100)).save(file)
                    image = module.image_evidence(file)
                    images.append(image)
                    matrix.append({'theme': theme, 'realized_theme': theme, 'background': theme,
                        'width': width, 'state': state, 'status': 'checked', 'viewport': [width, 700],
                        'full_size': [width, 700], 'scroll_regions': [], 'vertical_scroll': False,
                        'vertices': 30, 'meshes': 3, 'controls_checked': 15, 'clipped': 0, 'nonfinite': 0,
                        'overflow': False, 'bad': [], 'overlaps': [], 'errors': [], 'diag': '', 'tier': 'certified',
                        'gates_pass': True, 'step_pressed': ['true', 'false'] if state == 'assembled' else ['false', 'true'],
                        'explode': '0%' if state == 'assembled' else '100%',
                        'canvas': {'width': width, 'height': 600, 'client_width': width, 'client_height': 600}, 'image': image})
        for name in ('assembled', 'apart', 'save-certified', 'gate-failed'):
            file = self.output / f'{name}.png'
            Image.new('RGB', (1320, 700), (50, 60, 70)).save(file)
            images.append(module.image_evidence(file))
        self.report = {'schema': 1, 'pass': True, 'challenge': 'current-run', 'app_pid': 12345,
            'worker_pid': 54321, 'packaged': True, 'bundled_worker': True, 'worker_shutdown': True,
            'version': VERSION, 'architecture': 'arm64', 'source_sha': None, 'resources_path': str(self.resources),
            'worker_executable': str(self.resources / 'worker/cassquery-worker'), 'errors': [],
            'started': {'wall_time_ms': 1100, 'monotonic_ms': 1100}, 'checkpoints': checkpoints,
            'finished': {'wall_time_ms': 4000, 'monotonic_ms': 4000}, 'checks': list(module.CHECKS),
            'lifecycle': {'pass': True, 'secondary_pid': 22222, 'secondary_exit': 0, 'second_instance_events': 1,
                          'close_reopen': True, 'worker_pid_before': 54321, 'worker_pid_after': 54321},
            'matrix': matrix, 'images': images,
            'screenshots': [str(self.output / 'assembled.png'), str(self.output / 'apart.png')]}

    def validate(self, report=None, bundled=False):
        with patch.dict(os.environ, {}, clear=True):
            return module.validate_renderer(report or self.report, self.output, 'current-run', 12345,
                self.app, VERSION, 'arm64', self.started, self.finished, bundled=bundled)

    def test_fresh_complete_evidence_passes(self):
        self.validate()

    def test_identity_times_frozen_lifecycle_fail_closed(self):
        mutations = [lambda r: r.pop('challenge'), lambda r: r.update(challenge='prior-run'),
            lambda r: r.update(app_pid=12346), lambda r: r.update(worker_pid=True),
            lambda r: r.update(packaged=False), lambda r: r.update(bundled_worker=False),
            lambda r: r.update(worker_shutdown=False), lambda r: r.update(version='wrong'),
            lambda r: r.update(architecture='x64'), lambda r: r.update(source_sha='old'),
            lambda r: r['checkpoints'][0].update(monotonic_ms=0),
            lambda r: r['checkpoints'][1].update(monotonic_ms=1200),
            lambda r: r['checkpoints'][1].update(wall_time_ms=100000),
            lambda r: r['checkpoints'].pop(), lambda r: r['checks'].pop(),
            lambda r: r['lifecycle'].update(worker_pid_after=54322),
            lambda r: r['errors'].append('render crashed')]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                report = copy.deepcopy(self.report); mutation(report); self.validate(report)

    def test_zero_skipped_clipped_or_error_matrix_fails(self):
        changes = [{'vertices': 0}, {'meshes': 0}, {'controls_checked': 0}, {'status': 'skipped'},
            {'clipped': 1}, {'nonfinite': 1}, {'overflow': True}, {'bad': [{'category': 'text-clipped'}]},
            {'overlaps': [['caption', 'steps']]}, {'errors': ['pageerror']}, {'diag': 'worker failed'},
            {'viewport': [100, 100]}, {'realized_theme': 'wrong'}, {'tier': 'preview'},
            {'step_pressed': ['false', 'false']}, {'canvas': {'width': 0}}, {'gates_pass': False}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                report = copy.deepcopy(self.report); report['matrix'][0].update(change); self.validate(report)
        for matrix in ([], self.report['matrix'][:-1], self.report['matrix'][:-1] + [self.report['matrix'][0]]):
            with self.assertRaises(RuntimeError):
                module.validate_matrix(matrix)

    def test_scroll_requires_complete_document_and_supplemental_control_evidence(self):
        report = copy.deepcopy(self.report)
        report['matrix'][0]['full_size'][1] = 900
        with self.assertRaises(RuntimeError):
            self.validate(report)
        report = copy.deepcopy(self.report)
        report['matrix'][0]['scroll_regions'] = [{'id': 'ASIDE', 'client_height': 500, 'scroll_height': 700}]
        with self.assertRaises(RuntimeError):
            self.validate(report)
        report = copy.deepcopy(self.report)
        report['matrix'][0].pop('full_size')
        with self.assertRaises(RuntimeError):
            self.validate(report)

    def test_missing_stale_duplicate_tampered_image_fails(self):
        for mutation in (lambda r: r.update(screenshots=[]),
                         lambda r: r['screenshots'].__setitem__(1, r['screenshots'][0]),
                         lambda r: r['images'][0].update(sha256='0' * 64),
                         lambda r: r['images'][0].update(width=1),
                         lambda r: r['images'][1].update(path=r['images'][0]['path'])):
            with self.assertRaises(RuntimeError):
                report = copy.deepcopy(self.report); mutation(report); self.validate(report)
        image = Path(self.report['images'][0]['path'])
        image.unlink()
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_bundled_default_rejects_external_project(self):
        report = copy.deepcopy(self.report)
        report['checks'] = ['bundled default certified demo']
        report['images'] = report['images'][:-2]
        report['worker_status'] = {'api_version': 2, 'load_errors': [], 'projects': [{'name': 'TESTPLATE', 'root': str(self.resources / 'worker/_internal/testpart')}]}
        self.validate(report, bundled=True)
        report['worker_status']['projects'][0]['root'] = '/external/demo'
        with self.assertRaises(RuntimeError):
            self.validate(report, bundled=True)

    def test_runtime_checkpoint_not_just_expected_frozen_flag(self):
        executable = self.resources / 'worker/cassquery-worker'
        proof = {'schema': 1, 'pid': 54321, 'challenge': 'current-run', 'frozen': True,
                 'executable': str(executable), 'resource_root': str(executable.parent / '_internal'),
                 'wall_time_ms': 2000, 'monotonic_ms': 2000}
        module.validate_worker_proof(proof, 54321, 'current-run', self.started, self.finished, frozen=True, executable=executable)
        for change in ({'frozen': False}, {'pid': 1}, {'challenge': 'old'}, {'monotonic_ms': 0},
                       {'resource_root': '/external'}, {'executable': '/usr/bin/python3'}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                module.validate_worker_proof({**proof, **change}, 54321, 'current-run', self.started, self.finished, frozen=True, executable=executable)

    def test_early_exit_with_orphan_is_cleaned_on_missing_or_invalid_report(self):
        import subprocess
        import time
        for bad_report in (False, True):
            with self.subTest(bad_report=bad_report):
                binary = self.app / 'Contents/MacOS/CassQuery'
                binary.parent.mkdir(parents=True, exist_ok=True)
                pid_file = self.root / 'child.pid'
                code = f'''#!{sys.executable}
import os,subprocess,sys
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
Path({str(pid_file)!r}).write_text(str(child.pid))
if {bad_report!r}:
    Path(os.environ['CASSQUERY_SMOKE_DIR'],'renderer-raw.json').write_text('invalid JSON')
raise SystemExit(1)
'''
                binary.write_text(code)
                binary.chmod(0o755)
                env = {'HOME': str(self.root), 'PATH': '/usr/bin:/bin'}
                with self.assertRaises((RuntimeError, ValueError)):
                    module.launch(self.app, self.output, env, '1.0.0', 'arm64', bundled=False)
                pid = int(pid_file.read_text())
                for _ in range(50):
                    result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
                    if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                        break
                    time.sleep(0.05)
                else:
                    os.kill(pid, 9)
                    self.fail('failed native launch left its child running')

    def test_native_renderer_is_explicit_metal_not_vulkan_fallback(self):
        with patch.dict(os.environ, {'CASSQUERY_ANGLE': 'metal'}, clear=True):
            env, backend = module.renderer_environment({'PATH': '/usr/bin:/bin'})
            self.assertEqual(env['CASSQUERY_ANGLE'], 'metal')
            self.assertNotIn('CASSQUERY_SOFTWARE_GL', env)
            self.assertEqual(backend, 'metal-requested')
        with patch.dict(os.environ, {}, clear=True):
            env, backend = module.renderer_environment({'CASSQUERY_SOFTWARE_GL': '1', 'CASSQUERY_ANGLE': 'metal'})
            self.assertNotIn('CASSQUERY_SOFTWARE_GL', env)
            self.assertNotIn('CASSQUERY_ANGLE', env)
            self.assertEqual(backend, 'desktop-default')
        for values in ({'CASSQUERY_SOFTWARE_GL': '1'}, {'CASSQUERY_ANGLE': 'swiftshader'}, {'CASSQUERY_ANGLE': 'vulkan'}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True), self.assertRaises(RuntimeError):
                module.renderer_environment({})

    def test_native_failure_overwrites_stale_pass(self):
        file = self.output / 'renderer.json'
        file.write_text('{"schema":1,"pass":true}')
        with patch.object(module, 'verify_bundle', side_effect=RuntimeError('seal failed')):
            with self.assertRaises(RuntimeError):
                module.run_smoke(self.app, self.output, 'arm64')
        report = json.loads(file.read_text())
        self.assertIs(report['pass'], False)
        self.assertIn('seal failed', report['error'])


if __name__ == '__main__':
    unittest.main()
