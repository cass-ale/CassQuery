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
    resources = '/parser-only/CassQuery.app/Contents/Resources'
    preload_source = next(item for item in shell_files if item['path'] == 'shell/preload.cjs')
    preload_identity = {'schema': 1, 'method': 'owned BrowserWindow constructor', 'path': resources + '/app.asar/shell/preload.cjs',
                        'resources_path': resources, 'bytes': preload_source['bytes'], 'sha256': preload_source['sha256'], 'web_contents_id': 102}
    window = {'window_id': 101, 'web_contents_id': 102, 'resources_path': resources, 'worker_origin': 'http://127.0.0.1:3939',
              'project_root': '/parser-only/projects/WORKBENCHPLATE', 'preload_identity': preload_identity}
    settings = {'schema': 1, 'theme': 'system', 'editorFont': 'system-mono', 'editorFontSize': 14, 'tabSize': 4,
                'lineWrap': False, 'editorVisible': True, 'autoBuild': False, 'reducedMotion': 'system', 'updateCheck': 'enabled',
                'updateAutoDownload': False, 'updateChannel': 'stable', 'recentProjects': []}
    listed = {'project': 'WORKBENCHPLATE', 'root': window['project_root'], 'entry': 'part_entry.py', 'source': 'part_entry.py',
              'files': [{'path': 'part_entry.py', 'kind': 'python', 'bytes': 100}, {'path': 'viewer.json', 'kind': 'json', 'bytes': 200}], 'read_only': False}
    preload_evidence = {'node_integration': False, 'context_isolation': True, 'sandbox': True, 'bridge_present': True, 'project_root_matches': True,
        'preload_identity': preload_identity, 'bridge': {'keys': ['onCommand', 'recovery', 'setDirty', 'settings', 'update', 'workbench'],
        'function_types': ['function'] * 6, 'node_require_type': 'undefined', 'node_process_type': 'undefined'},
        'ipc': {'method': 'actual contextBridge invoke', 'window_id': 101, 'web_contents_id': 102, 'origin': window['worker_origin'],
        'settings_result': settings, 'workbench_result': listed, 'workbench_http_status': 200, 'workbench_http_response': copy.deepcopy(listed)}}
    png = parser_png(1320, 880)
    images = []
    labels = ['counterform-dark', 'counterform-light', 'graphite', 'midnight', 'forest', 'sand', 'paper', 'high-contrast', 'edit-certified', 'external-conflict', 'syntax-retained']
    theme_labels = ['Counterform Dark', 'Counterform Light', 'Graphite', 'Midnight', 'Forest', 'Sand', 'Paper', 'High Contrast']
    for index, label in enumerate(labels):
        name = label + '.png'; (proof / name).write_bytes(png)
        images.append(native_render_model({'path': name, 'bytes': len(png), 'sha256': sha(png), 'width': 1320, 'height': 880, 'viewport': [1320, 880]}, png))
    data['eight-themes']['states'] = [{'name': name, 'styling': f'parser-style-{i}', 'persisted': True, 'visible': True, 'image_sha256': sha(png)} for i, name in enumerate(theme_labels)]
    checks = [{'id': 'preload-ipc', 'pass': True, 'evidence': preload_evidence}]
    checks += [{'id': name, 'pass': True, 'evidence': data[name]} for name in ['edit-save-volume', 'external-conflict', 'syntax-retains-certified', 'eight-themes', 'font-tabs']]
    editor_result = {'schema': 1, 'platform': 'native-electron-workbench-editor', 'pass': False, 'qualifies_workbench': False, 'editor_checks_pass': True,
                     **identity, **window, 'driver': driver, 'native_source': native_source, 'checks': checks, 'images': images, 'errors': [],
                     'worker_requests': [{'action': 'list', 'project': 'WORKBENCHPLATE', 'status': 200, 'response': copy.deepcopy(listed)}]}
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
    main = {'schema': 1, 'source_sha': 'a' * 40, **identity, **window, 'driver': driver, 'native_source': native_source, 'result': editor_result}
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
        png = parser_png(); (self.output / 'pixel-input.png').write_bytes(png)
        sample_code = "const fs=require('node:fs'),s=require(process.argv[1]);console.log(JSON.stringify(s.analyzeCanvasRaster(fs.readFileSync(process.argv[2]),{x:0,y:0,width:800,height:600})));"
        sample = subprocess.run([self.node, '-e', sample_code, str(SUITE), str(self.output / 'pixel-input.png')], capture_output=True, text=True, timeout=10, check=True)
        analysis = json.loads(sample.stdout); raster_hash = analysis.pop('raster_sha256')
        for index, label in enumerate(themes + ['edit-certified', 'external-conflict', 'syntax-retained', 'step-roundtrip']):
            name = f'parser-image-{index}.png'; (self.output / name).write_bytes(png)
            record = {'path': name, 'bytes': len(png), 'sha256': sha(png), 'width': 1600, 'height': 1000,
                'viewport': [1600, 1000], 'full_size': [1600, 1000], 'dpr': 1, 'horizontal_overflow': False, 'description': label,
                'rendering': {'model_tier': 'certified', 'model_hash': 'a' * 16, 'vertices': 30, 'meshes': 3, 'clipped': 0, 'nonfinite': 0,
                    'context_lost': False, 'canvas_rect': {'x': 0, 'y': 0, 'width': 800, 'height': 600}, 'camera_aspect': 800 / 600,
                    'pixel_ratio': 1, 'buffer': [800, 600], 'drawing_buffer': [800, 600]}}
            layout = {key: copy.deepcopy(record[key]) for key in ('viewport', 'full_size', 'dpr', 'horizontal_overflow', 'rendering')}
            layout['rendering'].update(model_project='WORKBENCHPLATE', model_gen=1, model_built_at=1, camera=[1,2,3], target=[0,0,0],
                                       theme='sand', theme_choice='sand', panel='#aaa', source_fingerprint='a'*64)
            record['raster_sha256'] = raster_hash
            record['rendering'] = copy.deepcopy(layout['rendering'])
            record['raster_attempts'] = []
            for attempt_index in range(2):
                sample_name = f'{name}-sample-{attempt_index}.png'; (self.output / sample_name).write_bytes(png)
                sample_image = {'path': sample_name, 'bytes': len(png), 'sha256': sha(png), 'width': 1600, 'height': 1000}
                record['raster_attempts'].append({'index': attempt_index, 'readiness_started_at': 1, 'deadline_at': 14001,
                    'started_at': 2+attempt_index*3, 'captured_at': 3+attempt_index*3, 'completed_at': 4+attempt_index*3,
                    'before': copy.deepcopy(layout), 'after': copy.deepcopy(layout), 'image': sample_image, 'observed_bytes': len(png),
                    'observed_sha256': sha(png), 'raster_sha256': record['raster_sha256'], 'pixels': copy.deepcopy(analysis), 'status': 'rendered'})
            self.report['screenshots'].append(record)
            self.report.setdefault('capture_attempts', []).append({'method': 'bounded serial physical raster readiness', 'description': label,
                'started_at':1,'deadline_at':15001,'completed_at':10,'attempts': copy.deepcopy(record['raster_attempts']), 'status': 'stable'})
            if index < len(themes):
                self.data['eight-themes']['states'].append({'name': label, 'styling': f'parser-style-{index}', 'persisted': True, 'visible': True, 'image_sha256': sha(png)})
        for record in self.report['screenshots']: record['canvas_pixels'] = copy.deepcopy(analysis)
        (self.output / 'capture-attempts.json').write_text(json.dumps(self.report['capture_attempts']))
        groups = {'source-edit': ['capability-boundary', 'open-folder-file', 'edit-save-volume', 'external-conflict', 'syntax-retains-certified', 'draft-recovery'],
                  'cad-roundtrip': ['source-transform', 'step-export-import'], 'workbench-ui': ['eight-themes', 'font-tabs', 'shortcut-focus']}
        self.report['scenarios'] = [{'id': group, 'pass': True, 'checks': [{'id': name, 'pass': True, 'evidence': self.data[name]} for name in names]} for group, names in groups.items()]

    def validate(self, report=None):
        file = self.output / 'workbench-results.json'; value = self.report if report is None else report; file.write_text(json.dumps(value))
        (self.output / 'capture-attempts.json').write_text(json.dumps(value.get('capture_attempts', [])))
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

    def test_physical_raster_readiness_is_serial_bounded_and_spatial(self):
        # Capture MODEL: actual PNG bytes, transparent transport functions, no renderer/source state mutation.
        png = parser_png(); blank = parser_png(blank=True)
        # Recompress the same ordered pixels; compressed byte equality is not raster equality.
        raw = zlib.decompress(png[41:-16]); alternate_data = zlib.compress(raw, 1)
        kind = b'IDAT'; chunk = struct.pack('>I', len(alternate_data)) + kind + alternate_data + struct.pack('>I', zlib.crc32(kind + alternate_data) & 0xffffffff)
        alternate = png[:33] + chunk + png[-12:]
        # Move the same colors to different columns, preserving color histograms but changing spatial raster.
        rows = bytearray(raw); stride = 1600 * 3 + 1
        for row in range(1000):
            start = row * stride + 1
            pixels = [rows[start+i:start+i+3] for i in range(0, 4800, 3)]
            rows[start:start+4800] = b''.join(pixels[2:] + pixels[:2])
        swapped_data = zlib.compress(bytes(rows)); swapped_chunk = struct.pack('>I', len(swapped_data)) + kind + swapped_data + struct.pack('>I', zlib.crc32(kind + swapped_data) & 0xffffffff)
        swapped = png[:33] + swapped_chunk + png[-12:]
        for name, data in (('a.png', png), ('a2.png', alternate), ('b.png', swapped), ('blank.png', blank)): (self.output / name).write_bytes(data)
        (self.output / 'layout.json').write_text(json.dumps(self.report['screenshots'][0]['raster_attempts'][0]['before']))
        code = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),s=require(process.argv[1]);
