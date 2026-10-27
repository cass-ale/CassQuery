#!/usr/bin/env python3
"""Isolated packaged editor proof plus standard-permission CI native chooser attempts.

This is not the legacy 16-cell smoke, and it never grants TCC permissions or
promotes editor-only success to full workbench qualification.
"""
import argparse
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import urllib.request
import zlib

from macos_smoke import ROOT, architecture, file_evidence, inventory, require, verify_bundle, write_report
from release_evidence import read_json, source_snapshot
from png_evidence import dimensions


def measure_step_readback(file, *, source_sha):
    """Actual trusted-process CadQuery import; no report-selected executable or project Python."""
    import cadquery as cq
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    file = Path(file)
    before = hashlib.sha256(file.read_bytes()).hexdigest()
    shape = cq.importers.importStep(str(file), unit='MM').val()
    require(shape.isValid() and len(shape.Solids()) > 0, 'Native STEP import is not valid solid geometry')
    bounds = Bnd_Box(); BRepBndLib.AddOptimal_s(shape.wrapped, bounds, False, False); coords = bounds.Get()
    geometry = {'volume': shape.Volume(), 'solids': len(shape.Solids()), 'min': list(coords[:3]), 'max': list(coords[3:])}
    require(hashlib.sha256(file.read_bytes()).hexdigest() == before, 'Native STEP changed during actual readback')
    return {'schema': 1, 'method': 'actual CadQuery STEP import in native runner', 'step_sha256': before,
            'geometry': geometry, 'units': 'mm', 'variant': 'neutral', 'source_sha': source_sha,
            'cadquery_version': cq.__version__, 'python_version': platform.python_version()}


def same_geometry(actual, expected):
    for item in (actual, expected):
        require(type(item.get('volume')) in (int, float) and math.isfinite(item['volume']) and item['volume'] > 0,
                'Native geometry has invalid volume')
        require(type(item.get('solids')) is int and item['solids'] > 0, 'Native geometry has no solids')
        for key in ('min', 'max'):
            require(isinstance(item.get(key), list) and len(item[key]) == 3 and all(type(n) in (int, float) and math.isfinite(n) for n in item[key]),
                    'Native geometry has invalid bounds')
        require(all(item['max'][i] > item['min'][i] for i in range(3)), 'Native geometry has empty bounds')
    require(actual['solids'] == expected['solids'] and abs(actual['volume'] - expected['volume']) < expected['volume'] * 1e-6
            and all(abs(actual[key][i] - expected[key][i]) < 1e-5 for key in ('min', 'max') for i in range(3)),
            'Native STEP reimport contradicts claimed geometry')


def canvas_pixels(data, rect):
    """Validate fresh immutable PNG bytes; memoize only pure pixel analysis, never evidence verdicts."""
    require(type(data) is bytes, 'PNG cache input must be immutable bytes')
    dimensions(data)  # Fresh bounded format/CRC/deflate validation precedes every cache lookup.
    roi = tuple(rect.get(key) for key in ('x', 'y', 'width', 'height'))
    require(all(type(value) in (int, float) and math.isfinite(value) for value in roi)
            and roi[0] >= 0 and roi[1] >= 0 and roi[2] > 0 and roi[3] > 0, 'Invalid native canvas pixel region')
    counts = _canvas_pixels(data, roi)
    return dict(zip(('samples', 'distinct_colors', 'non_background_samples'), counts))


@lru_cache(maxsize=4)  # At most four validated <=32 MiB compressed byte keys; immutable result tuples.
def _canvas_pixels(data, roi):
    width, height = int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    channels = {2: 3, 6: 4}[data[25]]
    packed = bytearray(); offset = 8
    while offset < len(data):
        length = int.from_bytes(data[offset:offset + 4], 'big')
        if data[offset + 4:offset + 8] == b'IDAT': packed.extend(data[offset + 8:offset + 8 + length])
        offset += 12 + length
    stride = width * channels; expected = (stride + 1) * height
    decoder = zlib.decompressobj(); raw = decoder.decompress(bytes(packed), expected + 1)
    require(len(raw) == expected and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, 'Invalid PNG pixel stream')
    x, y, roi_width, roi_height = roi
    left, right = max(0, math.ceil(x + roi_width * .1)), min(width, math.floor(x + roi_width * .9))
    top, bottom = max(0, math.ceil(y + roi_height * .1)), min(height, math.floor(y + roi_height * .9))
    require(right > left and bottom > top, 'Native canvas pixel region is empty')
    histogram = {}; samples = 0; previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1); kind = raw[start]; row = bytearray(raw[start + 1:start + 1 + stride])
        if kind:
            for x in range(stride):
                a = row[x - channels] if x >= channels else 0; b = previous[x]; c = previous[x - channels] if x >= channels else 0
                if kind == 1: prediction = a
                elif kind == 2: prediction = b
                elif kind == 3: prediction = (a + b) // 2
                else:
                    p = a + b - c; distances = (abs(p - a), abs(p - b), abs(p - c))
                    prediction = (a, b, c)[distances.index(min(distances))]
                row[x] = (row[x] + prediction) & 255
        if top <= y < bottom:
            for x in range(left, right, 2):
                i = x * channels; color = (row[i] << 16) | (row[i + 1] << 8) | row[i + 2]
                histogram[color] = histogram.get(color, 0) + 1; samples += 1
        previous = row
    return samples, len(histogram), samples - max(histogram.values(), default=0)


def validate_rendered_image(image, data):
    rendering = image.get('rendering', {}); rect = rendering.get('canvas_rect', {})
    require(rendering.get('model_tier') == 'certified' and isinstance(rendering.get('model_hash'), str)
            and re.fullmatch(r'[a-f0-9]{16}', rendering['model_hash']), 'Native rendered certificate missing')
    require(all(type(rendering.get(key)) is int and rendering[key] > 0 for key in ('vertices', 'meshes'))
            and rendering.get('clipped') == 0 and rendering.get('nonfinite') == 0 and rendering.get('context_lost') is False,
            'Native projected geometry is missing or invalid')
    require(all(type(rect.get(key)) in (int, float) and math.isfinite(rect[key]) for key in ('x', 'y', 'width', 'height'))
            and rect['x'] >= 0 and rect['y'] >= 0 and rect['width'] > 0 and rect['height'] > 0, 'Invalid native canvas bounds')
    ratio = rendering.get('pixel_ratio'); aspect = rendering.get('camera_aspect'); scale = image.get('capture_scale')
    require(type(ratio) in (int, float) and math.isfinite(ratio) and 0 < ratio <= 2
            and type(aspect) in (int, float) and math.isfinite(aspect) and abs(aspect - rect['width'] / rect['height']) < 1e-6
            and type(scale) in (int, float) and math.isfinite(scale) and scale in (1, image.get('dpr')), 'Native canvas aspect/scale mismatch')
    require(rendering.get('buffer') == rendering.get('drawing_buffer') == [math.floor(rect['width'] * ratio), math.floor(rect['height'] * ratio)],
            'Native canvas and actual drawing buffer disagree')
    width, height = dimensions(data)
    require((rect['x'] + rect['width']) * scale <= width and (rect['y'] + rect['height']) * scale <= height,
            'Native captured frame does not cover the canvas')
    pixels = canvas_pixels(data, {key: value * scale for key, value in rect.items()})
    require(pixels == image.get('canvas_pixels') and pixels['samples'] > 0 and pixels['distinct_colors'] >= 8
            and pixels['non_background_samples'] > pixels['samples'] * .001, 'Captured native canvas is blank or contradicts physical pixels')


