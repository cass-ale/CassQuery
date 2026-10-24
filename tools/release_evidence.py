#!/usr/bin/env python3
"""Source identity and portable candidate evidence; never publishes anything."""
import argparse
import hashlib
import json
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


def validate_native_matrix(matrix):
    expected = {(theme, width, state) for theme in ('dark', 'light')
                for width in (450, 700, 1000, 1320) for state in ('assembled', 'apart')}
    require(isinstance(matrix, list) and len(matrix) == len(expected), 'incomplete native render matrix')
    keys = {(row.get('theme'), row.get('width'), row.get('state')) for row in matrix}
    require(keys == expected, 'missing or duplicate native render states')
    for row in matrix:
        require(row.get('status') == 'checked', 'native state skipped or unexecuted')
        require(row.get('vertices', 0) > 0 and row.get('meshes', 0) > 0, 'native state rendered no geometry')
        require(row.get('clipped') == 0 and row.get('nonfinite') == 0 and row.get('overflow') is False, 'native geometry or layout failure')
        require(all(row.get(field) == [] for field in ('bad', 'overlaps', 'errors')), 'native render diagnostics')
        require(row.get('realized_theme') == row['theme'] and row.get('controls_checked', 0) > 0, 'missing realized native coverage')
        require(row.get('gates_pass') is True and row.get('tier') == 'certified', 'native render did not show certified geometry')
        require(type(row.get('width')) is int and row.get('viewport') == [row['width'], 700], 'native viewport does not match requested state')
        require(row.get('diag') == '' and row.get('step_pressed') == (['true', 'false'] if row['state'] == 'assembled' else ['false', 'true'])
                and row.get('explode') == ('0%' if row['state'] == 'assembled' else '100%'), 'native controls do not show requested state')
        canvas = row.get('canvas', {})
        require(all(isinstance(canvas.get(field), (int, float)) and canvas[field] > 0 for field in ('width', 'height', 'client_width', 'client_height')), 'missing realized native canvas')


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


