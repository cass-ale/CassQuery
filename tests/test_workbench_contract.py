"""Typed parser/guard fixtures, not executed workbench CAD/browser/native proof."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import zlib
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
SUITE = ROOT / 'tools/workbench_suite.cjs'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def parser_png(width=1600, height=1000, blank=False):
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff)
    row = (b'\x30\x40\x50' * width) if blank else b''.join(bytes((30 + (x % 16) * 7, 50 + (x % 8) * 10, 70 + (x % 4) * 20)) for x in range(width))
    pixels = (b'\x00' + row) * height
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def observations():
    def shape(volume, minimum, maximum):
        return {'volume': volume, 'solids': 1, 'min': minimum, 'max': maximum}
    initial = shape(1000, [-20, -15, 0], [20, 15, 6])
    edited = shape(1360, [-21, -15, 0], [21, 15, 6])
    transformed = shape(4590, [-19.5, -33.5, 5], [25.5, 29.5, 14])
    return {
        'capability-boundary': {'without_capability': 403, 'wrong_capability': 403, 'status_contains_token': False},
        'open-folder-file': {'folder': {'project': 'WORKBENCHPLATE', 'entry': 'part_entry.py', 'files': [{'path': 'part_entry.py', 'bytes': 100}]},
                             'file': {'project': 'PLAINWORKBENCH', 'entry': 'adapter.py'}, 'original_file_before': 'a' * 64,
                             'original_file_after': 'a' * 64, 'editor_follows_selected_file': True},
        'edit-save-volume': {'before_source': 'a' * 64, 'after_source': 'b' * 64, 'before': initial, 'after': edited,
                             'save_status': 200, 'renderer_tier': 'certified', 'gates_pass': True,
                             'gate_ids': ['units', 'watertight', 'volume', 'thin-extent', 'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes']},
        'external-conflict': {'status': 409, 'draft_before': 'c' * 64, 'draft_after': 'c' * 64, 'external_source': 'd' * 64,
                             'disk_after': 'd' * 64, 'conflict_visible': True},
        'syntax-retains-certified': {'certified_before': 'e' * 16, 'certified_after': 'e' * 16, 'certified_announcements': 0,
                                     'diagnostic': 'SyntaxError: deliberately invalid public fixture', 'renderer_tier': 'certified'},
        'draft-recovery': {'stored_draft': 'f' * 64, 'restored_draft': 'f' * 64, 'disk_before': 'a' * 64, 'disk_after': 'a' * 64, 'production_store': True},
        'source-transform': {'transform': {'translation': [3, -2, 5], 'rotation': [0, 0, 90], 'scale': 1.5},
                             'persisted': {'translation': [3, -2, 5], 'rotation': [0, 0, 90], 'scale': 1.5},
                             'manifest_before': 'a' * 64, 'manifest_after': 'b' * 64, 'before': edited, 'after': transformed, 'renderer_tier': 'certified'},
        'step-export-import': {'exported': transformed, 'imported': copy.deepcopy(transformed), 'export_sha256': 'a' * 64,
                               'original_export_after_import': 'a' * 64, 'build_calls_before_export': 2, 'build_calls_after_export': 2,
                               'dirty_export_requests': 0, 'stale_export_rejected': True},
        'eight-themes': {'states': []},
        'font-tabs': {'font_size': 22, 'rendered_font_size': 22, 'tab_size': 2, 'inserted_spaces': 2, 'persistence_after_reload': True},
        'shortcut-focus': {'text_input_received_digit': True, 'focused_editor_camera_unchanged': True, 'outside_axis_camera_changed': True,
                           'rebuild_status': 202, 'find_visible': True, 'undo_restored_text': True},
    }


def native_render_model(image, data):
    """Synthetic projection metadata paired with genuine PNG pixels; not a rendered app."""
    from workbench_native_smoke import canvas_pixels
    image.update(dpr=1, capture_scale=1)
    image['rendering'] = {'model_tier': 'certified', 'model_hash': 'a' * 16, 'vertices': 30, 'meshes': 3,
                          'clipped': 0, 'nonfinite': 0, 'context_lost': False,
                          'canvas_rect': {'x': 0, 'y': 0, 'width': 800, 'height': 600}, 'camera_aspect': 800 / 600,
                          'pixel_ratio': 1, 'buffer': [800, 600], 'drawing_buffer': [800, 600]}
    image['canvas_pixels'] = canvas_pixels(data, image['rendering']['canvas_rect'])
    return image


def rehash_native_model(report, directory, full):
    """Rebuild all physical links after a deliberate MODEL mutation, without bypassing validators."""
    root = Path(directory); file = root / 'proof/workbench-qualification.json'; file.write_text(json.dumps(full))
    native_record = {'path': 'proof/workbench-qualification.json', 'bytes': file.stat().st_size, 'sha256': sha(file.read_bytes())}
    main_file = root / 'proof/native-workbench-main.json'; main = json.loads(main_file.read_text())
    main['result']['qualification'] = {**native_record, 'path': 'workbench-qualification.json'}
    main_file.write_text(json.dumps(main))
    editor_file = root / 'workbench-native.json'; editor = json.loads(editor_file.read_text())
    editor['native_main'] = {'path': 'proof/native-workbench-main.json', 'bytes': main_file.stat().st_size, 'sha256': sha(main_file.read_bytes())}
    editor_file.write_text(json.dumps(editor))
    value = copy.deepcopy(report); value['native_qualification'] = native_record
    value['editor_component'] = {'path': 'workbench-native.json', 'bytes': editor_file.stat().st_size, 'sha256': sha(editor_file.read_bytes())}
    return value


def native_qualification_model(directory):
    """Complete typed native protocol model; NEVER native execution evidence."""
    directory = Path(directory); proof = directory / 'proof'; proof.mkdir()
    data = observations(); identity = {'app_pid': 12345, 'worker_pid': 12346, 'challenge': 'a' * 64}
    driver_bytes = (ROOT / 'tools/native_workbench_probe.cjs').read_bytes()
    driver = {'path': str(ROOT / 'tools/native_workbench_probe.cjs'), 'bytes': len(driver_bytes), 'sha256': sha(driver_bytes)}
    package = json.loads((ROOT / 'package.json').read_text())
    shell_files = [{'path': name, 'bytes': (ROOT / name).stat().st_size, 'sha256': sha((ROOT / name).read_bytes())} for name in package['cassquerySources']['shell']]
    native_source = {'files': shell_files}
    png = parser_png(1320, 880)
    images = []
    labels = ['counterform-dark', 'counterform-light', 'graphite', 'midnight', 'forest', 'sand', 'paper', 'high-contrast', 'edit-certified', 'external-conflict', 'syntax-retained']
    theme_labels = ['Counterform Dark', 'Counterform Light', 'Graphite', 'Midnight', 'Forest', 'Sand', 'Paper', 'High Contrast']
    for index, label in enumerate(labels):
        name = label + '.png'; (proof / name).write_bytes(png)
        images.append(native_render_model({'path': name, 'bytes': len(png), 'sha256': sha(png), 'width': 1320, 'height': 880, 'viewport': [1320, 880]}, png))
    data['eight-themes']['states'] = [{'name': name, 'styling': f'parser-style-{i}', 'persisted': True, 'visible': True, 'image_sha256': sha(png)} for i, name in enumerate(theme_labels)]
    checks = [{'id': 'preload-ipc', 'pass': True, 'evidence': {'node_integration': False, 'context_isolation': True, 'sandbox': True, 'bridge_present': True, 'project_root_matches': True}}]
    checks += [{'id': name, 'pass': True, 'evidence': data[name]} for name in ['edit-save-volume', 'external-conflict', 'syntax-retains-certified', 'eight-themes', 'font-tabs']]
    editor_result = {'schema': 1, 'platform': 'native-electron-workbench-editor', 'pass': False, 'qualifies_workbench': False, 'editor_checks_pass': True,
                     **identity, 'driver': driver, 'native_source': native_source, 'checks': checks, 'images': images, 'errors': []}
    full_checks = {
        'native-open-file': {'path': '/parser-only/selected.py'},
        'native-open-folder': {'root': '/parser-only/folder'},
        'native-new-project': {'root': '/parser-only/new'},
        'native-transform': {'before': data['source-transform']['before'], 'after': data['source-transform']['after'], 'persisted': data['source-transform']['persisted']},
        'native-export-step': {'path': 'native-export.step', 'bytes': 0, 'sha256': None, 'geometry': data['source-transform']['after']},
        'native-import-roundtrip': {'imported_geometry': data['source-transform']['after']},
        'native-save-copy': {'copy_sha256': 'a' * 64, 'draft_sha256': 'a' * 64, 'original_sha256': 'b' * 64, 'active_project_changed': False},
        'native-cancel': {'registry_before': [], 'registry_after': []},
        'dirty-cancel': {'draft_before': 'a' * 64, 'draft_after': 'a' * 64, 'source_unchanged': True},
        'dirty-save': {'actual_source_sha256': 'a' * 64, 'expected_draft_sha256': 'a' * 64},
        'dirty-discard': {'source_sha256': 'a' * 64, 'saved_sha256': 'a' * 64},
    }
    step = b'ISO-10303-21;\nHEADER;ENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n'; (proof / 'native-export.step').write_bytes(step)
    full_checks['native-export-step'].update(bytes=len(step), sha256=sha(step))
    kinds = ['open', 'folder', 'folder', 'save', 'open', 'folder', 'save', 'folder', 'folder', 'cancel']
    targets = ['/parser-only/selected.py', '/parser-only/folder', '/parser-only', '/parser-only/native-export.step', '/parser-only/native-export.step',
               '/parser-only', '/parser-only/copy.py', '/parser-only/folder', '/parser-only/folder', None]
    records = []
    native_calls = []
    full_images = []
    for index, kind in enumerate(kinds):
        target = targets[index]
        result = {'canceled': True, 'filePaths': []} if kind == 'cancel' else {'canceled': False, **({'filePath': target} if kind == 'save' else {'filePaths': [target]})}
        image_name = f'native-os-{index + 1}.png'; (proof / image_name).write_bytes(png)
        image = {'path': image_name, 'bytes': len(png), 'sha256': sha(png), 'width': 1320, 'height': 880}; full_images.append(image)
        call = {'method': 'showSaveDialog' if kind == 'save' else 'showOpenDialog', 'result': result}; native_calls.append(call)
        records.append({'id': index + 1, 'kind': kind, 'target': target, 'observer_index': index, 'native_call': call,
                        'os_input': {'id': index + 1, 'pass': True, 'app_pid': identity['app_pid'], 'challenge': identity['challenge'], 'image': image}})
    for name in ('open-file', 'open-folder', 'new-project', 'transform', 'export', 'import', 'save-copy'):
        file = 'full-' + name + '.png'; (proof / file).write_bytes(png)
        full_images.append(native_render_model({'path': file, 'bytes': len(png), 'sha256': sha(png), 'width': 1320, 'height': 880}, png))
    (proof / 'native-dialogs.json').write_text(json.dumps({'schema': 1, 'app_pid': identity['app_pid'], 'challenge': identity['challenge'], 'calls': native_calls}))
    full = {'schema': 1, 'pass': True, 'qualifies_workbench': True, 'source_sha': 'a' * 40, **identity, 'driver': driver, 'native_source': native_source,
            'error': None, 'checks': [{'id': name, 'pass': True, 'evidence': value} for name, value in full_checks.items()],
            'images': full_images, 'native_dialog_selection': {'status': 'checked', 'records': records}}
    def record(file, relative):
        raw = file.read_bytes(); return {'path': relative, 'bytes': len(raw), 'sha256': sha(raw)}
    full_file = proof / 'workbench-qualification.json'; full_file.write_text(json.dumps(full))
    editor_result['qualification'] = record(full_file, 'workbench-qualification.json')
    main = {'schema': 1, 'source_sha': 'a' * 40, **identity, 'driver': driver, 'native_source': native_source, 'result': editor_result}
    main_file = proof / 'native-workbench-main.json'; main_file.write_text(json.dumps(main))
    editor = {'schema': 1, 'pass': True, 'qualifies_workbench': False, 'source_sha': 'a' * 40, 'architecture': 'arm64', **identity,
              'driver': driver, 'native_main': record(main_file, 'proof/native-workbench-main.json')}
    editor_file = directory / 'workbench-native.json'; editor_file.write_text(json.dumps(editor))
    source_files = [{'path': name, 'bytes': (ROOT / name).stat().st_size, 'executable': False, 'sha256': sha((ROOT / name).read_bytes())} for name in package['cassquerySources']['shell']]
    source_id = {'source_sha': 'a' * 40, 'files': source_files}
    source_fp = sha(json.dumps(source_id, sort_keys=True, separators=(',', ':')).encode())
    source_snapshot = {**source_id, 'schema': 1, 'clean': True, 'fingerprint': source_fp}
    source_file = directory / 'source-before.json'; source_file.write_text(json.dumps(source_snapshot))
    outer = {'schema': 1, 'pass': True, 'qualifies_workbench': True, 'source_sha': 'a' * 40, 'architecture': 'arm64',
             'source_fingerprint': source_fp, 'after_fingerprint': source_fp, 'source_snapshot': record(source_file, 'source-before.json'),
             'editor_component': record(editor_file, 'workbench-native.json'), 'native_qualification': record(full_file, 'proof/workbench-qualification.json'),
             'cleanup': {'pass': True}, 'temp_root_removed': True,
             'geometry_readback': {'schema': 1, 'method': 'actual CadQuery STEP import in native runner', 'step_sha256': sha(step),
               'geometry': data['source-transform']['after'], 'units': 'mm', 'variant': 'neutral', 'source_sha': 'a' * 40,
               'cadquery_version': '2.8.0', 'python_version': '3.12.10'}}
    return outer


class WorkbenchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get('CASSQUERY_NODE') or shutil.which('node')
        if not cls.node:
            raise RuntimeError('Node is required; workbench contract tests cannot skip')
        import test_integration_contract as legacy
        cls.legacy = legacy
        legacy.IntegrationReportContractTests.setUpClass()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.data = observations()
        legacy_case = self.legacy.IntegrationReportContractTests('test_valid_parser_contract')
        legacy_case.setUp()
        self.addCleanup(legacy_case.doCleanups)
        baseline = copy.deepcopy(legacy_case.report)
        directory = self.output / 'baseline'; directory.mkdir()
        for shot in baseline['screenshots']:
            shutil.copyfile(legacy_case.output / shot['file'], directory / shot['file'])
        raw = json.dumps(baseline).encode(); (directory / 'integration-results.json').write_bytes(raw)
        source_identity = {'source_sha': 'a' * 40, 'files': [
            {'path': 'worker.py', 'bytes': 1, 'executable': False, 'sha256': baseline['source']['worker_sha256']},
            {'path': 'viewer/app.js', 'bytes': 1, 'executable': False, 'sha256': baseline['source']['viewer_sha256']},
            {'path': 'tools/workbench_suite.cjs', 'bytes': 1, 'executable': False, 'sha256': 'b' * 64},
            *[{'path': f'source-{i}.py', 'bytes': i, 'executable': False, 'sha256': 'a' * 64} for i in range(7)]]}
        source_fingerprint = sha(json.dumps(source_identity, sort_keys=True, separators=(',', ':')).encode())
        self.report = {'schema': 1, 'platform': 'linux-source-browser-workbench', 'pass': True, 'error': None,
                       'method': 'real worker/CAD/files; DOM keyboard/pointer; production stores; simulated desktop chooser',
                       'native_dialog_selection': 'not checked', 'viewport': [1600, 1000], 'worker_pid': 12345, 'started_at': 1, 'completed_at': 2,
                       'source': {'before_fingerprint': source_fingerprint, 'after_fingerprint': source_fingerprint, **source_identity,
                                  'worker_sha256': baseline['source']['worker_sha256'], 'viewer_sha256': baseline['source']['viewer_sha256'], 'suite_sha256': 'b' * 64,
                                  'python_sources': copy.deepcopy(baseline['source']['python_sources'])},
                       'baseline': {'path': 'baseline/integration-results.json', 'sha256': sha(raw)},
                       'browser_errors': [], 'cleanup': {'errors': [], 'process_groups_gone': True, 'ports_closed': True},
                       'scenarios': [], 'files': [], 'screenshots': []}
        public_source = (ROOT / 'testpart/part_entry.py').read_bytes()
        conflict_text = 'parser conflict draft only\n'
        external_text = 'parser external source only\n'
        recovered_text = 'parser recovered draft only\n'
        payloads = {'before-edit.py': public_source, 'after-edit.py': public_source.replace(b'.box(40, 30, 6,', b'.box(42, 30, 6,'),
                    'plain-original.py': b'parser plain source only\n', 'conflict-draft.py': conflict_text.encode(), 'external-source.py': external_text.encode(),
                    'recovery.json': json.dumps({'schema': 1, 'drafts': [{'project': 'WORKBENCHPLATE', 'path': 'part_entry.py', 'text': recovered_text}]}).encode(),
                    'settings.json': json.dumps({'schema': 1, 'theme': 'high-contrast', 'editorFontSize': 22, 'tabSize': 2}).encode(),
                    'transformed-manifest.json': json.dumps({'workbench': {'transforms': {'Base': self.data['source-transform']['transform']}}}).encode(),
                    'transformed.step': b'ISO-10303-21;\nHEADER;ENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n',
                    'roundtrip.step': b'ISO-10303-21;\nHEADER;ENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n'}
        for name, data in payloads.items():
            # Typed parser bytes, never claimed to be an executed source save, OCP export or recovered draft.
            (self.output / name).write_bytes(data)
            self.report['files'].append({'path': name, 'bytes': len(data), 'sha256': sha(data)})
        self.data['edit-save-volume'].update(before_source=sha(payloads['before-edit.py']), after_source=sha(payloads['after-edit.py']))
        self.data['source-transform']['manifest_after'] = sha(payloads['transformed-manifest.json'])
        self.data['open-folder-file'].update(original_file_before=sha(payloads['plain-original.py']), original_file_after=sha(payloads['plain-original.py']))
        self.data['external-conflict'].update(draft_before=sha(conflict_text.encode()), draft_after=sha(conflict_text.encode()), external_source=sha(external_text.encode()), disk_after=sha(external_text.encode()))
        self.data['draft-recovery'].update(stored_draft=sha(recovered_text.encode()), restored_draft=sha(recovered_text.encode()))
        step_hash = next(r['sha256'] for r in self.report['files'] if r['path'] == 'transformed.step')
        self.data['step-export-import'].update(export_sha256=step_hash, original_export_after_import=step_hash)
        themes = ['Counterform Dark', 'Counterform Light', 'Graphite', 'Midnight', 'Forest', 'Sand', 'Paper', 'High Contrast']
        png = parser_png()
        for index, label in enumerate(themes + ['edit-certified', 'external-conflict', 'syntax-retained', 'step-roundtrip']):
            name = f'parser-image-{index}.png'; (self.output / name).write_bytes(png)
            record = {'path': name, 'bytes': len(png), 'sha256': sha(png), 'width': 1600, 'height': 1000,
                'viewport': [1600, 1000], 'full_size': [1600, 1000], 'dpr': 1, 'horizontal_overflow': False, 'description': label,
                'rendering': {'model_tier': 'certified', 'model_hash': 'a' * 16, 'vertices': 30, 'meshes': 3, 'clipped': 0, 'nonfinite': 0,
                    'context_lost': False, 'canvas_rect': {'x': 0, 'y': 0, 'width': 800, 'height': 600}, 'camera_aspect': 800 / 600,
                    'pixel_ratio': 1, 'buffer': [800, 600], 'drawing_buffer': [800, 600]}}
            self.report['screenshots'].append(record)
            if index < len(themes):
                self.data['eight-themes']['states'].append({'name': label, 'styling': f'parser-style-{index}', 'persisted': True, 'visible': True, 'image_sha256': sha(png)})
        pixel_code = "const fs=require('node:fs'),s=require(process.argv[1]);console.log(JSON.stringify(s.canvasPixels(fs.readFileSync(process.argv[2]),{x:0,y:0,width:800,height:600})));"
        result = subprocess.run([self.node, '-e', pixel_code, str(SUITE), str(self.output / 'parser-image-0.png')], capture_output=True, text=True, timeout=10, check=True)
        for record in self.report['screenshots']: record['canvas_pixels'] = json.loads(result.stdout)
        groups = {'source-edit': ['capability-boundary', 'open-folder-file', 'edit-save-volume', 'external-conflict', 'syntax-retains-certified', 'draft-recovery'],
                  'cad-roundtrip': ['source-transform', 'step-export-import'], 'workbench-ui': ['eight-themes', 'font-tabs', 'shortcut-focus']}
        self.report['scenarios'] = [{'id': group, 'pass': True, 'checks': [{'id': name, 'pass': True, 'evidence': self.data[name]} for name in names]} for group, names in groups.items()]

    def validate(self, report=None):
        file = self.output / 'workbench-results.json'; file.write_text(json.dumps(self.report if report is None else report))
        code = "const fs=require('node:fs'),s=require(process.argv[1]);try{s.validateReport(JSON.parse(fs.readFileSync(process.argv[2])),process.argv[3]);}catch(e){console.error(e.message);process.exitCode=1;}"
        return subprocess.run([self.node, '-e', code, str(SUITE), str(file), str(self.output)], capture_output=True, text=True, timeout=10, check=False)

    def evidence(self, report, name):
        return next(c['evidence'] for s in report['scenarios'] for c in s['checks'] if c['id'] == name)

    def test_complete_typed_contract_not_browser_or_native_execution(self):
        result = self.validate(); self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_duplicate_checks_or_native_label_are_red(self):
        for mutate in (lambda r: r['scenarios'].pop(), lambda r: r['scenarios'][0]['checks'].pop(),
                       lambda r: r['scenarios'].__setitem__(0, r['scenarios'][1]),
                       lambda r: r['scenarios'][0]['checks'].__setitem__(0, r['scenarios'][0]['checks'][1]),
                       lambda r: r.update(native_dialog_selection='checked'), lambda r: r.update(pass_=False)):
            value = copy.deepcopy(self.report); mutate(value)
            if 'pass_' in value: value['pass'] = value.pop('pass_')
            with self.subTest(mutation=mutate): self.assertNotEqual(self.validate(value).returncode, 0)

    def test_nonvacuous_source_geometry_conflict_and_settings_observations(self):
        mutations = [('edit-save-volume', lambda e: e['after'].update(volume=e['before']['volume'])),
                     ('external-conflict', lambda e: e.update(draft_after='0' * 64)),
                     ('external-conflict', lambda e: e.update(disk_after='0' * 64)),
                     ('syntax-retains-certified', lambda e: e.update(certified_announcements=1)),
                     ('draft-recovery', lambda e: e.update(production_store=False)),
                     ('source-transform', lambda e: e['after'].update(volume=e['before']['volume'])),
                     ('step-export-import', lambda e: e['imported'].update(volume=0)),
                     ('step-export-import', lambda e: e.update(build_calls_after_export=3)),
                     ('step-export-import', lambda e: e.update(dirty_export_requests=1)),
                     ('eight-themes', lambda e: e['states'].__setitem__(0, e['states'][1])),
                     ('font-tabs', lambda e: e.update(inserted_spaces=0)),
                     ('shortcut-focus', lambda e: e.update(focused_editor_camera_unchanged=False))]
        for name, mutate in mutations:
            report = copy.deepcopy(self.report); mutate(self.evidence(report, name))
            with self.subTest(check=name): self.assertNotEqual(self.validate(report).returncode, 0)
        for scenario in self.report['scenarios']:
            for check in scenario['checks']:
                value = copy.deepcopy(self.report); self.evidence(value, check['id']).clear()
                with self.subTest(empty=check['id']): self.assertNotEqual(self.validate(value).returncode, 0)

    def test_tampered_missing_escaped_and_non_image_bytes_fail(self):
        for kind in ('missing', 'hash', 'escape', 'none', 'bad-png', 'no-step'):
            value = copy.deepcopy(self.report)
            if kind == 'missing': value['files'][0]['path'] = 'absent.py'
            elif kind == 'hash': value['screenshots'][0]['sha256'] = '0' * 64
            elif kind == 'escape': value['files'][0]['path'] = '../outside'
            elif kind == 'none': value['screenshots'] = []
            elif kind == 'no-step': value['files'] = [r for r in value['files'] if r['path'] != 'transformed.step']
            else:
                file = self.output / value['screenshots'][0]['path']; file.write_bytes(b'\x89PNG\r\n\x1a\ninvalid fixture')
                value['screenshots'][0].update(bytes=file.stat().st_size, sha256=sha(file.read_bytes()))
            with self.subTest(kind=kind): self.assertNotEqual(self.validate(value).returncode, 0)
            if kind == 'bad-png': file.write_bytes(parser_png())

    def test_stale_source_cleanup_or_legacy_baseline_is_not_green(self):
        for mutate in (lambda r: r['source'].update(after_fingerprint='0' * 64),
                       lambda r: r['cleanup'].update(process_groups_gone=False),
                       lambda r: r['cleanup'].update(ports_closed=False),
                       lambda r: r['source'].update(worker_sha256='0' * 64),
                       lambda r: r['baseline'].update(sha256='0' * 64)):
            value = copy.deepcopy(self.report); mutate(value)
            with self.subTest(mutation=mutate): self.assertNotEqual(self.validate(value).returncode, 0)

    def test_portable_native_model_requires_separate_full_paths_and_readback(self):
        import workbench_native_smoke as native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); report = native_qualification_model(root)
            self.assertTrue(native.validate_qualification(report, root, expected_sha='a' * 40, expected_arch='arm64'))
            native.validate_qualification(report, root, expected_sha='a' * 40, expected_arch='arm64')
            # Portable mode never imports CAD or evaluates STEP; native True mode rejects this parser-only STEP.
            with self.assertRaises(Exception):
                native.validate_qualification(report, root, expected_sha='a' * 40, expected_arch='arm64', verify_geometry=True)
            for mutate in (lambda r: r.update(qualifies_workbench=False), lambda r: r.update(pass_=False),
                           lambda r: r['geometry_readback'].update(step_sha256='0' * 64),
                           lambda r: r['geometry_readback'].update(units='inch'),
                           lambda r: r['geometry_readback']['geometry'].update(volume=0),
                           lambda r: r.update(temp_root_removed=False)):
                value = copy.deepcopy(report); mutate(value)
                if 'pass_' in value: value['pass'] = value.pop('pass_')
                with self.subTest(mutation=mutate), self.assertRaises((ValueError, RuntimeError, KeyError)):
                    native.validate_qualification(value, root, expected_sha='a' * 40, expected_arch='arm64')
            # Physically rewrite/re-hash the entire linked chain; contradictory facts still fail.
            def rehash_full(full):
                return rehash_native_model(report, root, full)
            original = json.loads((root / 'proof/workbench-qualification.json').read_text())
            for kind in ('images', 'action-id', 'open-path'):
                full = copy.deepcopy(original)
                if kind == 'images': full['images'] = []
                elif kind == 'action-id': full['native_dialog_selection']['records'][0]['os_input']['id'] = 9999
                else: next(c for c in full['checks'] if c['id'] == 'native-open-file')['evidence']['path'] = '/unrelated'
                value = rehash_full(full)
                with self.subTest(rehashed=kind), self.assertRaises((ValueError, RuntimeError, KeyError)):
                    native.validate_qualification(value, root, expected_sha='a' * 40, expected_arch='arm64')
            value = rehash_full(original); value.pop('source_fingerprint'); value.pop('after_fingerprint')
            with self.assertRaises(RuntimeError): native.validate_qualification(value, root, expected_sha='a' * 40, expected_arch='arm64')
            # Portable mode never imports CAD or evaluates STEP; actual native run is mandatory separately.
            with self.assertRaises(Exception):
                native.validate_qualification(value, root, expected_sha='a' * 40, expected_arch='arm64', verify_geometry=True)

    def test_pixel_cache_is_content_keyed_and_returns_unshared_results(self):
        import workbench_native_smoke as native
        from unittest.mock import patch
        native._canvas_pixels.cache_clear()
        png = parser_png(64, 40); roi = {'x': 0, 'y': 0, 'width': 32, 'height': 20}
        with patch.object(native, 'dimensions', wraps=native.dimensions) as fresh_decode:
            first = native.canvas_pixels(png, roi)
            expected = dict(first); first['samples'] = -1
            second = native.canvas_pixels(bytes(bytearray(png)), dict(roi))
            self.assertEqual(second, expected); self.assertIsNot(first, second)
            self.assertEqual(fresh_decode.call_count, 2)
        info = native._canvas_pixels.cache_info(); self.assertEqual((info.hits, info.misses), (1, 1))
        self.assertIsInstance(native._canvas_pixels(png, tuple(roi[k] for k in ('x', 'y', 'width', 'height'))), tuple)
        native.canvas_pixels(png, {**roi, 'x': 1}); self.assertEqual(native._canvas_pixels.cache_info().misses, 2)
        blank = parser_png(64, 40, blank=True); counts = native.canvas_pixels(blank, roi)
        self.assertEqual(counts['non_background_samples'], 0); self.assertEqual(native._canvas_pixels.cache_info().misses, 3)

    def test_pixel_cache_has_four_entry_eviction_and_rejects_invalid_keys(self):
        import workbench_native_smoke as native
        native._canvas_pixels.cache_clear(); roi = {'x': 0, 'y': 0, 'width': 32, 'height': 20}
        images = [parser_png(width, 40) for width in range(64, 69)]
        for png in images: native.canvas_pixels(png, roi)
        info = native._canvas_pixels.cache_info(); self.assertEqual((info.maxsize, info.currsize, info.misses), (4, 4, 5))
        native.canvas_pixels(images[-1], roi); self.assertEqual(native._canvas_pixels.cache_info().hits, 1)
        native.canvas_pixels(images[0], roi); self.assertEqual(native._canvas_pixels.cache_info().misses, 6)
        before = native._canvas_pixels.cache_info()
        for data, invalid_roi in ((images[0][:-1], roi), (images[0] + b'0' * (32 * 1024**2), roi),
                                  (bytearray(images[0]), roi), (images[0], {**roi, 'x': float('nan')}),
                                  (images[0], {**roi, 'width': False}), (images[0], {**roi, 'height': 0})):
            with self.subTest(input_size=len(data), roi=invalid_roi), self.assertRaises((ValueError, RuntimeError)):
                native.canvas_pixels(data, invalid_roi)
            self.assertEqual(native._canvas_pixels.cache_info(), before)

    def test_native_measured_readback_and_physical_step_negative(self):
        import cadquery as cq
        import workbench_native_smoke as native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); report = native_qualification_model(root)
            step = root / 'proof/native-export.step'
            cq.exporters.export(cq.Workplane('XY').box(2, 3, 4), str(step), exportType='STEP')
            readback = native.measure_step_readback(step, source_sha='a' * 40)
            self.assertAlmostEqual(readback['geometry']['volume'], 24, places=9)
            self.assertEqual(readback['geometry']['min'], [-1, -1.5, -2])
            self.assertEqual(readback['geometry']['max'], [1, 1.5, 2])
            full = json.loads((root / 'proof/workbench-qualification.json').read_text())
            by_id = {c['id']: c['evidence'] for c in full['checks']}
            by_id['native-transform']['before']['volume'] = 24 / 3.375
            by_id['native-transform']['after'] = copy.deepcopy(readback['geometry'])
            by_id['native-export-step'].update(geometry=copy.deepcopy(readback['geometry']), bytes=step.stat().st_size, sha256=sha(step.read_bytes()))
            by_id['native-import-roundtrip']['imported_geometry'] = copy.deepcopy(readback['geometry'])
            # Real OCP imports may differ below retained gate tolerances. Descriptor records actual values, not these claims.
            by_id['native-import-roundtrip']['imported_geometry']['volume'] += 1e-8
            report = rehash_native_model(report, root, full); report['geometry_readback'] = readback
            self.assertNotEqual(report['geometry_readback']['geometry'], by_id['native-import-roundtrip']['imported_geometry'])
            native.validate_qualification(report, root, expected_sha='a' * 40, expected_arch='arm64', verify_geometry=True)
            for changes in ({'volume': 0}, {'min': [0, 0, 0]}, {'solids': 0}):
                bad = copy.deepcopy(report); bad['geometry_readback']['geometry'].update(changes)
                with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                    native.validate_qualification(bad, root, expected_sha='a' * 40, expected_arch='arm64')
            # A valid different STEP with every digest relinked still fails actual readback; no monkeypatching CAD.
            cq.exporters.export(cq.Workplane('XY').box(4, 3, 4), str(step), exportType='STEP')
            by_id['native-export-step'].update(bytes=step.stat().st_size, sha256=sha(step.read_bytes()))
            report = rehash_native_model(report, root, full); report['geometry_readback']['step_sha256'] = sha(step.read_bytes())
            with self.assertRaisesRegex(RuntimeError, 'contradicts claimed geometry'):
                native.validate_qualification(report, root, expected_sha='a' * 40, expected_arch='arm64', verify_geometry=True)

    def test_native_capture_rehashed_blank_and_scaled_pixels(self):
        import workbench_native_smoke as native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); report = native_qualification_model(root)
            native.validate_qualification(report, root, expected_sha='a' * 40, expected_arch='arm64')
            full = json.loads((root / 'proof/workbench-qualification.json').read_text())
            image = next(i for i in full['images'] if i['path'] == 'full-transform.png')
            blank = parser_png(1320, 880, blank=True); (root / 'proof' / image['path']).write_bytes(blank)
            image.update(bytes=len(blank), sha256=sha(blank)); image['canvas_pixels'] = native.canvas_pixels(blank, image['rendering']['canvas_rect'])
            bad = rehash_native_model(report, root, full)
            with self.assertRaisesRegex(RuntimeError, 'blank'):
                native.validate_qualification(bad, root, expected_sha='a' * 40, expected_arch='arm64')
        png = parser_png(1600, 1200); image = native_render_model({'width': 1600, 'height': 1200}, png)
        image.update(capture_scale=2, dpr=2)
        rect = {key: value * 2 for key, value in image['rendering']['canvas_rect'].items()}
        image['canvas_pixels'] = native.canvas_pixels(png, rect); native.validate_rendered_image(image, png)
        code = "const fs=require('node:fs'),s=require(process.argv[1]),r=JSON.parse(fs.readFileSync(process.argv[2]));const pixels=s.canvasPixels(fs.readFileSync(process.argv[3]),r.rect);s.validateRendering(r.image,pixels);console.log(JSON.stringify(pixels));"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'image.png').write_bytes(png); (root / 'model.json').write_text(json.dumps({'rect': rect, 'image': image}))
            result = subprocess.run([self.node, '-e', code, str(SUITE), str(root / 'model.json'), str(root / 'image.png')], capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout), image['canvas_pixels'])

    def test_native_portable_admission_needs_no_cad_or_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); report = native_qualification_model(root); (root / 'outer.json').write_text(json.dumps(report))
            code = "import json,sys;sys.path.insert(0,sys.argv[1]);import workbench_native_smoke as n;n.validate_qualification(json.load(open(sys.argv[2])),sys.argv[3],expected_sha='a'*40,expected_arch='arm64');assert 'cadquery' not in sys.modules and 'OCP' not in sys.modules"
            result = subprocess.run([sys.executable, '-S', '-c', code, str(ROOT / 'tools'), str(root / 'outer.json'), str(root)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_rendering_requires_current_projected_geometry_and_real_buffer(self):
        for changes in ({'vertices': 0}, {'meshes': 0}, {'model_hash': 'invalid'}, {'clipped': 1}, {'context_lost': True},
                        {'drawing_buffer': [800, 599]}, {'camera_aspect': 1}, {'pixel_ratio': 2}):
            report = copy.deepcopy(self.report); report['screenshots'][0]['rendering'].update(changes)
            with self.subTest(changes=changes): self.assertNotEqual(self.validate(report).returncode, 0)

    def test_rehashed_blank_full_canvas_does_not_render_geometry(self):
        image = self.report['screenshots'][0]; blank = parser_png(blank=True)
        (self.output / image['path']).write_bytes(blank); image.update(bytes=len(blank), sha256=sha(blank))
        self.data['eight-themes']['states'][0]['image_sha256'] = sha(blank)
        image['canvas_pixels'] = {'samples': 153600, 'distinct_colors': 1, 'non_background_samples': 0}
        self.assertNotEqual(self.validate().returncode, 0)

    def test_rehashed_small_capture_cannot_cover_full_viewport(self):
        image = self.report['screenshots'][0]; small = parser_png(100, 100)
        (self.output / image['path']).write_bytes(small)
        image.update(bytes=len(small), sha256=sha(small), width=100, height=100)
        self.data['eight-themes']['states'][0]['image_sha256'] = sha(small)
        self.assertNotEqual(self.validate().returncode, 0)

    def test_unknown_skip_option_cannot_select_partial_suite(self):
        result = subprocess.run([self.node, str(SUITE), '--skip-native'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)

    def test_native_probe_root_accepts_self_and_children_not_prefix_siblings(self):
        code = "const assert=require('node:assert/strict'),n=require(process.argv[1]);assert.equal(n.ownedPath('/tmp/owned','/tmp/owned'),true);assert.equal(n.ownedPath('/tmp/owned','/tmp/owned/user/proof'),true);assert.equal(n.ownedPath('/tmp/owned','/tmp/owned-other/proof'),false);assert.equal(n.ownedPath('/tmp/owned','/tmp/owned/../foreign'),false);"
        result = subprocess.run([self.node, '-e', code, str(ROOT / 'tools/native_workbench_probe.cjs')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_probe_cannot_promote_editor_subset_to_workbench_qualification(self):
        code = "const fs=require('node:fs'),n=require(process.argv[1]);const r=JSON.parse(fs.readFileSync(process.argv[2]));try{n.validateEditorReport(r,process.argv[3]);process.exitCode=1;}catch{process.exitCode=0;}"
        file = self.output / 'native.json'; file.write_text(json.dumps({'schema': 1, 'platform': 'native-electron-workbench-editor',
            'editor_checks_pass': True, 'qualifies_workbench': True, 'native_dialog_selection': {'status': 'checked'}}))
        result = subprocess.run([self.node, '-e', code, str(ROOT / 'tools/native_workbench_probe.cjs'), str(file), str(self.output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