def validate_editor(report, directory, *, expected_sha, expected_arch):
    """Validate physical native-main/driver/editor records, not OS-dialog approval."""
    directory = Path(directory)
    require(report.get('schema') == 1 and report.get('pass') is True and report.get('qualifies_workbench') is False,
            'Native editor subset missing or misrepresented as full workbench')
    require(report.get('source_sha') == expected_sha and report.get('architecture') == expected_arch, 'Native editor source/architecture mismatch')
    record = report.get('native_main', {})
    require(record.get('path') == 'proof/native-workbench-main.json', 'Missing native main record')
    file = directory / record['path']
    require(file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == record.get('sha256'), 'Native main record changed')
    main = read_json(file)
    require(main.get('source_sha') == expected_sha and main.get('app_pid') == report.get('app_pid') and main.get('worker_pid') == report.get('worker_pid')
            and main.get('challenge') == report.get('challenge') and main.get('driver') == report.get('driver'), 'Native process/driver identity mismatch')
    result = main.get('result', {})
    from release_evidence import validate_preload_proof
    validate_preload_proof(main, result)
    require(result.get('editor_checks_pass') is True and result.get('qualifies_workbench') is False and result.get('pass') is False,
            'Native editor result is incomplete or claims full workbench')
    require(result.get('challenge') == main['challenge'] and result.get('app_pid') == main['app_pid'] and result.get('worker_pid') == main['worker_pid'], 'Editor result contradicts native identity')
    require(type(report.get('app_pid')) is int and type(report.get('worker_pid')) is int and report['app_pid'] > 0 and report['worker_pid'] > 0
            and report['app_pid'] != report['worker_pid'], 'Missing actual process identities')
    require(main.get('native_source') == result.get('native_source'), 'Editor source closure contradicts main')
    expected = {name for name in read_json(ROOT / 'package.json')['cassquerySources']['shell']}
    sources = main.get('native_source', {}).get('files', [])
    require(len(sources) == len(expected) and {item.get('path') for item in sources} == expected, 'Native editor shell/preload closure incomplete')
    for item in sources:
        data = (ROOT / item['path']).read_bytes()
        require(item.get('sha256') == hashlib.sha256(data).hexdigest() and item.get('bytes') == len(data), 'Native editor compiled shell differs from source')
    checks = result.get('checks', [])
    require([c.get('id') for c in checks] == ['preload-ipc', 'edit-save-volume', 'external-conflict', 'syntax-retains-certified', 'eight-themes', 'font-tabs']
            and all(c.get('pass') is True for c in checks), 'Native editor required cases incomplete')
    by_id = {check['id']: check['evidence'] for check in checks}
    prefs = by_id['preload-ipc']
    require(prefs.get('node_integration') is False and prefs.get('context_isolation') is True and prefs.get('sandbox') is True
            and prefs.get('bridge_present') is True and prefs.get('project_root_matches') is True, 'Native preload/input provenance incomplete')
    edit = by_id['edit-save-volume']
    require(edit.get('save_status') == 200 and edit.get('renderer_tier') == 'certified' and edit.get('gates_pass') is True, 'Native edit/save certification incomplete')
    volumes = [edit.get(key, {}).get('volume') for key in ('before', 'after')]
    require(all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in volumes)
            and abs(volumes[1] - volumes[0] - 360) < 1e-4, 'Native edit volume observations contradict source change')
    conflict = by_id['external-conflict']
    require(conflict.get('status') == 409 and conflict.get('draft_before') == conflict.get('draft_after')
            and conflict.get('external_source') == conflict.get('disk_after') and conflict.get('draft_after') != conflict.get('disk_after')
            and conflict.get('conflict_visible') is True, 'Native conflict did not retain source/draft')
    syntax = by_id['syntax-retains-certified']
    require(syntax.get('certified_before') == syntax.get('certified_after') and syntax.get('certified_announcements') == 0
            and 'SyntaxError' in syntax.get('diagnostic', '') and syntax.get('renderer_tier') == 'certified', 'Native syntax failure did not retain certification')
    fonts = by_id['font-tabs']
    require(fonts.get('font_size') == fonts.get('rendered_font_size') == 22 and fonts.get('tab_size') == fonts.get('inserted_spaces') == 2
            and fonts.get('persistence_after_reload') is True, 'Native settings/input persistence incomplete')
    states = by_id['eight-themes'].get('states', [])
    names = ['Counterform Dark', 'Counterform Light', 'Graphite', 'Midnight', 'Forest', 'Sand', 'Paper', 'High Contrast']
    require(len(states) == 8 and sorted(s.get('name') for s in states) == sorted(names) and len({s.get('styling') for s in states}) == 8
            and all(s.get('persisted') is True and s.get('visible') is True for s in states), 'Native eight-theme coverage incomplete')
    images = result.get('images', [])
    require(len(images) >= 11 and len({image.get('path') for image in images}) == len(images), 'Native editor images incomplete/duplicate')
    for image in images:
        name = image.get('path')
        require(isinstance(name, str) and Path(name).name == name, 'Unsafe native editor image path')
        file = directory / 'proof' / name
        require(file.is_file() and not file.is_symlink(), 'Missing native editor image')
        data = file.read_bytes()
        require(hashlib.sha256(data).hexdigest() == image.get('sha256') and len(data) == image.get('bytes'), 'Native editor image changed')
        require(list(dimensions(data)) == [image.get('width'), image.get('height')], 'Native editor image dimensions contradict bytes')
        viewport, scale = image.get('viewport'), image.get('capture_scale')
        require(isinstance(viewport, list) and len(viewport) == 2 and all(type(v) is int and v >= 100 for v in viewport)
                and type(scale) in (int, float) and scale >= 1 and scale in (1, image.get('dpr'))
                and image['width'] == viewport[0] * scale and image['height'] == viewport[1] * scale,
                'Native editor image does not cover its actual viewport')
        validate_rendered_image(image, data)
    return result