def verify_candidate(root, manifest=None, *, expected_sha, expected_run_id, expected_arch,
                     expected_attempt=None, expected_version=None):
    root = Path(root)
    manifest = manifest or read_json(relative_file(root, 'candidate.json'))
    require(manifest.get('schema') == 1 and manifest.get('repository') == REPOSITORY, 'untrusted candidate schema/repository')
    require(re.fullmatch(r'[0-9a-f]{40}', expected_sha or ''), 'full expected source SHA required')
    require(manifest.get('source_sha') == expected_sha, 'candidate source mismatch')
    require(manifest.get('run_id') == expected_run_id and type(expected_run_id) is int and expected_run_id > 0, 'candidate run mismatch')
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
    require(before.get('schema') == 1 and before.get('clean') is True and before.get('source_sha') == expected_sha,
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
    for name, template in GATES.items():
        required_path = template.format(arch=expected_arch)
        record = gates[name]
        require(record == records.get(required_path), f'gate evidence is not digest-bound: {name}')
        report = read_json(relative_file(root, required_path))
        require(report.get('schema') == 1 and report.get('pass') is True, f'gate did not pass: {name}')
        if name == 'worker':
            ids = {'units', 'watertight', 'volume', 'thin-extent', 'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes'}
            gates_data = report.get('gates', {})
            checks = gates_data.get('checks', [])
            require(gates_data.get('pass') is True and len(checks) == 8
                    and {check.get('id') for check in checks} == ids
                    and all(check.get('status') == 'pass' for check in checks), 'frozen worker CAD gates incomplete')
            runtime = report.get('worker_runtime', {})
            require(runtime.get('pid') == report.get('worker_pid') and runtime.get('challenge') == report.get('challenge')
                    and runtime.get('frozen') is True and report.get('worker_shutdown') is True, 'frozen worker/runtime shutdown not proven')
            runtime_path = verify_record(root, required_path, report.get('runtime_evidence'))
            manifest_record(root, records, runtime_path)
            runtime_data = read_json(relative_file(root, runtime_path))
            require(runtime_data.get('pid') == runtime.get('pid') and runtime_data.get('challenge') == runtime.get('challenge')
                    and runtime_data.get('frozen') is True, 'worker runtime file contradicts gate evidence')
        if name == 'renderer':
            validate_native_matrix(report.get('matrix'))
            require(report.get('lifecycle', {}).get('pass') is True, 'production native lifecycle not verified')
            require(len(report.get('images', [])) >= 18, 'post-build/failure frames missing')
        if name in ('zip', 'dmg'):
            asset = report.get('asset', {})
            expected_path = f'dist/mac-app/CassQuery-{version}-mac-{expected_arch}.{name}'
            require(asset.get('sha256') == records[expected_path]['sha256'] and asset.get('bytes') == records[expected_path]['bytes'], 'package smoke does not bind final artifact bytes')
            launches = report.get('launches', {})
            expected_launches = {'mounted', 'copied'} if name == 'dmg' else {'extracted', 'copied'}
            require(set(launches) == expected_launches, 'final package launch coverage incomplete')
            for launch in launches.values():
                validate_native_matrix(launch.get('matrix'))
                native_path = verify_record(root, required_path, launch.get('native_report'))
                manifest_record(root, records, native_path)
                native = read_json(relative_file(root, native_path))
                require(native.get('schema') == 1 and native.get('pass') is True and native.get('source_sha') == expected_sha,
                        'package child native launch did not pass')
                validate_native_matrix(native.get('matrix'))
                require(matrix_identity(root, native_path, native.get('matrix')) == matrix_identity(root, required_path, launch.get('matrix')), 'package matrix contradicts native child evidence')
            require(report.get('temp_root_removed') is True, 'package cleanup not proven')
            if name == 'dmg':
                require(report.get('detach_completed') is True, 'DMG still mounted or detach skipped')
        images = report.get('images', [])
        require(len({image.get('path') for image in images}) == len(images), 'duplicate native image inventory')
        for image in images:
            path = verify_record(root, required_path, image)
            manifest_record(root, records, path)
            import importlib.util
            spec = importlib.util.spec_from_file_location('png_evidence', Path(__file__).with_name('png_evidence.py'))
            png = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(png)
            image_file = relative_file(root, path)
            require(image_file.stat().st_size <= 32 * 1024 * 1024, 'PNG evidence file exceeds bound')
            dimensions = png.dimensions(image_file.read_bytes())
            require(list(dimensions) == [image.get('width'), image.get('height')] and min(dimensions) > 0, 'image content/dimensions do not match evidence')
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
            require(isinstance(report.get('started_at'), (int, float)) and isinstance(report.get('completed_at'), (int, float))
                    and report['completed_at'] >= report['started_at'], 'invalid preflight checkpoint times')
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


def verify_run(run, jobs, expected_sha):
    require(run.get('repository', {}).get('full_name') == REPOSITORY, 'candidate run owned by wrong repository')
    require(run.get('head_repository', {}).get('full_name') == REPOSITORY, 'candidate from wrong repository')
    require(run.get('head_sha') == expected_sha and re.fullmatch(r'[0-9a-f]{40}', expected_sha or ''), 'candidate run source mismatch')
    require(run.get('event') == 'push' and run.get('head_branch') == 'main', 'candidate must be trusted main push')
    require(run.get('path') == '.github/workflows/macos-candidate.yml', 'wrong candidate workflow')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'candidate run is not successful and terminal')
    expected_names = {'candidate (macos-15, arm64)', 'candidate (macos-15-intel, x64)'}
    selected = [job for job in jobs if job.get('name') in expected_names]
    require(len(selected) == 2 and {job['name'] for job in selected} == expected_names, 'missing or duplicate native CI jobs')
    for job in selected:
        require(job.get('status') == 'completed' and job.get('conclusion') == 'success', 'native job did not pass')
        steps = job.get('steps', [])
        for required in REQUIRED_STEPS:
            matching = [step for step in steps if step.get('name') == required]
            require(len(matching) == 1 and matching[0].get('status') == 'completed'
                    and matching[0].get('conclusion') == 'success', f'missing/failed/skipped mandatory CI step: {required}')


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
