#!/usr/bin/env python3
"""Source identity and portable candidate evidence; never publishes anything."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import time

REPOSITORY = 'cass-ale/CassQuery'
ARCHES = ('arm64', 'x64')
REQUIRED_STEPS = (
    'Record runner context', 'Locked dependencies', 'Source identity snapshot',
    'Cheap fail-fast preflight', 'Native worker freeze',
    'Frozen real-CAD build, watcher and failure retention',
    'Assemble native app without publishing', 'Render the actual bundled application',
    'Package checked app', 'Verify and launch the final ZIP',
    'Verify and launch the final DMG', 'Candidate identity and hashes',
    'Keep candidate and checked/not-checked evidence',
)
GATES = {
    'preflight': 'evidence/preflight.json',
    'worker': 'evidence/worker-{arch}.json',
    'renderer': 'evidence/mac-{arch}/renderer.json',
    'zip': 'evidence/zip-{arch}/package.json',
    'dmg': 'evidence/dmg-{arch}/package.json',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(file):
    file = Path(file)
    require(file.stat().st_size <= 16 * 1024 * 1024, f'JSON evidence exceeds limit: {file.name}')
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, f'duplicate JSON key: {name}')
            result[name] = value
        return result
    def finite(value):
        raise ValueError(f'non-finite JSON evidence: {value}')
    return json.loads(file.read_text(), object_pairs_hook=unique, parse_constant=finite)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def source_snapshot(root):
    root = Path(root).resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], timeout=30)
    sha = git('rev-parse', 'HEAD').decode().strip()
    require(re.fullmatch(r'[0-9a-f]{40}', sha), 'source must have an exact commit SHA')
    names = git('ls-files', '-co', '--exclude-standard', '-z').split(b'\0')
    files = []
    for raw in sorted(set(name for name in names if name)):
        name = os.fsdecode(raw)
        file = root / name
        require(not file.is_symlink(), f'source symlink is not a reproducible release input: {name}')
        require(file.is_file(), f'missing source input: {name}')
        files.append({'path': name, 'bytes': file.stat().st_size,
                      'executable': bool(file.stat().st_mode & 0o111), 'sha256': digest(file)})
    identity = {'source_sha': sha, 'files': files}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'schema': 1, **identity, 'fingerprint': fingerprint,
            'clean': not bool(git('status', '--porcelain', '--untracked-files=all'))}


def relative_file(root, name):
    require(isinstance(name, str) and name and '\\' not in name, 'invalid artifact path')
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(part not in ('', '.', '..') for part in name.split('/')),
            'artifact path traversal')
    base = Path(root).resolve()
    file = base.joinpath(*path.parts)
    cursor = file
    while cursor != base:
        require(not cursor.is_symlink(), f'artifact symlink: {name}')
        cursor = cursor.parent
    require(file.is_file(), f'missing artifact file: {name}')
    require(file.resolve().is_relative_to(base), 'artifact path outside root')
    return file


def file_record(root, name):
    file = relative_file(root, name)
    return {'path': name, 'bytes': file.stat().st_size, 'sha256': digest(file)}


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_checkpoint(mark):
    require(isinstance(mark, dict) and all(finite_number(mark.get(k)) and mark[k] > 0
            for k in ('wall_time_ms', 'monotonic_ms')), 'missing or invalid checkpoint times')


def validate_native_matrix(matrix):
    """Pure producer contract shared by live native and portable qualification."""
    expected = {(theme, width, state) for theme in ('dark', 'light')
                for width in (450, 700, 1000, 1320) for state in ('assembled', 'apart')}
    require(isinstance(matrix, list) and len(matrix) == len(expected)
            and all(isinstance(row, dict) for row in matrix), 'incomplete native render matrix')
    keys = [(row.get('theme'), row.get('width'), row.get('state')) for row in matrix]
    require(set(keys) == expected and len(set(keys)) == len(keys), 'missing or duplicate native render states')
    for row in matrix:
        require(row.get('status') == 'checked', 'native state skipped or unexecuted')
        require(all(type(row.get(k)) is int and row[k] > 0 for k in ('vertices', 'meshes', 'controls_checked')), 'native state rendered no geometry or controls')
        require(type(row.get('triangles')) is int and row['triangles'] > 0 and row['vertices'] == row['triangles'] * 3
                and type(row.get('degenerate')) is int and row['degenerate'] == 0, 'zero/inconsistent eligible triangle coverage')
        require(all(type(row.get(k)) is int and row[k] == 0 for k in ('clipped', 'nonfinite'))
                and row.get('overflow') is False, 'native geometry or layout failure')
        require(all(row.get(field) == [] for field in ('bad', 'overlaps', 'errors')), 'native render diagnostics')
        require(row.get('realized_theme') == row['theme'], 'missing realized native coverage')
        require(row.get('gates_pass') is True and row.get('tier') == 'certified', 'native render did not show certified geometry')
        require(type(row.get('width')) is int and row.get('viewport') == [row['width'], 700], 'native viewport does not match requested state')
        full = row.get('full_size')
        require(isinstance(full, list) and len(full) == 2 and all(type(v) is int for v in full)
                and full[0] == row['width'] and full[1] >= 700
                and type(row.get('vertical_scroll')) is bool and isinstance(row.get('scroll_regions'), list), 'missing full-document/scroll geometry')
        require(row.get('diag') == '' and row.get('step_pressed') == (['true', 'false'] if row['state'] == 'assembled' else ['false', 'true'])
                and row.get('explode') == ('0%' if row['state'] == 'assembled' else '100%'), 'native controls do not show requested state')
        canvas = row.get('canvas', {})
        require(isinstance(canvas, dict) and all(finite_number(canvas.get(field)) and canvas[field] > 0
                for field in ('width', 'height', 'client_width', 'client_height')), 'missing realized native canvas')
        for key in ('image', 'controls_image') if row['scroll_regions'] else ('image',):
            image = row.get(key, {})
            require(isinstance(image, dict) and isinstance(image.get('path'), str)
                    and re.fullmatch(r'[0-9a-f]{64}', image.get('sha256', ''))
                    and all(type(image.get(k)) is int for k in ('width', 'height'))
                    and image['width'] >= full[0] and image['height'] >= full[1], 'native image does not cover viewport/full document')
        require(all(isinstance(region, dict) and region.get('id') == 'ASIDE'
                    and finite_number(region.get('client_height')) and finite_number(region.get('scroll_height'))
                    and 0 < region['client_height'] < region['scroll_height'] <= region['client_height'] * 2
                    for region in row['scroll_regions']), 'incomplete scroll-region coverage')
    for width in (450, 700, 1000, 1320):
        colors = [r.get('background') for r in matrix if r['width'] == width and r['state'] == 'assembled']
        require(all(isinstance(color, str) and color for color in colors) and colors[0] != colors[1], 'native theme style did not change')


NATIVE_CHECKS = {'certified demo', 'gate pass', 'assembled step', 'exploded step', 'reset view',
                 'save updates rendered model', 'camera preserved on rebuild',
                 'failed gate restores certified model', 'no renderer diagnostics'}


def validate_graphics(report, expected_arch):
    graphics = report.get('graphics', {})
    require(isinstance(graphics, dict), 'missing native graphics evidence')
    runtime = graphics.get('runtime', {})
    require(isinstance(runtime, dict) and all(isinstance(runtime.get(k), str) and runtime[k]
            for k in ('electron', 'chromium', 'node')) and runtime.get('platform') == 'darwin'
            and runtime.get('architecture') == expected_arch, 'missing native graphics runtime identity')
    display = graphics.get('display', {})
    require(isinstance(display, dict) and finite_number(display.get('scale_factor')) and display['scale_factor'] > 0, 'missing native display scale')
    for key in ('bounds', 'work_area', 'window_bounds'):
        rect = display.get(key, {})
        require(isinstance(rect, dict) and all(finite_number(rect.get(k)) for k in ('x', 'y', 'width', 'height'))
                and rect['width'] > 0 and rect['height'] > 0, 'missing native display/window geometry')
    require(isinstance(graphics.get('gpu'), dict) and graphics['gpu'], 'missing actual native GPU information')
    webgl = graphics.get('webgl', {})
    require(isinstance(webgl, dict) and webgl.get('context') == 'webgl2' and webgl.get('is_context_lost') is False
            and all(isinstance(webgl.get(k), str) and webgl[k] for k in ('vendor', 'renderer', 'version', 'shading_language'))
            and finite_number(webgl.get('dpr')) and webgl['dpr'] > 0, 'missing/live WebGL2 context evidence')
    for key in ('drawing_buffer', 'viewport'):
        size = webgl.get(key)
        require(isinstance(size, list) and len(size) == 2 and all(type(v) is int and v > 0 for v in size), 'missing WebGL buffer/viewport geometry')
    backend = report.get('render_backend_requested')
    require(backend in ('desktop-default', 'metal-requested', 'swiftshader-requested'), 'unsupported native render backend evidence')
    if backend == 'swiftshader-requested':
        renderer = webgl.get('unmasked_renderer')
        require(isinstance(renderer, str) and re.search('SwiftShader', renderer, re.IGNORECASE), 'requested software rendering lacks observed SwiftShader context')


NATIVE_SOURCE_PATHS = ('shell/main.js', 'shell/renderer_config.cjs', 'shell/runtime.cjs', 'shell/smoke.cjs')


def validate_native_source(value):
    require(isinstance(value, dict) and isinstance(value.get('files'), list), 'missing executed native source identity')
    files = value['files']
    require(len(files) == 4 and all(isinstance(item, dict) and set(item) == {'bytes', 'path', 'sha256'}
            and type(item.get('bytes')) is int and 0 < item['bytes'] <= 1024 * 1024
            and isinstance(item.get('path'), str) and re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', '')) for item in files)
            and tuple(item['path'] for item in files) == NATIVE_SOURCE_PATHS, 'invalid executed native source records')
    fingerprint = hashlib.sha256(json.dumps({'files': files}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(value.get('fingerprint') == fingerprint, 'executed native source fingerprint mismatch')


def camera_snapshot(value):
    require(isinstance(value, dict) and finite_number(value.get('radius')) and value['radius'] > 0, 'missing camera model radius')
    for key in ('direction', 'target', 'center'):
        vector = value.get(key)
        require(isinstance(vector, list) and len(vector) == 3 and all(finite_number(v) for v in vector), 'invalid camera snapshot vector')
    require(math.isclose(math.hypot(*value['direction']), 1, rel_tol=1e-6, abs_tol=1e-6), 'camera direction is not normalized')


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def validate_cameras(report, bundled, images):
    checks = report.get('camera_checks')
    require(isinstance(checks, list) and len(checks) == 2 and all(isinstance(row, dict) for row in checks)
            and {row.get('mode') for row in checks} == {'orbit', 'pan'}, 'missing native orbit/pan camera checks')
    default = [0.95, -1.35, 0.62]
    default = [v / math.hypot(*default) for v in default]
    for row in checks:
        require(row.get('status') == 'checked' and isinstance(row.get('method'), str) and row['method'], 'camera input/reset not checked')
        for key in ('before', 'after_drag', 'after_reset_700', 'after_reset_settled'):
            camera_snapshot(row.get(key))
        before, dragged = row['before'], row['after_drag']
        if row['mode'] == 'orbit':
            require(dot(before['direction'], dragged['direction']) < 0.999, 'native orbit input did not move camera')
        else:
            require(math.dist(before['target'], dragged['target']) > before['radius'] * 0.001, 'native pan input did not move target')
        for key in ('after_reset_700', 'after_reset_settled'):
            state = row[key]
            require(dot(state['direction'], default) > 0.9999 and math.dist(state['target'], state['center']) <= state['radius'] * 1e-6, 'native reset retained orbit/pan motion')
        for key, suffix in (('drag_image', 'drag'), ('reset_image', 'reset')):
            image = row.get(key)
            require(isinstance(image, dict) and image in images
                    and PurePosixPath(image.get('path', '')).name == f"{row['mode']}-{suffix}.png", 'missing native camera input/reset capture')
    if not bundled:
        saved = report.get('camera_save', {})
        require(isinstance(saved, dict) and isinstance(saved.get('method'), str) and saved['method'], 'missing settled camera/save evidence')
        for key in ('before_drag', 'before_save', 'after_save'):
            camera_snapshot(saved.get(key))
        require(dot(saved['before_drag']['direction'], saved['before_save']['direction']) < 0.999
                and dot(saved['before_save']['direction'], saved['after_save']['direction']) > 0.9999, 'save used default camera or stole settled direction')
        require(saved.get('image') in images and PurePosixPath(saved.get('image', {}).get('path', '')).name == 'save-camera.png', 'missing pre-save settled camera capture')


def validate_native_renderer(report, *, expected_sha, expected_arch, expected_version, bundled, started, finished):
    """Validate raw renderer semantics, without platform imports or filesystem I/O.

    The caller supplies actual launch bounds for live validation. Portable replay
    supplies digest-bound recorded bounds: internal conformance, not proof of execution.
    """
    require(isinstance(report, dict) and type(report.get('schema')) is int and report['schema'] == 1
            and report.get('pass') is True, 'native renderer did not pass')
    require(all(type(report.get(k)) is int and report[k] > 0 for k in ('app_pid', 'worker_pid'))
            and report['worker_pid'] != report['app_pid']
            and isinstance(report.get('challenge'), str) and bool(report['challenge']), 'missing native PID/challenge identity')
    require(all(report.get(k) is True for k in ('packaged', 'bundled_worker', 'worker_shutdown'))
            and report.get('errors') == [], 'native packaged worker/shutdown/diagnostics failure')
    require(report.get('source_sha') == expected_sha and report.get('architecture') == expected_arch
            and report.get('version') == expected_version, 'native source/architecture/version mismatch')
    validate_native_source(report.get('native_source'))
    validate_graphics(report, expected_arch)
    resources = report.get('resources_path')
    require(isinstance(resources, str) and PurePosixPath(resources).is_absolute()
            and report.get('worker_executable') == str(PurePosixPath(resources) / 'worker/cassquery-worker'), 'native worker/resources executable mismatch')
    validate_checkpoint(started)
    validate_checkpoint(finished)
    require(started['monotonic_ms'] < finished['monotonic_ms'] and started['wall_time_ms'] <= finished['wall_time_ms'], 'invalid native launch bounds')
    checkpoints = report.get('checkpoints')
    require(isinstance(checkpoints, list), 'missing native checkpoints')
    marks = [report.get('started'), *checkpoints, report.get('finished')]
    for mark in marks:
        validate_checkpoint(mark)
    ticks = [mark['monotonic_ms'] for mark in marks]
    require(len(marks) >= 8 and all(started['monotonic_ms'] <= v <= finished['monotonic_ms'] for v in ticks)
            and all(a < b for a, b in zip(ticks, ticks[1:]))
            and all(started['wall_time_ms'] - 1000 <= m['wall_time_ms'] <= finished['wall_time_ms'] + 1000 for m in marks), 'stale/nonmonotonic native checkpoints')
    names = [mark.get('name') for mark in checkpoints]
    required = {'renderer-start', 'certified-ready', 'production-controls', 'native-matrix', 'renderer-complete', 'worker-shutdown'}
    if not bundled:
        required |= {'save-certified', 'failed-gate-retained'}
    require(all(isinstance(name, str) for name in names) and len(names) == len(set(names)) and required.issubset(names), 'missing/duplicate native checkpoints')
    lifecycle = report.get('lifecycle', {})
    require(isinstance(lifecycle, dict) and lifecycle.get('pass') is True and lifecycle.get('close_reopen') is True
            and type(lifecycle.get('secondary_exit')) is int and lifecycle['secondary_exit'] == 0
            and type(lifecycle.get('secondary_pid')) is int and lifecycle['secondary_pid'] > 0
            and type(lifecycle.get('second_instance_events')) is int and lifecycle['second_instance_events'] == 1
            and all(type(lifecycle.get(k)) is int and lifecycle[k] == report['worker_pid'] for k in ('worker_pid_before', 'worker_pid_after')), 'native lifecycle worker lineage mismatch')
    checks = report.get('checks')
    require(isinstance(checks, list) and all(isinstance(check, str) for check in checks), 'missing native checks')
    if bundled:
        require('bundled default certified demo' in checks, 'missing cold default launch check')
        status = report.get('worker_status', {})
        projects = status.get('projects', [])
        require(status.get('api_version') == 2 and status.get('load_errors') == []
                and isinstance(projects, list) and len(projects) == 1 and projects[0].get('name') == 'TESTPLATE'
                and projects[0].get('root') == str(PurePosixPath(resources) / 'worker/_internal/testpart'), 'bundled default worker status/root mismatch')
    else:
        require(len(checks) == len(NATIVE_CHECKS) and set(checks) == NATIVE_CHECKS, 'incomplete native production checks')
        for key in ('save_state', 'failure_state'):
            state = report.get(key, {})
            require(isinstance(state, dict) and type(state.get('vertices')) is int and state['vertices'] > 0
                    and type(state.get('triangles')) is int and state['triangles'] > 0 and state['vertices'] == state['triangles'] * 3
                    and type(state.get('degenerate')) is int and state['degenerate'] == 0
                    and type(state.get('clipped')) is int and state['clipped'] == 0
                    and state.get('tier') == 'certified' and state.get('gates_pass') is True
                    and isinstance(state.get('hash'), str) and state['hash'], 'native save/failure state lost certified geometry')
        require(type(report['save_state'].get('nonfinite')) is int and report['save_state']['nonfinite'] == 0
                and report['save_state'].get('diag') == ''
                and report['failure_state']['hash'] == report['save_state']['hash'], 'native save/failure retention contradicts certified state')
    validate_native_matrix(report.get('matrix'))
    images = report.get('images')
    require(isinstance(images, list) and all(isinstance(image, dict) for image in images), 'missing native image inventory')
    required_images = {f"{row['theme']}-{row['width']}-{row['state']}.png" for row in report['matrix']}
    required_images |= {f"{row['theme']}-{row['width']}-{row['state']}-controls.png" for row in report['matrix'] if row['scroll_regions']}
    required_images |= {'orbit-drag.png', 'orbit-reset.png', 'pan-drag.png', 'pan-reset.png', 'assembled.png', 'apart.png'}
    if not bundled:
        required_images |= {'save-camera.png', 'save-certified.png', 'gate-failed.png'}
    paths = [image.get('path') for image in images]
    require(all(isinstance(path, str) and path for path in paths) and len(paths) == len(set(paths))
            and len(images) == len(required_images) and {PurePosixPath(path).name for path in paths} == required_images, 'native image inventory does not cover production captures')
    for row in report['matrix']:
        for key in ('image', 'controls_image') if row['scroll_regions'] else ('image',):
            image = row[key]
            suffix = '-controls' if key == 'controls_image' else ''
            require(image in images and PurePosixPath(image['path']).name == f"{row['theme']}-{row['width']}-{row['state']}{suffix}.png", 'native matrix image not in checked inventory')
    validate_cameras(report, bundled, images)
    for state in ('assembled', 'apart'):
        image = next(image for image in images if PurePosixPath(image['path']).name == state + '.png')
        source = next(row['image'] for row in report['matrix'] if row['theme'] == 'light' and row['width'] == 1320 and row['state'] == state)
        require(all(image.get(k) == source.get(k) for k in ('sha256', 'width', 'height')), 'native legacy capture contradicts matrix frame')
    shots = report.get('screenshots')
    require(isinstance(shots, list) and len(shots) == 2 and all(isinstance(path, str) for path in shots)
            and [PurePosixPath(path).name for path in shots] == ['assembled.png', 'apart.png']
            and all(path in paths for path in shots), 'native legacy screenshots missing')


def validate_runtime(proof, pid, challenge, *, executable, started, finished):
    require(type(pid) is int and pid > 0 and isinstance(challenge, str) and challenge
            and isinstance(proof, dict) and type(proof.get('schema')) is int and proof['schema'] == 1
            and type(proof.get('pid')) is int and proof['pid'] == pid and proof.get('challenge') == challenge
            and proof.get('frozen') is True, 'missing/mismatched frozen worker runtime identity')
    require(isinstance(executable, str) and PurePosixPath(executable).is_absolute()
            and proof.get('executable') == executable
            and proof.get('resource_root') == str(PurePosixPath(executable).parent / '_internal'), 'frozen worker runtime executable/resource root mismatch')
    for mark in (proof, started, finished):
        validate_checkpoint(mark)
    require(all(started[k] <= proof[k] <= finished[k] for k in ('monotonic_ms', 'wall_time_ms')), 'stale frozen worker runtime checkpoint')


def verify_record(root, report_path, record):
    require(isinstance(record, dict) and isinstance(record.get('path'), str), 'missing linked evidence record')
    prefix = PurePosixPath(report_path).parent
    path = (prefix / record['path']).as_posix()
    actual = file_record(root, path)
    require(actual['sha256'] == record.get('sha256'), 'linked evidence digest mismatch')
    if 'bytes' in record:
        require(actual['bytes'] == record['bytes'], 'linked evidence size mismatch')
    return path


def matrix_identity(root, report_path, matrix):
    values = []
    for row in matrix:
        item = dict(row)
        for key in ('image', 'controls_image'):
            if key in item:
                record = item[key]
                name = verify_record(root, report_path, record)
                item[key] = {**record, 'path': name}
        values.append(item)
    return values


def manifest_record(root, records, name):
    require(name in records, f'linked evidence is not in candidate inventory: {name}')
    require(file_record(root, name) == records[name], f'linked candidate evidence changed: {name}')


def images_identity(root, report_path, images, records, cache):
    require(isinstance(images, list), 'missing native image inventory')
    values = []
    for image in images:
        require(isinstance(image, dict), 'invalid native image record')
        name = verify_record(root, report_path, image)
        manifest_record(root, records, name)
        file = relative_file(root, name)
        require(1000 < file.stat().st_size <= 32 * 1024 * 1024, 'invalid native PNG evidence size')
        key = (records[name]['sha256'], records[name]['bytes'])
        if key not in cache:
            import importlib.util
            spec = importlib.util.spec_from_file_location('png_evidence', Path(__file__).with_name('png_evidence.py'))
            png = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(png)
            cache[key] = png.dimensions(file.read_bytes())
        dimensions = cache[key]
        require(all(type(image.get(k)) is int for k in ('width', 'height'))
                and list(dimensions) == [image['width'], image['height']] and min(dimensions) >= 100, 'image content/dimensions do not match evidence')
        values.append({**image, 'path': name})
    require(len({image['path'] for image in values}) == len(values), 'duplicate native image inventory')
    return values


def linked_json(root, report_path, record, records):
    path = verify_record(root, report_path, record)
    manifest_record(root, records, path)
    return path, read_json(relative_file(root, path))


def verify_native_report(root, report_path, report, *, expected_sha, expected_arch,
                         expected_version, records, source_records, cache):
    """Replay the final native wrapper AND its actual raw cold/warm contracts."""
    require(type(report.get('schema')) is int and report['schema'] == 1 and report.get('pass') is True,
            'package child native launch did not pass')
    launch = report.get('launch', {})
    require(isinstance(launch, dict) and type(launch.get('exit')) is int and launch['exit'] == 0
            and isinstance(launch.get('project_override'), str) and launch['project_override'], 'native launch exit/project context missing or failed')
    for mark in (report.get('started'), report.get('finished')):
        validate_checkpoint(mark)
    outer = report.get('checkpoints')
    require(isinstance(outer, list) and len(outer) >= 3, 'missing native wrapper checkpoint times')
    for mark in outer:
        validate_checkpoint(mark)
    require({'native-start', 'bundled-launch-complete', 'native-complete'}.issubset({m.get('name') for m in outer})
            and all(a['monotonic_ms'] < b['monotonic_ms'] for a, b in zip(outer, outer[1:])), 'missing/nonmonotonic native wrapper checkpoints')
    warm = dict(report)
    warm.update(started=report.get('renderer_started'), finished=report.get('renderer_finished'),
                checkpoints=report.get('renderer_checkpoints'))
    images = report.get('images')
    require(isinstance(images, list) and all(isinstance(image, dict) and isinstance(image.get('path'), str) for image in images), 'missing final native images')
    warm['images'] = [image for image in images if not image['path'].startswith('bundled/')]
    require(all(PurePosixPath(image['path']).name == image['path'] for image in warm['images']), 'native capture path does not match report directory')
    validate_native_renderer(warm, expected_sha=expected_sha, expected_arch=expected_arch,
                             expected_version=expected_version, bundled=False,
                             started=launch.get('started'), finished=launch.get('finished'))
    cold_path, cold = linked_json(root, report_path, report.get('bundled_report'), records)
    require(cold_path == (PurePosixPath(report_path).parent / 'bundled/renderer.json').as_posix(), 'wrong cold bundled report path')
    cold_launch = cold.get('launch', {})
    require(type(cold_launch.get('exit')) is int and cold_launch['exit'] == 0
            and cold_launch.get('project_override') is None, 'cold launch context contradicts default demo')
    validate_native_renderer(cold, expected_sha=expected_sha, expected_arch=expected_arch,
                             expected_version=expected_version, bundled=True,
                             started=cold_launch.get('started'), finished=cold_launch.get('finished'))
    require(cold.get('native_source') == report.get('native_source'), 'cold/warm executed native source differs')
    for item in report['native_source']['files']:
        expected = source_records.get(item['path'], {})
        require(item['sha256'] == expected.get('sha256') and item['bytes'] == expected.get('bytes'), 'executed native source differs from original source')
    require(cold.get('render_backend_requested') == report.get('render_backend_requested'), 'cold/warm renderer backend context differs')
    for child_launch in (cold_launch, launch):
        for key in ('started', 'finished'):
            require(all(report['started'][k] <= child_launch[key][k] <= report['finished'][k]
                    for k in ('monotonic_ms', 'wall_time_ms')), 'native child launch outside wrapper time bounds')
    require(cold_launch['finished']['monotonic_ms'] <= launch['started']['monotonic_ms'], 'cold/warm native launches overlap')
    require(all(report['started']['monotonic_ms'] <= mark['monotonic_ms'] <= report['finished']['monotonic_ms'] for mark in outer), 'native wrapper checkpoints outside launch bounds')
    require(all(PurePosixPath(image['path']).name == image['path'] for image in cold['images']), 'cold capture path does not match report directory')
    cold_images = images_identity(root, cold_path, cold['images'], records, cache)
    warm_images = images_identity(root, report_path, warm['images'], records, cache)
    require(images_identity(root, report_path, images, records, cache) == cold_images + warm_images,
            'final native image inventory contradicts cold/warm captures')
    _, proof = linked_json(root, report_path, report.get('worker_runtime_evidence'), records)
    require(proof == report.get('worker_runtime'), 'native runtime file contradicts inline evidence')
    validate_runtime(proof, report['worker_pid'], report['challenge'], executable=report['worker_executable'],
                     started=launch['started'], finished=launch['finished'])
    _, before = linked_json(root, report_path, report.get('bundle_before'), records)
    _, after = linked_json(root, report_path, report.get('bundle_after'), records)
    require(before == after and isinstance(before.get('records'), list) and before['records'], 'native bundle inventory changed or missing')
    tree = before['records']
    require(type(before.get('entries')) is int and before['entries'] == len(tree)
            and hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == before.get('sha256'), 'native bundle inventory digest/count mismatch')
    bundle = report.get('bundle', {})
    require(bundle.get('bundle_id') == 'com.cassquery.desktop' and bundle.get('architecture') == expected_arch
            and bundle.get('version') == expected_version and bundle.get('bundle_sha256') == before['sha256'], 'native bundle identity differs from inventory')
    by_path = {item.get('path'): item for item in tree}
    require(len(by_path) == len(tree), 'duplicate native bundle entries')
    machos = bundle.get('machos', [])
    require(isinstance(machos, list) and {'Contents/MacOS/CassQuery', 'Contents/Resources/worker/cassquery-worker'}.issubset({m.get('path') for m in machos}), 'missing native app/worker identity')
    for macho in machos:
        require(expected_arch in macho.get('architectures', []) and macho.get('sha256') == by_path.get(macho.get('path'), {}).get('sha256'), 'native Mach-O identity contradicts bundle inventory')
    sources = bundle.get('source_files', [])
    expected_sources = {name for name in source_records if name.startswith(('viewer/', 'testpart/'))
                        or name in ('worker.py', 'shell/main.js', 'shell/runtime.cjs', 'shell/smoke.cjs', 'shell/renderer_config.cjs')}
    require(isinstance(sources, list) and expected_sources, 'missing native/source parity evidence')
    found = set()
    for item in sources:
        matches = [name for name in expected_sources if item.get('path', '').endswith('/' + name) or item.get('path') == name]
        require(len(matches) == 1 and matches[0] not in found, 'native source parity path missing/duplicate')
        name = matches[0]
        require(item.get('sha256') == source_records[name]['sha256'] and item.get('bytes') == source_records[name]['bytes'], 'native bundled source differs from original source')
        found.add(name)
    require(found == expected_sources, 'native source parity coverage incomplete')
    icon = bundle.get('icon', {})
    require('assets/icons/cassquery.icns' in source_records and icon.get('sha256') == source_records['assets/icons/cassquery.icns']['sha256']
            and isinstance(icon.get('path'), str) and '/Contents/Resources/' in icon['path'], 'native icon differs from original source')
    icon_path = 'Contents/Resources/' + icon['path'].rsplit('/Contents/Resources/', 1)[1]
    require(icon.get('sha256') == by_path.get(icon_path, {}).get('sha256'), 'native icon contradicts bundle inventory')
    return cold_images + warm_images


def verify_candidate(root, manifest=None, *, expected_sha, expected_run_id, expected_arch,
                     expected_attempt=None, expected_version=None):
    root = Path(root)
    manifest = manifest or read_json(relative_file(root, 'candidate.json'))
    require(type(manifest.get('schema')) is int and manifest['schema'] == 1 and manifest.get('repository') == REPOSITORY, 'untrusted candidate schema/repository')
    require(re.fullmatch(r'[0-9a-f]{40}', expected_sha or ''), 'full expected source SHA required')
    require(manifest.get('source_sha') == expected_sha, 'candidate source mismatch')
    require(type(manifest.get('run_id')) is int and manifest['run_id'] == expected_run_id and type(expected_run_id) is int and expected_run_id > 0, 'candidate run mismatch')
    attempt = manifest.get('run_attempt')
    require(type(attempt) is int and attempt > 0, 'missing run attempt')
    if expected_attempt is not None:
        require(attempt == expected_attempt, 'candidate attempt mismatch')
    require(expected_arch in ARCHES and manifest.get('architecture') == expected_arch, 'candidate architecture mismatch')
    version = manifest.get('version')
    require(isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+', version), 'invalid candidate version')
    if expected_version is not None:
        require(version == expected_version, 'candidate version mismatch')
    require(re.fullmatch(r'[0-9a-f]{64}', manifest.get('source_fingerprint', '')), 'missing source fingerprint')
    require(manifest.get('distribution') == 'adhoc', 'Developer ID promotion requires a separately verified signing contract')
    files = manifest.get('files')
    require(isinstance(files, list) and files and len(files) <= 10000, 'empty or excessive candidate file inventory')
    names = [record.get('path') for record in files]
    require(all(isinstance(name, str) for name in names) and len(set(names)) == len(names), 'duplicate/invalid candidate files')
    records = {}
    for record in files:
        require(re.fullmatch(r'[0-9a-f]{64}', record.get('sha256', '')), 'invalid candidate file digest')
        require(type(record.get('bytes')) is int and 0 < record['bytes'] <= 4 * 1024**3, 'invalid candidate file size')
        actual = file_record(root, record['path'])
        require(actual == record, f'candidate bytes changed: {record["path"]}')
        records[record['path']] = record
    actual_names = {file.relative_to(root).as_posix() for file in root.rglob('*') if file.is_file()}
    require(actual_names == set(names) | {'candidate.json'}, 'unexpected or unlisted candidate files')
    for file in root.rglob('*'):
        require(not file.is_symlink(), 'candidate tree contains a symlink')
    require('evidence/source-before.json' in records, 'missing original source identity')
    before = read_json(relative_file(root, 'evidence/source-before.json'))
    require(type(before.get('schema')) is int and before['schema'] == 1 and before.get('clean') is True and before.get('source_sha') == expected_sha,
            'uncommitted or incorrect candidate source')
    before_files = before.get('files')
    require(isinstance(before_files, list) and before_files and len(before_files) <= 10000, 'source snapshot has no inputs')
    source_names = [file.get('path') for file in before_files]
    require(all(isinstance(name, str) and name and not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts for name in source_names)
            and len(set(source_names)) == len(source_names), 'source snapshot paths invalid')
    require(all(type(file.get('bytes')) is int and file['bytes'] >= 0 and re.fullmatch(r'[0-9a-f]{64}', file.get('sha256', ''))
                and type(file.get('executable')) is bool for file in before_files), 'invalid source input digest/size/mode')
    identity = {'source_sha': before.get('source_sha'), 'files': before_files}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(fingerprint == before.get('fingerprint') == manifest['source_fingerprint'], 'candidate source fingerprint not bound to original snapshot')
    expected_assets = {f'dist/mac-app/CassQuery-{version}-mac-{expected_arch}.{extension}' for extension in ('zip', 'dmg')}
    assets = manifest.get('assets')
    require(isinstance(assets, list) and len(assets) == 2 and {asset.get('path') for asset in assets} == expected_assets,
            'incorrect native package inventory')
    for asset in assets:
        require(asset == records.get(asset['path']), 'package not bound to file inventory')
    gates = manifest.get('gates')
    require(isinstance(gates, dict) and set(gates) == set(GATES), 'missing or unexpected gate records')
    source_records = {record['path']: record for record in before_files}
    image_cache = {}
    native_bundle_sha = None
    for name, template in GATES.items():
        required_path = template.format(arch=expected_arch)
        record = gates[name]
        require(record == records.get(required_path), f'gate evidence is not digest-bound: {name}')
        report = read_json(relative_file(root, required_path))
        require(type(report.get('schema')) is int and report['schema'] == 1 and report.get('pass') is True, f'gate did not pass: {name}')
        if name == 'worker':
            ids = {'units', 'watertight', 'volume', 'thin-extent', 'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes'}
            gates_data = report.get('gates', {})
            checks = gates_data.get('checks', [])
            require(gates_data.get('pass') is True and len(checks) == 8
                    and {check.get('id') for check in checks} == ids
                    and all(check.get('status') == 'pass' for check in checks), 'frozen worker CAD gates incomplete')
            runtime_path, runtime_data = linked_json(root, required_path, report.get('runtime_evidence'), records)
            require(runtime_data == report.get('worker_runtime'), 'worker runtime file contradicts gate evidence')
            marks = report.get('checkpoints', [])
            require(isinstance(marks, list) and len(marks) >= 2, 'missing worker checkpoint times')
            for mark in marks:
                validate_checkpoint(mark)
            require(all(a['monotonic_ms'] < b['monotonic_ms'] for a, b in zip(marks, marks[1:])), 'nonmonotonic worker checkpoints')
            require(report.get('worker_shutdown') is True, 'frozen worker/runtime shutdown not proven')
            validate_runtime(runtime_data, report.get('worker_pid'), report.get('challenge'),
                             executable=report.get('executable', {}).get('path'), started=marks[0], finished=marks[-1])
        if name == 'renderer':
            verify_native_report(root, required_path, report, expected_sha=expected_sha,
                                 expected_arch=expected_arch, expected_version=version, records=records,
                                 source_records=source_records, cache=image_cache)
            native_bundle_sha = report['bundle']['bundle_sha256']
        if name in ('zip', 'dmg'):
            asset = report.get('asset', {})
            expected_path = f'dist/mac-app/CassQuery-{version}-mac-{expected_arch}.{name}'
            require(asset.get('sha256') == records[expected_path]['sha256'] and asset.get('bytes') == records[expected_path]['bytes'], 'package smoke does not bind final artifact bytes')
            launches = report.get('launches', {})
            expected_launches = {'mounted', 'copied'} if name == 'dmg' else {'extracted', 'copied'}
            require(set(launches) == expected_launches, 'final package launch coverage incomplete')
            child_images = []
            for launch in launches.values():
                validate_native_matrix(launch.get('matrix'))
                native_path, native = linked_json(root, required_path, launch.get('native_report'), records)
                child_images.extend(verify_native_report(root, native_path, native, expected_sha=expected_sha,
                                    expected_arch=expected_arch, expected_version=version, records=records,
                                    source_records=source_records, cache=image_cache))
                require(matrix_identity(root, native_path, native.get('matrix')) == matrix_identity(root, required_path, launch.get('matrix')), 'package matrix contradicts native child evidence')
                require(images_identity(root, required_path, launch.get('images'), records, image_cache)
                        == images_identity(root, native_path, native['images'], records, image_cache), 'package launch images contradict native child evidence')
                require(launch.get('bundle') == native.get('bundle') and native['bundle']['bundle_sha256'] == native_bundle_sha,
                        'package native bundle differs from checked prepackaged app')
            # JSON object key order is not launch order; compare the complete verified inventory.
            require(sorted(images_identity(root, required_path, report.get('images'), records, image_cache), key=lambda image: image['path'])
                    == sorted(child_images, key=lambda image: image['path']),
                    'package aggregate images contradict native child evidence')
            _, checked = linked_json(root, required_path, report.get('checked_inventory'), records)
            _, packaged = linked_json(root, required_path, report.get('bundle_inventory'), records)
            require(checked == packaged and checked.get('sha256') == native_bundle_sha
                    and report.get('checked_bundle_sha256') == native_bundle_sha, 'package inventory differs from checked prepackaged app')
            require(report.get('temp_root_removed') is True, 'package cleanup not proven')
            if name == 'dmg':
                require(report.get('detach_completed') is True, 'DMG still mounted or detach skipped')
        images_identity(root, required_path, report.get('images', []), records, image_cache)
        require(type(report.get('run_attempt', 1)) is int and report.get('run_attempt', 1) == attempt, f'{name} attempt mismatch')
        if name == 'preflight':
            require(report.get('source_sha') == expected_sha and report.get('source_fingerprint') == manifest['source_fingerprint'], 'stale preflight identity')
            require(report.get('failures') == 0 and report.get('skipped') == 0 and type(report.get('tests')) is int and report['tests'] > 0, 'incomplete preflight tests')
            require(report.get('clean') is True and report.get('after_fingerprint') == manifest['source_fingerprint'], 'source changed during preflight')
            stages = report.get('stages', [])
            require(len(stages) == 5 and {stage.get('name') for stage in stages} == {'syntax', 'node', 'python', 'release-contract', 'brand-exports'}, 'missing source preflight stages')
            for stage in stages:
                require(stage.get('exit') == 0 and stage.get('path') == stage['name'] + '.log', 'preflight stage failed or log substituted')
                manifest_record(root, records, verify_record(root, required_path, stage))
                if stage.get('counts'):
                    linked_json(root, required_path, stage['counts'], records)
            from preflight import validate_preflight
            validate_preflight(report, relative_file(root, required_path).parent, pinned=set(source_records))
        for field, expected in (('source_sha', expected_sha), ('architecture', expected_arch), ('version', version)):
            actual = report.get(field)
            if field == 'architecture':
                actual = 'x64' if actual == 'x86_64' else actual
            require(actual == expected, f'{name} {field} mismatch')
    require(any(record['path'] == 'evidence/sbom.json' for record in files), 'missing supply-chain inventory')
    sbom = read_json(relative_file(root, 'evidence/sbom.json'))
    require(sbom.get('bomFormat') == 'CycloneDX' and sbom.get('specVersion') == '1.5' and isinstance(sbom.get('components'), list) and sbom['components'], 'empty or invalid SBOM')
    properties = {entry.get('name'): entry.get('value') for entry in sbom.get('metadata', {}).get('properties', [])}
    require(properties.get('source_sha') == expected_sha and re.fullmatch(r'[0-9a-f]{64}', properties.get('python-wheel-lock-sha256', ''))
            and re.fullmatch(r'[0-9a-f]{64}', properties.get('npm-lock-sha256', '')), 'SBOM source/lock identity missing')
    components = sbom.get('components')
    require(all(isinstance(component, dict) and component.get('type') == 'library'
                and isinstance(component.get('name'), str) and component['name']
                and isinstance(component.get('version'), str) and component['version']
                and isinstance(component.get('purl'), str) for component in components), 'malformed SBOM component')
    python_components = [component for component in components if component['purl'].startswith('pkg:pypi/')]
    npm_components = [component for component in components if component['purl'].startswith('pkg:npm/')]
    require(len(python_components) + len(npm_components) == len(components), 'unsupported SBOM dependency identity')
    for component in python_components + npm_components:
        expected_purl = (f"pkg:pypi/{component['name']}@{component['version']}" if component in python_components
                         else f"pkg:npm/{component['name'].replace('@', '%40')}@{component['version']}")
        require(component['purl'] == expected_purl, 'SBOM purl contradicts locked component name/version')
    require(len(python_components) == 51 and npm_components, 'SBOM dependency graph incomplete')
    require(all(component.get('hashes') and all(item.get('alg') == 'SHA-256' and re.fullmatch(r'[0-9a-f]{64}', item.get('content', ''))
                                               for item in component['hashes']) for component in python_components), 'Python SBOM wheel hashes missing')
    require(all(any(item.get('name') == 'npm:integrity' and item.get('value') for item in component.get('properties', []))
                for component in npm_components), 'npm SBOM integrity missing')
    require(len({component['purl'] for component in components}) == len(components), 'duplicate SBOM dependency component')
    source_records = {record['path']: record for record in before_files}
    for name in ('requirements-build.txt', 'package.json', 'package-lock.json', 'packaging/requirements-macos.lock', 'packaging/wheel-manifest.json'):
        manifest_record(root, records, name)
        require(name in source_records and records[name]['sha256'] == source_records[name]['sha256'], 'supply-chain inputs differ from original source')
    require(read_json(relative_file(root, 'package.json')).get('version') == version, 'candidate version differs from source package')
    require(properties['python-wheel-lock-sha256'] == records['packaging/requirements-macos.lock']['sha256']
            and properties['npm-lock-sha256'] == records['package-lock.json']['sha256'], 'SBOM lock hashes differ from source')
    requirements = {}
    for line in relative_file(root, 'requirements-build.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            name, package_version = line.split('==')
            requirements[re.sub(r'[-_.]+', '-', name).lower()] = package_version
    require({component['name']: component['version'] for component in python_components} == requirements, 'SBOM Python graph differs from source')
    npm_lock = read_json(relative_file(root, 'package-lock.json'))
    expected_npm = {(name.rsplit('node_modules/', 1)[-1], package['version'])
                    for name, package in npm_lock['packages'].items() if name and package.get('version')}
    require({(component['name'], component['version']) for component in npm_components} == expected_npm, 'SBOM npm graph differs from source')
    wheel_manifest = read_json(relative_file(root, 'packaging/wheel-manifest.json'))
    for component in python_components:
        expected_hashes = {wheel['sha256'] for wheel in wheel_manifest['wheels']
                           if wheel['name'] == component['name'] and wheel['version'] == component['version']}
        require({record['content'] for record in component['hashes']} == expected_hashes, 'SBOM Python wheel digests differ from source')
    npm_integrities = {}
    for name, package in npm_lock['packages'].items():
        if name and package.get('version'):
            key = (name.rsplit('node_modules/', 1)[-1], package['version'])
            if key in npm_integrities:
                require(npm_integrities[key] == package.get('integrity'), 'conflicting npm package integrity')
            npm_integrities[key] = package.get('integrity')
    for component in npm_components:
        integrity = [record['value'] for record in component['properties'] if record.get('name') == 'npm:integrity']
        require(integrity == [npm_integrities[(component['name'], component['version'])]], 'SBOM npm integrity differs from source')
    return manifest


INTEGRATION_STEPS = ('Record integration context', 'Source integration dependencies',
                     'Real CAD browser loop and mutation witness', 'Validate independent report bytes',
                     'Keep integration evidence')


def run_jobs(run, jobs):
    """An API envelope proves its count; a list is the complete paginator output."""
    require(type(run.get('id')) is int and run['id'] > 0 and type(run.get('run_attempt')) is int
            and run['run_attempt'] > 0, 'missing run identity/attempt')
    if isinstance(jobs, dict):
        require(type(jobs.get('total_count')) is int and isinstance(jobs.get('jobs'), list)
                and jobs['total_count'] == len(jobs['jobs']) and jobs.get('complete', True) is True,
                'truncated/incomplete jobs envelope')
        jobs = jobs['jobs']
    require(isinstance(jobs, list) and jobs and all(isinstance(job, dict) for job in jobs), 'missing jobs')
    ids = [job.get('id') for job in jobs]
    require(all(type(value) is int and value > 0 for value in ids) and len(ids) == len(set(ids)), 'missing/duplicate job identity')
    for job in jobs:
        require(type(job.get('run_id')) is int and job['run_id'] == run['id'] and job.get('head_sha') == run['head_sha'], 'job run/source identity mismatch')
        if 'run_attempt' in job:
            require(type(job['run_attempt']) is int and job['run_attempt'] == run['run_attempt'], 'job run attempt mismatch')
        if 'head_branch' in job:
            require(job['head_branch'] == run['head_branch'], 'job head branch mismatch')
    return jobs


def verify_workflow_run(run, jobs, expected_sha, path, names, steps):
    require(isinstance(run, dict) and run.get('repository', {}).get('full_name') == REPOSITORY
            and run.get('head_repository', {}).get('full_name') == REPOSITORY, 'candidate run owned by wrong repository')
    require(run.get('head_sha') == expected_sha and re.fullmatch(r'[0-9a-f]{40}', expected_sha or ''), 'candidate run source mismatch')
    require(run.get('event') == 'push' and run.get('head_branch') == 'main', 'candidate must be trusted main push')
    require(run.get('path') == path, 'wrong candidate workflow')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'candidate run is not successful and terminal')
    jobs = run_jobs(run, jobs)
    selected = [job for job in jobs if job.get('name') in names]
    require(len(selected) == len(names) and {job['name'] for job in selected} == names, 'missing or duplicate required CI jobs')
    for job in selected:
        require(job.get('status') == 'completed' and job.get('conclusion') == 'success', 'required CI job did not pass')
        actual_steps = job.get('steps', [])
        require(isinstance(actual_steps, list), 'missing required CI steps')
        for required in steps:
            matching = [step for step in actual_steps if step.get('name') == required]
            require(len(matching) == 1 and matching[0].get('status') == 'completed'
                    and matching[0].get('conclusion') == 'success', f'missing/failed/skipped mandatory CI step: {required}')


def verify_run(run, jobs, expected_sha):
    verify_workflow_run(run, jobs, expected_sha, '.github/workflows/macos-candidate.yml',
                        {'candidate (macos-15, arm64)', 'candidate (macos-15-intel, x64)'}, REQUIRED_STEPS)


def verify_integration_run(run, jobs, expected_sha):
    verify_workflow_run(run, jobs, expected_sha, '.github/workflows/integration.yml', {'integration'}, INTEGRATION_STEPS)
    require(len(jobs['jobs'] if isinstance(jobs, dict) else jobs) == 1, 'unexpected integration job inventory')


def stage_candidate(root, output, arch):
    root, output = Path(root).resolve(), Path(output).resolve()
    require(arch in ARCHES, 'invalid architecture')
    current = source_snapshot(root)
    require(current['clean'], 'release candidates require a clean committed source tree')
    version = json.loads((root / 'package.json').read_text())['version']
    run_id, attempt = int(os.environ.get('GITHUB_RUN_ID', '0')), int(os.environ.get('GITHUB_RUN_ATTEMPT', '0'))
    require(run_id > 0 and attempt > 0 and os.environ.get('GITHUB_SHA') == current['source_sha'], 'candidate must run under exact CI source identity')
    preflight = json.loads((root / GATES['preflight']).read_text())
    require(preflight.get('source_fingerprint') == current['fingerprint'], 'source changed after preflight')
    require(not output.exists(), 'refuse to overwrite a staged candidate')
    output.mkdir(parents=True)
    paths = set()
    for template in GATES.values():
        report = root / template.format(arch=arch)
        require(report.is_file(), f'missing gate report: {report}')
        if report.parent == root / 'evidence':
            paths.add(template.format(arch=arch))
        else:
            paths.update(str(file.relative_to(root).as_posix()) for file in report.parent.rglob('*') if file.is_file())
    preflight_report = read_json(root / GATES['preflight'])
    for stage in preflight_report.get('stages', []):
        name = stage['path']
        require('/' not in name and '\\' not in name, 'invalid preflight log path')
        if stage.get('counts'):
            counts_name = stage['counts']['path']
            require('/' not in counts_name and '\\' not in counts_name, 'invalid preflight counts path')
            paths.add('evidence/' + counts_name)
        paths.add('evidence/' + name)
    worker = read_json(root / GATES['worker'].format(arch=arch))
    for record in [worker.get('runtime_evidence'), worker.get('log'), *worker.get('retained_payloads', [])]:
        if record:
            name = record['path']
            require('/' not in name and '\\' not in name, 'invalid worker linked evidence path')
            paths.add('evidence/' + name)
    paths.update(('evidence/sbom.json', 'evidence/source-before.json', 'evidence/runner-context.txt',
                  'requirements-build.txt', 'package.json', 'package-lock.json', 'packaging/requirements-macos.lock',
                  'packaging/wheel-manifest.json'))
    asset_names = [f'dist/mac-app/CassQuery-{version}-mac-{arch}.{extension}' for extension in ('zip', 'dmg')]
    paths.update(asset_names)
    for name in sorted(paths):
        file = relative_file(root, name)
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
    records = [file_record(output, name) for name in sorted(paths)]
    by_path = {record['path']: record for record in records}
    manifest = {'schema': 1, 'repository': REPOSITORY, 'source_sha': current['source_sha'],
                'source_fingerprint': current['fingerprint'], 'version': version, 'architecture': arch,
                'run_id': run_id, 'run_attempt': attempt, 'distribution': 'adhoc',
                'created_at': time.time(), 'files': records, 'assets': [by_path[name] for name in asset_names],
                'gates': {name: by_path[template.format(arch=arch)] for name, template in GATES.items()}}
    (output / 'candidate.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify_candidate(output, manifest, expected_sha=current['source_sha'], expected_run_id=run_id,
                     expected_attempt=attempt, expected_arch=arch, expected_version=version)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    snap = sub.add_parser('snapshot')
    snap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    snap.add_argument('--output', type=Path, required=True)
    stage = sub.add_parser('stage')
    stage.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    stage.add_argument('--output', type=Path, required=True)
    stage.add_argument('--arch', choices=ARCHES, required=True)
    args = parser.parse_args()
    if args.action == 'snapshot':
        result = source_snapshot(args.root)
        require(result['clean'], 'candidate source snapshot must be clean')
        require(os.environ.get('GITHUB_SHA', result['source_sha']) == result['source_sha'], 'checkout differs from CI SHA')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    else:
        result = stage_candidate(args.root, args.output, args.arch)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