def validate_qualification(report, directory, *, expected_sha, expected_arch, verify_geometry=False):
    """Full native admission; editor-only or blocked records cannot qualify."""
    directory = Path(directory)
    require(report.get('schema') == 1 and report.get('pass') is True and report.get('qualifies_workbench') is True,
            'Full native workbench qualification is blocked/incomplete')
    require(report.get('source_sha') == expected_sha and report.get('architecture') == expected_arch, 'Full workbench identity mismatch')
    editor_record = report.get('editor_component', {})
    require(editor_record.get('path') == 'workbench-native.json', 'Missing editor component')
    editor_file = directory / editor_record['path']
    require(hashlib.sha256(editor_file.read_bytes()).hexdigest() == editor_record.get('sha256'), 'Editor component changed')
    editor = read_json(editor_file); validate_editor(editor, directory, expected_sha=expected_sha, expected_arch=expected_arch)
    native = report.get('native_qualification', {})
    require(native.get('path') == 'proof/workbench-qualification.json', 'Missing native full record')
    raw_file = directory / native['path']; raw_bytes = raw_file.read_bytes()
    require(hashlib.sha256(raw_bytes).hexdigest() == native.get('sha256') and len(raw_bytes) == native.get('bytes'), 'Native qualification bytes changed')
    native_full = read_json(raw_file)
    main = read_json(directory / editor['native_main']['path'])
    descriptor = main.get('result', {}).get('qualification', {})
    require(descriptor.get('path') == 'workbench-qualification.json' and descriptor.get('sha256') == native.get('sha256')
            and descriptor.get('bytes') == native.get('bytes'), 'Native main did not produce this full record')
    full = native_full
    require(full.get('pass') is True and full.get('qualifies_workbench') is True and full.get('error') is None,
            'Native full paths did not complete')
    require(set(editor.get('driver', {})) >= {'path', 'bytes', 'sha256'} and isinstance(report.get('source_fingerprint'), str)
            and re.fullmatch(r'[a-f0-9]{64}', report['source_fingerprint']) and report['source_fingerprint'] == report.get('after_fingerprint'),
            'Native full source checkpoint incomplete')
    snapshot_record = report.get('source_snapshot', {})
    require(snapshot_record.get('path') == 'source-before.json', 'Missing native original source snapshot')
    snapshot_file = directory / snapshot_record['path']
    require(hashlib.sha256(snapshot_file.read_bytes()).hexdigest() == snapshot_record.get('sha256'), 'Native source snapshot changed')
    snapshot = read_json(snapshot_file)
    calculated = hashlib.sha256(json.dumps({'source_sha': snapshot.get('source_sha'), 'files': snapshot.get('files')}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(snapshot.get('source_sha') == expected_sha and snapshot.get('clean') is True and calculated == snapshot.get('fingerprint') == report['source_fingerprint'],
            'Native source fingerprint not bound to original tested source')
    for key in ('app_pid', 'worker_pid', 'challenge', 'driver'):
        require(full.get(key) == editor.get(key), 'Native full/editor identity mismatch: ' + key)
    require(full.get('source_sha') == expected_sha, 'Native full source mismatch')
    required = {'native-open-file', 'native-open-folder', 'native-new-project', 'native-transform', 'native-export-step',
                'native-import-roundtrip', 'native-save-copy', 'native-cancel', 'dirty-cancel', 'dirty-save', 'dirty-discard'}
    checks = full.get('checks', [])
    require(len(checks) == len(required) and {c.get('id') for c in checks} == required and all(c.get('pass') is True for c in checks),
            'Native required operations missing or duplicated')
    by_id = {c['id']: c['evidence'] for c in checks}
    transform = by_id['native-transform']
    require(transform.get('persisted') == {'translation': [3, -2, 5], 'rotation': [0, 0, 90], 'scale': 1.5}, 'Native transform was not persisted')
    require(abs(transform['after']['volume'] / transform['before']['volume'] - 3.375) < 1e-5, 'Native transform volume contradiction')
    copied = by_id['native-save-copy']
    require(copied.get('copy_sha256') == copied.get('draft_sha256') and copied.get('copy_sha256') != copied.get('original_sha256')
            and copied.get('active_project_changed') is False, 'Native Save Copy lost draft or changed active source')
    require(by_id['dirty-cancel'].get('draft_before') == by_id['dirty-cancel'].get('draft_after') and by_id['dirty-cancel'].get('source_unchanged') is True,
            'Native dirty Cancel lost data')
    require(by_id['dirty-save'].get('actual_source_sha256') == by_id['dirty-save'].get('expected_draft_sha256'), 'Native dirty Save did not save')
    require(by_id['dirty-discard'].get('source_sha256') == by_id['dirty-discard'].get('saved_sha256'), 'Native Discard modified source')
    require(by_id['native-cancel'].get('registry_before') == by_id['native-cancel'].get('registry_after'), 'Native chooser Cancel changed registration')
    dialog = full.get('native_dialog_selection', {})
    records = dialog.get('records', [])
    require(dialog.get('status') == 'checked' and len(records) == 10 and len({r.get('id') for r in records}) == 10, 'Actual native chooser coverage incomplete')
    ledger = read_json(directory / 'proof/native-dialogs.json')
    require(ledger.get('app_pid') == full['app_pid'] and ledger.get('challenge') == full['challenge'], 'Unrelated native dialog observer')
    kinds = ['open', 'folder', 'folder', 'save', 'open', 'folder', 'save', 'folder', 'folder', 'cancel']
    require([item.get('kind') for item in records] == kinds, 'Native operation/dialog sequence incomplete')
    require(by_id['native-open-file'].get('path') == records[0].get('target'), 'Native opened file not bound to chooser')
    require(by_id['native-open-folder'].get('root') == records[1].get('target'), 'Native opened folder not bound to chooser')
    require(Path(by_id['native-new-project'].get('root', '')).parent == Path(records[2].get('target', '')), 'Native created project not bound to chosen parent')
    for item in records:
        native_call, ui = item.get('native_call', {}), item.get('os_input', {})
        index = item.get('observer_index')
        require(type(index) is int and 0 <= index < len(ledger.get('calls', [])) and ledger['calls'][index] == native_call,
                'Native dialog call not bound to actual observer')
        require(ui.get('id') == item.get('id') and ui.get('pass') is True and ui.get('app_pid') == full['app_pid'] and ui.get('challenge') == full['challenge'], 'Native OS input is unverified')
        result = native_call.get('result', {})
        if item['kind'] == 'cancel':
            require(result.get('canceled') is True and result.get('filePaths') == [], 'Actual native cancellation not observed')
        else:
            returned = result.get('filePath') if item['kind'] == 'save' else (result.get('filePaths') or [None])[0]
            require(result.get('canceled') is False and isinstance(returned, str) and Path(returned) == Path(item['target']), 'Native returned path contradicts selection')
    images = full.get('images', [])
    required_images = {f'full-{name}.png' for name in ('open-file', 'open-folder', 'new-project', 'transform', 'export', 'import', 'save-copy')}
    require(isinstance(images, list) and len(images) >= 17 and len({image.get('path') for image in images}) == len(images)
            and required_images.issubset({image.get('path') for image in images}), 'Native full frame inventory incomplete or duplicate')
    for item in records:
        image = item['os_input'].get('image')
        require(isinstance(image, dict) and image in images, 'Native OS panel image missing from actual inventory')
    for image in images:
        name = image.get('path'); require(isinstance(name, str) and Path(name).name == name, 'Unsafe full native image')
        file = directory / 'proof' / name; data = file.read_bytes()
        require(hashlib.sha256(data).hexdigest() == image.get('sha256') and list(dimensions(data)) == [image.get('width'), image.get('height')], 'Full native image changed')
        if name in required_images: validate_rendered_image(image, data)
    exported = by_id['native-export-step']; export_file = directory / 'proof' / exported['path']
    require(exported.get('path') == 'native-export.step' and hashlib.sha256(export_file.read_bytes()).hexdigest() == exported.get('sha256'), 'Native STEP bytes changed')
    expected = by_id['native-import-roundtrip']['imported_geometry']
    exported_geometry = exported.get('geometry', {})
    require(type(expected.get('volume')) in (int, float) and math.isfinite(expected['volume']) and expected['volume'] > 0
            and abs(exported_geometry.get('volume', 0) - expected['volume']) < expected['volume'] * 1e-6
            and all(isinstance(expected.get(key), list) and len(expected[key]) == 3 and all(type(n) in (int, float) and math.isfinite(n) for n in expected[key])
                    and isinstance(exported_geometry.get(key), list) and len(exported_geometry[key]) == 3
                    and all(abs(exported_geometry[key][i] - expected[key][i]) < 1e-5 for i in range(3)) for key in ('min', 'max')),
            'Native exported/imported BREP observations contradict')
    readback = report.get('geometry_readback', {})
    require(readback.get('schema') == 1 and readback.get('method') == 'actual CadQuery STEP import in native runner'
            and readback.get('step_sha256') == exported['sha256']
            and readback.get('units') == 'mm' and readback.get('variant') == 'neutral'
            and readback.get('source_sha') == expected_sha and isinstance(readback.get('cadquery_version'), str)
            and isinstance(readback.get('python_version'), str),
            'Native actual geometry readback proof missing or stale')
    same_geometry(readback.get('geometry', {}), expected)
    if verify_geometry:
        # Always read actual STEP bytes in this trusted process; portable callers never import CAD.
        actual = measure_step_readback(export_file, source_sha=expected_sha)
        require(actual['step_sha256'] == exported['sha256'] and actual['cadquery_version'] == readback['cadquery_version']
                and actual['python_version'] == readback['python_version'], 'Native readback runtime or bytes changed')
        same_geometry(actual['geometry'], readback['geometry'])
        same_geometry(actual['geometry'], expected)
    require(report.get('cleanup', {}).get('pass') is True and report.get('temp_root_removed') is True, 'Native cleanup incomplete')
    return full


def status(port):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=5) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    require(len(data) <= 4 * 1024 * 1024, 'Worker status exceeds bound')
    return json.loads(data)


def deny_ci_python_local_network():
    """Owner-authorized denial of one exact system prompt, only on GitHub Mac runners."""
    if os.environ.get('CASSQUERY_CI_DENY_PYTHON_LOCAL_NETWORK') != '1':
        return {'status': 'not-requested'}
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('CI') == 'true'
            and os.environ.get('RUNNER_OS') == 'macOS' and platform.system() == 'Darwin',
            'Python local-network consent handling is restricted to GitHub macOS CI')
    script = '''tell application "System Events"
  set matches to {}
  set systemIDs to {"com.apple.UserNotificationCenter", "com.apple.CoreServicesUIAgent"}
  repeat with systemID in systemIDs
    set candidates to application processes whose bundle identifier is (contents of systemID)
    if (count candidates) > 1 then error "Ambiguous system consent process"
    repeat with targetProcess in candidates
      if (count windows of targetProcess) > 8 then error "System consent window bound exceeded"
      repeat with targetWindow in windows of targetProcess
        set elements to entire contents of targetWindow
        if (count elements) > 256 then error "System consent element bound exceeded"
        set exactPrompt to false
        set denyButtons to {}
        repeat with itemElement in elements
          if role of itemElement is "AXStaticText" then
            set textValue to value of itemElement
            if textValue is "Allow “Python” to find devices on local networks?" or textValue is "Allow \\"Python\\" to find devices on local networks?" then set exactPrompt to true
          else if role of itemElement is "AXButton" then
            set buttonName to name of itemElement
            if buttonName is "Don't Allow" or buttonName is "Don’t Allow" then set end of denyButtons to itemElement
          end if
        end repeat
        if exactPrompt then
          if (count denyButtons) is not 1 then error "Exact Python consent deny control is missing or ambiguous"
          set end of matches to {targetProcess, targetWindow, item 1 of denyButtons}
        end if
      end repeat
    end repeat
  end repeat
  if (count matches) is 0 then return "not-present"
  if (count matches) is not 1 then error "Ambiguous Python local-network consent prompt"
  set matched to item 1 of matches
  set denyButton to item 3 of matched
  if not enabled of denyButton then error "Python local-network deny control disabled"
  click denyButton
  set closeDeadline to (current date) + 3
  repeat while exists (item 2 of matched)
    if (current date) >= closeDeadline then error "Python local-network consent prompt did not close"
    delay 0.1
  end repeat
  return "denied"
end tell'''
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, timeout=20)
    require(result.returncode == 0, 'CI Python local-network denial failed: ' + result.stderr.strip()[:1000])
    status = result.stdout.strip()
    require(status in ('not-present', 'denied'), 'Unexpected CI local-network consent receipt')
    return {'status': status, 'decision': 'deny', 'prompt': 'Python local-network access',
            'scope': 'temporary GitHub macOS runner; owner authorized'}