const root=process.argv[2],layout=JSON.parse(fs.readFileSync(path.join(root,'layout.json'))),clone=x=>structuredClone(x);
const pngs=Object.fromEntries(['a','a2','b','blank'].map(name=>[name,fs.readFileSync(path.join(root,name+'.png'))]));
assert.notDeepEqual(pngs.a,pngs.a2);assert.deepEqual(s.analyzeCanvasRaster(pngs.a,layout.rendering.canvas_rect),s.analyzeCanvasRaster(pngs.a2,layout.rendering.canvas_rect));
assert.deepEqual(s.canvasPixels(pngs.a,layout.rendering.canvas_rect),s.canvasPixels(pngs.b,layout.rendering.canvas_rect));
assert.notEqual(s.analyzeCanvasRaster(pngs.a,layout.rendering.canvas_rect).raster_sha256,s.analyzeCanvasRaster(pngs.b,layout.rendering.canvas_rect).raster_sha256);
async function scenario(name,sequence,mutation){let clock=1,takes=0,observations=0;const attempts=[];
 const ops={now:()=>clock,observe:async()=>{clock++;observations++;const value=clone(layout);if(mutation&&observations>1)value.rendering[mutation]=mutation==='camera'?[9,2,3]:'foreign';return value;},
 take:async()=>{clock++;return pngs[sequence[Math.min(takes++,sequence.length-1)]];},
 retain:async(bytes,index,before)=>{clock++;const name='sample-'+index+'.png';fs.writeFileSync(path.join(root,name),bytes);return{path:name,bytes:bytes.length,sha256:require('node:crypto').createHash('sha256').update(bytes).digest('hex'),width:1600,height:1000};}};
 if(name==='varying'){ops.take=async()=>{clock++;return pngs[takes++%2?'b':'a'];};}
 if(name==='timeout'){ops.retain=async()=>{clock+=15001;return{};};}
 if(name==='hung-retention'){ops.take=async()=>{clock=14999;return pngs.a;};ops.retain=async()=>new Promise(()=>{});}
 if(['blank','varying','timeout','hung-retention','identity'].includes(name)){await assert.rejects(s.captureStableRaster(ops,attempts));assert.ok(attempts.length<=8);}
 else{await s.captureStableRaster(ops,attempts);s.validateRasterAttempts(attempts,root);}
 if(name==='transient')assert.deepEqual(attempts.map(a=>a.status),['blank','rendered','rendered']);
 if(name==='converge')assert.equal(attempts.length,3);
 if(name==='blank'||name==='varying')assert.equal(attempts.length,8);
 if(name==='identity')assert.equal(takes,1,'Metadata change must fail immediately, not reset readiness');
}
(async()=>{await scenario('same',['a','a2']);await scenario('transient',['blank','a','a2']);await scenario('converge',['a','b','b']);
 await scenario('blank',['blank']);await scenario('varying',['a','b']);await scenario('timeout',['a']);await scenario('hung-retention',['a']);
 for(const field of ['model_hash','camera','theme','source_fingerprint'])await scenario('identity',['a'],field);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([self.node, '-e', code, str(SUITE), str(self.output)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entire_capture_lifecycle_shares_one_deadline(self):
        # MODELS ONLY: execute the actual outer capture body, including session creation, cleanup and final writes.
        png = parser_png(100, 100); (self.output / 'small.png').write_bytes(png)
        code = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),s=require(process.argv[1]);
const source=fs.readFileSync(process.argv[1],'utf8'),start=source.indexOf('  async function capture(description'),end=source.indexOf('  function retain(',start);assert.ok(start>0&&end>start);
const bytes=fs.readFileSync(process.argv[2]),root=process.argv[3],clone=x=>structuredClone(x);
async function scenario(mode){
 const output=path.join(root,mode);fs.mkdirSync(output);let clock=1000,detached=false;const report={viewport:[100,100],screenshots:[]};
 const layout={viewport:[100,100],full_size:[100,100],dpr:1,horizontal_overflow:false,rendering:{model_tier:'certified',model_project:'PUBLIC',model_hash:'a'.repeat(16),model_gen:1,model_built_at:1,
  vertices:30,meshes:1,clipped:0,nonfinite:0,context_lost:false,canvas_rect:{x:0,y:0,width:100,height:100},stage_size:[100,100],buffer:[100,100],drawing_buffer:[100,100],pixel_ratio:1,camera_aspect:1,camera:[1,2,3],target:[0,0,0]}};
 const session={send:async()=>{clock+=100;return{data:bytes.toString('base64')};},detach:async()=>{detached=true;clock+=mode==='detach-timeout'?15001:100;}};
 const page={context:()=>({newCDPSession:async()=>{clock+=mode==='create-timeout'?15001:100;return session;}}),evaluate:async()=>{clock+=100;return clone(layout);}};
 const nativeFS=Object.create(fs);nativeFS.writeFileSync=(file,data)=>{fs.writeFileSync(file,data);if(mode==='journal-timeout'&&path.basename(file)==='capture-attempts.json')clock+=15001;
  if(mode==='alias-timeout'&&path.basename(file)==='shot.png')clock+=15001;};
 const context={assert,fs:nativeFS,path,page,report,output,Date:{now:()=>clock},setTimeout,clearTimeout,Buffer,
  captureScripts:s.captureScripts,validateCaptureIdentity:s.validateCaptureIdentity,digest:data=>require('node:crypto').createHash('sha256').update(data).digest('hex'),
  validateRasterAttempts:s.validateRasterAttempts,validateRendering:s.validateRendering,
  captureStableRaster:(ops,attempts)=>s.captureStableRaster({...ops,now:()=>clock},attempts)};
 vm.runInNewContext(source.slice(start,end)+'globalThis.actualCapture=capture;',context);
 if(mode==='healthy'){await context.actualCapture('shot');assert.equal(report.screenshots.length,1);assert.ok(detached);assert.equal(report.capture_attempts[0].status,'stable');}
 else{await assert.rejects(context.actualCapture('shot'),/deadline/);assert.equal(report.screenshots.length,0);assert.equal(report.capture_attempts[0].status,'failed');}
 assert.equal(report.capture_attempts[0].started_at,1000);assert.equal(report.capture_attempts[0].deadline_at,16000);
}
(async()=>{for(const mode of ['healthy','create-timeout','detach-timeout','journal-timeout','alias-timeout'])await scenario(mode);})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([self.node, '-e', code, str(SUITE), str(self.output / 'small.png'), str(self.output)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_raster_camera_identity_allows_only_machine_roundoff(self):
        code = r"""
const assert=require('node:assert/strict'),s=require(process.argv[1]);
const original={viewport:[1600,1000],full_size:[1600,1000],dpr:1,horizontal_overflow:false,rendering:{
 model_tier:'certified',model_project:'WORKBENCHPLATE',model_hash:'115cbd1b88c84efe',model_gen:1,model_built_at:1792940835.9744327,
 canvas_rect:{x:712,y:202,width:592,height:770},stage_size:[592,770],buffer:[592,770],drawing_buffer:[592,770],pixel_ratio:1,
 camera_aspect:592/770,camera:[61.9191595537647,-87.99038462903351,56.41039886666711],target:[0,0,16],theme:'counterform-light',theme_choice:'system',panel:'#eee8db',source_fingerprint:'a'.repeat(64)}};
const changed=structuredClone(original);changed.rendering.camera=[61.91915955376471,-87.99038462903243,56.410398866666625];
s.validateRasterIdentity(original,changed,'actual Workbench18 roundoff');
for(const key of ['camera','target'])for(let index=0;index<3;index++){
 const noise=structuredClone(original);const magnitude=128*Number.EPSILON*Math.max(1,Math.abs(noise.rendering[key][index]));
 noise.rendering[key][index]+=magnitude/2;s.validateRasterIdentity(original,noise,'bounded coordinate roundoff');
 for(const delta of [1e-8,1e-3,magnitude*2]){const moved=structuredClone(original);moved.rendering[key][index]+=delta;
  assert.throws(()=>s.validateRasterIdentity(original,moved,'real camera change'));}
 const nonfinite=structuredClone(original);nonfinite.rendering[key][index]=NaN;assert.throws(()=>s.validateRasterIdentity(original,nonfinite,'nonfinite camera'));
}
for(const field of ['model_hash','model_gen','model_built_at','theme','panel','source_fingerprint','camera_aspect']){
 const other=structuredClone(original);other.rendering[field]=typeof other.rendering[field]==='number'?other.rendering[field]+Math.max(1e-12,Math.abs(other.rendering[field])*Number.EPSILON*4):'foreign';
 assert.throws(()=>s.validateRasterIdentity(original,other,'noncamera identity must remain exact'));
}
"""
        result = subprocess.run([self.node, '-e', code, str(SUITE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Physically coherent consumer evidence must use the same comparison as the producer.
        value = copy.deepcopy(self.report)
        for shot in value['screenshots']:
            for attempt in shot['raster_attempts']:
                attempt['after']['rendering']['camera'][1] += 32 * 2.220446049250313e-16
        for shot, journal in zip(value['screenshots'], value['capture_attempts']): journal['attempts'] = copy.deepcopy(shot['raster_attempts'])
        self.assertEqual(self.validate(value).returncode, 0)
        for field in ('camera', 'target'):
            moved = copy.deepcopy(value); moved['screenshots'][0]['raster_attempts'][0]['after']['rendering'][field][0] += 1e-8
            moved['capture_attempts'][0]['attempts'] = copy.deepcopy(moved['screenshots'][0]['raster_attempts'])
            self.assertNotEqual(self.validate(moved).returncode, 0)

    def test_capture_journal_timeline_must_contain_each_sample(self):
        for kind in ('early-inner-deadline', 'foreign-action-origin', 'action-before-samples'):
            value = copy.deepcopy(self.report); shot = value['screenshots'][0]; journal = value['capture_attempts'][0]
            if kind == 'early-inner-deadline':
                for attempt in shot['raster_attempts']: attempt['deadline_at'] = 2
            elif kind == 'foreign-action-origin': journal.update(started_at=100000, deadline_at=115000, completed_at=100001)
            else: journal['completed_at'] = 2
            journal['attempts'] = copy.deepcopy(shot['raster_attempts'])
            with self.subTest(coherent=kind): self.assertNotEqual(self.validate(value).returncode, 0)

    def test_raster_attempt_evidence_cannot_be_dropped_reordered_or_reclassified(self):
        for kind in ('missing', 'reordered', 'blank-label', 'spatial-hash', 'metadata', 'deadline', 'missing-image'):
            value = copy.deepcopy(self.report); shot = value['screenshots'][0]
            if kind == 'missing': shot['raster_attempts'].pop()
            elif kind == 'reordered': shot['raster_attempts'].reverse()
            elif kind == 'blank-label': shot['raster_attempts'][0]['status'] = 'blank'
            elif kind == 'spatial-hash': shot['raster_attempts'][0]['raster_sha256'] = '0' * 64
            elif kind == 'metadata': shot['raster_attempts'][0]['after']['rendering']['model_hash'] = 'b' * 16
            elif kind == 'deadline': shot['raster_attempts'][0]['completed_at'] = 15001
            else: (self.output / shot['raster_attempts'][0]['image']['path']).unlink()
            value['capture_attempts'][0]['attempts'] = copy.deepcopy(shot['raster_attempts'])
            with self.subTest(kind=kind): self.assertNotEqual(self.validate(value).returncode, 0)
            if kind == 'missing-image': (self.output / shot['raster_attempts'][0]['image']['path']).write_bytes(parser_png())

    def test_native_dialog_ledger_is_published_atomically(self):
        code = r'''
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'cq-ledger-')),file=path.join(root,'native-dialogs.json');
const source=fs.readFileSync(process.argv[1],'utf8');let concurrentReads=0;
const observed=Object.create(fs);observed.writeFileSync=(target,data)=>{if(fs.existsSync(file)){JSON.parse(fs.readFileSync(file));concurrentReads++;assert.notEqual(target,file);}return fs.writeFileSync(target,data);};
const win={once(){}},dialog={showOpenDialog:async()=>({canceled:false,filePaths:['/public/part.py']}),showSaveDialog:async()=>({canceled:true}),showMessageBox:async()=>({response:0})};
const body=source.slice(source.indexOf('function observeNativeDialogs('),source.indexOf('const FULL_CHECKS'));
const context=vm.createContext({fs:observed,path,process,require:name=>name==='electron'?{dialog}:require(name)});vm.runInContext(body+';globalThis.observe=observeNativeDialogs;',context);
(async()=>{try{context.observe(root,win,'owned');fs.writeFileSync(path.join(root,'native-action.json'),JSON.stringify({id:1}));await dialog.showOpenDialog(win,{});const value=JSON.parse(fs.readFileSync(file));assert.equal(value.calls[0].result.filePaths[0],'/public/part.py');assert.ok(value.calls[0].completed_at>=value.calls[0].started_at);assert.equal(concurrentReads,2);assert.equal(fs.existsSync(file+'.tmp'),false);}finally{fs.rmSync(root,{recursive:true,force:true});}})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([self.node, '-e', code, str(ROOT / 'tools/native_workbench_probe.cjs')], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_save_requires_real_exact_destination_completion(self):
        from unittest.mock import patch
        import workbench_native_smoke as native
        for mode in ('saved', 'wrong-path', 'canceled', 'foreign-action', 'no-completion'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                proof = Path(directory).resolve(); clock = [0.0]; finished = [False]; scripts = []
                target = str(proof / 'export.step'); challenge = 'c' * 64
                (proof / 'native-action.json').write_text(json.dumps({'schema':1,'id':4,'app_pid':1234,'challenge':challenge,'kind':'save','target':target,'trust':False,'observer_index':0}))
                class Process:
                    pid = 1234
                    def poll(self):
                        if (proof / 'native-action-result.json').exists(): (proof / 'native-workbench-main.json').write_text('{}')
                        return None
                def read(file):
                    if file.name != 'native-dialogs.json': return json.loads(file.read_text())
                    call = {'method':'showSaveDialog','action_id':99 if mode=='foreign-action' else 4,'result':None}
                    if finished[0] and mode!='no-completion': call['result']={'canceled':mode=='canceled','filePath':target if mode!='wrong-path' else str(proof / 'wrong.step')}
                    return {'app_pid':1234,'challenge':challenge,'calls':[call]}
                def input(pid, commands, *, deadline=None):
                    self.assertEqual(pid,1234); scripts.append(commands)
                    if 'clickedSave' in commands:
                        self.assertIn('set focused of nameField to true', commands)
                        self.assertIn('repeat until (value of nameField is "export" or value of nameField is "export.step")', commands)
                        self.assertIn('Native directory input acknowledgement deadline', commands)
                        self.assertIn('AXFocusedUIElement', commands)
                        self.assertIn('Native directory text field not focused', commands)
                        self.assertIn('set the clipboard to "export"', commands)
                        self.assertIn('keystroke "v" using command down', commands)
                        self.assertNotIn('set the clipboard to "export.step"', commands)
                        self.assertIn('if not enabled of button "Save"', commands)
                        self.assertEqual(commands.count('key code 36'), 1)
                        finished[0]=True
                def shot(file): file.write_bytes(b'panel');return {'path':str(file),'bytes':5,'sha256':'d'*64,'width':1000,'height':700}
                with patch.object(native.time,'monotonic',side_effect=lambda:clock[0]), patch.object(native.time,'sleep',side_effect=lambda n:clock.__setitem__(0,clock[0]+n)), patch.object(native,'read_json',side_effect=read), patch.object(native,'os_input',side_effect=input), patch.object(native,'screenshot',side_effect=shot), patch.object(native,'deny_ci_python_local_network',return_value={'status':'not-requested'}):
                    native.serve_native_actions(Process(),proof,challenge,proof)
                receipt=json.loads((proof / 'native-action-result.json').read_text())
                self.assertEqual(receipt['pass'],mode=='saved')
                if mode=='saved': self.assertEqual(receipt['native_call']['result']['filePath'],target)
                else: self.assertTrue(receipt['error'])

    def test_native_cancel_click_requires_the_actual_canceled_result(self):
        from unittest.mock import patch
        import workbench_native_smoke as native
        for mode in ('canceled', 'not-canceled', 'paths-returned', 'wrong-action'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                proof=Path(directory).resolve();clock=[0.0];clicked=[False];challenge='e'*64
                (proof/'native-action.json').write_text(json.dumps({'schema':1,'id':10,'app_pid':1234,'challenge':challenge,'kind':'cancel','trust':False,'observer_index':0}))
                class Process:
                    pid=1234
                    def poll(self):
                        if(proof/'native-action-result.json').exists():(proof/'native-workbench-main.json').write_text('{}')
                        return None
                def read(file):
                    if file.name!='native-dialogs.json':return json.loads(file.read_text())
                    return {'app_pid':1234,'challenge':challenge,'calls':[{'method':'showOpenDialog','action_id':9 if mode=='wrong-action' else 10,'result':{'canceled':mode!='not-canceled','filePaths':['/unexpected']if mode=='paths-returned'else[]}if clicked[0]else None}]}
                def input(pid,commands,*,deadline=None):
                    if 'click button "Cancel"' in commands:
                        self.assertIn('if not enabled of button "Cancel"',commands);clicked[0]=True
                def shot(file):file.write_bytes(b'panel');return{'path':str(file),'bytes':5,'sha256':'a'*64,'width':1000,'height':700}
                with patch.object(native.time,'monotonic',side_effect=lambda:clock[0]),patch.object(native.time,'sleep',side_effect=lambda n:clock.__setitem__(0,clock[0]+n)),patch.object(native,'read_json',side_effect=read),patch.object(native,'os_input',side_effect=input),patch.object(native,'screenshot',side_effect=shot),patch.object(native,'deny_ci_python_local_network',return_value={'status':'not-requested'}):
                    native.serve_native_actions(Process(),proof,challenge,proof)
                self.assertEqual(json.loads((proof/'native-action-result.json').read_text())['pass'],mode=='canceled')

    def test_fixture_clipboard_input_cannot_run_outside_github_ci(self):
        from unittest.mock import patch
        import workbench_native_smoke as native
        with patch.dict(native.os.environ, {}, clear=True), patch.object(native.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'restricted to disposable GitHub'):
                native.os_input(1234, 'set the clipboard to "public fixture"')
            run.assert_not_called()

    def test_native_transform_wait_rejects_the_previous_certificate(self):
        source = (ROOT / 'tools/native_workbench_probe.cjs').read_text()
        condition = 'index.hash !== beforeTransformIndex.hash && index.built_at > beforeTransformIndex.built_at'
        self.assertIn(condition, source)
        self.assertIn("traffic.trace.slice(transformTraceStart).some(item => item.action === 'transform'", source)
        code = "const assert=require('node:assert/strict');const beforeTransformIndex={hash:'old',built_at:10};const fresh=index=>" + condition + ";assert.equal(fresh({hash:'old',built_at:11}),false);assert.equal(fresh({hash:'new',built_at:10}),false);assert.equal(fresh({hash:'new',built_at:11}),true);"
        result = subprocess.run([self.node, '-e', code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_project_form_uses_actual_submit_control(self):
        source = (ROOT / 'tools/native_workbench_probe.cjs').read_text()
        full = source[source.index('async function qualifyWorkbench('):source.index('async function probe(')]
        self.assertEqual(full.count("() => click(win, '#project-create')"), 2)
        self.assertNotIn("key('Enter')", full)
        self.assertIn("await select('NATIVEROUNDTRIP', true)", full)
        self.assertIn("await click(win, '#buildbtn')", full)
        self.assertIn("explicit native rebuild after saved source", full)

    def test_python_network_denial_is_explicit_github_ci_only(self):
        # Modelled process receipts; actual prompt interaction must pass native CI.
        from unittest.mock import patch
        import workbench_native_smoke as native
        flag = 'CASSQUERY_CI_DENY_PYTHON_LOCAL_NETWORK'
        allowed = {flag: '1', 'GITHUB_ACTIONS': 'true', 'CI': 'true', 'RUNNER_OS': 'macOS'}
        with patch.dict(native.os.environ, {}, clear=True), patch.object(native.subprocess, 'run') as run:
            self.assertEqual(native.deny_ci_python_local_network(), {'status': 'not-requested'})
            run.assert_not_called()
        for key in ('GITHUB_ACTIONS', 'CI', 'RUNNER_OS'):
            invalid = {**allowed, key: 'false'}
            with self.subTest(key=key), patch.dict(native.os.environ, invalid, clear=True), \
                    patch.object(native.platform, 'system', return_value='Darwin'), patch.object(native.subprocess, 'run') as run:
                with self.assertRaises(RuntimeError): native.deny_ci_python_local_network()
                run.assert_not_called()
        for status in ('denied', 'not-present', 'unexpected'):
            result = subprocess.CompletedProcess([], 0, status + '\n', '')
            with self.subTest(status=status), patch.dict(native.os.environ, allowed, clear=True), \
                    patch.object(native.platform, 'system', return_value='Darwin'), patch.object(native.subprocess, 'run', return_value=result) as run:
                if status == 'unexpected':
                    with self.assertRaises(RuntimeError): native.deny_ci_python_local_network()
                else:
                    self.assertEqual(native.deny_ci_python_local_network()['status'], status)
                script = run.call_args.args[0][-1]
                self.assertIn('com.apple.UserNotificationCenter', script)
                self.assertIn('Allow “Python” to find devices on local networks?', script)
                self.assertIn('if (count matches) is not 1 then error', script)
                self.assertIn('if not enabled of denyButton then error', script)
                self.assertIn('click denyButton', script)
                self.assertNotIn('click button "Allow"', script)
                self.assertEqual(run.call_args.kwargs['timeout'], 20)
        with patch.dict(native.os.environ, allowed, clear=True), patch.object(native.platform, 'system', return_value='Darwin'), \
                patch.object(native.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'Permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'Permission denied'): native.deny_ci_python_local_network()

    def test_native_trust_waits_for_matching_dialog_events(self):
        # MODELS ONLY: actual wrapper function, fixed OS script stub, virtual event clock; not native macOS proof.
        from unittest.mock import patch
        import workbench_native_smoke as native
        challenge = 'a' * 64
        cases = ('delayed', 'late-button', 'missing-trust', 'wrong-action', 'wrong-path', 'wrong-buttons', 'cancel-response', 'early-trust', 'no-button', 'duplicate-trust', 'foreign-ledger', 'wrong-panel', 'boolean-response', 'linked-followup', 'invalid-followup-parent')
        for mode in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                proof = Path(directory).resolve(); target = proof / 'project'; target.mkdir(); clock = [0.0]; selected = [False]; clicked = [False]; scripts = []
                action = {'schema':1,'id':2,'app_pid':1234,'challenge':challenge,'kind':'folder','target':str(target),'trust':True,'observer_index':0}
                if mode in ('linked-followup', 'invalid-followup-parent'): action.update(observer_index=1, dialog_action_id=1)
                (proof / 'native-action.json').write_text(json.dumps(action))
                class Process:
                    pid = 1234
                    def poll(self):
                        if (proof / 'native-action-result.json').exists(): (proof / 'native-workbench-main.json').write_text('{}')
                        return None
                def ledger():
                    chooser = {'method':'showMessageBox' if mode=='wrong-panel' else 'showOpenDialog','action_id':2,'properties':['openDirectory'],'started_at':1000,'result':None}
                    if selected[0] and clock[0] >= 10.7:
                        chooser.update(result={'canceled':False,'filePaths':[str(target) if mode!='wrong-path' else str(proof / 'foreign')]},completed_at=11700)
                    calls = [chooser]
                    if mode in ('linked-followup', 'invalid-followup-parent'):
                        chooser.update(action_id=1, properties=['openDirectory', 'createDirectory'], started_at=10000)
                        calls.insert(0, {'method':'showOpenDialog','action_id':1,'properties':['openFile'], 'started_at':1000,'completed_at':9000,
                                         'result':{'canceled':mode=='invalid-followup-parent','filePaths':[str(proof / 'export.step')]}})
                    if selected[0] and clock[0] >= 10.8 and mode!='missing-trust':
                        trust = {'method':'showMessageBox','action_id':99 if mode=='wrong-action' else 2,
                                 'buttons':['Other','Cancel'] if mode=='wrong-buttons' else ['Trust and Open','Cancel'],
                                 'started_at':11000 if mode=='early-trust' else 11800,'result':None}
                        if clicked[0]: trust.update(result={'response':1 if mode=='cancel-response' else False if mode=='boolean-response' else 0},completed_at=12000)
                        calls.append(trust)
                        if mode=='duplicate-trust': calls.append(copy.deepcopy(trust))
                    return {'schema':1,'app_pid':1234,'challenge':'foreign' if mode=='foreign-ledger' else challenge,'calls':calls}
                def read(file): return ledger() if file.name=='native-dialogs.json' else json.loads(file.read_text())
                def os_input(pid, commands, *, deadline=None):
                    self.assertEqual(pid,1234);scripts.append((clock[0],commands,deadline))
                    if 'keystroke "g"' in commands:
                        self.assertNotIn('Trust and Open',commands); selected[0]=True;clock[0]+=1.4
                    elif 'clickedOpen' in commands:
                        self.assertIn('if exists button "Open"', commands)
                        self.assertIn('if enabled of button "Open"', commands)
                        self.assertLessEqual(deadline, 20)
                    elif 'clickedTrust' in commands:
                        index = action['observer_index']
                        self.assertTrue(ledger()['calls'][index].get('completed_at'));self.assertEqual(ledger()['calls'][index+1]['action_id'],2)
                        self.assertGreaterEqual(clock[0],10.8);self.assertIn('if exists button "Trust and Open"',commands)
                        self.assertIn('if enabled of button "Trust and Open"',commands)
                        self.assertLessEqual(deadline,20);self.assertIn('repeat while (current date) < trustDeadline',commands)
                        if mode=='no-button': raise RuntimeError('Matching Trust and Open control unavailable')
                        if mode=='late-button': clock[0]+=2.0
                        clicked[0]=True
                    return 'bounded button names'
                def screenshot(file): file.write_bytes(b'raw panel');return {'path':str(file),'bytes':9,'sha256':'b'*64,'width':1000,'height':700}
                with patch.object(native.time,'monotonic',side_effect=lambda:clock[0]), patch.object(native.time,'sleep',side_effect=lambda amount:clock.__setitem__(0,clock[0]+amount)), \
                     patch.object(native,'read_json',side_effect=read), patch.object(native,'os_input',side_effect=os_input), patch.object(native,'screenshot',side_effect=screenshot):
                    native.serve_native_actions(Process(),proof,challenge,proof)
                response = json.loads((proof / 'native-action-result.json').read_text())
                self.assertEqual(response['pass'],mode in ('delayed','late-button','linked-followup'))
                if response['pass']:
                    self.assertEqual(response['trust_call']['result']['response'],0)
                    self.assertEqual(response['native_call']['result']['filePaths'],[str(target)])
                    self.assertTrue(clicked[0]);self.assertLess(clock[0],20)
                else:
                    self.assertTrue(response['error']);self.assertIn('failure_image',response)
                    if mode in ('missing-trust','wrong-action','wrong-path','wrong-buttons','early-trust','duplicate-trust','foreign-ledger','wrong-panel'): self.assertFalse(clicked[0])

    def test_native_closed_select_typeahead_requires_real_effects(self):
        # MODELS ONLY: execute the actual helper; no model success claims native platform qualification.
        code = r"""
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8'),start=source.indexOf('  async function selectOption(selector, value) {'),end=source.indexOf('  async function text(selector, value)',start);assert.ok(start>0&&end>start);
async function model(mode){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-select-MODEL-')),report={},events=new Map(),tabs=['tabs','lazy-tabs'].includes(mode);
 const positive=['healthy','tabs','lazy','lazy-tabs'].includes(mode);
 const selector=tabs?'#tab-size':'#theme-select',value=tabs?'2':'counterform-dark',label=tabs?'2 spaces':'Counterform Dark';
 const input={id:selector.slice(1),disabled:mode==='disabled',value:tabs?'4':'system',selectedIndex:0,matches:()=>mode==='popup',
  options:[{value:tabs?'4':'system',text:tabs?'4 spaces':'System'},{value,text:label}]};
 const close={id:'settings-close',closest:()=>({})},after={id:'after',closest:()=>({})};input.closest=()=>({});
 const document={activeElement:close,documentElement:{dataset:{theme:'counterform-light'}},querySelector:()=>input,getElementById:()=>({open:true}),
  addEventListener(type,listener){events.set(type,listener);},removeEventListener(type){events.delete(type);}};
 const window={},context=vm.createContext({window,document,JSON,requestAnimationFrame:callback=>callback()});let typed='';
 const settingsFile=path.join(directory,'workbench-settings.json');
 if(!['lazy','lazy-tabs','missing-after-input'].includes(mode))fs.writeFileSync(settingsFile,JSON.stringify({theme:'system',tabSize:4}));
 const win={webContents:{sendInputEvent(event){assert.equal(event.type,'char');assert.ok(!event.modifiers);typed+=event.keyCode;
  if(typed===label&&mode!=='noop'){
   input.value=mode==='wrong-value'?'foreign':value;input.selectedIndex=mode==='wrong-value'?0:1;
   if(mode!=='no-event')for(const type of ['input','change'])events.get(type)?.({type,target:input,isTrusted:mode!=='untrusted'});
   document.documentElement.dataset.theme=value;
   if(!['unchanged-persist','missing-after-input'].includes(mode))fs.writeFileSync(settingsFile,mode==='malformed-after-input'?'not JSON':JSON.stringify({theme:value,tabSize:Number(value)}));
  }
 }}};
 const observedFS=Object.create(fs);
 observedFS.readFileSync=(file,...args)=>{if(mode==='read-denied'&&file===settingsFile)throw Object.assign(new Error('Actual read denied'),{code:'EACCES'});return fs.readFileSync(file,...args);};
 const globals={assert,report,fs:observedFS,path,directory,win,process:{env:{CASSQUERY_USER_DATA:directory}},
  js:async code=>vm.runInContext(code,context),waitFor:async(_win,code)=>{if(!vm.runInContext(code,context))throw Error('Observed condition missing');},
  key:code=>{assert.equal(code,'Tab');if(mode!=='wrong-focus')document.activeElement=document.activeElement===input?after:input;},
  until:async(_label,predicate)=>{const value=await predicate();if(!value)throw Error('Persisted effect missing');return value;},image:async()=>{}};
 vm.runInNewContext(source.slice(start,end)+'globalThis.actualSelect=selectOption;',globals);
 try{
  if(positive){await globals.actualSelect(selector,value);assert.equal(input.value,value);assert.equal(document.activeElement,after);}
  else await assert.rejects(globals.actualSelect(selector,value));
  const audit=JSON.parse(fs.readFileSync(path.join(directory,'select-diagnostics.json')))[0];assert.equal(audit.pass,positive);
  assert.equal(audit.method,'native keyboard closed-select typeahead');
  if(mode==='read-denied'){assert.equal(audit.phases.length,0);assert.equal(typed,'');}
  else assert.ok(audit.phases.length>0);
  if(['lazy','lazy-tabs'].includes(mode)){assert.equal(audit.phases[0].persisted,null);assert.equal(audit.phases[1].persisted[tabs?'tabSize':'theme'],tabs?2:value);}
  if(mode==='missing-after-input'){assert.equal(audit.phases[0].persisted,null);assert.equal(fs.existsSync(settingsFile),false);}
  assert.ok(!window.__cqNativeSelectAudit&&events.size===0);
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
}
(async()=>{for(const mode of['healthy','tabs','lazy','lazy-tabs','missing-after-input','malformed-after-input','read-denied','wrong-focus','popup','disabled','noop','no-event','untrusted','wrong-value','unchanged-persist'])await model(mode);})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        file = self.output / 'native-select-models.cjs'; file.write_text(code)
        result = subprocess.run([self.node, str(file), str(ROOT / 'tools/native_workbench_probe.cjs')], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_capture_waits_for_certified_identity_not_stable_preview(self):
        # Explicit scheduler/layout MODELS exercising the real serialized readiness code.
        code = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),s=require(process.argv[1]);
const scripts=s.captureScripts(function measured(){return {vertices:30,meshes:1,clipped:0,nonfinite:0};});
async function model(mode){
 let clock=0,frames=0,cameraY=2,targetX=0;const cq={model:{tier:'certified',hash:'a'.repeat(16),index:{project:'WORKBENCHPLATE',gen:2,built_at:5}},
  status:{state:mode==='error'?'error':'certified'},rebuilding:false,camera:{aspect:1,position:{toArray:()=>[1,cameraY,3]}},controls:{target:{toArray:()=>[targetX,0,0]}}};
 const canvas={width:100,height:100,getBoundingClientRect:()=>({left:0,top:0,width:100,height:100}),getContext:()=>({drawingBufferWidth:100,drawingBufferHeight:100,isContextLost:()=>false})};
 const context={window:{__cq:cq},document:{images:[],documentElement:{scrollHeight:100,scrollWidth:100,dataset:{theme:'sand',themeChoice:'sand'}},querySelector:()=>canvas,getElementById:()=>({clientWidth:100,clientHeight:100})},
  getComputedStyle:()=>({getPropertyValue:()=> '#aaa'}),
  innerWidth:100,innerHeight:100,devicePixelRatio:1,scrollX:0,scrollY:0,performance:{now:()=>clock},
  requestAnimationFrame(callback){clock+=100;frames++;if(mode==='preview'||(mode==='transition'&&frames<=4))cq.model.tier='preview';else cq.model.tier='certified';
   if(mode==='changing')cq.model.hash=(frames%4<2?'a':'b').repeat(16);if(mode==='building')cq.status.state='building-certified';
   if(mode==='camera-drift'||(mode==='camera-settles'&&frames<=8))cameraY+=Number.EPSILON*8;
   if(mode==='target-drift'||(mode==='target-settles'&&frames<=8))targetX+=Number.EPSILON*8;
   callback();}};
 const promise=vm.runInNewContext(scripts.settled,context);
 if(['preview','changing','building','camera-drift','target-drift'].includes(mode)){
  await assert.rejects(promise,error=>{assert.match(error.message,/did not settle/);const marker='; last_readiness=';
   assert.ok(error.message.includes(marker));const recent=JSON.parse(error.message.split(marker)[1]);assert.equal(recent.length,2);
   assert.ok(recent[0].observed_at<recent[1].observed_at&&recent[1].observed_at<=10000);
   assert.ok(recent.every(snapshot=>snapshot.stable<2&&snapshot.rendering.camera.length===3&&snapshot.rendering.target.length===3));
   if(['camera-drift','target-drift'].includes(mode)){assert.ok(recent.every(snapshot=>snapshot.aligned&&snapshot.key_changed));
    assert.notDeepEqual(recent[0].rendering[mode==='camera-drift'?'camera':'target'],recent[1].rendering[mode==='camera-drift'?'camera':'target']);}
   return true;});return;
 }
 const value=await promise;assert.equal(value.rendering.model_tier,'certified');assert.ok(frames>=6);
 if(mode==='transition')assert.ok(frames>=10,'Preview ticks cannot count toward certified readiness');
 if(['camera-settles','target-settles'].includes(mode))assert.ok(frames>=12,'Moving control coordinates cannot count toward fixed-point readiness');
 return JSON.parse(JSON.stringify(value));
}
(async()=>{
 for(const mode of ['preview','transition','changing','error','building','camera-drift','target-drift','camera-settles','target-settles'])await model(mode);
 const before=await model('healthy');s.validateCaptureIdentity(before,before,{project:'WORKBENCHPLATE',hash:'a'.repeat(16),built_at:5});
 for(const [key,value]of[['model_tier','preview'],['model_project','FOREIGN'],['model_hash','b'.repeat(16)],['model_gen',3],['model_built_at',6]]){
  const after=structuredClone(before);after.rendering[key]=value;assert.throws(()=>s.validateCaptureIdentity(before,after));
 }
 assert.throws(()=>s.validateCaptureIdentity(before,before,{project:'WORKBENCHPLATE',hash:'b'.repeat(16),built_at:5}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([self.node, '-e', code, str(SUITE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_native_text_helper_requires_input_acknowledgements(self):
        # MODELS ONLY: execute the actual helper body, not a copied implementation or native proof.
        code = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),crypto=require('node:crypto');
const source=fs.readFileSync(process.argv[2],'utf8'),start=source.indexOf('  async function text(selector, value) {'),end=source.indexOf('  async function until(label, predicate)',start);
assert.ok(start>0&&end>start);const body=source.slice(start,end);
async function model(mode){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-native-text-MODEL-')),calls=[],report={},events=new Map();
 const numeric=mode==='number',selector=numeric?'#editor-font-size':'#source-editor',expected=numeric?'22':'A\nΩ—B';
 const input={id:selector.slice(1),type:numeric?'number':'textarea',value:numeric?'14':'old source',readOnly:mode==='readonly',disabled:false,selectionStart:numeric?null:3,selectionEnd:numeric?null:3};
 const doc={path:'part_entry.py',baseSha:'a'.repeat(64),revision:2,dirty:false,text:input.value};
 const document={activeElement:{id:'old-focus'},querySelector:()=>input,addEventListener(type,fn){events.set(type,fn);},removeEventListener(type){events.delete(type);}};
 const window={__workbench:{active:doc}},context=vm.createContext({window,document,JSON});
 const emit=(type,more={})=>events.get(type)?.({type,target:input,isTrusted:true,...more});
 const win={focus(){calls.push('focus');},webContents:{selectAll(){calls.push('select-all');if(mode!=='partial-selection'){input.selectionStart=numeric?null:0;input.selectionEnd=numeric?null:input.value.length;}emit('select');},async insertText(value){calls.push('IME');if(mode==='noop')return;
  if(mode!=='no-events')emit('beforeinput',{data:value,inputType:'insertText'});input.value=value;input.selectionStart=numeric?null:value.length;input.selectionEnd=numeric?null:value.length;
  if(mode!=='no-document'){doc.text=value;doc.revision++;doc.dirty=true;}if(mode!=='no-events')emit('input',{data:value,inputType:'insertText'});
 }}};
 const globals={assert,Buffer,fs,path,report,directory,win,hash:bytes=>crypto.createHash('sha256').update(bytes).digest('hex'),
  js:async code=>vm.runInContext(code,context),click:async()=>{calls.push('click');if(mode!=='wrong-focus')document.activeElement=input;},
  shortcut:()=>{throw Error('Bulk IME setup must not claim a keyboard Select All shortcut');},
  waitFor:async(_win,code)=>{calls.push('ack');if(!vm.runInContext(code,context))throw Error('Model acknowledgement absent');},
  bounded:async work=>work(),image:async()=>{calls.push('failure-capture');},console:{error(){}}};
 vm.runInNewContext(body+'globalThis.actualText=text;',globals);
 try{
  if(['healthy','number'].includes(mode))await globals.actualText(selector,expected);else await assert.rejects(globals.actualText(selector,expected));
  if(['wrong-focus','readonly','partial-selection'].includes(mode))assert.ok(!calls.includes('IME'));
  if(['healthy','number'].includes(mode)){assert.equal(input.value,expected);assert.ok(calls.indexOf('IME')>calls.indexOf('select-all'));}
  const raw=JSON.parse(fs.readFileSync(path.join(directory,'text-input-1.json')));assert.equal(raw.pass,['healthy','number'].includes(mode));
  assert.equal(raw.selection_method,'native webContents.selectAll edit command');
  assert.equal(raw.method,'native focused IME text insertion with native Edit selection');
  assert.ok(raw.phases.length>0&&raw.phases.every(phase=>Buffer.byteLength(phase.value)<=16384));
  assert.ok(!window.__cqNativeInputAudit&&events.size===0);
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
}
(async()=>{for(const mode of['healthy','number','wrong-focus','readonly','partial-selection','noop','no-events','no-document'])await model(mode);})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        file = self.output / 'native-text-models.cjs'; file.write_text(code)
        result = subprocess.run([self.node, str(file), str(ROOT / 'tools/native_workbench_probe.cjs')], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_red_native_raw_proof_is_bounded_and_preserved_byte_for_byte(self):
        import signal
        import workbench_native_smoke as native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); user = root / 'user'; user.mkdir(); proof = user / 'proof'; proof.mkdir(); output = root / 'output'; output.mkdir()
            raw = b'{"pass":false,"errors":["actual input failure"]}\n'
            (proof / 'workbench-editor.json').write_bytes(raw)
            (proof / 'text-input-1.json').write_bytes(b'{"active":"source-editor","value":"public fixture"}\n')
            process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], start_new_session=True)
            try:
                os.killpg(process.pid, signal.SIGTERM); process.wait(timeout=5)
                records = native.retain_native_proof(proof, output, user)
                self.assertEqual(len(records), 2)
                for item in records:
                    data = (output / item['path']).read_bytes()
                    self.assertEqual(sha(data), item['sha256']); self.assertEqual(len(data), item['bytes'])
                shutil.rmtree(user)
                self.assertEqual((output / 'proof/workbench-editor.json').read_bytes(), raw)
                self.assertFalse(json.loads(raw)['pass'])
            finally:
                if process.poll() is None: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)

    def test_native_raw_proof_rejects_escaped_unbounded_and_changed_bytes(self):
        import workbench_native_smoke as native
        from unittest.mock import patch
        for mode in ('symlink', 'directory', 'oversize', 'aggregate', 'count', 'changed', 'existing-output'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); user = root / 'user'; user.mkdir(); proof = user / 'proof'; proof.mkdir(); output = root / 'output'; output.mkdir()
                file = proof / 'raw.json'; file.write_bytes(b'{"pass":false}')
                if mode == 'symlink': (proof / 'external').symlink_to(root / 'foreign')
                elif mode == 'directory': (proof / 'nested').mkdir()
                elif mode == 'oversize':
                    with file.open('wb') as stream: stream.truncate(32 * 1024**2 + 1)
                elif mode == 'aggregate':
                    for index in range(5):
                        with (proof / f'large-{index}').open('wb') as stream: stream.truncate(32 * 1024**2)
                elif mode == 'count':
                    for index in range(256): (proof / f'raw-{index}').write_bytes(b'')
                elif mode == 'existing-output': (output / 'proof').mkdir()
                if mode == 'changed':
                    original_write = Path.write_bytes
                    def replace(target, data):
                        value = original_write(target, data)
                        if target.parent == output / 'proof': original_write(file, b'new external bytes')
                        return value
                    with patch.object(Path, 'write_bytes', replace), self.assertRaises(RuntimeError): native.retain_native_proof(proof, output, user)
                else:
                    with self.subTest(mode=mode), self.assertRaises(RuntimeError): native.retain_native_proof(proof, output, user)

    def test_constructor_preload_identity_and_ipc_contract_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); outer = native_qualification_model(root)
            main = json.loads((root / 'proof/native-workbench-main.json').read_text())
            report = main['result']; resources = root / 'CassQuery.app/Contents/Resources'
            preload = resources / 'app.asar/shell/preload.cjs'; preload.parent.mkdir(parents=True)
            original = (ROOT / 'shell/preload.cjs').read_bytes(); preload.write_bytes(original)
            identity = {**report['preload_identity'], 'resources_path': str(resources), 'path': str(preload)}
            report.update(preload_identity=identity, resources_path=str(resources))
            evidence = report['checks'][0]['evidence']; evidence['preload_identity'] = identity
            packet = {'identity': identity, 'report': report, 'evidence': evidence,
                      'context': {'resourcesPath': str(resources), 'webContentsId': report['web_contents_id'], 'nativeSource': report['native_source']}}
            file = root / 'preload-model.json'
            code = "const fs=require('node:fs'),n=require(process.argv[1]),p=JSON.parse(fs.readFileSync(process.argv[2]));n.validatePreloadIdentity(p.identity,p.context);n.validatePreloadIPC(p.evidence,p.report);"
            def validate(value):
                file.write_text(json.dumps(value))
                return subprocess.run([self.node, '-e', code, str(ROOT / 'tools/native_workbench_probe.cjs'), str(file)], capture_output=True, text=True, timeout=10)
            positive = validate(packet); self.assertEqual(positive.returncode, 0, positive.stderr)
            mutations = [lambda p: p.update(identity=None), lambda p: p['identity'].update(path=str(root / 'foreign/preload.cjs')),
                lambda p: p['identity'].update(path=preload.as_uri()), lambda p: p['identity'].update(sha256='0' * 64),
                lambda p: p['identity'].update(bytes=0), lambda p: p['identity'].update(web_contents_id=999),
                lambda p: p['context'].update(resourcesPath=str(root / 'foreign')), lambda p: p['evidence']['bridge']['keys'].pop(),
                lambda p: p['evidence']['bridge']['keys'].append('invoke'), lambda p: p['evidence']['bridge'].update(node_require_type='function'),
                lambda p: p['evidence']['bridge'].update(node_process_type='object'), lambda p: p['evidence']['ipc'].update(window_id=999),
                lambda p: p['evidence']['ipc'].update(web_contents_id=999), lambda p: p['evidence']['ipc'].update(origin='http://127.0.0.1:4040'),
                lambda p: p['evidence']['ipc'].update(settings_result={'schema': 1}), lambda p: p['evidence']['ipc'].update(workbench_http_status=500),
                lambda p: p['evidence']['ipc'].update(workbench_result={}), lambda p: p['report'].update(worker_requests=[]),
                lambda p: p['report']['worker_requests'][0].update(status=500)]
            foreign = root / 'foreign/preload.cjs'; foreign.parent.mkdir(); foreign.write_bytes(original)
            for mutation in mutations:
                self.assertEqual(validate(packet).returncode, 0)
                value = copy.deepcopy(packet); mutation(value)
                with self.subTest(mutation=mutation): self.assertNotEqual(validate(value).returncode, 0)
            for name, field, replacement in (('part_entry.py', 'bytes', 0), ('part_entry.py', 'kind', 'asset'),
                                              ('viewer.json', 'bytes', 0), ('viewer.json', 'kind', 'python')):
                self.assertEqual(validate(packet).returncode, 0)
                value = copy.deepcopy(packet)
                replies = [value['evidence']['ipc']['workbench_result'], value['evidence']['ipc']['workbench_http_response'],
                           value['report']['worker_requests'][0]['response']]
                for reply in replies: next(entry for entry in reply['files'] if entry['path'] == name)[field] = replacement
                with self.subTest(required_file=name, field=field): self.assertNotEqual(validate(value).returncode, 0)
            empty_helper = copy.deepcopy(packet)
            for reply in (empty_helper['evidence']['ipc']['workbench_result'], empty_helper['evidence']['ipc']['workbench_http_response'],
                          empty_helper['report']['worker_requests'][0]['response']):
                reply['files'].append({'path': 'helper.py', 'kind': 'python', 'bytes': 0})
            self.assertEqual(validate(empty_helper).returncode, 0, 'An unrelated empty helper is valid source inventory')
            preload.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
            self.assertNotEqual(validate(packet).returncode, 0, 'Changed actual preload bytes must not reuse the identity')
            preload.write_bytes(original)
            self.assertEqual(validate(packet).returncode, 0)

    def test_portable_preload_receipt_rehashed_contradictions_are_rejected(self):
        import workbench_native_smoke as native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); report = native_qualification_model(root)
            native.validate_qualification(report, root, expected_sha='a' * 40, expected_arch='arm64')
            full = json.loads((root / 'proof/workbench-qualification.json').read_text())
            main_file = root / 'proof/native-workbench-main.json'; healthy_main = json.loads(main_file.read_text())
            for kind in ('identity-path', 'identity-window', 'bridge', 'ipc-trace', 'ipc-closed', 'read-only', 'entry-empty', 'entry-asset', 'manifest-empty', 'manifest-python'):
                main_file.write_text(json.dumps(healthy_main))
                value = rehash_native_model(report, root, full)
                native.validate_qualification(value, root, expected_sha='a' * 40, expected_arch='arm64')
                main = copy.deepcopy(healthy_main); evidence = main['result']['checks'][0]['evidence']
                if kind == 'identity-path': main['result']['preload_identity']['path'] = '/foreign/preload.cjs'
                elif kind == 'identity-window': main['result']['web_contents_id'] = 999
                elif kind == 'bridge': evidence['bridge']['keys'].pop()
                elif kind == 'ipc-trace': main['result']['worker_requests'] = []
                elif kind == 'ipc-closed': evidence['ipc']['workbench_http_status'] = 500
                elif kind == 'read-only': evidence['ipc']['workbench_result']['read_only'] = True
                else:
                    name = 'part_entry.py' if kind.startswith('entry-') else 'viewer.json'
                    field = 'bytes' if kind.endswith('empty') else 'kind'
                    replacement = 0 if field == 'bytes' else 'asset' if kind == 'entry-asset' else 'python'
                    for reply in (evidence['ipc']['workbench_result'], evidence['ipc']['workbench_http_response'], main['result']['worker_requests'][0]['response']):
                        next(entry for entry in reply['files'] if entry['path'] == name)[field] = replacement
                main_file.write_text(json.dumps(main)); value = rehash_native_model(report, root, full)
                with self.subTest(rehashed=kind), self.assertRaises((ValueError, RuntimeError)):
                    native.validate_qualification(value, root, expected_sha='a' * 40, expected_arch='arm64')

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
