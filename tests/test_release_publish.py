"""Bounded real-byte fixtures for the protected publication boundary (no GitHub writes)."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import struct
import sys
import zlib
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_evidence as evidence
spec = importlib.util.spec_from_file_location("release_publish", ROOT / "tools/release_publish.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SHA = "a" * 40
TAG_SHA = "b" * 40
LOCKED_INPUTS = {name: (ROOT / name).read_bytes() for name in
                 ("package.json", "requirements-build.txt", "package-lock.json", "packaging/requirements-macos.lock", "packaging/wheel-manifest.json")}
NATIVE_SOURCE_NAMES = ('worker.py', 'shell/main.js', 'shell/runtime.cjs', 'shell/smoke.cjs', 'shell/renderer_config.cjs',
    'viewer/app.js', 'viewer/brand.svg', 'viewer/index.html', 'viewer/style.css', 'viewer/vendor/LICENSE.three',
    'viewer/vendor/three.core.js', 'viewer/vendor/three.module.js', 'viewer/vendor/addons/controls/OrbitControls.js',
    'viewer/vendor/addons/environments/RoomEnvironment.js', 'viewer/vendor/addons/utils/BufferGeometryUtils.js',
    'testpart/part_entry.py', 'testpart/viewer.json', 'assets/icons/cassquery.icns')
SOURCE_INPUTS = {**LOCKED_INPUTS, **{name: (ROOT / name).read_bytes() for name in NATIVE_SOURCE_NAMES},
                 **{file.relative_to(ROOT).as_posix(): file.read_bytes() for file in (ROOT / 'tests').glob('test_*.py')},
                 **{file.relative_to(ROOT).as_posix(): file.read_bytes() for file in (ROOT / 'tests').glob('*.cjs')}}
SOURCE_FILES = [{"path": name, "bytes": len(data), "executable": False, "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(SOURCE_INPUTS.items())]
FINGERPRINT = hashlib.sha256(json.dumps({"source_sha": SHA, "files": SOURCE_FILES}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
VERSION = json.loads(LOCKED_INPUTS["package.json"])["version"]
TAG = "v" + VERSION
major, minor, patch_version = map(int, VERSION.split("."))
OTHER_VERSION = f"{major}.{minor}.{patch_version + 1}"
OTHER_TAG = "v" + OTHER_VERSION
RUN_ID = 100
PNG_BYTES = {}


def record(name, data):
    return {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def protocol_png(width, height, index=0):
    """Complete RGB PNG bytes, cached by actual frame size; never native proof."""
    key = width, height, index
    if key not in PNG_BYTES:
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
        pattern = bytes(x * 37 % 256 for x in range(256))
        rows = []
        for y in range(height):
            shift = ((y * 83 + index) * 173) % 256
            row = pattern[shift:] + pattern[:shift]
            rows.append(b'\x00' + (row * ((width * 3 + 255) // 256))[:width * 3])
        PNG_BYTES[key] = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                          + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))
    return PNG_BYTES[key]


def candidate_bytes(arch, mutate=None, extra=None):
    # These are complete protocol fixtures, not claims of a rendered native app.
    files = dict(LOCKED_INPUTS)
    identity = {"schema": 1, "pass": True, "source_sha": SHA, "architecture": arch, "version": VERSION}
    for suffix in ("zip", "dmg"):
        files[f"dist/mac-app/CassQuery-{VERSION}-mac-{arch}.{suffix}"] = f"original-{arch}-{suffix}".encode()

    def put(name, value):
        files[name] = json.dumps(value).encode()
        return record(name, files[name])

    def linked(name, report_dir):
        return {**record(name, files[name]), "path": str(Path(name).relative_to(report_dir))}

    resources = '/protocol-only/CassQuery.app/Contents/Resources'
    started = {'wall_time_ms': 1000, 'monotonic_ms': 1000}
    finished = {'wall_time_ms': 5000, 'monotonic_ms': 5000}
    source_records = {r['path']: r for r in SOURCE_FILES}
    tree = [{'path': '.', 'kind': 'directory', 'mode': 493}]
    for name in ('Contents/MacOS/CassQuery', 'Contents/Resources/worker/cassquery-worker'):
        tree.append({'path': name, 'kind': 'file', 'mode': 493, 'bytes': 24,
                     'sha256': hashlib.sha256(('protocol-only-' + arch + name).encode()).hexdigest()})
    icon = source_records['assets/icons/cassquery.icns']
    tree.append({'path': 'Contents/Resources/icon.icns', 'kind': 'file', 'mode': 420,
                 'bytes': icon['bytes'], 'sha256': icon['sha256']})
    tree.sort(key=lambda r: r['path'])
    inventory = {'records': tree, 'entries': len(tree), 'bytes': sum(r.get('bytes', 0) for r in tree),
                 'sha256': hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    bundle = {'bundle_id': 'com.cassquery.desktop', 'architecture': arch, 'version': VERSION,
              'bundle_sha256': inventory['sha256'], 'icon': {**icon, 'path': resources + '/icon.icns'},
              'machos': [{'path': r['path'], 'architectures': [arch], 'sha256': r['sha256']} for r in tree if r['mode'] == 493 and r['kind'] == 'file'],
              'source_files': [{**source_records[name], 'path': '/protocol-source/' + name} for name in NATIVE_SOURCE_NAMES if name != 'assets/icons/cassquery.icns']}

    def prefixed(value, prefix):
        if isinstance(value, dict):
            return {k: prefix + '/' + v if k == 'path' and isinstance(v, str) and not v.startswith('/') else prefixed(v, prefix) for k, v in value.items()}
        if isinstance(value, list):
            return [prefixed(v, prefix) for v in value]
        return value

    def raw_renderer(directory, bundled):
        images, matrix = [], []
        def image(name, width, height, index):
            path = directory + '/' + name
            files[path] = protocol_png(width, height, index)
            return {**linked(path, directory), 'width': width, 'height': height}
        camera_checks = []
        direction = [0.95, -1.35, 0.62]
        length = sum(v * v for v in direction) ** 0.5
        direction = [v / length for v in direction]
        base_camera = {'direction': direction, 'target': [0, 0, 0], 'center': [0, 0, 0], 'radius': 10}
        for index, mode in enumerate(('orbit', 'pan')):
            dragged = copy.deepcopy(base_camera)
            if mode == 'orbit':
                dragged['direction'] = [1, 0, 0]
            else:
                dragged['target'] = [1, 0, 0]
            drag_image = image(mode + '-drag.png', 1320, 700, 80 + index * 2)
            reset_image = image(mode + '-reset.png', 1320, 700, 81 + index * 2)
            images.extend((drag_image, reset_image))
            camera_checks.append({'mode': mode, 'status': 'checked',
                'method': 'native pointer drag then Reset; observed at 700ms and 3s',
                'before': copy.deepcopy(base_camera), 'after_drag': dragged,
                'after_reset_700': copy.deepcopy(base_camera), 'after_reset_settled': copy.deepcopy(base_camera),
                'drag_image': drag_image, 'reset_image': reset_image})
        for index, (theme, width, state) in enumerate((t, w, s) for t in ('dark', 'light') for w in (450, 700, 1000, 1320) for s in ('assembled', 'apart')):
            captured = image(f'{theme}-{width}-{state}.png', width, 700, index)
            images.append(captured)
            matrix.append({'theme': theme, 'width': width, 'state': state, 'status': 'checked',
                'vertices': 30, 'triangles': 10, 'degenerate': 0, 'meshes': 3, 'clipped': 0, 'nonfinite': 0, 'overflow': False,
                'bad': [], 'overlaps': [], 'errors': [], 'realized_theme': theme, 'controls_checked': 15,
                'gates_pass': True, 'tier': 'certified', 'viewport': [width, 700], 'diag': '',
                'background': 'black' if theme == 'dark' else 'white', 'full_size': [width, 700], 'scroll_regions': [], 'vertical_scroll': False,
                'step_pressed': ['true', 'false'] if state == 'assembled' else ['false', 'true'],
                'explode': '0%' if state == 'assembled' else '100%', 'hash': 'protocol-certified-geometry',
                'canvas': {'width': width, 'height': 700, 'client_width': width, 'client_height': 700}, 'image': captured})
        for index, name in enumerate(('assembled', 'apart') if bundled else ('assembled', 'apart', 'save-camera', 'save-certified', 'gate-failed')):
            images.append(image(name + '.png', 1320, 700, 14 + index if name in ('assembled', 'apart') else 40 + index))
        names = ['renderer-start', 'certified-ready', 'production-controls', 'native-matrix']
        if not bundled:
            names += ['save-certified', 'failed-gate-retained']
        names += ['renderer-complete', 'worker-shutdown']
        offset = 0 if bundled else 5000
        launch_started = {key: value + offset for key, value in started.items()}
        launch_finished = {key: value + offset for key, value in finished.items()}
        executed = [{'bytes': source_records[name]['bytes'], 'path': name, 'sha256': source_records[name]['sha256']}
                    for name in evidence.NATIVE_SOURCE_PATHS]
        native_source = {'files': executed, 'fingerprint': hashlib.sha256(json.dumps({'files': executed}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        raw = {**identity, 'matrix': matrix, 'images': images, 'camera_checks': camera_checks, 'native_source': native_source,
            'lifecycle': {'pass': True, 'close_reopen': True, 'secondary_exit': 0, 'secondary_pid': 12,
                'second_instance_events': 1, 'worker_pid_before': 11, 'worker_pid_after': 11},
            'worker_pid': 11, 'app_pid': 10, 'packaged': True, 'bundled_worker': True, 'worker_shutdown': True,
            'challenge': 'fixture', 'errors': [], 'resources_path': resources, 'worker_executable': resources + '/worker/cassquery-worker',
            'render_backend_requested': 'metal-requested',
            'graphics': {'runtime': {'electron': '45.0.0', 'chromium': 'protocol-fixture', 'node': '22.23.4', 'platform': 'darwin', 'architecture': arch},
                'display': {'scale_factor': 1, 'bounds': {'x': 0, 'y': 0, 'width': 1920, 'height': 1080},
                    'work_area': {'x': 0, 'y': 0, 'width': 1920, 'height': 1080}, 'window_bounds': {'x': 0, 'y': 0, 'width': 1320, 'height': 700}},
                'gpu': {'protocol_fixture_only': True},
                'webgl': {'context': 'webgl2', 'vendor': 'protocol-vendor', 'renderer': 'protocol-renderer', 'version': 'WebGL 2.0',
                    'shading_language': 'WebGL GLSL ES 3.00', 'unmasked_vendor': 'protocol-vendor', 'unmasked_renderer': 'protocol-renderer',
                    'is_context_lost': False, 'drawing_buffer': [1000, 700], 'viewport': [1320, 700], 'dpr': 1}},
            'started': {'wall_time_ms': 1100 + offset, 'monotonic_ms': 1100 + offset}, 'finished': {'wall_time_ms': 4000 + offset, 'monotonic_ms': 4000 + offset},
            'checkpoints': [{'name': name, 'wall_time_ms': 1200 + offset + i * 200, 'monotonic_ms': 1200 + offset + i * 200} for i, name in enumerate(names)],
            'checks': ['bundled default certified demo'] if bundled else sorted(evidence.NATIVE_CHECKS),
            'screenshots': ['assembled.png', 'apart.png'],
            'worker_status': {'api_version': 2, 'load_errors': [], 'projects': [{'name': 'TESTPLATE', 'root': resources + '/worker/_internal/testpart'}]},
            'launch': {'started': launch_started, 'finished': launch_finished, 'exit': 0, 'path': '/usr/bin:/bin:/usr/sbin:/sbin', 'project_override': None if bundled else '/public-protocol/demo'}}
        if not bundled:
            saved_camera = copy.deepcopy(base_camera)
            saved_camera['direction'] = [1, 0, 0]
            raw['camera_save'] = {'method': 'native orbit to non-default direction; observe settled camera before saving',
                'before_drag': copy.deepcopy(base_camera), 'before_save': saved_camera, 'after_save': copy.deepcopy(saved_camera),
                'image': next(image for image in images if image['path'] == 'save-camera.png')}
            state = {'vertices': 30, 'triangles': 10, 'degenerate': 0, 'clipped': 0, 'nonfinite': 0, 'diag': '', 'tier': 'certified', 'gates_pass': True, 'hash': 'protocol-certified-geometry'}
            raw['save_state'] = state
            raw['failure_state'] = dict(state)
        evidence.validate_native_renderer(raw, expected_sha=SHA, expected_arch=arch, expected_version=VERSION,
                                          bundled=bundled, started=launch_started, finished=launch_finished)
        return raw

    def renderer(directory):
        cold = raw_renderer(directory + '/bundled', True)
        put(directory + '/bundled/renderer.json', cold)
        raw = raw_renderer(directory, False)
        runtime_path = directory + '/worker-runtime.json'
        runtime = {'schema': 1, 'pid': 11, 'challenge': 'fixture', 'frozen': True, 'executable': raw['worker_executable'],
                   'resource_root': resources + '/worker/_internal', 'wall_time_ms': 8000, 'monotonic_ms': 8000}
        put(runtime_path, runtime)
        put(directory + '/bundle-before.json', inventory)
        put(directory + '/bundle-after.json', inventory)
        native = {**raw, 'images': prefixed(cold['images'], 'bundled') + raw['images'],
            'renderer_started': raw['started'], 'renderer_finished': raw['finished'], 'renderer_checkpoints': raw['checkpoints'],
            'started': {'wall_time_ms': 900, 'monotonic_ms': 900}, 'finished': {'wall_time_ms': 11000, 'monotonic_ms': 11000},
            'checkpoints': [{'name': 'native-start', 'wall_time_ms': 950, 'monotonic_ms': 950},
                {'name': 'bundled-launch-complete', 'wall_time_ms': 5100, 'monotonic_ms': 5100},
                {'name': 'native-complete', 'wall_time_ms': 10900, 'monotonic_ms': 10900}],
            'bundled_report': linked(directory + '/bundled/renderer.json', directory),
            'worker_runtime': runtime, 'worker_runtime_evidence': linked(runtime_path, directory),
            'bundle_before': linked(directory + '/bundle-before.json', directory), 'bundle_after': linked(directory + '/bundle-after.json', directory),
            'bundle': bundle}
        put(directory + '/renderer.json', native)
        return native

    renderer(f'evidence/mac-{arch}')
    for extension in ('zip', 'dmg'):
        directory = f'evidence/{extension}-{arch}'
        launches, aggregate = {}, []
        for label in (('mounted', 'copied') if extension == 'dmg' else ('extracted', 'copied')):
            native = renderer(directory + '/' + label)
            launches[label] = {'matrix': prefixed(native['matrix'], label), 'images': prefixed(native['images'], label), 'bundle': bundle,
                              'native_report': linked(directory + '/' + label + '/renderer.json', directory)}
            aggregate.extend(launches[label]['images'])
        put(directory + '/checked-inventory.json', inventory)
        put(directory + '/bundle-inventory.json', inventory)
        asset = f'dist/mac-app/CassQuery-{VERSION}-mac-{arch}.{extension}'
        put(directory + '/package.json', {**identity, 'asset': record(asset, files[asset]), 'images': aggregate,
            'checked_inventory': linked(directory + '/checked-inventory.json', directory),
            'bundle_inventory': linked(directory + '/bundle-inventory.json', directory), 'checked_bundle_sha256': inventory['sha256'],
            'launches': launches, 'temp_root_removed': True, 'detach_completed': True})
    stages = []
    python_counts = {'schema': 1, 'tests': 4, 'collected': 4, 'failures': 0, 'skipped': 0,
                     'files': sorted(name for name in SOURCE_INPUTS if name.startswith('tests/test_') and name.endswith('.py'))}
    put('evidence/python-counts.json', python_counts)
    for index, stage in enumerate(('syntax', 'node', 'python', 'release-contract', 'brand-exports')):
        path = 'evidence/' + stage + '.log'
        if stage == 'node':
            text = 'TAP version 13\n' + ''.join(f'ok {i} - synthetic protocol fixture {i}\n' for i in range(1, 5))
            text += '1..4\n# tests 4\n# suites 0\n# pass 4\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n# duration_ms 250\n'
        elif stage == 'python':
            text = 'Synthetic protocol fixture, not actual CI\nRan 4 tests in 0.100s\n\nOK\n'
        else:
            text = 'bounded successful protocol fixture, not actual CI\n'
        files[path] = text.encode()
        item = {'name': stage, 'exit': 0, **linked(path, 'evidence'), 'started_at': 10 + index,
                'completed_at': 10.25 + index, 'duration_s': 0.25}
        if stage in ('node', 'python'):
            item.update(tests=4, failures=0, skipped=0)
        if stage == 'node':
            item['files'] = sorted(name for name in SOURCE_INPUTS if name.startswith('tests/') and name.endswith('.cjs'))
        if stage == 'python':
            item['counts'] = linked('evidence/python-counts.json', 'evidence')
        stages.append(item)
    put('evidence/preflight.json', {**identity, 'source_fingerprint': FINGERPRINT, 'after_fingerprint': FINGERPRINT,
        'clean': True, 'failures': 0, 'skipped': 0, 'tests': 8, 'stages': stages, 'started_at': 9, 'completed_at': 16})
    runtime_path = f'evidence/worker-{arch}-runtime.json'
    runtime = {'schema': 1, 'pid': 11, 'challenge': 'fixture', 'frozen': True, 'executable': '/protocol-only/cassquery-worker',
               'resource_root': '/protocol-only/_internal', 'wall_time_ms': 2000, 'monotonic_ms': 2000}
    put(runtime_path, runtime)
    ids = ('units', 'watertight', 'volume', 'thin-extent', 'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes')
    put(f'evidence/worker-{arch}.json', {**identity, 'gates': {'pass': True, 'checks': [{'id': gate, 'status': 'pass'} for gate in ids]},
        'worker_pid': 11, 'challenge': 'fixture', 'worker_runtime': runtime, 'executable': {'path': runtime['executable']},
        'checkpoints': [{'name': 'worker-smoke-start', **started}, {'name': 'worker-smoke-complete', **finished}],
        'worker_shutdown': True, 'runtime_evidence': linked(runtime_path, 'evidence')})
    components = []
    wheels = json.loads(LOCKED_INPUTS["packaging/wheel-manifest.json"])["wheels"]
    for line in LOCKED_INPUTS["requirements-build.txt"].decode().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        canonical = m.re.sub(r"[-_.]+", "-", name).lower()
        components.append({"type": "library", "name": canonical, "version": version,
                           "purl": f"pkg:pypi/{canonical}@{version}",
                           "hashes": [{"alg": "SHA-256", "content": sha} for sha in sorted(
                               {wheel["sha256"] for wheel in wheels if wheel["name"] == canonical})]})
    packages = json.loads(LOCKED_INPUTS["package-lock.json"])["packages"]
    npm = {}
    for path, item in packages.items():
        if path and item.get("version"):
            name = path.rsplit("node_modules/", 1)[-1]
            version = item["version"]
            npm[name, version] = {"type": "library", "name": name, "version": version,
                                  "purl": f'pkg:npm/{name.replace("@", "%40")}@{version}',
                                  "properties": [{"name": "npm:integrity", "value": item.get("integrity", "")}]}
    components.extend(npm.values())
    put("evidence/sbom.json", {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": components,
                                "metadata": {"properties": [{"name": "source_sha", "value": SHA},
                                                            {"name": "python-wheel-lock-sha256", "value": hashlib.sha256(LOCKED_INPUTS["packaging/requirements-macos.lock"]).hexdigest()},
                                                            {"name": "npm-lock-sha256", "value": hashlib.sha256(LOCKED_INPUTS["package-lock.json"]).hexdigest()}]}})
    put("evidence/source-before.json", {"schema": 1, "clean": True, "source_sha": SHA,
                                         "fingerprint": FINGERPRINT, "files": SOURCE_FILES})
    files["evidence/runner-context.txt"] = b"bounded native CI protocol fixture\n"
    records = [record(name, data) for name, data in sorted(files.items())]
    by_name = {x["path"]: x for x in records}
    manifest = {"schema": 1, "repository": m.REPOSITORY, "source_sha": SHA,
                "source_fingerprint": FINGERPRINT, "version": VERSION, "architecture": arch,
                "run_id": RUN_ID, "run_attempt": 1, "distribution": "adhoc", "files": records,
                "assets": [by_name[f"dist/mac-app/CassQuery-{VERSION}-mac-{arch}.{suffix}"] for suffix in ("zip", "dmg")],
                "gates": {gate: by_name[template.format(arch=arch)] for gate, template in evidence.GATES.items()}}
    if mutate:
        mutate(manifest, files)
    files["candidate.json"] = json.dumps(manifest, indent=2).encode()
    if extra:
        files.update(extra)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue(), files


class FakeAPI:
    """Endpoint-level fixture; runs the production paginator and evidence verifier."""
    items = m.GitHub.items

    def __init__(self):
        self.calls = []
        self.environment = {"id": 700, "name": "releases", "protection_rules": [{"type": "required_reviewers",
                            "prevent_self_review": True, "reviewers": [{"type": "User", "reviewer": {"id": 900}}]}],
                            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        self.policies = [{"id": 701, "name": "v*", "type": "tag"}]
        repo = {"id": 1, "full_name": m.REPOSITORY}
        self.run = {"id": RUN_ID, "run_attempt": 1, "repository": repo, "head_repository": repo,
                    "head_sha": SHA, "event": "push", "head_branch": "main", "status": "completed",
                    "conclusion": "success", "path": m.CANDIDATE_WORKFLOW, "workflow_id": 5}
        self.jobs = [{"id": 20 + i, "run_id": RUN_ID, "head_sha": SHA, "run_attempt": 1, "head_branch": "main", "name": name, "status": "completed", "conclusion": "success",
                      "steps": [{"name": step, "status": "completed", "conclusion": "success"}
                                for step in evidence.REQUIRED_STEPS]}
                     for i, name in enumerate(("candidate (macos-15, arm64)", "candidate (macos-15-intel, x64)"))]
        self.integration_run = {**copy.deepcopy(self.run), 'id': 101, 'run_number': 11, 'workflow_id': 7,
                                'path': '.github/workflows/integration.yml'}
        self.integration_jobs = [{'id': 31, 'run_id': 101, 'head_sha': SHA, 'run_attempt': 1, 'head_branch': 'main',
                                 'name': 'integration', 'status': 'completed', 'conclusion': 'success',
                                 'steps': [{'name': step, 'status': 'completed', 'conclusion': 'success'} for step in evidence.INTEGRATION_STEPS]}]
        self.archives = {}
        self.originals = {}
        self.artifacts = []
        for i, arch in enumerate(m.ARCHES):
            data, files = candidate_bytes(arch)
            self.archives[300 + i] = data
            self.originals[arch] = files
            self.artifacts.append({"id": 300 + i, "name": f"CassQuery-mac-{arch}-{SHA}", "expired": False,
                                   "size_in_bytes": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                                   "workflow_run": {"id": RUN_ID, "head_sha": SHA, "head_branch": "main",
                                                    "repository_id": 1, "head_repository_id": 1}})
        self.releases = []
        self.latest_override = "automatic"
        self.asset_data = {}
        self.assets = []
        self.current_run = {"id": 200, "run_attempt": 1, "event": "workflow_dispatch", "head_branch": TAG,
                            "head_sha": SHA, "status": "in_progress", "actor": {"login": m.OWNER},
                            "triggering_actor": {"login": m.OWNER}, "repository": repo, "head_repository": repo,
                            "path": m.WORKFLOW, "workflow_id": 6}
        self.approval_jobs = [{"name": name, "status": "completed", "conclusion": "success",
                               "steps": [{"name": step, "status": "completed", "conclusion": "success"}]}
                              for name, step in (("prepare", "Verify candidate and protection"),
                                                 ("approve", "Recheck protected approval boundary"))]
        self.reviews = [{"state": "approved", "user": {"id": 900, "login": "reviewer", "type": "User"},
                         "environments": [{"id": 700, "name": "releases"}]}]
        self.tag_type = "tag"
        self.tag_sha = TAG_SHA
        self.tag_commit = SHA
        self.failures = {}
        self.on_call = None

    def json(self, path, *, method="GET", payload=None, missing=False):
        self.calls.append((method, path))
        base = path.split("?", 1)[0]
        if self.on_call:
            self.on_call(method, base)
        if base in self.failures:
            raise m.ReleaseError(self.failures[base])
        tail = base.removeprefix(m.PREFIX)
        if method == "POST" and tail == "/releases":
            result = {"id": 400, **payload}
            self.releases.append(result)
            return copy.deepcopy(result)
        if method == "PATCH" and tail == "/releases/400":
            self.releases[-1].update(payload)
            return copy.deepcopy(self.releases[-1])
        if tail == "/environments/releases":
            return copy.deepcopy(self.environment)
        if tail == "/environments/releases/deployment-branch-policies":
            return {"branch_policies": copy.deepcopy(self.policies), "total_count": len(self.policies)}
        if tail == "/git/ref/tags/" + TAG:
            return {"ref": "refs/tags/" + TAG, "object": {"type": self.tag_type, "sha": self.tag_sha}}
        if tail.startswith("/git/tags/"):
            return {"sha": self.tag_sha, "tag": TAG, "object": {"type": "commit", "sha": self.tag_commit}}
        if tail == '/actions/workflows/integration.yml':
            return {'id': 7, 'path': '.github/workflows/integration.yml'}
        if tail == '/actions/workflows/7/runs':
            return {'workflow_runs': [copy.deepcopy(self.integration_run)], 'total_count': 1}
        if tail == '/actions/runs/101':
            return copy.deepcopy(self.integration_run)
        if tail == f"/actions/runs/101/attempts/{self.integration_run['run_attempt']}/jobs":
            return {'jobs': copy.deepcopy(self.integration_jobs), 'total_count': len(self.integration_jobs)}
        if tail == "/actions/workflows/macos-candidate.yml":
            return {"id": 5, "path": m.CANDIDATE_WORKFLOW}
        if tail == "/actions/workflows/release.yml":
            return {"id": 6, "path": m.WORKFLOW}
        if tail == f"/actions/runs/{RUN_ID}":
            return copy.deepcopy(self.run)
        if tail == f"/actions/runs/{RUN_ID}/attempts/{self.run['run_attempt']}/jobs":
            return {"jobs": copy.deepcopy(self.jobs), "total_count": len(self.jobs)}
        if tail == f"/actions/runs/{RUN_ID}/artifacts":
            return {"artifacts": copy.deepcopy(self.artifacts), "total_count": len(self.artifacts)}
        if tail == "/actions/runs/200":
            return copy.deepcopy(self.current_run)
        if tail == "/actions/runs/200/attempts/1/jobs":
            return {"jobs": copy.deepcopy(self.approval_jobs), "total_count": len(self.approval_jobs)}
        if tail == "/actions/runs/200/approvals":
            return copy.deepcopy(self.reviews)
        if tail == "/releases/latest":
            if self.latest_override != "automatic":
                return copy.deepcopy(self.latest_override)
            public = [x for x in self.releases if not x["draft"]]
            return copy.deepcopy(public[-1]) if public else None
        if tail == "/releases":
            return copy.deepcopy(self.releases)
        if tail == "/releases/400":
            return copy.deepcopy(self.releases[-1])
        if tail == "/releases/400/assets":
            return copy.deepcopy(self.assets)
        raise AssertionError((method, path, payload))

    def download(self, path, output, *, limit=m.MAX_ARCHIVE):
        self.calls.append(("DOWNLOAD", path))
        if "/actions/artifacts/" in path:
            data = self.archives[int(path.split("/")[-2])]
        else:
            data = self.asset_data[int(path.split("/")[-1])]
        m.require(len(data) <= limit, "download exceeds size limit")
        with output.open("xb") as stream:
            stream.write(data)

    def upload(self, release_id, name, path):
        self.calls.append(("UPLOAD", name))
        asset = {"id": 500 + len(self.assets), "name": name, "state": "uploaded", "size": path.stat().st_size}
        self.assets.append(asset)
        self.asset_data[asset["id"]] = path.read_bytes()
        return copy.deepcopy(asset)

    def rewrite_candidate(self, arch, mutate=None, extra=None):
        data, files = candidate_bytes(arch, mutate, extra)
        i = m.ARCHES.index(arch)
        self.archives[300 + i] = data
        self.originals[arch] = files
        self.artifacts[i].update(size_in_bytes=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.api = FakeAPI()
        self.trusted_source = {"schema": 1, "source_sha": SHA, "clean": True, "fingerprint": FINGERPRINT, "files": SOURCE_FILES}
        # Mock the Git filesystem boundary only; candidate/run verifiers remain real.
        snapshot = patch.object(evidence, "source_snapshot", side_effect=lambda root: copy.deepcopy(self.trusted_source))
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.event = self.work / "event.json"
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REPOSITORY": m.REPOSITORY, "GITHUB_REPOSITORY_OWNER": m.OWNER,
                    "GITHUB_REF": f"refs/tags/{TAG}", "GITHUB_SHA": SHA,
                    "GITHUB_WORKFLOW_REF": f"{m.REPOSITORY}/{m.WORKFLOW}@refs/tags/{TAG}",
                    "GITHUB_ACTOR": m.OWNER, "GITHUB_TRIGGERING_ACTOR": m.OWNER,
                    "GITHUB_JOB": "publish", "GITHUB_RUN_ID": "200", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_EVENT_PATH": str(self.event)}
        self.set_event("draft")

    def set_event(self, mode):
        self.event.write_text(json.dumps({"inputs": {"candidate_run_id": str(RUN_ID), "tag": TAG,
                                                    "allow_ad_hoc": "true", "mode": mode}}))

    def prepare(self, **kwargs):
        directory = self.work / f"prepare-{len(list(self.work.iterdir()))}"
        directory.mkdir()
        return m.prepare(self.api, directory, run_id=RUN_ID, tag=kwargs.get("tag", TAG), expected_sha=SHA, allow_ad_hoc=kwargs.get("allow_ad_hoc", True))

    def execute(self, plan, mode="draft"):
        self.set_event(mode)
        directory = self.work / f"execute-{len(list(self.work.iterdir()))}"
        directory.mkdir()
        return m.execute(self.api, plan, directory, mode=mode, env=self.env)

    def writes(self):
        return [x for x in self.api.calls if x[0] in ("POST", "PATCH", "UPLOAD", "DELETE")]

    def test_dry_run_uses_real_common_verifier_and_writes_nothing(self):
        plan = self.prepare()
        self.assertEqual(len(plan["inventory"]), 7)
        self.assertFalse(self.writes())
        self.assertEqual(plan["inventory"]["candidate-arm64.json"]["path"].read_bytes(), self.api.originals["arm64"]["candidate.json"])

    def test_tag_version_must_match_trusted_package_before_any_api(self):
        with self.assertRaisesRegex(ValueError, "trusted source package.json"):
            self.prepare(tag=OTHER_TAG)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.writes())

    def test_dirty_or_different_publication_source_blocks_before_network(self):
        for change in ({"clean": False}, {"source_sha": "d" * 40}, {"fingerprint": "f" * 64}):
            with self.subTest(change=change):
                saved = copy.deepcopy(self.trusted_source)
                self.trusted_source.update(change)
                with self.assertRaisesRegex(ValueError, "source|fingerprints"):
                    self.prepare()
                self.assertFalse(self.writes())
                self.trusted_source = saved

    def test_explicit_ad_hoc_opt_in_is_required(self):
        with self.assertRaisesRegex(ValueError, "ad-hoc"):
            self.prepare(allow_ad_hoc=False)
        self.assertFalse(self.writes())

    def test_first_release_creates_only_draft_and_downloads_all_assets(self):
        plan = self.prepare()
        result = self.execute(plan)
        self.assertTrue(result["draft"])
        self.assertTrue(self.api.releases[0]["draft"])
        self.assertEqual(len(self.api.assets), 7)
        self.assertFalse(any(method == "PATCH" for method, path in self.writes()))
        downloaded = [path for method, path in self.api.calls if method == "DOWNLOAD" and "/releases/assets/" in path]
        self.assertEqual(len(downloaded), 7)
        for asset in self.api.assets:
            self.assertEqual(self.api.asset_data[asset["id"]], plan["inventory"][asset["name"]]["path"].read_bytes())

    def test_publish_happens_after_all_downloaded_hashes_and_approval_recheck(self):
        plan = self.prepare()
        result = self.execute(plan, "publish")
        self.assertFalse(result["draft"])
        operations = [method for method, path in self.api.calls]
        public = next(i for i, (method, path) in enumerate(self.api.calls) if method == "PATCH")
        self.assertEqual(sum(method == "DOWNLOAD" and "/releases/assets/" in path for method, path in self.api.calls[:public]), 7)
        self.assertGreater(public, max(i for i, x in enumerate(operations) if x == "UPLOAD"))
        self.assertFalse(self.api.releases[0]["draft"])

    def test_resume_matching_draft_never_overwrites_existing_assets(self):
        plan = self.prepare()
        self.execute(plan)
        original_count = len(self.writes())
        self.execute(self.prepare())
        self.assertEqual(len(self.writes()), original_count)

    def test_same_size_existing_draft_corruption_blocks_before_any_write(self):
        self.execute(self.prepare())
        asset = self.api.assets[0]
        self.api.asset_data[asset["id"]] = b"X" * asset["size"]
        self.api.calls.clear()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.execute(self.prepare(), "publish")
        self.assertFalse(self.writes())

    def test_same_size_server_corruption_after_upload_blocks_publish(self):
        original = self.api.upload
        def corrupt(*args):
            result = original(*args)
            self.api.asset_data[result["id"]] = b"X" * result["size"]
            return result
        self.api.upload = corrupt
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.execute(self.prepare(), "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))
        self.assertTrue(self.api.releases[0]["draft"])

    def test_unknown_or_duplicate_draft_assets_block(self):
        self.execute(self.prepare())
        for change in (lambda assets: assets.append({"id": 900, "name": "unexpected", "size": 1, "state": "uploaded"}),
                       lambda assets: assets.append(copy.deepcopy(assets[0]))):
            with self.subTest(change=change):
                saved = copy.deepcopy(self.api.assets)
                change(self.api.assets)
                self.api.calls.clear()
                with self.assertRaisesRegex(ValueError, "inventory"):
                    self.execute(self.prepare())
                self.assertFalse(self.writes())
                self.api.assets = saved

    def test_published_same_tag_or_nonmonotonic_version_never_writes(self):
        for version in (TAG, "v999.0.0", OTHER_TAG, "garbage", "v01.0.0"):
            with self.subTest(version=version):
                self.api.releases = [{"id": 400, "draft": False, "prerelease": False, "tag_name": version}]
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_first_release_404_must_agree_with_enumerated_history(self):
        self.api.latest_override = None
        self.api.releases = [{"id": 399, "draft": False, "prerelease": False, "tag_name": "v0.9.0"}]
        with self.assertRaisesRegex(ValueError, "404 contradicts"):
            self.prepare()

    def test_missing_unprotected_self_approval_or_wrong_tag_policy_blocks(self):
        changes = [lambda: setattr(self.api, "environment", None),
                   lambda: self.api.environment.update(protection_rules=[]),
                   lambda: self.api.environment["protection_rules"][0].update(reviewers=[]),
                   lambda: self.api.environment["protection_rules"][0].update(prevent_self_review=None),
                   lambda: self.api.environment.update(deployment_branch_policy=None),
                   lambda: self.api.policies[0].update(name="*"),
                   lambda: self.api.policies[0].update(type="branch"),
                   lambda: self.api.policies.append({"id": 702, "name": "dev", "type": "branch"})]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())
                self.assertFalse(any("/actions/artifacts/" in path for method, path in self.api.calls))

    def test_run_fail_stale_wrong_source_fork_pr_manual_and_skipped_native_steps(self):
        changes = [lambda: self.api.run.update(status="in_progress"), lambda: self.api.run.update(conclusion="failure"),
                   lambda: self.api.run.update(head_sha="d" * 40), lambda: self.api.run.update(event="pull_request"),
                   lambda: self.api.run.update(event="workflow_dispatch"), lambda: self.api.run.update(head_branch="dev"),
                   lambda: self.api.run["head_repository"].update(full_name="fork/CassQuery"),
                   lambda: self.api.run.update(workflow_id=99), lambda: self.api.run.update(run_attempt=2),
                   lambda: self.api.jobs.pop(), lambda: self.api.jobs[0].update(conclusion="failure")]
        for step in evidence.REQUIRED_STEPS:
            changes.append(lambda step=step: next(x for x in self.api.jobs[0]["steps"] if x["name"] == step).update(conclusion="skipped"))
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_lightweight_wrong_target_or_moved_tag_blocks(self):
        for field, value in (("tag_type", "commit"), ("tag_commit", "d" * 40)):
            with self.subTest(field=field):
                self.api = FakeAPI()
                setattr(self.api, field, value)
                with self.assertRaises(ValueError):
                    self.prepare()
        self.api = FakeAPI()
        plan = self.prepare()
        self.api.tag_sha = "e" * 40
        with self.assertRaisesRegex(ValueError, "tag moved"):
            self.execute(plan)
        self.assertFalse(self.writes())

    def test_tag_moved_during_upload_leaves_draft_unpublished(self):
        original = self.api.upload
        def move(*args):
            result = original(*args)
            self.api.tag_sha = "e" * 40
            return result
        self.api.upload = move
        with self.assertRaisesRegex(ValueError, "tag moved"):
            self.execute(self.prepare(), "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))

    def test_expired_wrong_identity_duplicate_or_corrupted_api_artifact_blocks(self):
        changes = [lambda: self.api.artifacts[0].update(expired=True),
                   lambda: self.api.artifacts[0].update(digest="sha256:" + "0" * 64),
                   lambda: self.api.artifacts[0].update(name="wrong"),
                   lambda: self.api.artifacts[0]["workflow_run"].update(head_sha="d" * 40),
                   lambda: self.api.artifacts[0]["workflow_run"].update(repository_id=99),
                   lambda: self.api.artifacts[0].update(id=self.api.artifacts[1]["id"]),
                   lambda: self.api.artifacts.pop(), lambda: self.api.artifacts.append(copy.deepcopy(self.api.artifacts[0]))]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_real_manifest_mismatch_missing_gate_corruption_and_signing_do_not_pass(self):
        def stale_report(manifest, files):
            name = evidence.GATES["preflight"]
            report = json.loads(files[name])
            report.update(source_sha="d" * 40)
            files[name] = json.dumps(report).encode()
            item = record(name, files[name])
            manifest["files"] = [item if x["path"] == name else x for x in manifest["files"]]
            manifest["gates"]["preflight"] = item
        changes = [lambda manifest, files: manifest.update(source_sha="d" * 40),
                   lambda manifest, files: manifest.update(run_attempt=2),
                   lambda manifest, files: manifest.update(version=OTHER_VERSION),
                   lambda manifest, files: manifest.update(distribution="developer-id"),
                   lambda manifest, files: manifest["gates"].pop("dmg"),
                   lambda manifest, files: files.update({manifest["assets"][0]["path"]: b"corrupt"}),
                   stale_report]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                self.api.rewrite_candidate("arm64", change)
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_unlisted_artifact_file_is_not_promoted(self):
        self.api.rewrite_candidate("arm64", extra={"dist/mac-app/unreviewed.zip": b"not reviewed"})
        with self.assertRaisesRegex(ValueError, "unlisted"):
            self.prepare()
        self.assertFalse(self.writes())

    def test_different_architecture_source_fingerprints_are_not_same_candidate(self):
        def mismatch(manifest, files):
            before_path = "evidence/source-before.json"
            before = json.loads(files[before_path])
            before["files"].append({"path": "extra-source.txt", "bytes": 1, "executable": False, "sha256": "d" * 64})
            before["fingerprint"] = hashlib.sha256(json.dumps({"source_sha": SHA, "files": before["files"]},
                                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            files[before_path] = json.dumps(before).encode()
            manifest["files"] = [record(before_path, files[before_path]) if x["path"] == before_path else x for x in manifest["files"]]
            manifest["source_fingerprint"] = before["fingerprint"]
            name = evidence.GATES["preflight"]
            report = json.loads(files[name])
            report["source_fingerprint"] = manifest["source_fingerprint"]
            report["after_fingerprint"] = manifest["source_fingerprint"]
            files[name] = json.dumps(report).encode()
            item = record(name, files[name])
            manifest["files"] = [item if x["path"] == name else x for x in manifest["files"]]
            manifest["gates"]["preflight"] = item
        self.api.rewrite_candidate("x64", mismatch)
        with self.assertRaisesRegex(ValueError, "fingerprints"):
            self.prepare()

    def test_tag_dispatch_keeps_candidate_main_push_only(self):
        plan = self.prepare()
        self.assertEqual(self.env["GITHUB_REF"], "refs/tags/" + TAG)
        self.assertEqual(self.api.current_run["head_branch"], TAG)
        self.assertEqual(self.api.run["head_branch"], "main")
        self.assertTrue(self.execute(plan)["draft"])
        self.api = FakeAPI()
        self.api.run["head_branch"] = TAG
        with self.assertRaisesRegex(ValueError, "main push"):
            self.prepare()
        self.assertFalse(self.writes())

    def test_wrong_dispatch_tag_or_main_branch_blocks_all_writes(self):
        plan = self.prepare()
        for key, value in (("GITHUB_REF", "refs/heads/main"), ("GITHUB_REF", "refs/tags/" + OTHER_TAG),
                           ("GITHUB_WORKFLOW_REF", f"{m.REPOSITORY}/{m.WORKFLOW}@refs/tags/{OTHER_TAG}")):
            with self.subTest(key=key, value=value):
                saved = self.env[key]
                self.env[key] = value
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.env[key] = saved
                self.assertFalse(self.writes())
        for head_branch in ("main", OTHER_TAG, "refs/tags/" + TAG):
            with self.subTest(server_head=head_branch):
                self.api.current_run["head_branch"] = head_branch
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.assertFalse(self.writes())

    def test_reported_tag_policy_without_reviewers_blocks_before_download_or_write(self):
        self.api.environment["protection_rules"] = [{"type": "branch_policy"}]
        with self.assertRaisesRegex(ValueError, "configured reviewers"):
            self.prepare()
        self.assertFalse(self.writes())
        self.assertFalse(any(method == "DOWNLOAD" for method, path in self.api.calls))

    def test_execute_guard_rejects_local_nonowner_wrong_branch_wrong_sha_and_rerun(self):
        plan = self.prepare()
        for key, value in (("GITHUB_ACTIONS", "false"), ("GITHUB_ACTOR", "other"), ("GITHUB_TRIGGERING_ACTOR", "other"),
                           ("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/dev"), ("GITHUB_SHA", "d" * 40),
                           ("GITHUB_JOB", "other"), ("GITHUB_RUN_ATTEMPT", "2"),
                           ("GITHUB_WORKFLOW_REF", f"{m.REPOSITORY}/{m.WORKFLOW}@refs/heads/main")):
            with self.subTest(key=key):
                saved = self.env[key]
                self.env[key] = value
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.env[key] = saved
                self.assertFalse(self.writes())

    def test_server_context_missing_stale_failed_skipped_or_self_review_blocks_write(self):
        changes = [lambda: self.api.current_run.update(head_sha="d" * 40),
                   lambda: self.api.current_run.update(workflow_id=999),
                   lambda: self.api.current_run.update(run_attempt=2),
                   lambda: self.api.approval_jobs[0].update(conclusion="failure"),
                   lambda: self.api.approval_jobs[1]["steps"][0].update(conclusion="skipped"),
                   lambda: self.api.reviews.clear(), lambda: self.api.reviews[0].update(state="rejected"),
                   lambda: self.api.reviews[0]["user"].update(login=m.OWNER),
                   lambda: self.api.reviews[0]["environments"][0].update(id=999)]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                plan = self.prepare()
                change()
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.assertFalse(self.writes())

    def test_api_errors_in_authorization_never_become_bootstrap(self):
        for endpoint in ("/releases/latest", "/releases", "/environments/releases", f"/actions/runs/{RUN_ID}"):
            for status in (401, 403, 429, 500):
                with self.subTest(endpoint=endpoint, status=status):
                    self.api = FakeAPI()
                    self.api.failures[m.PREFIX + endpoint] = f"HTTP {status}"
                    with self.assertRaises(ValueError):
                        self.prepare()
                    self.assertFalse(self.writes())

    def test_candidate_rerun_during_promotion_blocks_before_publication(self):
        plan = self.prepare()
        def change(method, path):
            if method == "POST":
                self.api.run["run_attempt"] = 2
        self.api.on_call = change
        with self.assertRaises(ValueError):
            self.execute(plan, "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))

    def test_removed_environment_after_upload_does_not_publish(self):
        plan = self.prepare()
        original = self.api.upload
        def remove(*args):
            result = original(*args)
            self.api.environment = None
            return result
        self.api.upload = remove
        with self.assertRaises(ValueError):
            self.execute(plan, "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))


class Response(io.BytesIO):
    def __init__(self, status, data=b"", headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class NetworkTests(unittest.TestCase):
    def test_only_get_explicit_404_is_missing_never_other_errors(self):
        for status in (401, 403, 429, 500, 302):
            with self.subTest(status=status):
                api = m.GitHub("secret", Opener([Response(status, b'{"message":"Not Found"}')]))
                with self.assertRaisesRegex(ValueError, f"HTTP {status}"):
                    api.json(m.PREFIX + "/releases/latest", missing=True)
        api = m.GitHub("secret", Opener([Response(404, b'{"message":"Not Found"}')]))
        self.assertIsNone(api.json(m.PREFIX + "/releases/latest", missing=True))
        for method in ("GET", "POST", "PATCH"):
            with self.subTest(method=method):
                api = m.GitHub("secret", Opener([Response(404)]))
                with self.assertRaises(ValueError):
                    api.json(m.PREFIX + "/releases", method=method, missing=method != "GET")

    def test_bad_duplicate_or_oversized_json_is_rejected(self):
        for payload in (b"not-json", b'{"id":1,"id":2}', b'{"value":NaN}', b"x" * (m.MAX_JSON + 1)):
            api = m.GitHub("secret", Opener([Response(200, payload)]))
            with self.assertRaises(ValueError):
                api.json(m.PREFIX + "/releases/latest")

    def test_redirect_drops_authorization_and_streams_exact_bytes(self):
        opener = Opener([Response(302, headers={"Location": "https://release-assets.githubusercontent.com/file?sig=private"}),
                         Response(200, b"bytes", {"Content-Length": "5"})])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "file"
            m.GitHub("secret", opener).download(m.PREFIX + "/releases/assets/5", output, limit=5)
            self.assertEqual(output.read_bytes(), b"bytes")
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer secret")
        self.assertIsNone(opener.requests[1].get_header("Authorization"))

    def test_untrusted_nested_or_plaintext_redirect_never_receives_credentials(self):
        for url in ("http://release-assets.githubusercontent.com/x", "https://evil.example/x", "https://api.github.com@evil.example/x",
                    "https://release-assets.githubusercontent.com.evil.example/x", "https://release-assets.githubusercontent.com:444/x",
                    "https://user@release-assets.githubusercontent.com/x"):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                opener = Opener([Response(302, headers={"Location": url})])
                with self.assertRaises(ValueError):
                    m.GitHub("secret", opener).download(m.PREFIX + "/releases/assets/5", Path(directory) / "file")
                self.assertEqual(len(opener.requests), 1)
        with tempfile.TemporaryDirectory() as directory:
            opener = Opener([Response(302, headers={"Location": "https://release-assets.githubusercontent.com/x"}),
                             Response(302, headers={"Location": "https://evil.example/x"})])
            with self.assertRaises(ValueError):
                m.GitHub("secret", opener).download(m.PREFIX + "/releases/assets/5", Path(directory) / "file")
            self.assertEqual(len(opener.requests), 2)

    def test_truncated_oversized_or_failed_binary_download_blocks(self):
        for response in (Response(200, b"short", {"Content-Length": "10"}), Response(200, b"too-long"),
                         Response(200, b"x", {"Content-Length": "100"}), Response(403)):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    m.GitHub("secret", Opener([response])).download(m.PREFIX + "/releases/assets/5", Path(directory) / "file", limit=6)

    def test_paginated_inventory_enumerates_second_page_and_rejects_truncation(self):
        first = [{"id": i} for i in range(100)]
        second = [{"id": 100}]
        opener = Opener([Response(200, json.dumps(first).encode()), Response(200, json.dumps(second).encode())])
        rows = m.GitHub("secret", opener).items(m.PREFIX + "/releases")
        self.assertEqual(len(rows), 101)
        self.assertIn("page=2", opener.requests[1].full_url)
        payload = {"jobs": first[:1], "total_count": 2}
        with self.assertRaisesRegex(ValueError, "truncated"):
            m.GitHub("secret", Opener([Response(200, json.dumps(payload).encode())])).items(m.PREFIX + "/actions/runs/100/jobs", "jobs")

    def test_authentication_error_body_is_not_printed_or_retried(self):
        opener = Opener([urllib.error.HTTPError("https://api.github.com/x", 403, "secret-body", {}, io.BytesIO(b"secret-body"))])
        with self.assertRaisesRegex(ValueError, "HTTP 403") as caught:
            m.GitHub("secret", opener).json(m.PREFIX + "/releases")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(opener.requests), 1)


class ArchiveTests(unittest.TestCase):
    def extract(self, entries):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "candidate.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
                for name, data in entries:
                    target.writestr(name, data)
            root = Path(directory) / "out"
            m.extract_candidate(archive, root)
            return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_safe_wrapper_preserves_exact_bytes(self):
        self.assertEqual(self.extract([("candidate.json", b"{}"), ("evidence/test.json", b"evidence")]),
                         {"candidate.json": b"{}", "evidence/test.json": b"evidence"})

    def test_traversal_absolute_backslash_controls_symlink_and_collision(self):
        paths = ["../x", "/x", "a/../../x", "a\\x", "a//x", "a/./x", "C:/x", "a\nx"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.extract([(path, b"x")])
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.extract([(link, b"../target")])
        for entries in ([("a", b"x"), ("a/b", b"x")], [("a", b"x"), ("A", b"x")]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.extract(entries)

    def test_zip_bomb_file_count_and_corrupt_archive_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "bomb"):
            self.extract([("bomb", b"x" * (2 * 1024 * 1024))])
        with patch.object(m, "MAX_FILES", 1), self.assertRaisesRegex(ValueError, "count"):
            self.extract([("a", b"a"), ("b", b"b")])
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            archive.write_bytes(b"bad")
            with self.assertRaises((m.ReleaseError, zipfile.BadZipFile)):
                m.extract_candidate(archive, Path(directory) / "out")

    def test_zip_metadata_limits_reject_before_zipinfo_allocation(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for i in range(10000):
                archive.writestr(f"entry-{i}", b"")
        raw = bytearray(buffer.getvalue())
        end = raw.rfind(b"PK\x05\x06")
        fixtures = [(bytes(raw), "file count")]
        forged_count = bytearray(raw)
        struct.pack_into("<HH", forged_count, end + 8, 1, 1)
        fixtures.append((bytes(forged_count), "actual file count"))
        huge_directory = bytearray(raw)
        struct.pack_into("<HH", huge_directory, end + 8, 1, 1)
        struct.pack_into("<L", huge_directory, end + 12, m.MAX_DIRECTORY + 1)
        fixtures.append((bytes(huge_directory), "central directory exceeds"))
        zip64 = bytearray(raw)
        struct.pack_into("<HH", zip64, end + 8, 65535, 65535)
        fixtures.append((bytes(zip64), "ZIP64"))
        for data, message in fixtures:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "wrapper.zip"
                path.write_bytes(data)
                with patch.object(zipfile.ZipFile, "_RealGetContents", side_effect=AssertionError("unbounded metadata allocation")) as allocation:
                    with self.assertRaisesRegex(ValueError, message):
                        m.extract_candidate(path, Path(directory) / "out")
                    allocation.assert_not_called()

    def test_zip64_locator_in_ordinary_entry_comment_is_rejected_before_allocation(self):
        # Ordinary EOCD claims one entry; CPython selects a hidden 10,001-entry
        # ZIP64 directory because its locator occupies EOCD-20 inside a comment.
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            for i in range(10000):
                archive.writestr(f"entry-{i}", b"")
        raw = inner.getvalue()
        end = raw.rfind(b"PK\x05\x06")
        hidden_size, hidden_offset = struct.unpack_from("<LL", raw, end + 12)
        hidden = raw[hidden_offset:hidden_offset + hidden_size]
        outer = io.BytesIO()
        entry = zipfile.ZipInfo("outer")
        entry.comment = b"X" * 76
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr(entry, hidden)
        polyglot = bytearray(outer.getvalue())
        end = polyglot.rfind(b"PK\x05\x06")
        directory_size, directory_offset = struct.unpack_from("<LL", polyglot, end + 12)
        header_size = directory_size - 76
        start = directory_offset - len(hidden)
        zip64_start = end - 76
        zip64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 10001, 10001,
                            len(hidden) + header_size, start)
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_start, 1)
        polyglot[zip64_start:end] = zip64 + locator
        self.assertEqual(struct.unpack_from("<H", polyglot, end + 10)[0], 1)
        with zipfile.ZipFile(io.BytesIO(polyglot)) as different_parser:
            self.assertEqual(len(different_parser.infolist()), 10001)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrapper.zip"
            path.write_bytes(polyglot)
            with patch.object(zipfile.ZipFile, "_RealGetContents", side_effect=AssertionError("metadata allocation")) as allocation:
                with self.assertRaisesRegex(ValueError, "ZIP64 locator"):
                    m.extract_candidate(path, Path(directory) / "out")
                allocation.assert_not_called()

    def test_zip64_entry_extra_rejected_before_zipinfo_allocation(self):
        buffer = io.BytesIO()
        entry = zipfile.ZipInfo("file")
        entry.extra = struct.pack("<HHQQ", 1, 16, 1, 1)
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(entry, b"x")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrapper.zip"
            path.write_bytes(buffer.getvalue())
            with patch.object(zipfile.ZipFile, "_RealGetContents", side_effect=AssertionError("metadata allocation")) as allocation:
                with self.assertRaisesRegex(ValueError, "ZIP64"):
                    m.extract_candidate(path, Path(directory) / "out")
                allocation.assert_not_called()


class WorkflowTests(unittest.TestCase):
    def document(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())

    def assert_workflow(self, document):
        trigger = document.get("on", document.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        inputs = trigger["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["mode"]["default"], "draft")
        self.assertFalse(inputs["allow_ad_hoc"]["default"])
        self.assertEqual(set(inputs), {"candidate_run_id", "tag", "mode", "allow_ad_hoc"})
        self.assertEqual(document["permissions"], {})
        self.assertFalse(document["concurrency"]["cancel-in-progress"])
        jobs = document["jobs"]
        self.assertEqual(set(jobs), {"prepare", "approve", "publish"})
        self.assertEqual(jobs["approve"]["needs"], "prepare")
        self.assertEqual(jobs["publish"]["needs"], ["prepare", "approve"])
        self.assertNotIn("environment", jobs["prepare"])
        self.assertEqual(jobs["approve"]["environment"], "releases")
        self.assertNotIn("environment", jobs["publish"])
        self.assertEqual(jobs["prepare"]["if"],
                         "github.repository == 'cass-ale/CassQuery' && github.ref == format('refs/tags/{0}', inputs.tag) && "
                         "startsWith(github.ref, 'refs/tags/v') && github.event_name == 'workflow_dispatch' && "
                         "github.actor == github.repository_owner && "
                         "github.triggering_actor == github.repository_owner && github.run_attempt == 1")
        for name, job in jobs.items():
            self.assertEqual(job["permissions"], {"contents": "write" if name == "publish" else "read", "actions": "read"})
            self.assertNotIn("continue-on-error", job)
            self.assertNotIn("if", job if name != "prepare" else {})
            self.assertEqual(len(job["steps"]), 2)
            checkout, step = job["steps"]
            self.assertEqual(checkout["uses"], "actions/checkout@11d5960a326750d5838078e36cf38b85af677262")
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertFalse(checkout["with"]["persist-credentials"])
            self.assertNotIn("if", step)
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("${{", step["run"])
            self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', step["run"])
            self.assertEqual("--execute" in step["run"], name == "publish")
            self.assertNotIn("build", step["run"])
            self.assertNotIn("GITHUB_OUTPUT", step["run"])
            invocation = 'python3 tools/release_publish.py --candidate-run-id "$CANDIDATE_RUN_ID" --tag "$EXISTING_TAG" --source-sha "$GITHUB_SHA"'
            if name == "publish":
                invocation += ' --mode "$RELEASE_MODE" --execute'
            invocation += ' "${flags[@]}"'
            expected = ('set -euo pipefail\n'
                        'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"\n'
                        'flags=()\n'
                        'if [ "$ALLOW_AD_HOC" = true ]; then flags+=(--allow-ad-hoc); fi\n'
                        + invocation + '\n')
            self.assertEqual(step["run"], expected)

    def test_semantic_dispatch_permissions_and_environment_chain(self):
        self.assert_workflow(self.document())

    def test_dispatch_guard_trust_predicate_and_disjunction_mutations_fail(self):
        document = self.document()
        expected = document["jobs"]["prepare"]["if"]
        mutations = ["true || " + expected, expected + " || true"]
        terms = expected.split(" && ")
        mutations.extend(" && ".join(terms[:i] + terms[i + 1:]) for i in range(len(terms)))
        for mutated in mutations:
            with self.subTest(guard=mutated):
                copy_doc = copy.deepcopy(document)
                copy_doc["jobs"]["prepare"]["if"] = mutated
                with self.assertRaises(AssertionError):
                    self.assert_workflow(copy_doc)

    def test_workflow_skip_permissions_bypass_and_noop_mutations_fail(self):
        changes = [lambda doc: doc["jobs"]["approve"]["steps"][1].update({"if": "false"}),
                   lambda doc: doc["jobs"]["approve"].update({"continue-on-error": True}),
                   lambda doc: doc["jobs"]["publish"].update(needs=["prepare"]),
                   lambda doc: doc["jobs"]["prepare"].update(environment="releases"),
                   lambda doc: doc["jobs"]["prepare"]["permissions"].update(contents="write"),
                   lambda doc: doc["jobs"]["publish"]["steps"][0]["with"].update(ref="main"),
                   lambda doc: doc["jobs"]["prepare"]["steps"][1].update(run=doc["jobs"]["prepare"]["steps"][1]["run"].replace("python3 tools/", "echo python3 tools/"))]
        for change in changes:
            with self.subTest(change=change):
                document = self.document()
                change(document)
                with self.assertRaises(AssertionError):
                    self.assert_workflow(document)


if __name__ == "__main__":
    unittest.main()