def os_input(pid, commands, *, deadline=None):
    require(type(pid) is int and pid > 0, 'Native input needs the owned app PID')
    timeout = 20 if deadline is None else min(20, deadline - time.monotonic())
    require(timeout > 0, 'Standard native input exceeded its existing deadline')
    script = ('tell application "System Events"\n'
              f'  set targetProcess to first process whose unix id is {pid}\n'
              '  set frontmost of targetProcess to true\n'
              '  tell targetProcess\n' + commands + '\n  end tell\nend tell')
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, timeout=timeout)
    require(result.returncode == 0, 'Standard native input unavailable: ' + result.stderr.strip()[:1500])
    return result.stdout[:16000]


def screenshot(file):
    subprocess.run(['/usr/sbin/screencapture', '-x', str(file)], check=True, timeout=10)
    data = file.read_bytes(); size = dimensions(data)
    require(min(size) > 100, 'Native panel capture is empty')
    return {**file_evidence(file), 'width': size[0], 'height': size[1]}


def native_dialog_attempts(pid, port, output, public_projects, proof, challenge):
    """Actual UI keyboard and native panels; failures retain an explicit blocked gate."""
    result = {'status': 'blocked', 'method': 'standard CI System Events keyboard; real worker registration', 'cases': [], 'error': None}
    try:
        def observed():
            value = read_json(proof / 'native-dialogs.json')
            require(value.get('app_pid') == pid and value.get('challenge') == challenge, 'Unrelated native dialog observer')
            return value['calls']
        for name, shortcut, selected in [('file', 'keystroke "o" using command down', public_projects['file']),
                                          ('folder', 'keystroke "o" using {command down, shift down}', public_projects['folder'])]:
            before = status(port); calls_before = len(observed())
            os_input(pid, '    ' + shortcut + '\n    delay 0.7')
            panel = screenshot(output / f'native-{name}-panel.png')
            os_input(pid, '    keystroke "g" using {command down, shift down}\n    delay 0.4\n'
                     '    keystroke ' + json.dumps(str(selected)) + '\n    key code 36\n    delay 0.6\n    key code 36\n    delay 0.6\n'
                     '    try\n      click button "Trust and Open" of sheet 1 of window 1\n'
                     '    on error\n      click button "Trust and Open" of window 1\n    end try')
            expected_root = selected.parent if name == 'file' else selected
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                after = status(port)
                if any(Path(p['root']).resolve() == expected_root.resolve() for p in after['projects']):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Native chooser did not register its actual selected public path')
            require(not any(Path(p['root']).resolve() == expected_root.resolve() for p in before['projects']), 'Native dialog proof reused a registered startup project')
            native_calls = observed()[calls_before:]
            require(len(native_calls) == 1 and native_calls[0].get('result', {}).get('canceled') is False
                    and len(native_calls[0]['result'].get('filePaths', [])) == 1
                    and Path(native_calls[0]['result']['filePaths'][0]).resolve() == selected.resolve(), 'Actual native dialog result does not match keyboard selection')
            result['cases'].append({'kind': name, 'selected_path': str(selected), 'registered_root': str(expected_root),
                                    'actual_registration': True, 'panel': panel, 'native_result': native_calls[0],
                                    'source_sha256': hashlib.sha256((expected_root / 'part_entry.py').read_bytes()).hexdigest()})
        before = status(port); calls_before = len(observed())
        os_input(pid, '    keystroke "o" using command down\n    delay 0.7')
        panel = screenshot(output / 'native-cancel-panel.png'); os_input(pid, '    key code 53\n    delay 0.5')
        after = status(port)
        identity = lambda value: sorted((p['name'], p['root']) for p in value['projects'])
        require(identity(before) == identity(after), 'Native cancellation changed project registration')
        native_calls = observed()[calls_before:]
        require(len(native_calls) == 1 and native_calls[0].get('result', {}).get('canceled') is True
                and native_calls[0]['result'].get('filePaths') == [], 'Native cancellation did not return the actual canceled result')
        result['cases'].append({'kind': 'cancel', 'registration_unchanged': True, 'panel': panel, 'native_result': native_calls[0]})
        result['status'] = 'checked'
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result['error'] = str(error)
    return result


