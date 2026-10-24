#!/usr/bin/env python3
"""Exercise the actual worker, CAD gates, watcher and fail-closed retention."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

from macos_smoke import (ROOT, architecture, checkpoint, file_evidence, instrument_project,
                         require, validate_worker_proof, write_report)


def get_bytes(port, endpoint):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{endpoint}', timeout=5) as response:
        return response.read()


def get(port, endpoint):
    return json.loads(get_bytes(port, endpoint))


def until(check, message, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise RuntimeError(f'Timed out: {message}')


def certified_gates(value):
    ids = {'units', 'watertight', 'volume', 'thin-extent', 'triangulation',
           'payload-roundtrip', 'budget-tris', 'budget-bytes'}
    checks = value.get('checks', [])
    require(value.get('pass') is True and len(checks) == 8 and {c.get('id') for c in checks} == ids
            and all(c.get('status') == 'pass' for c in checks), 'Incomplete/failed eight certified gates')


def run_smoke(worker, output, python=None, arch=None):
    worker, output = Path(worker).resolve(), Path(output).resolve()
    python = Path(python).absolute() if python else None
    arch = architecture(arch or platform.machine())
    output.parent.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT / 'package.json').read_text())['version']
    report = {'schema': 1, 'pass': False, 'source_sha': os.environ.get('GITHUB_SHA') or None,
              'architecture': arch, 'version': version, 'checks': [], 'checked': [],
              'not_checked': ['native renderer', 'final ZIP/DMG', 'human visual approval'],
              'checkpoints': [checkpoint('worker-smoke-start')], 'images': []}
    write_report(output, report)
    process, busy_http, busy_ws = None, None, None
    proof = output.with_name(f'{output.stem}-runtime.json')
    proof.unlink(missing_ok=True)
    log_file = output.with_suffix('.log')
    try:
        require(architecture(platform.machine()) == arch, 'Worker smoke must use native target architecture')
        report['executable'] = file_evidence(python or worker)
        report['worker_input'] = file_evidence(worker)
        with tempfile.TemporaryDirectory(prefix='cq-worker-smoke-') as scratch:
            root = Path(scratch)
            project = root / 'demo'
            shutil.copytree(ROOT / 'testpart', project, ignore=shutil.ignore_patterns('__pycache__'))
            report['fixture_inputs'] = [file_evidence(project / name) for name in ('viewer.json', 'part_entry.py')]
            instrument_project(project, proof)
            registry = root / 'registry.json'
            for _ in range(100):
                busy_http, busy_ws = socket.socket(), socket.socket()
                busy_http.bind(('127.0.0.1', 0))
                start = busy_http.getsockname()[1]
                try:
                    if start > 64476:
                        raise OSError('port too high')
                    busy_ws.bind(('127.0.0.1', start + 1001))
                    break
                except OSError:
                    busy_http.close(); busy_ws.close()
            else:
                raise RuntimeError('Could not reserve collision test sockets')
            challenge = uuid.uuid4().hex
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith('CASSQUERY_') and key not in ('PYTHONHOME', 'PYTHONPATH')}
            env.update({'HOME': scratch, 'CASSQUERY_DATA_DIR': str(root / 'data'),
                        'CASSQUERY_SMOKE_CHALLENGE': challenge, 'PYTHONDONTWRITEBYTECODE': '1'})
            if not python:
                env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
            command = ([str(python)] if python else []) + [str(worker), str(project), '--port', str(start), '--registry-file', str(registry)]
            started = checkpoint('worker-launch')
            with log_file.open('w') as log:
                process = subprocess.Popen(command, env=env, cwd=scratch, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    until(lambda: registry.exists() or process.poll() is not None, 'worker readiness')
                    require(process.poll() is None, f'Worker failed readiness: {log_file.read_text(errors="replace")[-4000:]}')
                    info = json.loads(registry.read_text())
                    require(info.get('pid') == process.pid and type(info.get('started')) in (int, float)
                            and started['wall_time_ms'] / 1000 <= info['started'] <= time.time(), 'Stale/unowned readiness checkpoint')
                    port = info['port']
                    require(type(port) is int and start + 2 <= port < start + 60, 'Busy HTTP/WebSocket ports not skipped')
                    report['readiness'] = info
                    report['challenge'] = challenge
                    report['worker_pid'] = process.pid
                    report['checks'].append('paired port collision handling and owned readiness')
                    report['checkpoints'].append(checkpoint('owned-ready'))
                    until(lambda: get(port, '/api/status')['projects'][0]['state'] == 'certified', 'initial certified CAD build')
                    status = get(port, '/api/status')
                    require(status.get('api_version') == 2 and status.get('load_errors') == [] and len(status.get('projects', [])) == 1
                            and status['projects'][0]['name'] == 'TESTPLATE', 'Invalid actual worker status')
                    gates = get(port, '/api/gates?project=TESTPLATE')
                    certified_gates(gates)
                    report['gates'] = gates
                    report['status'] = status
                    report['checks'].append('real CAD demo passes all eight certified gates')
                    report['checkpoints'].append(checkpoint('eight-gates-certified'))
                    original = get(port, '/payload/TESTPLATE-certified.json')
                    entry = project / 'part_entry.py'
                    source = entry.read_text()
                    require('.circle(4)' in source, 'Save mutation would be a no-op')
                    entry.write_text(source.replace('.circle(4)', '.circle(5)'))
                    changed = until(lambda: (value if (value := get(port, '/payload/TESTPLATE-certified.json'))['hash'] != original['hash'] else None), 'save-triggered preview/certified rebuild')
                    require(get(port, '/api/status')['projects'][0]['hashes'].get('preview'), 'Save did not publish preview')
                    certified_gates(get(port, '/api/gates?project=TESTPLATE'))
                    report['checks'].append('real save -> preview -> certified geometry change')
                    report['checkpoints'].append(checkpoint('save-certified'))
                    retained_bytes = get_bytes(port, '/payload/TESTPLATE-certified.json')
                    manifest = json.loads((project / 'viewer.json').read_text())
                    manifest['unit_scale'] = 2
                    (project / 'viewer.json').write_text(json.dumps(manifest))
                    until(lambda: get(port, '/api/status')['projects'][0]['state'] == 'gate-failed', 'deliberate units failure')
                    failed_gates = get(port, '/api/gates?project=TESTPLATE')
                    require(failed_gates.get('pass') is False and any(c.get('id') == 'units' and c.get('status') == 'fail' for c in failed_gates.get('checks', [])), 'Wrong intentional gate failure')
                    require(get_bytes(port, '/payload/TESTPLATE-certified.json') == retained_bytes, 'Failed gate changed prior certified index bytes')
                    payload_files = []
                    for name, metadata in changed['variants'].items():
                        data = get_bytes(port, metadata['url'])
                        require(json.loads(data)['parts'], 'Prior geometry unavailable')
                        require(hashlib.sha256(data).hexdigest()[:16] == metadata['hash'], 'Retained geometry bytes/hash mismatch')
                        file = output.with_name(f'{output.stem}-retained-{name}.json')
                        file.write_bytes(data)
                        payload_files.append(file_evidence(file))
                    index_file = output.with_name(f'{output.stem}-retained-index.json')
                    index_file.write_bytes(retained_bytes)
                    report['retained_payloads'] = [file_evidence(index_file), *payload_files]
                    report['checks'].append('failed certified gate retains prior certified index and geometry')
                    report['failed_gates'] = failed_gates
                    report['checkpoints'].append(checkpoint('failed-gate-retained'))
                    runtime = json.loads(proof.read_text())
                    validate_worker_proof(runtime, process.pid, challenge, started, checkpoint('runtime-read'), frozen=not bool(python), executable=python or worker)
                    report['worker_runtime'] = runtime
                    report['frozen'] = runtime['frozen']
                    report['runtime_evidence'] = file_evidence(proof)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    report['worker_shutdown'] = process.poll() is not None
                    report['checkpoints'].append(checkpoint('worker-shutdown'))
        report['checked'] = [*report['checks'], 'actual PID/challenge/sys.frozen/executable checkpoint', 'owned process shutdown', 'retained exact payload byte hashes']
        report['checkpoints'].append(checkpoint('worker-smoke-complete'))
        report['pass'] = True
    except Exception as error:
        report['pass'] = False
        report['error'] = f'{type(error).__name__}: {error}'
        report['checked'] = list(report['checks'])
        report['not_checked'].append('remaining worker assurance stages; see error')
        report['checkpoints'].append(checkpoint('worker-smoke-failed'))
        raise
    finally:
        if busy_http:
            busy_http.close()
        if busy_ws:
            busy_ws.close()
        if log_file.exists():
            report['log'] = file_evidence(log_file)
        write_report(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', required=True, type=Path)
    parser.add_argument('--python', type=Path, help='interpreter for an unfrozen worker.py')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--arch', choices=('arm64', 'x86_64', 'x64'))
    args = parser.parse_args()
    print(json.dumps(run_smoke(args.worker, args.output, args.python, args.arch), indent=2))


if __name__ == '__main__':
    main()
