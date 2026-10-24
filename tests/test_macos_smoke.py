"""Native evidence boundary regressions; no app/keychain/process operations."""
import copy
import hashlib
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
sys.path.insert(0, str(ROOT / 'tools'))
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
                        'vertices': 30, 'triangles': 10, 'degenerate': 0, 'meshes': 3, 'controls_checked': 15, 'clipped': 0, 'nonfinite': 0,
                        'overflow': False, 'bad': [], 'overlaps': [], 'errors': [], 'diag': '', 'tier': 'certified',
                        'gates_pass': True, 'step_pressed': ['true', 'false'] if state == 'assembled' else ['false', 'true'],
                        'explode': '0%' if state == 'assembled' else '100%',
                        'canvas': {'width': width, 'height': 600, 'client_width': width, 'client_height': 600}, 'image': image})
        for name in ('assembled', 'apart', 'save-certified', 'gate-failed'):
            file = self.output / f'{name}.png'
            if name in ('assembled', 'apart'):
                file.write_bytes((self.output / f'light-1320-{name}.png').read_bytes())
            else:
                Image.new('RGB', (1320, 700), (50, 60, 70)).save(file)
            images.append(module.image_evidence(file))
        for name in ('orbit-drag', 'orbit-reset', 'pan-drag', 'pan-reset', 'save-camera'):
            file = self.output / f'{name}.png'
            Image.new('RGB', (1320, 700), (100 + len(images), 60, 70)).save(file)
            images.append(module.image_evidence(file))
        by_name = {Path(image['path']).name: image for image in images}
        default = (0.95, -1.35, 0.62)
        length = sum(v * v for v in default) ** 0.5
        home = {'direction': [v / length for v in default], 'target': [0, 0, 0], 'center': [0, 0, 0], 'radius': 2}
        orbit = {'direction': [1, 0, 0], 'target': [0, 0, 0], 'center': [0, 0, 0], 'radius': 2}
        pan = {**home, 'target': [1, 0, 0]}
        self.report = {'schema': 1, 'pass': True, 'challenge': 'current-run', 'app_pid': 12345,
            'worker_pid': 54321, 'packaged': True, 'bundled_worker': True, 'worker_shutdown': True,
            'version': VERSION, 'architecture': 'arm64', 'source_sha': None, 'resources_path': str(self.resources),
            'render_backend_requested': 'desktop-default',
            # Synthetic telemetry is only a schema fixture, never device evidence.
            'graphics': {'runtime': {'electron': '45.0.0', 'chromium': '154.0.0.0', 'node': '26.0.0',
                                     'platform': 'darwin', 'architecture': 'arm64'},
                         'display': {'bounds': {'x': 0, 'y': 0, 'width': 1320, 'height': 900},
                                     'work_area': {'x': 0, 'y': 0, 'width': 1320, 'height': 900},
                                     'scale_factor': 2,
                                     'window_bounds': {'x': 0, 'y': 0, 'width': 1320, 'height': 740}},
                         'gpu': {'gpuDevice': [{'active': True, 'vendorId': 0, 'deviceId': 0}]},
                         'webgl': {'context': 'webgl2', 'vendor': 'fixture vendor', 'renderer': 'fixture renderer',
                                   'version': 'WebGL 2.0 fixture', 'shading_language': 'WebGL GLSL ES 3.00 fixture',
                                   'unmasked_vendor': 'fixture vendor', 'unmasked_renderer': 'fixture hardware',
                                   'is_context_lost': False, 'drawing_buffer': [2640, 1400], 'viewport': [1320, 700], 'dpr': 2}},
            'worker_executable': str(self.resources / 'worker/cassquery-worker'), 'errors': [],
            'started': {'wall_time_ms': 1100, 'monotonic_ms': 1100}, 'checkpoints': checkpoints,
            'finished': {'wall_time_ms': 4000, 'monotonic_ms': 4000}, 'checks': list(module.CHECKS),
            'lifecycle': {'pass': True, 'secondary_pid': 22222, 'secondary_exit': 0, 'second_instance_events': 1,
                          'close_reopen': True, 'worker_pid_before': 54321, 'worker_pid_after': 54321},
            'matrix': matrix, 'images': images,
            'camera_checks': [{'mode': mode, 'status': 'checked',
                               'method': 'native pointer drag then Reset; observed at 700ms and 3s',
                               'before': copy.deepcopy(home), 'after_drag': copy.deepcopy(orbit if mode == 'orbit' else pan),
                               'after_reset_700': copy.deepcopy(home), 'after_reset_settled': copy.deepcopy(home),
                               'drag_image': by_name[f'{mode}-drag.png'], 'reset_image': by_name[f'{mode}-reset.png']}
                              for mode in ('orbit', 'pan')],
            'camera_save': {'method': 'native orbit to non-default direction; observe settled camera before saving',
                            'before_drag': copy.deepcopy(home), 'before_save': copy.deepcopy(orbit),
                            'after_save': {**copy.deepcopy(orbit), 'radius': 3}, 'image': by_name['save-camera.png']},
            'save_state': {'vertices': 30, 'triangles': 10, 'degenerate': 0, 'clipped': 0, 'nonfinite': 0, 'tier': 'certified',
                           'gates_pass': True, 'hash': 'certified-save', 'diag': ''},
            'failure_state': {'vertices': 30, 'triangles': 10, 'degenerate': 0, 'clipped': 0, 'nonfinite': 0, 'tier': 'certified',
                              'gates_pass': True, 'hash': 'certified-save', 'diag': 'deliberate failed gate'},
            'screenshots': [str(self.output / 'assembled.png'), str(self.output / 'apart.png')]}
        source = {'files': [{'bytes': (ROOT / name).stat().st_size, 'path': name, 'sha256': module.sha256(ROOT / name)}
                            for name in module.SHELL_FILES]}
        source['fingerprint'] = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.report['native_source'] = source

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
        report['images'] = [image for image in report['images']
                            if Path(image['path']).name not in ('save-camera.png', 'save-certified.png', 'gate-failed.png')]
        for key in ('camera_save', 'save_state', 'failure_state'):
            report.pop(key)
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

    def test_native_renderer_accepts_explicit_packaged_swiftshader_and_metal(self):
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
        with patch.dict(os.environ, {'CASSQUERY_ANGLE': 'swiftshader'}, clear=True):
            env, backend = module.renderer_environment({})
            self.assertEqual(env['CASSQUERY_ANGLE'], 'swiftshader')
            self.assertEqual(backend, 'swiftshader-requested')
        for values in ({'CASSQUERY_SOFTWARE_GL': '1'}, {'CASSQUERY_ANGLE': 'vulkan'},
                       {'CASSQUERY_ANGLE': 'swiftshader', 'CASSQUERY_SOFTWARE_GL': '1'}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True), self.assertRaises(RuntimeError):
                module.renderer_environment({})

    def test_save_failure_semantics_use_the_shared_strict_validator(self):
        from release_evidence import validate_native_renderer
        for key in ('save_state', 'failure_state'):
            for change in ({'vertices': 0}, {'vertices': True}, {'tier': 'preview'}, {'gates_pass': False}, {'hash': ''}):
                report = copy.deepcopy(self.report); report[key].update(change)
                with self.subTest(key=key, change=change):
                    with self.assertRaises(RuntimeError):
                        self.validate(report)
                    with self.assertRaises(ValueError):
                        validate_native_renderer(report, expected_sha=None, expected_arch='arm64', expected_version=VERSION,
                                                 bundled=False, started=self.started, finished=self.finished)
        report = copy.deepcopy(self.report); report['failure_state']['hash'] = 'wrong retained hash'
        with self.assertRaises(RuntimeError): self.validate(report)

    def test_requested_software_needs_actual_unmasked_webgl_observation(self):
        report = copy.deepcopy(self.report)
        report['render_backend_requested'] = 'swiftshader-requested'
        report['graphics']['webgl']['unmasked_renderer'] = 'ANGLE (fixture SwiftShader driver)'
        with patch.dict(os.environ, {'CASSQUERY_ANGLE': 'swiftshader'}, clear=True):
            module.validate_renderer(report, self.output, 'current-run', 12345, self.app, VERSION,
                                     'arm64', self.started, self.finished, bundled=False)
            for observed in ('', 'ANGLE (fixture hardware)', None):
                bad = copy.deepcopy(report); bad['graphics']['webgl']['unmasked_renderer'] = observed
                with self.subTest(observed=observed), self.assertRaises(RuntimeError):
                    module.validate_renderer(bad, self.output, 'current-run', 12345, self.app, VERSION,
                                             'arm64', self.started, self.finished, bundled=False)
        with self.assertRaises(RuntimeError): self.validate(report)  # Claim differs from actual requested env.

    def test_native_camera_input_reset_and_save_semantics_fail_closed(self):
        mutations = [
            lambda r: r.update(camera_checks=[]),
            lambda r: r['camera_checks'][0].update(after_drag=copy.deepcopy(r['camera_checks'][0]['before'])),
            lambda r: r['camera_checks'][1].update(after_drag=copy.deepcopy(r['camera_checks'][1]['before'])),
            lambda r: r['camera_checks'][0]['after_reset_700'].update(direction=[1, 0, 0]),
            lambda r: r['camera_checks'][1]['after_reset_settled'].update(target=[1, 0, 0]),
            lambda r: r['camera_save'].update(before_save=copy.deepcopy(r['camera_save']['before_drag'])),
            lambda r: r['camera_save']['after_save'].update(direction=[0, 1, 0]),
            lambda r: r['matrix'][0].update(triangles=11),
            lambda r: r['matrix'][0].update(degenerate=1),
        ]
        for mutation in mutations:
            report = copy.deepcopy(self.report); mutation(report)
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                self.validate(report)

    def test_native_source_tuple_is_bound_to_actual_checked_shell(self):
        for change in ('missing', 'different-source'):
            report = copy.deepcopy(self.report)
            if change == 'missing':
                report.pop('native_source')
            else:
                report['native_source']['files'][0]['sha256'] = '0' * 64
                identity = {'files': report['native_source']['files']}
                report['native_source']['fingerprint'] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.validate(report)

    def test_nonsymlink_app_root_failure_overwrites_prior_pass(self):
        target = self.root / 'other.app'; target.mkdir()
        self.app.symlink_to(target)
        file = self.output / 'renderer.json'; file.write_text('{"schema":1,"pass":true}')
        with self.assertRaisesRegex(RuntimeError, 'nonsymlink app root'):
            module.run_smoke(self.app, self.output, 'arm64')
        self.assertIs(json.loads(file.read_text())['pass'], False)

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