def serve_native_actions(process, proof, challenge, user):
    """Respond only to the fixed driver's owned UI requests; no selector substitution."""
    completed = set()
    local_network_consent = None
    deadline = time.monotonic() + 480
    main_file = proof / 'native-workbench-main.json'
    while time.monotonic() < deadline and not main_file.is_file():
        require(process.poll() is None, 'Actual packaged app exited before completing proof')
        action_file = proof / 'native-action.json'
        if not action_file.is_file():
            time.sleep(0.1); continue
        action = read_json(action_file)
        identity = action.get('id')
        if identity in completed:
            time.sleep(0.1); continue
        require(type(identity) is int and identity > 0 and action.get('challenge') == challenge and action.get('app_pid') == process.pid,
                'Unrelated native input request')
        response = {'schema': 1, 'id': identity, 'challenge': challenge, 'app_pid': process.pid, 'pass': False, 'error': None}
        try:
            kind = action.get('kind'); require(kind in ('open', 'folder', 'save', 'cancel'), 'Unsupported native input kind')
            target = action.get('target')
            if kind != 'cancel':
                require(isinstance(target, str) and Path(target).is_absolute() and Path(target).resolve().is_relative_to(user), 'Native chooser target escaped owned root')
            call_deadline = min(deadline, time.monotonic() + 15)
            while time.monotonic() < call_deadline:
                ledger = read_json(proof / 'native-dialogs.json')
                require(ledger.get('challenge') == challenge and ledger.get('app_pid') == process.pid, 'Native dialog observer identity mismatch')
                index = action.get('observer_index'); require(type(index) is int and index >= 0, 'Invalid native observer index')
                if len(ledger['calls']) > index:
                    break
                if main_file.is_file(): raise RuntimeError('Driver stopped before native panel opened')
                time.sleep(0.1)
            else:
                raise RuntimeError('Real native panel did not open inside its deadline')
            expected_method = 'showSaveDialog' if kind == 'save' else 'showOpenDialog'
            dialog_identity = action.get('dialog_action_id', identity)
            require(type(dialog_identity) is int and dialog_identity > 0, 'Invalid native dialog action identity')
            if dialog_identity != identity:
                require(kind == 'folder' and dialog_identity == identity - 1 and index > 0
                        and 'createDirectory' in ledger['calls'][index].get('properties', []), 'Unrelated native follow-up dialog')
                previous = ledger['calls'][index - 1]
                previous_result = previous.get('result')
                require(previous.get('method') == 'showOpenDialog' and previous.get('action_id') == dialog_identity
                        and previous.get('properties') == ['openFile'] and isinstance(previous_result, dict) and previous_result.get('canceled') is False
                        and isinstance(previous_result.get('filePaths'), list) and len(previous_result['filePaths']) == 1
                        and isinstance(previous_result['filePaths'][0], str) and Path(previous_result['filePaths'][0]).resolve().is_relative_to(user)
                        and Path(previous_result['filePaths'][0]).suffix.lower() in ('.step', '.stp')
                        and type(previous.get('completed_at')) in (int, float) and type(ledger['calls'][index].get('started_at')) in (int, float)
                        and previous['completed_at'] <= ledger['calls'][index]['started_at'],
                        'Native import destination has no completed parent file selection')
            require(ledger['calls'][index].get('method') == expected_method and ledger['calls'][index].get('action_id') == dialog_identity,
                    'Unrelated native panel before selection')
            if action.get('trust') is True:
                require(ledger['calls'][index].get('method') == 'showOpenDialog' and ledger['calls'][index].get('action_id') == dialog_identity
                        and ('openDirectory' if kind == 'folder' else 'openFile') in ledger['calls'][index].get('properties', []),
                        'Unrelated native panel before trusted selection')
            if local_network_consent is None or local_network_consent.get('status') != 'denied':
                local_network_consent = deny_ci_python_local_network()
            response['local_network_consent'] = local_network_consent
            action_deadline = min(deadline, time.monotonic() + 20)
            button = 'Save' if kind == 'save' else 'Open'
            os_input(process.pid, '    set panelDeadline to (current date) + ' + str(max(0, action_deadline-time.monotonic())) + '\n'
                     '    set panelReady to false\n    repeat while (current date) < panelDeadline\n'
                     '      if exists window 1 then\n        if exists sheet 1 of window 1 then\n'
                     '          if exists button ' + json.dumps(button) + ' of sheet 1 of window 1 then\n            set panelReady to true\n            exit repeat\n          end if\n'
                     '        else if exists button ' + json.dumps(button) + ' of window 1 then\n          set panelReady to true\n          exit repeat\n        end if\n      end if\n'
                     '      delay 0.1\n    end repeat\n    if not panelReady then error "Owned native file panel unavailable" number -1728', deadline=action_deadline)
            image = screenshot(proof / f'native-os-{identity}.png'); image['path'] = Path(image['path']).name; response['image'] = image
            commands = ''
            if kind == 'cancel':
                os_input(process.pid, '    key code 53\n    delay 0.3')
            elif kind == 'save':
                target_path = Path(target)
                require(target_path.suffix in ('.step', '.stp', '.stl', '.brep', '.py', '.zip'), 'Unsupported native Save suffix')
                native_name = target_path.stem  # NSSavePanel owns the selected file-type extension.
                os_input(process.pid, '    set saveDeadline to (current date) + ' + str(max(0, action_deadline-time.monotonic())) + '\n'
                         '    set savePanel to window 1\n    if exists sheet 1 of window 1 then set savePanel to sheet 1 of window 1\n'
                         '    if not exists button "Save" of savePanel then error "Owned native Save panel missing"\n'
                         '    keystroke "g" using {command down, shift down}\n'
                         '    repeat until exists sheet 1 of savePanel\n      if (current date) >= saveDeadline then error "Native Go To panel deadline"\n      delay 0.1\n    end repeat\n'
                         '    set goPanel to sheet 1 of savePanel\n'
                         '    if exists combo box 1 of goPanel then\n      set goField to combo box 1 of goPanel\n    else\n      set goField to text field 1 of goPanel\n    end if\n'
                         '    set focused of goField to true\n'
                         '    set goField to value of attribute "AXFocusedUIElement" of targetProcess\n'
                         '    if role of goField is not "AXTextField" and role of goField is not "AXComboBox" then error "Native directory text field not focused"\n'
                         '    keystroke "a" using command down\n    keystroke ' + json.dumps(str(target_path.parent)) + '\n'
                         '    repeat until (value of goField is ' + json.dumps(str(target_path.parent)) + ' or value of goField is ' + json.dumps(str(target_path.parent) + '/') + ')\n'
                         '      if (current date) >= saveDeadline then error "Native directory input acknowledgement deadline: " & (value of goField as text)\n      delay 0.1\n    end repeat\n'
                         '    key code 36\n    repeat while exists goPanel\n'
                         '      if (current date) >= saveDeadline then error "Native directory selection deadline"\n      delay 0.1\n    end repeat\n'
                         '    set nameField to text field 1 of savePanel\n    set focused of nameField to true\n'
                         '    keystroke "a" using command down\n    keystroke ' + json.dumps(native_name) + '\n'
                         '    repeat until value of nameField is ' + json.dumps(native_name) + '\n'
                         '      if (current date) >= saveDeadline then error "Native filename input acknowledgement deadline"\n      delay 0.1\n    end repeat\n'
                         '    if not enabled of button "Save" of savePanel then error "Owned native Save control disabled"\n'
                         '    click button "Save" of savePanel\n    set clickedSave to true', deadline=action_deadline)
                while time.monotonic() < action_deadline:
                    latest = read_json(proof / 'native-dialogs.json')
                    require(latest.get('challenge') == challenge and latest.get('app_pid') == process.pid, 'Unrelated native Save observer')
                    call = latest['calls'][index]
                    if call.get('result') is not None:
                        require(call.get('method') == 'showSaveDialog' and call.get('action_id') == identity
                                and call['result'].get('canceled') is False and call['result'].get('filePath') == target,
                                'Actual native Save destination differs from requested target')
                        response['native_call'] = call
                        break
                    time.sleep(0.1)
                else: raise RuntimeError('Matching native Save completion deadline')
            else:
                commands = '    keystroke "g" using {command down, shift down}\n    delay 0.4\n    keystroke ' + json.dumps(target) + '\n    key code 36\n    delay 0.5'
                os_input(process.pid, commands, deadline=action_deadline)
                remaining = max(0, action_deadline - time.monotonic())
                os_input(process.pid, '    set openDeadline to (current date) + ' + str(remaining) + '\n'
                         '    set clickedOpen to false\n    repeat while (current date) < openDeadline\n'
                         '      if exists window 1 then\n        if exists sheet 1 of window 1 then\n'
                         '          if exists button "Open" of sheet 1 of window 1 then\n'
                         '            if enabled of button "Open" of sheet 1 of window 1 then\n'
                         '              click button "Open" of sheet 1 of window 1\n              set clickedOpen to true\n              exit repeat\n            end if\n          end if\n'
                         '        else if exists button "Open" of window 1 then\n'
                         '          if enabled of button "Open" of window 1 then\n'
                         '            click button "Open" of window 1\n            set clickedOpen to true\n            exit repeat\n          end if\n        end if\n      end if\n'
                         '      delay 0.1\n    end repeat\n'
                         '    if not clickedOpen then error "Matching native Open control unavailable" number -1728', deadline=action_deadline)
                if action.get('trust') is True:
                    def current_calls():
                        value = read_json(proof / 'native-dialogs.json')
                        require(value.get('challenge') == challenge and value.get('app_pid') == process.pid, 'Native trust observer identity mismatch')
                        return value['calls']
                    def await_dialog(label, predicate):
                        while time.monotonic() < action_deadline:
                            require(process.poll() is None and not main_file.is_file(), 'Driver stopped before matching native trust completed')
                            found = predicate(current_calls())
                            if found is not None:
                                return found
                            time.sleep(0.1)
                        raise RuntimeError('Matching native dialog deadline: ' + label)
                    def selected_call(calls):
                        if len(calls) <= index:
                            return None
                        call = calls[index]
                        require(call.get('method') == 'showOpenDialog' and call.get('action_id') == dialog_identity, 'Unrelated native chooser result')
                        if call.get('result') is None or call.get('completed_at') is None:
                            return None
                        require(type(call['completed_at']) in (int, float) and type(call.get('started_at')) in (int, float)
                                and call['completed_at'] >= call['started_at'], 'Invalid native chooser completion chronology')
                        result = call['result']
                        require(result.get('canceled') is False and isinstance(result.get('filePaths'), list) and len(result['filePaths']) == 1
                                and isinstance(result['filePaths'][0], str) and Path(result['filePaths'][0]).resolve() == Path(target).resolve(),
                                'Actual native chooser result differs from requested target')
                        return call
                    selected = await_dialog('actual chooser completion', selected_call)
                    def trust_call(calls):
                        if len(calls) <= index + 1:
                            return None
                        require(len([entry for entry in calls[index + 1:] if entry.get('method') == 'showMessageBox' and entry.get('action_id') == identity]) == 1,
                                'Duplicate native trust dialog for this action')
                        call = calls[index + 1]
                        require(call.get('method') == 'showMessageBox' and call.get('action_id') == identity
                                and call.get('buttons') == ['Trust and Open', 'Cancel']
                                and call.get('started_at', 0) >= selected['completed_at'], 'Unrelated native trust dialog')
                        return call
                    trust = await_dialog('actual Trust and Open start', trust_call)
                    require(trust.get('result') is None, 'Native trust responded before the owned input')
                    remaining = max(0, action_deadline - time.monotonic())
                    os_input(process.pid, '    set trustDeadline to (current date) + ' + str(remaining) + '\n'
                             '    set clickedTrust to false\n    repeat while (current date) < trustDeadline\n'
                             '      if exists window 1 then\n        if exists sheet 1 of window 1 then\n'
                             '          if exists button "Trust and Open" of sheet 1 of window 1 then\n'
                             '            if enabled of button "Trust and Open" of sheet 1 of window 1 then\n'
                             '              click button "Trust and Open" of sheet 1 of window 1\n              set clickedTrust to true\n              exit repeat\n            end if\n          end if\n        end if\n'
                             '        if exists button "Trust and Open" of window 1 then\n'
                             '          if enabled of button "Trust and Open" of window 1 then\n'
                             '            click button "Trust and Open" of window 1\n            set clickedTrust to true\n            exit repeat\n          end if\n        end if\n      end if\n'
                             '      delay 0.1\n    end repeat\n'
                             '    if not clickedTrust then error "Matching Trust and Open control unavailable" number -1728', deadline=action_deadline)
                    def trust_response(calls):
                        call = trust_call(calls)
                        if call is None or call.get('result') is None or call.get('completed_at') is None:
                            return None
                        require(type(call['result'].get('response')) is int and call['result']['response'] == 0 and type(call['completed_at']) in (int, float)
                                and call['completed_at'] >= call['started_at'], 'Actual native trust was not accepted')
                        return call
                    response['trust_call'] = await_dialog('actual Trust and Open response', trust_response)
                    response['native_call'] = selected
            response['pass'] = True
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            response['error'] = str(error)
            try:
                failed_image = screenshot(proof / f'native-os-error-{identity}.png'); failed_image['path'] = Path(failed_image['path']).name
                response['failure_image'] = failed_image
                response['accessibility'] = os_input(process.pid,
                    '    set names to {}\n    if exists window 1 then\n      repeat with itemButton in buttons of window 1\n'
                    '        if (count names) >= 20 then exit repeat\n        set end of names to name of itemButton\n      end repeat\n'
                    '      if exists sheet 1 of window 1 then\n        repeat with itemButton in buttons of sheet 1 of window 1\n'
                    '          if (count names) >= 40 then exit repeat\n          set end of names to name of itemButton\n        end repeat\n      end if\n    end if\n    return names',
                    deadline=min(deadline, time.monotonic() + 5))
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as diagnostic:
                response['diagnostic_error'] = str(diagnostic)
        temporary = proof / 'native-action-result.tmp'
        temporary.write_text(json.dumps(response) + '\n'); os.replace(temporary, proof / 'native-action-result.json'); completed.add(identity)
    require(main_file.is_file(), 'Native workbench did not complete its bounded editor/full phases')
    return read_json(main_file)


