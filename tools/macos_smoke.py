#!/usr/bin/env python3
"""Fail-closed native app/resource/render assurance with immutable bundle evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time
import uuid

from release_evidence import (NATIVE_CHECKS as CHECKS, NATIVE_SOURCE_PATHS, PYTHON_SOURCE_PATHS,
                              source_manifest, validate_native_matrix, validate_native_renderer,
                              validate_python_modules)

ROOT = Path(__file__).resolve().parents[1]
SHELL_FILES = NATIVE_SOURCE_PATHS
MACHO_MAGIC = {bytes.fromhex(s) for s in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
                                        'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def architecture(value):
    aliases = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64', 'x64': 'x64'}
    require(value in aliases, f'Unsupported architecture: {value}')
    return aliases[value]


def sha256(file):
    digest = hashlib.sha256()
    with Path(file).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_report(file, report):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    base = file.parent.resolve()

    def portable(value, key=None):
        if isinstance(value, dict):
            return {name: portable(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [portable(item, key) for item in value]
        if isinstance(value, str) and key in ('path', 'screenshots'):
            candidate = Path(value)
            if candidate.is_absolute():
                candidate = candidate.resolve()
                if candidate.is_relative_to(base):
                    return candidate.relative_to(base).as_posix()
        return value

    temporary = file.with_name(f'.{file.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(portable(report), indent=2, sort_keys=True) + '\n')
    os.replace(temporary, file)


def file_evidence(file):
    file = Path(file)
    return {'path': str(file), 'sha256': sha256(file), 'bytes': file.stat().st_size}


def checkpoint(name):
    return {'name': name, 'wall_time_ms': time.time() * 1000, 'monotonic_ms': time.monotonic() * 1000}


def inventory(app):
    """Only the explicitly supplied app, no symlink traversal; sized before hashing.

    Current app: 2699 records (including root)/1.37GB. Bounds reject accidental broad filesystem
    roots and unexpectedly expanded packages, rather than scanning them forever.
    """
    app = Path(app)
    require(not app.is_symlink(), 'Inventory requires a nonsymlink app root')
    app = app.resolve()
    require(app.suffix == '.app' and (app / 'Contents/Info.plist').is_file(), 'Inventory requires an explicit app bundle')
    entries, total = [(app, app.lstat())], 0
    for directory, dirs, files in os.walk(app, followlinks=False):
        for name in sorted(dirs + files):
            file = Path(directory) / name
            metadata = file.lstat()
            total += metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
            entries.append((file, metadata))
            require(len(entries) <= 10000 and total <= 4 * 1024**3, 'App inventory exceeds reviewed 10000-entry/4GiB scope')
    # Check every link/type before hashing any bundle resource. A later unsafe
    # entry must not authorize earlier reads through an out-of-bundle link.
    for file, metadata in entries:
        if stat.S_ISLNK(metadata.st_mode):
            require(file.resolve().is_relative_to(app), f'Bundle symlink escapes app: {file}')
            require(file.exists(), f'Dangling bundle symlink: {file}')
        else:
            require(stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode), f'Unsupported bundle entry: {file}')
    records = []
    for file, metadata in entries:
        record = {'path': file.relative_to(app).as_posix(), 'mode': stat.S_IMODE(metadata.st_mode)}
        if stat.S_ISLNK(metadata.st_mode):
            record.update(kind='symlink', target=os.readlink(file))
        elif stat.S_ISDIR(metadata.st_mode):
            record['kind'] = 'directory'
        else:
            record.update(kind='file', bytes=metadata.st_size, sha256=sha256(file))
        records.append(record)
    records.sort(key=lambda record: record['path'])
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'sha256': digest, 'entries': len(records), 'bytes': total, 'records': records}


def checked_bundle_inventory(app, recorded_file, output):
    """Bind the prepackaged app to its prior native inventory, retaining exact bytes."""
    recorded_file = Path(recorded_file)
    require(recorded_file.is_file() and not recorded_file.is_symlink()
            and 0 < recorded_file.stat().st_size <= 8 * 1024 * 1024, 'Missing/invalid checked-app inventory')
    with recorded_file.open('rb') as stream:
        data = stream.read(8 * 1024 * 1024 + 1)
    require(len(data) <= 8 * 1024 * 1024, 'Checked-app inventory exceeds bound')
    baseline = json.loads(data)
    require(inventory(app) == baseline, 'Checked app changed since its native inventory')
    retained = Path(output) / 'checked-inventory.json'
    retained.write_bytes(data)
    return baseline, file_evidence(retained)


def asar_contents(file, names):
    """Read only requested packed files; reject malformed/unpacked/link entries."""
    result = {}
    with Path(file).open('rb') as stream:
        fixed = stream.read(16)
        require(len(fixed) == 16, 'Truncated ASAR')
        size, header_size, pickle_size, json_size = struct.unpack('<4I', fixed)
        require(size == 4 and 0 < json_size <= 1024 * 1024 and header_size == pickle_size + 4
                and json_size <= pickle_size - 4, 'Malformed ASAR header')
        header = json.loads(stream.read(json_size))
        for name in names:
            node = header
            for part in name.split('/'):
                node = node.get('files', {}).get(part, {})
            require('size' in node and 'offset' in node and not node.get('unpacked') and not node.get('link'), f'Missing packed source: {name}')
            length, offset = node['size'], int(node['offset'])
            require(type(length) is int and 0 <= length <= 1024 * 1024 and offset >= 0, f'Invalid packed source: {name}')
            stream.seek(8 + header_size + offset)
            data = stream.read(length)
            require(len(data) == length, f'Truncated packed source: {name}')
            result[name] = data
    return result


def source_files(folder):
    # Only these small, explicit public source directories; no build/output tree.
    return sorted(file for file in folder.rglob('*') if file.is_file() and '__pycache__' not in file.parts)


def verify_bundle(app, arch, version):
    arch = architecture(arch)
    tree = inventory(app)
    app = Path(app).resolve()
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    require(info.get('CFBundleIdentifier') == 'com.cassquery.desktop', 'Wrong bundle identity')
    require(info.get('CFBundleExecutable') == 'CassQuery', 'Wrong bundle executable')
    require(info.get('CFBundleShortVersionString') == version and info.get('CFBundleVersion') == version, 'Wrong bundle version')
    resources = app / 'Contents/Resources'
    icon_name = info.get('CFBundleIconFile', '')
    require(icon_name and Path(icon_name).name == icon_name, 'Invalid bundle icon path')
    icon = resources / icon_name
    require(icon.is_file() and sha256(icon) == sha256(ROOT / 'assets/icons/cassquery.icns'), 'Bundle/source icon bytes differ')
    source = []
    for folder in ('viewer', 'testpart'):
        for file in source_files(ROOT / folder):
            relative = file.relative_to(ROOT)
            for copy in (resources / 'source' / relative, resources / 'worker/_internal' / relative):
                require(copy.is_file() and sha256(copy) == sha256(file), f'Packaged source/resource mismatch: {copy}')
            source.append(file_evidence(file))
    for name in PYTHON_SOURCE_PATHS:
        original = ROOT / name
        for copy in (resources / 'source' / name, resources / 'worker/_internal/source' / name):
            require(copy.is_file() and sha256(copy) == sha256(original), f'Packaged Python source mismatch: {name}')
        source.append(file_evidence(original))
    packed_names = ['package.json', *SHELL_FILES]
    packed = asar_contents(resources / 'app.asar', packed_names)
    package = json.loads((ROOT / 'package.json').read_text())
    packed_package = json.loads(packed['package.json'])
    require(packed_package['version'] == version and packed_package.get('main') == package['main'], 'Packed package version/entry differs')
    require(source_manifest(packed_package) == source_manifest(package), 'Packed executable source manifest differs')
    for name in packed_names[1:]:
        require(packed[name] == (ROOT / name).read_bytes(), f'Packed shell source mismatch: {name}')
        source.append(file_evidence(ROOT / name))
    machos = []
    for entry in tree['records']:
        if entry['kind'] != 'file':
            continue
        file = app / entry['path']
        with file.open('rb') as stream:
            magic = stream.read(4)
        if magic not in MACHO_MAGIC:
            continue
        archs = subprocess.check_output(['lipo', '-archs', str(file)], text=True, timeout=30).split()
        require(arch in [architecture(a) for a in archs], f'Wrong embedded Mach-O architecture: {file} {archs}')
        machos.append({'path': entry['path'], 'architectures': [architecture(a) for a in archs], 'sha256': entry['sha256']})
    expected = {'Contents/MacOS/CassQuery', 'Contents/Resources/worker/cassquery-worker'}
    require(expected.issubset({m['path'] for m in machos}), 'Missing native app/worker Mach-O')
    for relative in expected:
        require(os.access(app / relative, os.X_OK), f'Nonexecutable native binary: {relative}')
    libraries = app / 'Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries'
    driver, loader = libraries / 'libvk_swiftshader.dylib', libraries / 'libvulkan.dylib'
    require(loader.is_symlink() and os.readlink(loader) == driver.name and driver.is_file()
            and not driver.is_symlink() and loader.resolve() == driver.resolve(), 'Missing/invalid contained Vulkan discovery alias')
    # Signing changes the reviewed archive driver bytes. Match the currently
    # sealed driver, not its pre-sign hash used by the afterPack hook.
    driver_record = next((entry for entry in tree['records'] if entry['path'] == driver.relative_to(app).as_posix()), {})
    require(sha256(loader) == driver_record.get('sha256'), 'Vulkan loader alias differs from inventoried driver')
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True, timeout=120)
    return {'bundle_id': info['CFBundleIdentifier'], 'architecture': arch, 'version': version,
            'icon': file_evidence(icon), 'source_files': source, 'machos': machos,
            'bundle_sha256': tree['sha256']}, tree


def image_evidence(file):
    from PIL import Image
    file = Path(file)
    require(file.is_file() and not file.is_symlink() and file.stat().st_size > 1000, f'Missing rendered image: {file}')
    with Image.open(file) as picture:
        require(picture.format == 'PNG' and min(picture.size) >= 100, f'Invalid captured PNG: {file}')
        dimensions = picture.size
        picture.verify()
    return {**file_evidence(file), 'width': dimensions[0], 'height': dimensions[1]}


def validate_matrix(records):
    try:
        validate_native_matrix(records)
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def validate_renderer(report, output, challenge, pid, app, version, arch, started, finished, *, bundled):
    try:
        validate_native_renderer(report, expected_sha=os.environ.get('GITHUB_SHA') or None,
                                 expected_arch=arch, expected_version=version, bundled=bundled,
                                 started=started, finished=finished)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    require(report.get('challenge') == challenge and report['app_pid'] == pid, 'Stale/unrelated renderer evidence')
    source = {'files': [{'bytes': (ROOT / name).stat().st_size, 'path': name, 'sha256': sha256(ROOT / name)} for name in SHELL_FILES]}
    source['fingerprint'] = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(report.get('native_source') == source, 'Raw native shell source differs from checked source')
    requested = os.environ.get('CASSQUERY_ANGLE', '')
    expected_backend = f'{requested}-requested' if requested else 'desktop-default'
    require(report.get('render_backend_requested') == expected_backend, 'Renderer backend request differs from actual launch')
    if requested == 'swiftshader':
        observed = report.get('graphics', {}).get('webgl', {}).get('unmasked_renderer')
        require(isinstance(observed, str) and 'swiftshader' in observed.casefold(), 'Requested SwiftShader was not observed in actual WebGL telemetry')
    resources = (Path(app) / 'Contents/Resources').resolve()
    require(Path(report['resources_path']).resolve() == resources
            and Path(report['worker_executable']).resolve() == resources / 'worker/cassquery-worker', 'Wrong worker/resources executable')
    if bundled:
        require(Path(report['worker_status']['projects'][0]['root']).resolve() == resources / 'worker/_internal/testpart', 'Default launch did not use frozen bundled demo')
    expected = [output / 'assembled.png', output / 'apart.png']
    require(report.get('screenshots') == [str(file) for file in expected], 'Wrong legacy screenshot paths')
    images = report['images']
    seen = set()
    for image in images:
        file = Path(image.get('path', ''))
        require(file.is_absolute() and file.parent == output and file not in seen, 'Unsafe/duplicate image path')
        seen.add(file)
        actual = image_evidence(file)
        require(all(image.get(key) == actual[key] for key in ('sha256', 'width', 'height')), 'Image hash/dimensions mismatch')
    for row in report['matrix']:
        image = row.get('image', {})
        file = Path(image.get('path', ''))
        require(file == output / f"{row['theme']}-{row['width']}-{row['state']}.png" and image in images, 'Missing matrix image')
        require(image['width'] >= row['width'] and image['height'] >= 700, 'Matrix image dimensions do not cover frame')
        if row.get('full_size'):
            require(image['width'] >= row['full_size'][0] and image['height'] >= row['full_size'][1], 'Matrix capture misses complete document')
        for index, capture in enumerate(row['scroll_captures']):
            image = capture['image']
            suffix = f'-controls-{index}' if index else ''
            require(image in images and image.get('path') == str(output / f"{row['theme']}-{row['width']}-{row['state']}{suffix}.png"), 'Missing actual scroll capture')
            require(image['width'] >= row['full_size'][0] and image['height'] >= row['full_size'][1], 'Scroll capture misses complete document')
    return report


def instrument_project(project, proof):
    # Trusted public fixture executes this in the actual worker process. Unlike
    # an expected-frozen CLI flag, sys.frozen/_MEIPASS here are runtime evidence.
    entry = project / 'part_entry.py'
    code = "\nimport sys as _cq_sys, os as _cq_os, json as _cq_json, time as _cq_time, importlib as _cq_importlib\n"
    modules = [Path(name).stem for name in PYTHON_SOURCE_PATHS if name != 'worker.py']
    code += f"_cq_modules = {{name: _cq_importlib.import_module(name) for name in {modules!r}}}\n"
    code += f"with open({str(proof)!r}, 'w') as _cq_proof:\n"
    code += "    _cq_json.dump({'schema':1, 'pid':_cq_os.getpid(), 'challenge':_cq_os.environ.get('CASSQUERY_SMOKE_CHALLENGE'), 'frozen':getattr(_cq_sys,'frozen',False), 'executable':_cq_sys.executable, 'resource_root':getattr(_cq_sys,'_MEIPASS',None), 'modules':{name:{'path':module.__file__, 'loader':type(module.__loader__).__name__} for name,module in _cq_modules.items()}, 'wall_time_ms':_cq_time.time()*1000, 'monotonic_ms':_cq_time.monotonic()*1000}, _cq_proof)\n"
    entry.write_text(entry.read_text() + code)


def validate_worker_proof(proof, pid, challenge, started, finished, *, frozen, executable):
    require(proof.get('schema') == 1 and proof.get('pid') == pid and proof.get('challenge') == challenge, 'Stale worker runtime checkpoint')
    require(proof.get('frozen') is frozen and Path(proof.get('executable', '')).resolve() == Path(executable).resolve(), 'Worker runtime frozen/executable mismatch')
    require(type(proof.get('monotonic_ms')) in (int, float) and started['monotonic_ms'] <= proof['monotonic_ms'] <= finished['monotonic_ms']
            and type(proof.get('wall_time_ms')) in (int, float) and started['wall_time_ms'] <= proof['wall_time_ms'] <= finished['wall_time_ms'], 'Stale worker runtime time')
    if frozen:
        require(Path(proof.get('resource_root', '')).resolve() == (Path(executable).parent / '_internal').resolve(), 'Wrong frozen resource root')
        try:
            validate_python_modules(proof)
        except ValueError as error:
            raise RuntimeError(str(error)) from error


def wait_dead(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise RuntimeError(f'App quit left worker {pid} running')


def cleanup_launch(process):
    """Terminate only this launch's own process group on every result path."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGTERM:
            time.sleep(0.1)
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def launch(app, output, env, version, arch, *, bundled):
    require(not output.is_relative_to(app), 'Evidence must not be written inside bundle')
    output.mkdir(parents=True, exist_ok=True)
    for file in output.glob('*.png'):
        file.unlink()
    report_file = output / 'renderer-raw.json'
    report_file.unlink(missing_ok=True)
    challenge = uuid.uuid4().hex
    env = {**env, 'CASSQUERY_SMOKE_DIR': str(output), 'CASSQUERY_SMOKE_CHALLENGE': challenge}
    started = checkpoint('launch-start')
    with (output / 'app.log').open('w') as log:
        process = subprocess.Popen([str(app / 'Contents/MacOS/CassQuery')], env=env, cwd=env['HOME'],
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            try:
                code = process.wait(timeout=360)  # Includes up to90s cold frozen-worker startup and the native renderer checks.
            except subprocess.TimeoutExpired:
                raise RuntimeError('Packaged app did not finish renderer smoke') from None
            finished = checkpoint('launch-exit')
            require(report_file.is_file(), f'App exited {code} without renderer evidence. Native log:\n{(output / "app.log").read_text(errors="replace")[-4000:]}')
            report = json.loads(report_file.read_text())
            require(code == 0, f'Packaged renderer exited {code}: {report.get("error")}')
            validate_renderer(report, output, challenge, process.pid, app, version, arch, started, finished, bundled=bundled)
            wait_dead(report['worker_pid'])
            report['launch'] = {'started': started, 'finished': finished, 'exit': code, 'path': env['PATH'], 'project_override': env.get('CASSQUERY_PROJECTS')}
            return report
        finally:
            cleanup_launch(process)


def renderer_environment(base):
    result = dict(base)
    result.pop('CASSQUERY_SOFTWARE_GL', None)
    result.pop('CASSQUERY_ANGLE', None)
    requested = os.environ.get('CASSQUERY_ANGLE', '')
    require(not os.environ.get('CASSQUERY_SOFTWARE_GL'), 'Native macOS smoke must not use the legacy software-GL override')
    require(requested in ('', 'metal', 'swiftshader'), 'Unsupported native macOS ANGLE backend')
    if requested:
        result['CASSQUERY_ANGLE'] = requested
    return result, (f'{requested}-requested' if requested else 'desktop-default')


def run_smoke(app, output, arch, signed=False):
    supplied_app = Path(app)
    app, output, arch = supplied_app.resolve(), Path(output).resolve(), architecture(arch)
    version = json.loads((ROOT / 'package.json').read_text())['version']
    report = {'schema': 1, 'pass': False, 'source_sha': os.environ.get('GITHUB_SHA') or None,
              'version': version, 'architecture': arch, 'checked': [], 'not_checked': [],
              'started': checkpoint('native-runner-start'),
              'checkpoints': [checkpoint('native-start')], 'images': []}
    require(not output.is_relative_to(app), 'Evidence must be outside app')
    output.mkdir(parents=True, exist_ok=True)
    write_report(output / 'renderer.json', report)
    try:
        require(not supplied_app.is_symlink(), 'Native smoke requires a nonsymlink app root')
        identity, before = verify_bundle(app, arch, version)
        write_report(output / 'bundle-before.json', before)
        report['bundle'] = identity
        report['checked'] += ['source/bundle icon and version bytes', 'packed shell and frozen demo/viewer source parity', 'all embedded Mach-O architectures', 'ad-hoc bundle seal']
        require(architecture(platform.machine()) == arch, 'Native smoke must run on the target architecture')
        if signed:
            for command in (['spctl', '--assess', '--type', 'execute', '--verbose=2', str(app)],
                            ['xcrun', 'stapler', 'validate', str(app)]):
                subprocess.run(command, check=True, timeout=120)
            report['checked'].append('Gatekeeper and stapled ticket')
        else:
            report['not_checked'].append('Developer ID, hardened runtime, Gatekeeper acceptance and notarization (ad-hoc candidate)')
        with tempfile.TemporaryDirectory(prefix='cassquery-mac-smoke-') as scratch:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith('CASSQUERY_') and key not in ('PYTHONHOME', 'PYTHONPATH', 'ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS', 'NODE_PATH')}
            env.update({'CASSQUERY_USER_DATA': str(Path(scratch) / 'user'),
                        'CASSQUERY_DATA_DIR': str(Path(scratch) / 'worker'),
                        'HOME': scratch, 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
            env, report['render_backend_requested'] = renderer_environment(env)
            report['not_checked'].append('hardware/display coverage beyond this explicit native backend and host')
            bundled = launch(app, output / 'bundled', env, version, arch, bundled=True)
            write_report(output / 'bundled/renderer.json', bundled)
            report['bundled_report'] = file_evidence(output / 'bundled/renderer.json')
            report['checked'].append('cold default bundled demo launch without project/interpreter PATH override')
            report['checkpoints'].append(checkpoint('bundled-launch-complete'))
            project, proof = Path(scratch) / 'demo', output / 'worker-runtime.json'
            shutil.copytree(ROOT / 'testpart', project, ignore=shutil.ignore_patterns('__pycache__'))
            proof.unlink(missing_ok=True)
            instrument_project(project, proof)
            env['CASSQUERY_PROJECTS'] = str(project)
            # Same userData and worker cache: second actual process is the warm launch.
            custom = launch(app, output, env, version, arch, bundled=False)
            runtime = json.loads(proof.read_text())
            validate_worker_proof(runtime, custom['worker_pid'], custom['challenge'], custom['launch']['started'], custom['launch']['finished'], frozen=True, executable=app / 'Contents/Resources/worker/cassquery-worker')
            report.update({key: value for key, value in custom.items() if key not in ('pass', 'checkpoints', 'images', 'started', 'finished')})
            report['renderer_started'] = custom['started']
            report['renderer_finished'] = custom['finished']
            report['renderer_checkpoints'] = custom['checkpoints']
            report['worker_runtime'] = runtime
            report['worker_runtime_evidence'] = file_evidence(proof)
            report['images'] = bundled['images'] + custom['images']
            report['checked'] += ['warm native launch with reused isolated state', 'actual worker PID/challenge/sys.frozen runtime checkpoint',
                                  'compiled workbench/CADIO PYZ module loader and resource-root imports',
                                  'real secondary process single-instance exclusion', 'native window close/production activate reopen with same warm worker',
                                  'production input controls', 'dark/light 450/700/1000/1320 rendered geometry/layout matrix', 'save/gate retention', 'owned worker shutdown']
        after = inventory(app)
        write_report(output / 'bundle-after.json', after)
        require(after == before, 'Smoke mutated immutable app bundle')
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True, timeout=120)
        report['bundle_before'] = file_evidence(output / 'bundle-before.json')
        report['bundle_after'] = file_evidence(output / 'bundle-after.json')
        report['checked'].append('byte/mode/symlink immutable bundle and seal after smoke')
        report['not_checked'] += ['human visual approval of every frame', 'clean/quarantined Finder/browser-download launch', 'minimum macOS version', 'display scale matrix beyond actual host DPR']
        report['visual_review'] = 'required'
        report['signed_checks'] = signed
        report['native_checks'] = ['bundled Python (no PATH interpreter)', 'isolated writable state', 'native app launch', 'worker shutdown']
        report['checkpoints'].append(checkpoint('native-complete'))
        report['pass'] = True
    except Exception as error:
        report['pass'] = False
        report['error'] = f'{type(error).__name__}: {error}'
        report['not_checked'].append('remaining native assurance stages; see error')
        report['checkpoints'].append(checkpoint('native-failed'))
        raise
    finally:
        report['finished'] = checkpoint('native-runner-exit')
        write_report(output / 'renderer.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--arch', choices=('arm64', 'x64', 'x86_64'))
    parser.add_argument('--signed', action='store_true')
    args = parser.parse_args()
    report = run_smoke(args.app, args.output, args.arch or platform.machine(), args.signed)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
