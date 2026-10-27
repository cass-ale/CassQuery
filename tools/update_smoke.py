#!/usr/bin/env python3
"""Real, opt-in macOS CI install/rollback proof on owned TEMP app copies only.

This qualifies the custom install helper mechanism against a separately resealed
fixture version, not a future GitHub release, Squirrel, dialogs, or UI consent.
There is no simulated-success mode. Missing native readiness/ACK fails closed.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import selectors
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
GATE_IDS = {'units', 'watertight', 'volume', 'thin-extent', 'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes'}
KIND = 'native-update-fixture'
PROTECTED_KEYS = {'userdata/workbench-settings.json', 'userdata/workbench-recovery.json', 'userdata/sentinel',
                  'project/part_entry.py', 'project/viewer.json', 'recovery/part_entry.py', 'recovery/viewer.json'}


def require(value, message):
    if not value:
        raise ValueError(message)


def sha256(file):
    digest = hashlib.sha256()
    with Path(file).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_json(file):
    file = Path(file)
    require(file.is_file() and not file.is_symlink() and file.stat().st_size <= 16 * 1024**2, 'missing/unsafe/oversized JSON evidence')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate evidence key')
            value[key] = item
        return value
    return json.loads(file.read_text(), object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite evidence')))


def atomic_json(file, value):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, file)


def record(file, directory):
    file, directory = Path(file).resolve(), Path(directory).resolve()
    require(file.is_relative_to(directory) and file.is_file(), 'evidence outside output directory')
    return {'path': file.relative_to(directory).as_posix(), 'bytes': file.stat().st_size, 'sha256': sha256(file)}


def linked(directory, value):
    require(isinstance(value, dict) and isinstance(value.get('path'), str), 'missing linked evidence')
    name = PurePosixPath(value['path'])
    require(not name.is_absolute() and all(p not in ('', '.', '..') for p in value['path'].split('/')), 'unsafe linked evidence path')
    file = Path(directory).resolve().joinpath(*name.parts)
    require(not file.is_symlink() and file.is_file() and file.resolve().is_relative_to(Path(directory).resolve()), 'unsafe linked evidence file')
    require(type(value.get('bytes')) is int and file.stat().st_size == value['bytes'] and sha256(file) == value.get('sha256'), 'evidence bytes changed')
    return file


def checkpoint(name):
    return {'name': name, 'wall_time_ms': time.time() * 1000, 'monotonic_ms': time.monotonic() * 1000}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_ready(value, *, source_sha, version, arch, challenge, app, started, finished):
    """Pure semantic checks on the ACTUAL Agent2 ready-PID record."""
    require(isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1 and value.get('pass') is True, 'native app readiness failed')
    require(value.get('source_sha') == source_sha and value.get('version') == version and value.get('architecture') == arch
            and value.get('challenge') == challenge, 'native readiness source/version/arch/nonce mismatch')
    require(all(type(value.get(k)) is int and value[k] > 1 for k in ('app_pid', 'worker_pid'))
            and value['app_pid'] != value['worker_pid'], 'missing actual app/worker PIDs')
    require(value.get('packaged') is True and value.get('bundled_worker') is True and value.get('errors') == [], 'native packaged worker/renderer diagnostics')
    resources = str(PurePosixPath(app) / 'Contents/Resources')
    require(value.get('resources_path') == resources and value.get('worker_executable') == resources + '/worker/cassquery-worker', 'native readiness used unrelated app/worker')
    marks = [value.get('started', {}), *value.get('checkpoints', [])]
    require(len(marks) >= 2 and all(isinstance(m, dict) and all(finite(m.get(k)) for k in ('wall_time_ms', 'monotonic_ms')) for m in marks), 'missing native readiness clocks')
    require(all(started['monotonic_ms'] <= m['monotonic_ms'] <= finished['monotonic_ms']
                and started['wall_time_ms'] - 1000 <= m['wall_time_ms'] <= finished['wall_time_ms'] + 1000 for m in marks)
            and all(a['monotonic_ms'] < b['monotonic_ms'] for a, b in zip(marks, marks[1:])), 'stale native readiness clocks')
    status = value.get('worker_status', {})
    projects = status.get('projects', [])
    require(status.get('api_version') == 2 and status.get('load_errors') == [] and len(projects) == 1
            and projects[0].get('name') == 'TESTPLATE' and projects[0].get('state') == 'certified', 'owned public model did not certify')
    model = value.get('model', {})
    gates = value.get('gates', model.get('gates', {}))
    require(isinstance(model, dict) and isinstance(gates, dict), 'invalid certified model evidence')
    checks = gates.get('checks', [])
    require(isinstance(checks, list) and all(isinstance(c, dict) for c in checks), 'invalid certified gate checks')
    require(model.get('tier') == 'certified' and isinstance(model.get('hash'), str) and re.fullmatch(r'[0-9a-f]{16}', model['hash'])
            and projects[0].get('gates_pass') is True and projects[0].get('hashes', {}).get('certified') == model['hash']
            and gates.get('pass') is True and len(checks) == 8 and {c.get('id') for c in checks} == GATE_IDS
            and all(c.get('status') == 'pass' for c in checks), 'missing eight actual certified CAD gates')
    images = value.get('images')
    require(isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict)
            and isinstance(images[0].get('path'), str) and PurePosixPath(images[0]['path']).name == f"ready-{value['app_pid']}.png"
            and re.fullmatch(r'[0-9a-f]{64}', images[0].get('sha256', ''))
            and all(type(images[0].get(k)) is int and images[0][k] > 0 for k in ('width', 'height')), 'missing actual ready-PID frame')
    import release_evidence
    release_evidence.validate_native_source(value.get('native_source'))
    release_evidence.validate_graphics(value, arch)


def validate_report(report, directory, *, expected_sha=None, expected_arch=None, expected_version=None):
    require(report.get('schema') == 1 and type(report.get('schema')) is int and report.get('kind') == KIND
            and report.get('pass') is True and report.get('native_execution') is True and report.get('fixture_only') is True, 'native update fixture proof incomplete')
    for key, expected in (('source_sha', expected_sha), ('architecture', expected_arch), ('version', expected_version)):
        if expected is not None:
            require(report.get(key) == expected, 'update proof identity mismatch')
    require(re.fullmatch(r'[0-9a-f]{40}', report.get('source_sha', '')) and re.fullmatch(r'[0-9a-f]{64}', report.get('source_fingerprint', '')), 'missing update source identity')
    require(re.fullmatch(r'(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)', report.get('fixture_version', ''))
            and tuple(map(int, report['fixture_version'].split('.'))) > tuple(map(int, report['version'].split('.'))), 'fixture target is not a distinct newer version')
    require(report.get('input_app_unchanged') is True, 'supplied source app mutation not checked')
    source = load_json(linked(directory, report.get('source_before')))
    require(source.get('clean') is True and source.get('source_sha') == report['source_sha'] and isinstance(source.get('files'), list), 'unclean/unbound update source snapshot')
    identity = {'source_sha': source['source_sha'], 'files': source['files']}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(fingerprint == source.get('fingerprint') == report['source_fingerprint'] == report.get('after_fingerprint'), 'update source changed during proof')
    source_files = {item['path']: item for item in source['files']}
    require(report.get('cleanup_complete') is True, 'owned update processes/scratch not cleaned')
    cases = report.get('cases', [])
    require(isinstance(cases, list) and len(cases) == 2 and {c.get('name') for c in cases} == {'install', 'rollback'}, 'missing install/rollback execution')
    attempts = [case.get('token_fingerprint') for case in cases]
    require(all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in attempts)
            and len(set(attempts)) == len(cases), 'install attempts must use distinct fresh fingerprints')
    for case in cases:
        require(case.get('status') == 'checked' and case.get('helper_default_operations') is True, 'mocked/skipped update helper')
        require(case.get('helper_exit') == (0 if case['name'] == 'install' else 1), 'unexpected helper process result')
        before, after = case.get('protected_before'), case.get('protected_after')
        require(isinstance(before, list) and before and isinstance(after, list), 'missing protected file proof')
        require({r.get('key') for r in before} == {r.get('key') for r in after} == PROTECTED_KEYS, 'missing protected settings/draft/project source proof')
        require({r['key']: (r['sha256'], r['bytes']) for r in before} == {r['key']: (r['sha256'], r['bytes']) for r in after}, 'update changed protected user/project data')
        require(len({r['key'] for r in before}) == len(before) and len({r['key'] for r in after}) == len(after), 'duplicate protected file proof')
        for value in before + after:
            linked(directory, value)
        require(re.fullmatch(r'[0-9a-f]{64}', case.get('token_fingerprint', '')), 'missing install attempt fingerprint')
        ready_record = case.get('ready')
        ready = load_json(linked(directory, ready_record))
        expected = report['fixture_version'] if case['name'] == 'install' else report['version']
        validate_ready(ready, source_sha=report['source_sha'], version=expected, arch=report['architecture'],
            challenge=report['challenge'], app=case['app'], started=case['started'], finished=case['finished'])
        initial = load_json(linked(directory, case.get('initial_ready')))
        validate_ready(initial, source_sha=report['source_sha'], version=report['version'], arch=report['architecture'],
            challenge=report['challenge'], app=case['app'], started=case['started'], finished=case['finished'])
        require(initial['app_pid'] != ready['app_pid'] and initial['worker_pid'] != ready['worker_pid'], 'restart reused original app/worker')
        images = case.get('images')
        expected_names = {PurePosixPath(image['path']).name for owner in (initial, ready) for image in owner['images']}
        require(isinstance(images, list) and len(images) == len(expected_names) == 2
                and all(isinstance(item, dict) and item.get('path') in expected_names for item in images)
                and len({item['path'] for item in images}) == 2, 'ready frame inventory is missing, duplicated or orphaned')
        for owner in (initial, ready):
            for item in owner['native_source']['files']:
                require(item['sha256'] == source_files.get(item['path'], {}).get('sha256')
                        and item['bytes'] == source_files.get(item['path'], {}).get('bytes'), 'native installed code differs from update proof source')
            for image in owner['images']:
                item = next(r for r in images if r['path'] == PurePosixPath(image['path']).name)
                require(item.get('sha256') == image['sha256'], 'missing actual ready frame')
                import png_evidence
                dimensions = png_evidence.dimensions(linked(directory, item).read_bytes())
                viewport = owner['graphics']['webgl']['viewport']
                require(list(dimensions) == [image['width'], image['height']]
                        and dimensions[0] >= viewport[0] and dimensions[1] >= viewport[1], 'native ready frame misses realized viewport')
        require(type(case.get('helper_pid')) is int and case['helper_pid'] > 1
                and case['helper_pid'] not in (initial['app_pid'], ready['app_pid']), 'missing actual helper subprocess identity')
        request = load_json(linked(directory, case.get('request')))
        require(request.get('schema') == 1 and request.get('token_fingerprint') == case['token_fingerprint']
                and request.get('oldPid') == initial['app_pid'] and request.get('app') == case['app']
                and request.get('arch') == report['architecture'] and request.get('currentVersion') == report['version']
                and request.get('version') == report['fixture_version'], 'install request does not match original process/source context')
        expected_hash = request.get('newHash') if case['name'] == 'install' else request.get('oldHash')
        require(expected_hash == case.get('expected_app_hash'), 'app inventory is not bound to actual install request')
        inspection = load_json(linked(directory, case.get('inspection')))
        require(inspection.get('version') == expected and inspection.get('arch') == report['architecture']
                and inspection.get('app') == case['app']
                and inspection.get('inventory', {}).get('sha256') == case.get('actual_app_hash'), 'post-install inspection contradicts app identity/seal')
        ack = load_json(linked(directory, case.get('ack')))
        require(ack.get('pid') == ready['app_pid'] and ack.get('version') == expected and ack.get('arch') == report['architecture']
                and ack.get('app') == case['app'] and ack.get('token_fingerprint') == case.get('token_fingerprint'), 'unrelated/stale restart ACK')
        result = load_json(linked(directory, case.get('result')))
        require(result.get('token_fingerprint') == case.get('token_fingerprint'), 'unbound helper result')
        require(case.get('expected_app_hash') and re.fullmatch(r'[0-9a-f]{64}', case['expected_app_hash'])
                and case.get('actual_app_hash') == case['expected_app_hash'] and case.get('codesign_verified') is True, 'installed/restored app seal/inventory mismatch')
        if case['name'] == 'install':
            require(result.get('pass') is True and result.get('version') == report['fixture_version']
                    and result.get('updatedPid') == ready['app_pid'] and result.get('pgid') == ready['app_pid']
                    and ack.get('rollback') is False, 'fixture install did not complete')
        else:
            require(result.get('pass') is False and result.get('rollbackError') is None and result.get('rollbackAcknowledged') is True
                    and result.get('rollbackRestored') is True and result.get('updatedReaped') is True and result.get('updatedGroupGone') is True
                    and type(result.get('updatedPid')) is int and result.get('updatedGroup') == result['updatedPid']
                    and (type(result.get('updatedExit')) is int or isinstance(result.get('updatedSignal'), str))
                    and result.get('restoredPid') == ready['app_pid'] and ack.get('rollback') is True
                    and case.get('failed_group_members_after_restore') == [], 'rollback was not acknowledged or left live failed children')
    return report


def command(argv, *, cwd=None, env=None, timeout=120):
    result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    require(result.returncode == 0, f'command failed: {Path(argv[0]).name}: {result.stderr.decode(errors="replace")[-2000:]}')
    require(len(result.stdout) <= 16 * 1024**2, 'command output exceeded proof limit')
    return result.stdout


def node_json(node, script, args, *, env=None, timeout=120):
    return json.loads(command([node, '-e', script, *map(str, args)], cwd=ROOT, env=env, timeout=timeout))


ASAR_PATCH = r'''
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
(async()=>{const a=require(process.argv[1]),file=process.argv[2],folder=process.argv[3],version=process.argv[4],bad=process.argv[5]==='bad';
if(fs.statSync(file).size>8*1024*1024)throw Error('fixture ASAR exceeds explicit shell scope');
a.extractAll(file,folder);const p=path.join(folder,'package.json'),v=JSON.parse(fs.readFileSync(p));
v.version=version;if(bad)v.main='shell/__intentional_update_failure__.cjs';fs.writeFileSync(p,JSON.stringify(v));
await a.createPackage(folder,file+'.fixture');fs.renameSync(file+'.fixture',file);a.uncache(file);
const header=a.getRawHeader(file);console.log(JSON.stringify({header_sha256:crypto.createHash('sha256').update(header.headerString).digest('hex')}));
})().catch(e=>{console.error(e.message);process.exitCode=1;});'''

CONTROLLER = r'''
const fs=require('node:fs'),{fork}=require('node:child_process');
const request=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const child=fork(process.argv[2],[process.argv[1]],{execPath:process.argv[3],execArgv:[],env:{...process.env,ELECTRON_RUN_AS_NODE:'1'},stdio:['ignore','ignore','pipe','ipc']});
child.stderr.pipe(process.stderr);child.on('message',m=>{if(m?.type==='ready'&&m.token===request.token)console.log(JSON.stringify({type:'ready',helper_pid:child.pid}));});
child.on('error',e=>{console.error(e.message);process.exitCode=1;});child.on('exit',(code,signal)=>{console.log(JSON.stringify({type:'exit',helper_pid:child.pid,code,signal}));process.exitCode=code===0?0:1;});'''


def inspect_bundle(node, helper, app, arch, version):
    return node_json(node, 'require(process.argv[1]).inspectBundle(process.argv[2],process.argv[3],process.argv[4]).then(v=>console.log(JSON.stringify(v))).catch(e=>{console.error(e.message);process.exitCode=1})', [helper, app, arch, version])


def make_fixture(app, target, version, node, scratch, *, bad=False):
    command(['/usr/bin/ditto', str(app), str(target)], timeout=180)
    result = node_json(node, ASAR_PATCH, [ROOT / 'node_modules/@electron/asar', target / 'Contents/Resources/app.asar', scratch, version, 'bad' if bad else 'good'])
    plist_file = target / 'Contents/Info.plist'
    data = plistlib.loads(plist_file.read_bytes())
    data['CFBundleShortVersionString'] = data['CFBundleVersion'] = version
    if 'ElectronAsarIntegrity' in data:
        require('Resources/app.asar' in data['ElectronAsarIntegrity'], 'unexpected embedded ASAR integrity mapping')
        data['ElectronAsarIntegrity']['Resources/app.asar'] = {'algorithm': 'SHA256', 'hash': result['header_sha256']}
    plist_file.write_bytes(plistlib.dumps(data))
    command(['/usr/bin/codesign', '--force', '--sign', '-', '--preserve-metadata=identifier,entitlements,requirements,flags', str(target)], timeout=120)
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(target)], timeout=120)
    return {'version': version, 'bad_main_fixture': bad, 'mutations': ['Info.plist versions', 'ASAR package.version/header integrity', 'fresh ad-hoc seal'] + (['intentional nonexistent ASAR main entry'] if bad else [])}


def group_members(group):
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,stat='], stdout=subprocess.PIPE, check=True, timeout=10)
    require(len(result.stdout) <= 4 * 1024**2, 'process inventory exceeds bound')
    return [{'pid': int(parts[0]), 'state': parts[2]} for line in result.stdout.decode().splitlines()
            if len(parts := line.split()) == 3 and parts[1] == str(group)]


def process_identity(pid):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=,pgid=,lstart=,comm='], stdout=subprocess.PIPE, timeout=10)
    text = result.stdout.decode().strip()
    if not text:
        return None
    parts = text.split(maxsplit=7)
    require(len(parts) >= 8 and int(parts[0]) == os.getuid() and int(parts[1]) == pid, 'process group is not this proof owner\'s session')
    return text


def stop_groups(groups, processes, controls):
    """Only sessions launched by this driver or nonce-bound helper in its owned temp app."""
    for process in processes:
        if process.poll() is None:
            groups.setdefault(process.pid, process_identity(process.pid))
    for directory, token, app in controls:
        for name in ('launch.json', 'ack.json', 'rollback-ack.json', 'result.json'):
            file = directory / name
            if not file.is_file():
                continue
            value = load_json(file)
            require(value.get('token') == token, 'refusing cleanup of an unrelated install attempt')
            pids = [value.get(key) for key in ('pid', 'updatedPid', 'restoredPid') if type(value.get(key)) is int]
            for pid in pids:
                if not group_members(pid):
                    continue
                identity = process_identity(pid)
                if identity is not None:
                    require(str(app / 'Contents/MacOS/CassQuery') in identity, 'refusing cleanup of an unrelated application PID')
                groups.setdefault(pid, identity)
    for pid, identity in groups.items():
        for sig, timeout in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            if not group_members(pid):
                break
            current = process_identity(pid)
            require(current is None or (identity is not None and current == identity), 'refusing to signal a reused proof process PID')
            os.killpg(pid, sig)
            end = time.monotonic() + timeout
            while time.monotonic() < end and group_members(pid):
                for process in processes:
                    if process.pid == pid:
                        process.poll()  # Reap our actual subprocess, not just signal it.
                time.sleep(0.05)
        require(not group_members(pid), 'owned fixture process group was not reaped')
    for process in processes:
        process.wait(timeout=5)


def wait_for(call, *, timeout=90, message='proof timed out'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = call()
        if result:
            return result
        time.sleep(0.1)
    raise ValueError(message)


def ready_for(output, *, pid=None, version, challenge, after):
    for file in output.glob('ready-*.json'):
        if file.stat().st_mtime_ns < after:
            continue
        value = load_json(file)
        if (pid is None or value.get('app_pid') == pid) and value.get('version') == version and value.get('challenge') == challenge:
            return value, file
    return None


def protected_snapshot(files, output, label):
    values = []
    for key, file in files.items():
        require(file.is_file() and not file.is_symlink(), 'protected fixture file missing or redirected')
        destination = output / label / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
        values.append({'key': key, **record(destination, output)})
    return values


def redact_control(file, output, name):
    raw = load_json(file)
    def redact(value):
        if isinstance(value, dict):
            return {('token_fingerprint' if key == 'token' else key): hashlib.sha256(item.encode()).hexdigest()
                    if key == 'token' and isinstance(item, str) else redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value
    require(isinstance(raw.get('token'), str) and re.fullmatch(r'[0-9a-f]{64}', raw['token']), 'missing control nonce')
    value = redact(raw)
    value['raw_sha256'] = sha256(file)
    destination = output / name
    atomic_json(destination, value)
    return value, record(destination, output)


def run(app, output, arch, node, fixture_version):
    require(platform.system() == 'Darwin', 'Native update proof requires the real macOS CI host; no simulated fallback')
    resolved_node = shutil.which(node)
    require(resolved_node is not None, 'explicit Node executable is unavailable')
    node = str(Path(resolved_node).resolve())
    require(not app.is_symlink(), 'source app must not be redirected')
    app, output = app.resolve(), output.resolve()
    require(app.suffix == '.app' and not output.is_relative_to(app), 'explicit app and outside-app evidence required')
    import release_evidence
    import macos_smoke
    source = release_evidence.source_snapshot(ROOT)
    require(source['clean'] and source['source_sha'] == os.environ.get('GITHUB_SHA', source['source_sha']), 'native update proof requires exact clean source')
    version = load_json(ROOT / 'package.json')['version']
    require(re.fullmatch(r'(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)', fixture_version) and fixture_version != version, 'distinct canonical fixture version required')
    macos_smoke.verify_bundle(app, arch, version)
    report = {'schema': 1, 'kind': KIND, 'pass': False, 'native_execution': True, 'fixture_only': True,
        'source_sha': source['source_sha'], 'source_fingerprint': source['fingerprint'], 'architecture': arch,
        'version': version, 'fixture_version': fixture_version, 'challenge': uuid.uuid4().hex + uuid.uuid4().hex,
        'started': checkpoint('update-proof-start'), 'cases': [], 'not_checked': ['future GitHub release installation', 'Squirrel/Developer ID', 'UI consent/OS dialogs', 'private projects or actual user installation']}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not output.is_symlink() and output.stat().st_uid == os.getuid() and not output.stat().st_mode & 0o022, 'unsafe native proof output ownership')
    atomic_json(output / 'source-before.json', source)
    report['source_before'] = record(output / 'source-before.json', output)
    atomic_json(output / 'update.json', report)
    owned_groups, processes, controls = {}, [], []
    scratch = None
    completed = False
    try:
        # Retain scratch until ALL owned processes are reaped, even on an exception.
        with contextlib.nullcontext(tempfile.mkdtemp(prefix='cassquery-update-fixture-')) as temporary:
            scratch = Path(temporary).resolve()
            inventory = macos_smoke.inventory(app)
            report['input_app'] = {'path': str(app), 'sha256': inventory['sha256'], 'bytes': inventory['bytes'], 'entries': inventory['entries']}
            require(shutil.disk_usage(scratch).free >= 4 * inventory['bytes'] + 1024**3, 'insufficient disk for independent fixture/staging copies; no hardlink fallback')
            installed_parent = scratch / 'installation'; installed_parent.mkdir(mode=0o700)
            installed = installed_parent / 'CassQuery.app'
            candidate, broken = scratch / 'next/CassQuery.app', scratch / 'broken/CassQuery.app'
            candidate.parent.mkdir(); broken.parent.mkdir()
            report['fixtures'] = [make_fixture(app, candidate, fixture_version, node, scratch / 'asar-good'),
                                  make_fixture(app, broken, fixture_version, node, scratch / 'asar-bad', bad=True)]
            helper = scratch / 'verified-helper.cjs'
            helper.write_bytes(macos_smoke.asar_contents(app / 'Contents/Resources/app.asar', ['shell/update_install.cjs'])['shell/update_install.cjs'])
            require(sha256(helper) == sha256(ROOT / 'shell/update_install.cjs'), 'proof helper differs from packed/current source')
            user_data = scratch / 'user-data'; user_data.mkdir(mode=0o700)
            project = scratch / 'project'; shutil.copytree(ROOT / 'testpart', project, ignore=shutil.ignore_patterns('__pycache__'))
            recovery_project = scratch / 'recovery-project'; shutil.copytree(ROOT / 'testpart', recovery_project, ignore=shutil.ignore_patterns('__pycache__'))
            # Fixture stores are populated through the ACTUAL persistence helper; this is not UI editing proof.
            node_json(node, 'const s=require(process.argv[1]).createStores(process.argv[2]);s.patchSettings({updateCheck:"manual",updateAutoDownload:false});s.saveRecovery({root:process.argv[3],project:"RECOVERYPLATE",path:"part_entry.py",text:process.argv[4],base_sha256:process.argv[5]});console.log("{}")',
                [ROOT / 'shell/desktop.cjs', user_data, recovery_project, (recovery_project / 'part_entry.py').read_text() + '\n# unsaved recovery fixture\n', sha256(recovery_project / 'part_entry.py')])
            sentinel = user_data / 'update-proof-sentinel'; sentinel.write_bytes(os.urandom(128))
            protected = {'userdata/workbench-settings.json': user_data / 'workbench-settings.json',
                'userdata/workbench-recovery.json': user_data / 'workbench-recovery.json', 'userdata/sentinel': sentinel,
                'project/part_entry.py': project / 'part_entry.py', 'project/viewer.json': project / 'viewer.json',
                'recovery/part_entry.py': recovery_project / 'part_entry.py', 'recovery/viewer.json': recovery_project / 'viewer.json'}
            env = {k: v for k, v in os.environ.items() if k not in ('ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS', 'NODE_PATH', 'PYTHONHOME', 'PYTHONPATH') and not k.startswith('CASSQUERY_')}
            env.update(HOME=str(scratch), PATH='/usr/bin:/bin:/usr/sbin:/sbin', CASSQUERY_USER_DATA=str(user_data),
                CASSQUERY_DATA_DIR=str(scratch / 'worker-data'), CASSQUERY_PROJECTS=str(project), CASSQUERY_ANGLE='swiftshader',
                CASSQUERY_UPDATE_PROOF_DIR=str(output), CASSQUERY_UPDATE_PROOF_CHALLENGE=report['challenge'])
            for name, target in (('install', candidate), ('rollback', broken)):
                if installed.exists():
                    shutil.rmtree(installed)
                command(['/usr/bin/ditto', str(app), str(installed)], timeout=180)
                case = {'name': name, 'status': 'pending', 'helper_default_operations': True, 'app': str(installed), 'started': checkpoint(name + '-start')}
                report['cases'].append(case)
                launch_time = time.time_ns()
                log = (output / (name + '-old-app.log')).open('w')
                old = subprocess.Popen([str(installed / 'Contents/MacOS/CassQuery')], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                processes.append(old)
                owned_groups[old.pid] = process_identity(old.pid)
                initial, initial_file = wait_for(lambda: ready_for(output, pid=old.pid, version=version, challenge=report['challenge'], after=launch_time), message='old real app did not become ready')
                case['initial_ready'] = record(initial_file, output)
                case['images'] = [record(Path(image['path']), output) for image in initial['images']]
                validate_ready(initial, source_sha=source['source_sha'], version=version, arch=arch, challenge=report['challenge'], app=str(installed), started=case['started'], finished=checkpoint('old-ready-observed'))
                case['protected_before'] = protected_snapshot(protected, output, name + '/before')
                prepared = node_json(node, 'require(process.argv[1]).copyCandidate(process.argv[2],process.argv[3],process.argv[4],process.argv[5],process.argv[6]).then(v=>console.log(JSON.stringify(v))).catch(e=>{console.error(e.message);process.exitCode=1})', [helper, target, installed, arch, version, fixture_version], timeout=600)
                token = uuid.uuid4().hex + uuid.uuid4().hex
                control = user_data / 'updates' / ('stage-' + token); control.mkdir(parents=True, mode=0o700)
                request = {'schema': 1, 'token': token, 'oldPid': old.pid, 'arch': arch, 'currentVersion': version, 'version': fixture_version, **prepared}
                request_file = control / 'install.json'; atomic_json(request_file, request)
                _, case['request'] = redact_control(request_file, output, name + '/request.json')
                helper_file = control / 'helper.cjs'; shutil.copyfile(helper, helper_file); helper_file.chmod(0o600)
                helper_log = (output / (name + '-helper.log')).open('w')
                controls.append((control, token, installed))
                controller = subprocess.Popen([node, '-e', CONTROLLER, str(request_file), str(helper_file), str(installed / 'Contents/MacOS/CassQuery')], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=helper_log, text=True, start_new_session=True)
                processes.append(controller)
                owned_groups[controller.pid] = process_identity(controller.pid)
                selector = selectors.DefaultSelector(); selector.register(controller.stdout, selectors.EVENT_READ)
                require(selector.select(300), 'real install helper failed to signal readiness')
                helper_ready = json.loads(controller.stdout.readline()); require(helper_ready.get('type') == 'ready'
                    and type(helper_ready.get('helper_pid')) is int and helper_ready['helper_pid'] > 1, 'helper exited before wait-for-old-app')
                case['helper_pid'] = helper_ready['helper_pid']
                # Native opt-in quit hook exercises app.quit / owned worker shutdown, not an OS dialog.
                atomic_json(output / f'quit-{old.pid}.json', {'challenge': report['challenge']})
                old.wait(timeout=30); log.close()
                wait_for(lambda: not group_members(old.pid), timeout=15, message='old app/worker group remained live before replacement')
                owned_groups.pop(old.pid, None)
                helper_exit = controller.wait(timeout=600); selector.close(); helper_log.close()
                case['helper_exit'] = helper_exit
                require(helper_exit == (0 if name == 'install' else 1), 'unexpected real helper completion')
                result, case['result'] = redact_control(control / 'result.json', output, name + '/result.json')
                case['token_fingerprint'] = hashlib.sha256(token.encode()).hexdigest()
                ack_name = 'ack.json' if name == 'install' else 'rollback-ack.json'
                ack, case['ack'] = redact_control(control / ack_name, output, name + '/ack.json')
                owned_groups[ack['pid']] = process_identity(ack['pid'])
                current_version = fixture_version if name == 'install' else version
                ready, ready_file = wait_for(lambda: ready_for(output, pid=ack['pid'], version=current_version, challenge=report['challenge'], after=launch_time), message='installed/restored real app lacks ready record')
                case['ready'] = record(ready_file, output)
                case['images'].extend(record(Path(image['path']), output) for image in ready['images'])
                case['finished'] = checkpoint(name + '-finished')
                validate_ready(ready, source_sha=source['source_sha'], version=current_version, arch=arch, challenge=report['challenge'], app=str(installed), started=case['started'], finished=case['finished'])
                inspected = inspect_bundle(node, helper, installed, arch, current_version)
                inspection_file = output / name / 'inspection.json'; atomic_json(inspection_file, inspected)
                case['inspection'] = record(inspection_file, output)
                case['actual_app_hash'] = inspected['inventory']['sha256']
                case['expected_app_hash'] = prepared['newHash'] if name == 'install' else prepared['oldHash']
                case['codesign_verified'] = True  # inspectBundle performed real codesign and native Mach-O checks.
                if name == 'rollback':
                    launched = load_json(control / 'launch.json')
                    require(launched['token'] == token and launched['pid'] == launched['pgid'], 'failed application group is not owned')
                    case['failed_group_members_after_restore'] = group_members(launched['pgid'])
                case['protected_after'] = protected_snapshot(protected, output, name + '/after')
                atomic_json(output / f'quit-{ack["pid"]}.json', {'challenge': report['challenge']})
                wait_for(lambda: not group_members(ack['pid']), timeout=30, message='proved app/worker group failed to quit')
                owned_groups.pop(ack['pid'], None)
                case['status'] = 'checked'
                # Only helper-created owned staging is removed, after readiness/reaping proof.
                shutil.rmtree(Path(prepared['stage']))
            require(macos_smoke.inventory(app) == inventory, 'fixture proof mutated the supplied source app')
            report['input_app_unchanged'] = True
            after = release_evidence.source_snapshot(ROOT)
            require(after['fingerprint'] == source['fingerprint'] and after['clean'], 'source changed during native fixture proof')
            report['after_fingerprint'] = after['fingerprint']
            completed = True
            report['finished'] = checkpoint('update-proof-complete')
    except Exception as error:
        report.update(pass_=False, error=f'{type(error).__name__}: {error}', finished=checkpoint('update-proof-failed'))
        report['pass'] = report.pop('pass_')
        raise
    finally:
        try:
            stop_groups(owned_groups, processes, controls)
            if scratch is not None:
                shutil.rmtree(scratch)
            report['cleanup_complete'] = True
            if completed:
                report['pass'] = True
                validate_report(report, output, expected_sha=source['source_sha'], expected_arch=arch, expected_version=version)
        except Exception as cleanup:
            report['pass'] = False
            report['cleanup_complete'] = False
            report['error'] = '; '.join(filter(None, [report.get('error'), str(cleanup)]))
            if scratch is not None and scratch.exists():
                report['retained_scratch'] = str(scratch)
        atomic_json(output / 'update.json', report)
    require(report['pass'], report.get('error', 'native update proof did not complete'))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--arch', choices=('arm64', 'x64'))
    parser.add_argument('--node', default='node')
    parser.add_argument('--fixture-version')
    parser.add_argument('--validate-report', type=Path)
    args = parser.parse_args()
    if args.validate_report:
        validate_report(load_json(args.validate_report), args.validate_report.parent)
        return
    if not all((args.app, args.output, args.arch)):
        parser.error('--app, --output and --arch are required')
    version = load_json(ROOT / 'package.json')['version']
    parts = [int(v) for v in version.split('.')]
    fixture = args.fixture_version or f'{parts[0]}.{parts[1]}.{parts[2] + 1}'
    run(args.app, args.output, args.arch, args.node, fixture)


if __name__ == '__main__':
    main()