def retain_native_proof(proof, output, owned_root):
    """Bank bounded flat raw evidence after owned processes exit, even for red runs."""
    proof, output, owned_root = Path(proof), Path(output), Path(owned_root).resolve()
    require(not proof.is_symlink() and proof.is_dir() and proof.resolve().is_relative_to(owned_root), 'Raw proof escaped its owned state')
    anchor = proof.lstat(); canonical = proof.resolve()
    files = []
    for entry in proof.iterdir():
        require(len(files) < 256, 'Raw proof file count exceeds bound')
        metadata = entry.lstat()
        require(stat.S_ISREG(metadata.st_mode) and not entry.is_symlink() and metadata.st_size <= 32 * 1024**2,
                'Raw proof contains unsafe or oversized evidence')
        files.append((entry, metadata))
    require(sum(metadata.st_size for _, metadata in files) <= 128 * 1024**2, 'Raw proof bytes exceed bound')
    destination = output / 'proof'; require(not destination.exists(), 'Raw proof output must be fresh')
    destination.mkdir()
    records = []
    for file, before in sorted(files, key=lambda item: item[0].name):
        with file.open('rb') as stream: data = stream.read(32 * 1024**2 + 1)
        require(len(data) == before.st_size, 'Raw proof changed before copy')
        target = destination / file.name; target.write_bytes(data)
        after = file.lstat()
        require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) ==
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                and not file.is_symlink() and target.read_bytes() == data, 'Raw proof changed during copy')
        current = proof.lstat()
        require(not proof.is_symlink() and proof.resolve() == canonical and
                (current.st_dev, current.st_ino) == (anchor.st_dev, anchor.st_ino), 'Raw proof state root changed')
        records.append({'path': 'proof/' + file.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return records


def run(app, output, arch):
    require(platform.system() == 'Darwin' and os.environ.get('GITHUB_ACTIONS') == 'true', 'Native workbench launch/input is CI-only; do not touch the live installed app')
    app, output, arch = Path(app).resolve(), Path(output).resolve(), architecture(arch)
    require(architecture(platform.machine()) == arch, 'Native workbench needs the actual target architecture')
    require(not output.exists(), 'Native workbench requires fresh evidence output')
    output.mkdir(parents=True)
    source = source_snapshot(ROOT); package = read_json(ROOT / 'package.json')
    require(source['clean'] and os.environ.get('GITHUB_SHA') == source['source_sha'], 'Native workbench needs clean exact CI source')
    write_report(output / 'source-before.json', source)
    driver = (ROOT / 'tools/native_workbench_probe.cjs').resolve()
    require(Path(os.environ.get('GITHUB_WORKSPACE', '')).resolve() == ROOT.resolve(), 'Driver must come from the exact CI workspace')
    report = {'schema': 1, 'pass': False, 'qualifies_workbench': False, 'source_sha': source['source_sha'], 'source_fingerprint': source['fingerprint'],
              'architecture': arch, 'version': package['version'], 'native_dialog_selection': {'status': 'not attempted'},
              'driver': file_evidence(driver), 'started_at': time.time(), 'not_checked': ['full workbench qualification', 'update install/rollback', 'human visual approval']}
    write_report(output / 'workbench-native.json', report)
    full_report = {'schema': 1, 'pass': False, 'qualifies_workbench': False, 'source_sha': source['source_sha'],
                   'source_snapshot': {**file_evidence(output / 'source-before.json'), 'path': 'source-before.json'},
                   'source_fingerprint': source['fingerprint'], 'architecture': arch, 'version': package['version'],
                   'native_dialog_selection': {'status': 'blocked'}, 'error': 'Native full paths not completed'}
    write_report(output / 'workbench-qualification.json', full_report)
    temporary = Path(tempfile.mkdtemp(prefix='cassquery-workbench-native-')).resolve()
    process = None; proof = None; user = None; main = None
    try:
        _, before = verify_bundle(app, arch, package['version'])
        copied = temporary / 'copy/CassQuery.app'; copied.parent.mkdir()
        subprocess.run(['/usr/bin/ditto', str(app), str(copied)], check=True, timeout=120)
        require(inventory(copied) == before, 'Native workbench copy changed bundle bytes/modes')
        user = temporary / 'user'; user.mkdir(); projects = user / 'projects'; projects.mkdir()
        fixture = projects / 'WORKBENCHPLATE'; shutil.copytree(ROOT / 'testpart', fixture, ignore=shutil.ignore_patterns('__pycache__'))
        manifest = read_json(fixture / 'viewer.json'); manifest['name'] = 'WORKBENCHPLATE'; (fixture / 'viewer.json').write_text(json.dumps(manifest))
        selections = {}
        for kind, name in [('file', 'NATIVEDIALOGFILE'), ('folder', 'NATIVEDIALOGFOLDER')]:
            folder = projects / name; shutil.copytree(ROOT / 'testpart', folder, ignore=shutil.ignore_patterns('__pycache__'))
            value = read_json(folder / 'viewer.json'); value['name'] = name; (folder / 'viewer.json').write_text(json.dumps(value))
            selections[kind] = folder / 'part_entry.py' if kind == 'file' else folder
        proof = user / 'proof'; proof.mkdir()
        challenge = os.urandom(32).hex(); report['challenge'] = challenge
        env = {key: value for key, value in os.environ.items() if not key.startswith('CASSQUERY_') and key not in ('PYTHONHOME', 'PYTHONPATH', 'NODE_OPTIONS', 'NODE_PATH', 'ELECTRON_RUN_AS_NODE')}
        env.update(CASSQUERY_USER_DATA=str(user), CASSQUERY_DATA_DIR=str(user / 'worker'), CASSQUERY_PROJECTS=str(fixture),
                   CASSQUERY_SMOKE_DIR=str(proof), CASSQUERY_WORKBENCH_SMOKE_DIR=str(proof), CASSQUERY_WORKBENCH_SMOKE_CHALLENGE=challenge,
                   CASSQUERY_WORKBENCH_SMOKE_DRIVER=str(driver), CASSQUERY_WORKBENCH_SMOKE_DRIVER_SHA=report['driver']['sha256'],
                   CASSQUERY_ANGLE='swiftshader', HOME=str(temporary), PATH='/usr/bin:/bin:/usr/sbin:/sbin')
        with (output / 'app.log').open('w') as log:
            process = subprocess.Popen([str(copied / 'Contents/MacOS/CassQuery')], cwd=temporary, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            main = serve_native_actions(process, proof, challenge, user)
            report.update(app_pid=process.pid, worker_pid=main.get('worker_pid'))
            require(main.get('app_pid') == process.pid and main.get('challenge') == challenge and main.get('driver') == report['driver'], 'Unrelated native process/driver receipt')
            result = main.get('result', {})
            require(result.get('editor_checks_pass') is True, 'Native editor cases failed: ' + str(result.get('errors')))
            native_full = read_json(proof / 'workbench-qualification.json')
            report['native_dialog_selection'] = native_full.get('native_dialog_selection', {'status': 'blocked'})
            if native_full.get('pass') is True and native_full.get('qualifies_workbench') is True:
                os_input(process.pid, '    keystroke "q" using command down')
                require(process.wait(timeout=15) == 0, 'Production quit failed after native dialog proof')
                report['production_quit_checked'] = True
            else:
                report['production_quit_checked'] = False
            report['pass'] = True
            full_report.update(app_pid=process.pid, worker_pid=main['worker_pid'], challenge=challenge,
                               driver=report['driver'], native_dialog_selection=native_full.get('native_dialog_selection', {'status': 'blocked'}))
            full_report['pass'] = native_full.get('pass') is True and native_full.get('qualifies_workbench') is True
            full_report['qualifies_workbench'] = full_report['pass']
            full_report['error'] = native_full.get('error')
        require(inventory(copied) == before and inventory(app) == before, 'Native workbench mutated a sealed bundle')
        after = source_snapshot(ROOT); require(after['fingerprint'] == source['fingerprint'], 'Source changed during native workbench proof')
        report['after_fingerprint'] = after['fingerprint']
    except Exception as error:
        report['pass'] = False; report['error'] = f'{type(error).__name__}: {error}'
    finally:
        cleanup_errors = []
        if process is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try: os.killpg(process.pid, sig)
                except ProcessLookupError: break
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: continue
            for _ in range(50):
                alive = False
                for pid in (process.pid, report.get('worker_pid')):
                    if type(pid) is not int: continue
                    try: os.kill(pid, 0); alive = True
                    except ProcessLookupError: pass
                try: os.killpg(process.pid, 0); alive = True
                except ProcessLookupError: pass
                if not alive: break
                time.sleep(0.1)
            else:
                cleanup_errors.append('Owned app/worker still running after cleanup')
        if not cleanup_errors and proof is not None:
            try:
                report['raw_proof_files'] = retain_native_proof(proof, output, user)
                driver_bytes = driver.read_bytes()
                require(len(driver_bytes) == report['driver']['bytes'] and hashlib.sha256(driver_bytes).hexdigest() == report['driver']['sha256'],
                        'Driver source changed before evidence retention')
                (output / 'native-driver.cjs').write_bytes(driver_bytes)
                report['retained_driver'] = {**file_evidence(output / 'native-driver.cjs'), 'path': 'native-driver.cjs'}
                after = source_snapshot(ROOT); require(after['fingerprint'] == source['fingerprint'], 'Source changed during native proof')
                report['after_fingerprint'] = after['fingerprint']
                main_file = output / 'proof/native-workbench-main.json'
                if main_file.is_file(): report['native_main'] = {**file_evidence(main_file), 'path': 'proof/native-workbench-main.json'}
                full_file = output / 'proof/workbench-qualification.json'
                if full_file.is_file():
                    full_report['native_qualification'] = {**file_evidence(full_file), 'path': 'proof/workbench-qualification.json'}
                if report['pass']: validate_editor(report, output, expected_sha=source['source_sha'], expected_arch=arch)
            except Exception as error:
                report['pass'] = False
                cleanup_errors.append(f'Raw proof retention failed: {type(error).__name__}: {error}')
        if not cleanup_errors:
            shutil.rmtree(temporary)
        else:
            report['retained_temp_root'] = str(temporary)
            report['pass'] = False
        report['cleanup'] = {'pass': not cleanup_errors, 'errors': cleanup_errors, 'mode': 'production quit' if report.get('production_quit_checked') else 'owned process group cleanup; not native quit proof'}
        report['completed_at'] = time.time(); report['temp_root_removed'] = not temporary.exists()
        write_report(output / 'workbench-native.json', report)
        full_report['editor_component'] = {**file_evidence(output / 'workbench-native.json'), 'path': 'workbench-native.json'}
        full_report['cleanup'] = report['cleanup']; full_report['temp_root_removed'] = report['temp_root_removed']
        full_report['after_fingerprint'] = report.get('after_fingerprint'); full_report['completed_at'] = report['completed_at']
        full_report['pass'] = full_report['pass'] and report['pass'] and not cleanup_errors
        full_report['qualifies_workbench'] = full_report['pass']
        if full_report['pass']:
            try:
                native = read_json(output / full_report['native_qualification']['path'])
                export = next(check['evidence'] for check in native['checks'] if check['id'] == 'native-export-step')
                full_report['geometry_readback'] = measure_step_readback(output / 'proof' / export['path'], source_sha=source['source_sha'])
                validate_qualification(full_report, output, expected_sha=source['source_sha'], expected_arch=arch, verify_geometry=True)
            except Exception as error:
                full_report['pass'] = full_report['qualifies_workbench'] = False
                full_report['error'] = f'{type(error).__name__}: {error}'
        write_report(output / 'workbench-qualification.json', full_report)
    require(report['pass'], report.get('error', 'Native editor proof incomplete'))
    require(full_report['pass'], full_report.get('error', 'Native full workbench proof blocked'))
    return full_report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arch', choices=('arm64', 'x64', 'x86_64'), required=True)
    args = parser.parse_args(); print(json.dumps(run(args.app, args.output, args.arch), indent=2))
