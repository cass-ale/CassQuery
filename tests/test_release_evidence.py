import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_evidence', ROOT / 'tools/release_evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
SHA = 'a' * 40


def candidate_fixture(root):
    # Reuse the endpoint fixture's real bytes, not a mocked candidate verifier.
    # Its complete native report schema is explicitly synthetic, never native proof.
    import io
    import zipfile
    from test_release_publish import candidate_bytes
    archive, _ = candidate_bytes('arm64')
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(root)
    manifest = json.loads((root / 'candidate.json').read_text())
    global SHA
    SHA = manifest['source_sha']
    return manifest


class EvidenceTests(unittest.TestCase):
    def test_candidate_byte_mutations_and_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = candidate_fixture(root)
            kwargs = dict(expected_sha=SHA, expected_run_id=manifest['run_id'], expected_arch='arm64', expected_attempt=1)
            self.assertEqual(evidence.verify_candidate(root, manifest, **kwargs), manifest)
            for key, value in [('source_sha', 'c' * 40), ('run_id', 124), ('run_attempt', 2),
                               ('architecture', 'x64'), ('schema', 2), ('source_fingerprint', '')]:
                bad = copy.deepcopy(manifest)
                bad[key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    evidence.verify_candidate(root, bad, **kwargs)
            for field in ('assets', 'gates', 'files'):
                bad = copy.deepcopy(manifest)
                bad[field] = [] if field != 'gates' else {}
                with self.subTest(field=field), self.assertRaises(ValueError):
                    evidence.verify_candidate(root, bad, **kwargs)
            file = root / manifest['assets'][0]['path']
            old = file.read_bytes()
            file.write_bytes(b'X' + old[1:])
            with self.assertRaisesRegex(ValueError, 'bytes changed'):
                evidence.verify_candidate(root, manifest, **kwargs)

    def test_false_gate_report_rejected_even_when_rehashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = candidate_fixture(root)
            name = 'evidence/mac-arm64/renderer.json'
            (root / name).write_text('{"pass":false}')
            record = evidence.file_record(root, name)
            manifest['files'] = [record if item['path'] == name else item for item in manifest['files']]
            manifest['gates']['renderer'] = record
            with self.assertRaisesRegex(ValueError, 'gate did not pass'):
                evidence.verify_candidate(root, manifest, expected_sha=SHA, expected_run_id=manifest['run_id'], expected_arch='arm64')

    def test_paths_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('../escape', '/absolute', 'a/../b', 'a//b', 'a\\b'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    evidence.relative_file(root, name)
            (root / 'file').write_bytes(b'ok')
            (root / 'link').symlink_to(root / 'file')
            with self.assertRaises(ValueError):
                evidence.relative_file(root, 'link')

    def test_actual_ci_steps_not_just_green_workflow(self):
        run = {'id': 100, 'run_attempt': 1, 'repository': {'full_name': evidence.REPOSITORY}, 'head_repository': {'full_name': evidence.REPOSITORY}, 'head_sha': SHA,
               'event': 'push', 'head_branch': 'main', 'path': '.github/workflows/macos-candidate.yml',
               'status': 'completed', 'conclusion': 'success'}
        jobs = [{'id': 20 + i, 'run_id': 100, 'head_sha': SHA, 'name': name, 'status': 'completed', 'conclusion': 'success',
                 'steps': [{'name': step, 'status': 'completed', 'conclusion': 'success'} for step in evidence.REQUIRED_STEPS]}
                for i, name in enumerate(('candidate (macos-15, arm64)', 'candidate (macos-15-intel, x64)'))]
        evidence.verify_run(run, jobs, SHA)
        for conclusion in ('skipped', 'failure', 'cancelled', None):
            bad = copy.deepcopy(jobs)
            bad[0]['steps'][3]['conclusion'] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                evidence.verify_run(run, bad, SHA)
        with self.assertRaises(ValueError):
            evidence.verify_run(run, jobs[:1], SHA)
        for key, value in [('head_sha', 'c' * 40), ('event', 'pull_request'), ('head_branch', 'other')]:
            bad = {**run, key: value}
            with self.assertRaises(ValueError):
                evidence.verify_run(bad, jobs, SHA)

    def test_physical_rehashed_nested_evidence_still_fails(self):
        import io
        import zipfile
        from test_release_publish import candidate_bytes, FakeAPI
        import test_release_publish as fixtures
        def rehash(manifest, files, name):
            record = {'path': name, 'bytes': len(files[name]), 'sha256': hashlib.sha256(files[name]).hexdigest()}
            manifest['files'] = [record if item['path'] == name else item for item in manifest['files']]
            for gate, value in manifest['gates'].items():
                if value['path'] == name:
                    manifest['gates'][gate] = record
        for target, change in (
            ('evidence/preflight.json', lambda report: report.update(stages=[])),
            ('evidence/preflight.json', lambda report: report.update(clean=False)),
            ('evidence/mac-arm64/renderer.json', lambda report: report.update(images=[report['images'][0]] * 18)),
            ('evidence/mac-arm64/renderer.json', lambda report: report['matrix'][0].update(viewport=[1, 1])),
            ('evidence/worker-arm64-runtime.json', lambda report: report.update(frozen=False)),
            ('evidence/sbom.json', lambda report: report.update(components=['bad'])),
            ('evidence/sbom.json', lambda report: report['components'][0]['hashes'][0].update(content='0' * 64)),
            ('evidence/sbom.json', lambda report: next(component for component in report['components'] if component['purl'].startswith('pkg:npm/'))['properties'][0].update(value='not-the-locked-integrity')),
        ):
            def mutate(manifest, files):
                report = json.loads(files[target])
                change(report)
                files[target] = json.dumps(report).encode()
                rehash(manifest, files, target)
                if target.endswith('-runtime.json'):
                    worker_name = 'evidence/worker-arm64.json'
                    worker = json.loads(files[worker_name])
                    worker['runtime_evidence']['sha256'] = hashlib.sha256(files[target]).hexdigest()
                    worker['runtime_evidence']['bytes'] = len(files[target])
                    files[worker_name] = json.dumps(worker).encode()
                    rehash(manifest, files, worker_name)
            data, _ = candidate_bytes('arm64', mutate=mutate)
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    archive.extractall(root)
                with self.assertRaises((ValueError, TypeError, KeyError)):
                    evidence.verify_candidate(root, expected_sha=fixtures.SHA, expected_run_id=fixtures.RUN_ID, expected_arch='arm64')

    def test_report_relative_matrix_paths_refer_to_same_physical_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / 'evidence/dmg-arm64/mounted'
            child.mkdir(parents=True)
            image = child / 'frame.png'
            image.write_bytes(b'physical image bytes')
            record = {'path': 'frame.png', 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(), 'width': 100, 'height': 100}
            local = [{'theme': 'dark', 'image': record}]
            wrapper = [{'theme': 'dark', 'image': {**record, 'path': 'mounted/frame.png'}}]
            self.assertEqual(evidence.matrix_identity(root, 'evidence/dmg-arm64/mounted/renderer.json', local),
                             evidence.matrix_identity(root, 'evidence/dmg-arm64/package.json', wrapper))
            wrapper[0]['image']['sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                evidence.matrix_identity(root, 'evidence/dmg-arm64/package.json', wrapper)

    def test_png_header_only_is_not_a_render(self):
        import importlib.util
        module_spec = importlib.util.spec_from_file_location('png_reader', ROOT / 'tools/png_evidence.py')
        png = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(png)
        from test_release_publish import candidate_bytes
        data, files = candidate_bytes('arm64')
        image = files['evidence/mac-arm64/bundled/dark-450-assembled.png']
        self.assertEqual(png.dimensions(image), (450, 700))
        with self.assertRaises(ValueError):
            png.dimensions(image[:33])

    def test_source_snapshot_detects_dirty_and_untracked_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'source').write_text('one')
            subprocess.run(['git', '-C', str(root), 'add', 'source'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'base'], check=True)
            before = evidence.source_snapshot(root)
            self.assertTrue(before['clean'])
            (root / 'source').write_text('two')
            after = evidence.source_snapshot(root)
            self.assertFalse(after['clean'])
            self.assertNotEqual(before['fingerprint'], after['fingerprint'])
            (root / 'extra').write_text('new')
            self.assertNotEqual(after['fingerprint'], evidence.source_snapshot(root)['fingerprint'])


class NativeConformanceTests(unittest.TestCase):
    """Actual verifier + physical rehashed protocol evidence, never native proof."""

    def setUp(self):
        import test_release_publish as fixtures
        self.fixtures = fixtures
        _, self.files = fixtures.candidate_bytes('arm64')
        self.scratch = tempfile.TemporaryDirectory(prefix='cq-conformance-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        self.reset()
        self.verify()  # One complete positive; reset restores these exact bytes for each mutation.

    def reset(self):
        for name, data in self.files.items():
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)

    def verify(self):
        return evidence.verify_candidate(self.root, expected_sha=self.fixtures.SHA,
            expected_run_id=self.fixtures.RUN_ID, expected_attempt=1, expected_arch='arm64', expected_version=self.fixtures.VERSION)

    def report(self, name):
        return json.loads((self.root / name).read_text())

    def put(self, name, value):
        (self.root / name).write_text(json.dumps(value, indent=2) + '\n')

    def refresh(self):
        # Rehash physical linked children bottom-up, then the complete candidate.
        def links(value, directory):
            if isinstance(value, dict):
                if isinstance(value.get('path'), str) and 'sha256' in value:
                    path = directory / value['path']
                    if path.is_file() and path.resolve().is_relative_to(self.root):
                        value['sha256'] = evidence.digest(path)
                        if 'bytes' in value:
                            value['bytes'] = path.stat().st_size
                for item in value.values():
                    links(item, directory)
            elif isinstance(value, list):
                for item in value:
                    links(item, directory)
        # This helper mutates legacy native reports only. New feature fixtures include
        # intentionally opaque protected *.json bytes and exact raw API responses;
        # never parse/reserialize those unrelated proof files during a legacy mutation.
        files = [p for p in (self.root / 'evidence').rglob('*.json') if p.name != 'source-before.json'
                 and not p.relative_to(self.root / 'evidence').as_posix().startswith(('workbench-', 'update-', 'updatecheck-'))]
        for file in sorted(files, key=lambda p: (len(p.parts), p.name not in ('renderer.json', 'package.json', 'preflight.json', 'worker-arm64.json')), reverse=True):
            value = json.loads(file.read_text())
            links(value, file.parent)
            file.write_text(json.dumps(value, indent=2) + '\n')
        manifest = self.report('candidate.json')
        manifest['files'] = [evidence.file_record(self.root, name) for name in sorted(self.files) if name != 'candidate.json']
        records = {record['path']: record for record in manifest['files']}
        manifest['gates'] = {name: records[path.format(arch='arm64')] for name, path in evidence.GATES.items()}
        manifest['assets'] = [records[asset['path']] for asset in manifest['assets']]
        self.put('candidate.json', manifest)

    def test_public_quota_fallback_requires_physical_digest_and_candidate_closure(self):
        name = 'evidence/updatecheck-arm64.json'
        report = self.report(name)
        metadata_name = 'evidence/' + report['metadata']['path']
        metadata = self.report(metadata_name)
        metadata['metadata_source'] = 'public-github-release'
        for asset in metadata['assets']:
            asset['browser_download_url'] = 'https://github.com/' + evidence.REPOSITORY + '/releases/download/v0.1.0/' + asset['name']
        candidate = {'schema': 1, 'repository': evidence.REPOSITORY, 'version': '0.1.0',
                     'architecture': 'arm64', 'source_sha': 'b' * 40,
                     'assets': [{'path': 'dist/mac-app/' + a['name'], 'bytes': a['size'], 'sha256': a['digest'][7:]}
                                for a in metadata['assets'] if a['name'].endswith('.zip')]}
        candidate_bytes = json.dumps(candidate).encode()
        base = 'https://github.com/' + evidence.REPOSITORY + '/releases'
        rows = [(a['name'], a['digest'][7:]) for a in metadata['assets']]
        rows.append(('candidate-arm64.json', hashlib.sha256(candidate_bytes).hexdigest()))
        html = ''.join(f'<a href="/{evidence.REPOSITORY}/releases/download/v0.1.0/{n}"></a><clipboard-copy aria-label="Copy to clipboard digest for {n}" value="sha256:{h}"></clipboard-copy>' for n, h in rows).encode()
        checksum_name = 'evidence/' + report['checksums']['path']
        data = {'assets': html, 'candidate': candidate_bytes, 'checksums': (self.root / checksum_name).read_bytes()}
        report['public_sources'] = []
        for kind, raw in data.items():
            file = 'evidence/updatecheck-public-' + kind + ('.json' if kind == 'candidate' else '.txt')
            (self.root / file).write_bytes(raw); self.files[file] = raw
            item = evidence.file_record(self.root, file); item['path'] = Path(file).name
            endpoint = base + '/expanded_assets/v0.1.0' if kind == 'assets' else base + '/download/v0.1.0/' + ('candidate-arm64.json' if kind == 'candidate' else 'SHA256SUMS.txt')
            report['public_sources'].append({'kind': kind, 'endpoint': endpoint, **item})
        report['api_quota'] = {'status': 403, 'headers': {'x-ratelimit-remaining': '0'}}
        report['statuses'][0].update(status=403)
        self.put(metadata_name, metadata)
        report['metadata'].update(bytes=(self.root / metadata_name).stat().st_size, sha256=evidence.digest(self.root / metadata_name))
        report['metadata_bytes'] = report['metadata']['bytes']; report['metadata_sha256'] = report['metadata']['sha256']
        self.put(name, report); self.refresh(); self.verify()
        report['api_quota']['headers']['x-ratelimit-remaining'] = '1'
        self.put(name, report); self.refresh()
        with self.assertRaisesRegex(ValueError, 'quota exhaustion'): self.verify()
        report['api_quota']['headers']['x-ratelimit-remaining'] = '0'
        bad = report['public_sources'][1]
        path = self.root / 'evidence' / bad['path']; path.write_bytes(candidate_bytes + b' ')
        bad.update(bytes=path.stat().st_size, sha256=evidence.digest(path))
        self.put(name, report); self.refresh()
        with self.assertRaisesRegex(ValueError, 'candidate raw digest'): self.verify()

    def test_complete_producer_protocol_and_all_physical_images_pass(self):
        self.verify()
        import importlib.util
        spec = importlib.util.spec_from_file_location('portable_png_test', ROOT / 'tools/png_evidence.py')
        png = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(png)
        for name, data in self.files.items():
            if name.endswith('.png'):
                width, height = png.dimensions(data)
                self.assertGreaterEqual(min(width, height), 100)
                if name.startswith(('evidence/mac-', 'evidence/zip-', 'evidence/dmg-')):
                    self.assertGreaterEqual(width, 450)
                    self.assertEqual(height, 700)  # The unchanged legacy16-cell viewport.
                # New workbench/update images use their producer's realized viewport,
                # validated by the corresponding full gate, not the legacy700px rule.

    def test_actual_producer_canonical_json_preserves_complete_package_inventory(self):
        from macos_smoke import write_report
        for suffix, first in (('zip', 'extracted'), ('dmg', 'mounted')):
            name = f'evidence/{suffix}-arm64/package.json'
            report = self.report(name)
            # Producer executes first then copied, but write_report sorts object keys.
            report['launches'] = {label: report['launches'][label] for label in (first, 'copied')}
            original_images = copy.deepcopy(report['images'])
            write_report(self.root / name, report)
            stored = self.report(name)
            self.assertEqual(list(stored['launches']), ['copied', first])
            self.assertEqual(stored['images'], original_images)
            self.assertTrue(stored['images'][0]['path'].startswith(first + '/'))
            self.refresh()
            self.verify()

    def test_canonical_package_inventory_still_rejects_duplicate_missing_and_tampered_records(self):
        from macos_smoke import write_report
        name = 'evidence/zip-arm64/package.json'
        for mutation in (lambda r: r['images'].append(copy.deepcopy(r['images'][0])),
                         lambda r: r['images'].pop(),
                         lambda r: r['images'][0].update(width=r['images'][0]['width'] + 1)):
            self.reset()
            report = self.report(name)
            mutation(report)
            write_report(self.root / name, report)
            self.refresh()
            with self.assertRaises(ValueError):
                self.verify()

    def test_dmg_readonly_proof_preserves_volume_semantics_and_mac_alias(self):
        import plistlib
        name = 'evidence/dmg-arm64/mount-info.plist'
        file = self.root / name
        data = plistlib.loads(file.read_bytes())
        data.update(WritableMedia=True, Writable=True)
        data['MountPoint'] = data['MountPoint'].replace('/private/var/', '/var/', 1)
        file.write_bytes(plistlib.dumps(data, sort_keys=True))
        self.refresh()
        self.verify()

    def test_dmg_requires_requested_and_verified_proof_flags(self):
        name = 'evidence/dmg-arm64/package.json'
        for change in (lambda r: r.pop('mount'), lambda r: r['mount'].pop('read_only_verified'),
                       lambda r: r['mount'].update(read_only_verified=False),
                       lambda r: r['mount'].update(read_only_verified=1),
                       lambda r: r['mount'].update(read_only_requested=False)):
            self.reset()
            report = self.report(name)
            change(report)
            self.put(name, report)
            self.refresh()
            with self.assertRaisesRegex(ValueError, 'read-only DMG mount proof'):
                self.verify()

    def test_dmg_physical_plists_rehashed_writable_or_foreign_mounts_reject(self):
        import plistlib
        cases = [
            ('mount-info.plist', lambda data: data.update(WritableVolume=True)),
            ('mount-info.plist', lambda data: data.update(WritableVolume=0)),
            ('mount-info.plist', lambda data: data.update(WritableVolume='false')),
            ('mount-info.plist', lambda data: data.pop('WritableVolume')),
            ('mount-info.plist', lambda data: data.update(DeviceNode='/dev/disk99s1')),
            ('mount-info.plist', lambda data: data.pop('DeviceNode')),
            ('mount-info.plist', lambda data: data.update(MountPoint='/private/var/foreign/mounted')),
            ('mount-info.plist', lambda data: data.pop('MountPoint')),
            ('attach.plist', lambda data: data['system-entities'][0].update(**{'dev-entry': '/dev/disk99s1'})),
            ('attach.plist', lambda data: data['system-entities'][0].update(**{'mount-point': '/private/var/foreign/mounted'})),
            ('attach.plist', lambda data: data['system-entities'].append(copy.deepcopy(data['system-entities'][0]))),
            ('attach.plist', lambda data: data.update(**{'system-entities': [data['system-entities'][1]]})),
        ]
        for name, change in cases:
            with self.subTest(name=name, change=change):
                self.reset()
                file = self.root / 'evidence/dmg-arm64' / name
                data = plistlib.loads(file.read_bytes())
                change(data)
                file.write_bytes(plistlib.dumps(data, sort_keys=True))
                self.refresh()  # Real changed plist and every link/inventory are rehashed.
                with self.assertRaises(ValueError):
                    self.verify()

    def test_actual_raw_contract_is_shared_and_portable(self):
        report = self.report('evidence/mac-arm64/renderer.json')
        report.update(started=report['renderer_started'], finished=report['renderer_finished'], checkpoints=report['renderer_checkpoints'])
        report['images'] = [image for image in report['images'] if not image['path'].startswith('bundled/')]
        evidence.validate_native_renderer(report, expected_sha=self.fixtures.SHA, expected_arch='arm64', expected_version=self.fixtures.VERSION,
            bundled=False, started=report['launch']['started'], finished=report['launch']['finished'])
        # Pure helper neither imports Pillow nor performs a native operation.
        with mock.patch.dict('sys.modules', {'PIL': None}):
            evidence.validate_native_matrix(report['matrix'])

    def test_physically_rehashed_renderer_contradictions_fail(self):
        name = 'evidence/mac-arm64/renderer.json'
        mutations = [
            ('missing document geometry', lambda r: [row.pop(k) for row in r['matrix'] for k in ('full_size', 'scroll_regions', 'vertical_scroll')]),
            ('document not captured', lambda r: r['matrix'][0].update(full_size=[450, 900])),
            ('missing scroll controls', lambda r: r['matrix'][0].update(scroll_regions=[{'id': 'ASIDE', 'client_height': 500, 'scroll_height': 700}])),
            ('missing image links', lambda r: [row.pop('image') for row in r['matrix']]),
            ('same theme colors', lambda r: [row.update(background='same') for row in r['matrix']]),
            ('boolean execution counts', lambda r: r['matrix'][0].update(vertices=True, meshes=True, controls_checked=True)),
            ('boolean projection/canvas', lambda r: r['matrix'][0].update(clipped=False, nonfinite=False, canvas={'width': True, 'height': True, 'client_width': True, 'client_height': True})),
            ('missing drawn triangles', lambda r: r['matrix'][0].pop('triangles')),
            ('inconsistent drawn references', lambda r: r['matrix'][0].update(triangles=99)),
            ('degenerate eligible triangles', lambda r: r['matrix'][0].update(degenerate=1)),
            ('boolean degenerate count', lambda r: r['matrix'][0].update(degenerate=False)),
            ('renderer errors', lambda r: r.update(errors=['renderer crashed'])),
            ('not packaged/frozen', lambda r: r.update(packaged=False, bundled_worker=False)),
            ('not shut down', lambda r: r.update(worker_shutdown=False)),
            ('missing worker PID', lambda r: r.pop('worker_pid')),
            ('boolean app PID', lambda r: r.update(app_pid=True)),
            ('missing challenge', lambda r: r.pop('challenge')),
            ('wrong executable', lambda r: r.update(worker_executable='/usr/bin/python3')),
            ('missing clocks', lambda r: r.update(renderer_checkpoints=[])),
            ('stale clocks', lambda r: r['renderer_checkpoints'][0].update(monotonic_ms=0)),
            ('missing graphics', lambda r: r.pop('graphics')),
            ('lost WebGL context', lambda r: r['graphics']['webgl'].update(is_context_lost=True)),
            ('unobserved software backend', lambda r: r.update(render_backend_requested='swiftshader-requested')),
            ('missing camera input evidence', lambda r: r.pop('camera_checks')),
            ('unmoved orbit', lambda r: r['camera_checks'][0]['after_drag'].update(direction=r['camera_checks'][0]['before']['direction'])),
            ('unmoved pan', lambda r: r['camera_checks'][1]['after_drag'].update(target=r['camera_checks'][1]['before']['target'])),
            ('reset retained orbit at700ms', lambda r: r['camera_checks'][0]['after_reset_700'].update(direction=[1, 0, 0])),
            ('reset retained pan at3s', lambda r: r['camera_checks'][1]['after_reset_settled'].update(target=[1, 0, 0])),
            ('save used default camera', lambda r: r['camera_save']['before_save'].update(direction=r['camera_save']['before_drag']['direction'])),
            ('save stole settled camera', lambda r: r['camera_save']['after_save'].update(direction=r['camera_save']['before_drag']['direction'])),
            ('missing settled save capture', lambda r: r['camera_save'].pop('image')),
            ('missing executed source tuple', lambda r: r.pop('native_source')),
            ('invalid source fingerprint', lambda r: r['native_source'].update(fingerprint='0' * 64)),
            ('missing checks', lambda r: r.update(checks=[])),
            ('missing wrapper clocks', lambda r: r.update(checkpoints=[])),
            ('wrapper child bounds disagree', lambda r: r['started'].update(monotonic_ms=7000)),
            ('wrong worker lineage', lambda r: r['lifecycle'].update(worker_pid_after=999)),
            ('missing screenshot aliases', lambda r: r.update(screenshots=[])),
            ('save state has no geometry', lambda r: r['save_state'].update(vertices=0)),
            ('failure state has no geometry', lambda r: r['failure_state'].update(vertices=0)),
            ('failure retained wrong hash', lambda r: r['failure_state'].update(hash='different')),
        ]
        for label, change in mutations:
            with self.subTest(label=label):
                self.reset()
                report = self.report(name)
                change(report)
                self.put(name, report)
                self.refresh()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_physical_tiny_png_rehashed_top_and_package_child_fails_geometry(self):
        import random
        import struct
        import zlib
        rng = random.Random(9)
        raw = b''.join(b'\0' + rng.randbytes(300) for _ in range(100))
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
        data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 100, 100, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
        self.assertGreater(len(data), 1000)
        self.assertEqual(len(zlib.decompress(zlib.compress(raw))), 30100)
        for directory in ('evidence/mac-arm64', 'evidence/zip-arm64/extracted'):
            with self.subTest(directory=directory):
                self.reset()
                basename = 'dark-1320-assembled.png'
                (self.root / directory / basename).write_bytes(data)
                report = self.report(directory + '/renderer.json')
                row = next(row for row in report['matrix'] if row['theme'] == 'dark' and row['width'] == 1320 and row['state'] == 'assembled')
                row['image'].update(width=100, height=100)
                for image in report['images']:
                    if image['path'] == basename:
                        image.update(width=100, height=100)
                self.put(directory + '/renderer.json', report)
                if directory.endswith('/extracted'):
                    package = self.report('evidence/zip-arm64/package.json')
                    for row in package['launches']['extracted']['matrix']:
                        if row['image']['path'] == 'extracted/' + basename:
                            row['image'].update(width=100, height=100)
                    for image in package['images'] + package['launches']['extracted']['images']:
                        if image['path'] == 'extracted/' + basename:
                            image.update(width=100, height=100)
                    self.put('evidence/zip-arm64/package.json', package)
                self.refresh()
                with self.assertRaisesRegex(ValueError, 'cover viewport/full document'):
                    self.verify()

    def test_rehashed_linked_cold_runtime_inventory_and_package_contradictions_fail(self):
        mutations = [
            ('evidence/mac-arm64/bundled/renderer.json', lambda r: r.update(**{'pass': False})),
            ('evidence/mac-arm64/worker-runtime.json', lambda r: r.update(frozen=False)),
            ('evidence/mac-arm64/bundle-after.json', lambda r: r['records'][1].update(sha256='0' * 64)),
            ('evidence/zip-arm64/extracted/renderer.json', lambda r: r.update(architecture='x64', version='99.0.0')),
            ('evidence/zip-arm64/package.json', lambda r: r.update(images=[])),
            ('evidence/zip-arm64/package.json', lambda r: r['launches']['extracted'].update(images=[])),
            ('evidence/zip-arm64/package.json', lambda r: r.update(checked_bundle_sha256='0' * 64)),
            ('evidence/zip-arm64/checked-inventory.json', lambda r: r.update(sha256='0' * 64)),
            ('evidence/sbom.json', lambda r: r['components'][0].update(purl='pkg:pypi/wrong@99.0.0')),
            ('evidence/sbom.json', lambda r: next(c for c in r['components'] if c['purl'].startswith('pkg:npm/')).update(purl='pkg:npm/wrong@99.0.0')),
        ]
        for name, change in mutations:
            with self.subTest(name=name):
                self.reset()
                report = self.report(name)
                change(report)
                self.put(name, report)
                self.refresh()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_frozen_worker_missing_identity_cannot_satisfy_none_equality(self):
        for change in (lambda r: (r.pop('pid'), r.pop('challenge')),
                       lambda r: r.update(executable='/usr/bin/python3', resource_root='/external', monotonic_ms=0)):
            self.reset()
            path = 'evidence/worker-arm64-runtime.json'
            proof = self.report(path)
            change(proof)
            self.put(path, proof)
            worker = self.report('evidence/worker-arm64.json')
            worker['worker_runtime'] = proof
            if 'pid' not in proof:
                worker.pop('worker_pid')
                worker.pop('challenge')
            self.put('evidence/worker-arm64.json', worker)
            self.refresh()
            with self.assertRaises(ValueError):
                self.verify()

    def test_executed_source_tuple_is_bound_even_when_fingerprint_recomputed(self):
        report = self.report('evidence/mac-arm64/renderer.json')
        changed = copy.deepcopy(report['native_source'])
        changed['files'][-1]['sha256'] = '0' * 64
        changed['fingerprint'] = hashlib.sha256(json.dumps({'files': changed['files']}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        for name in ('evidence/mac-arm64/renderer.json', 'evidence/mac-arm64/bundled/renderer.json'):
            value = self.report(name)
            value['native_source'] = changed
            self.put(name, value)
        self.refresh()
        with self.assertRaisesRegex(ValueError, 'executed native source differs from original source'):
            self.verify()

    def test_common_job_identity_and_complete_envelopes(self):
        api = self.fixtures.FakeAPI()
        evidence.verify_run(api.run, {'jobs': api.jobs, 'total_count': 2}, self.fixtures.SHA)
        evidence.verify_integration_run(api.integration_run, {'jobs': api.integration_jobs, 'total_count': 1}, self.fixtures.SHA)
        for run, jobs, verify in ((api.run, api.jobs, evidence.verify_run),
                                  (api.integration_run, api.integration_jobs, evidence.verify_integration_run)):
            for change in (lambda rows: rows[0].update(run_id=999), lambda rows: rows[0].update(head_sha='f' * 40),
                           lambda rows: rows[0].update(id=True), lambda rows: rows[0].update(run_attempt=2),
                           lambda rows: rows[0]['steps'][0].update(conclusion='skipped')):
                rows = copy.deepcopy(jobs)
                change(rows)
                with self.assertRaises(ValueError):
                    verify(run, rows, self.fixtures.SHA)
            with self.assertRaises(ValueError):
                verify(run, {'jobs': jobs, 'total_count': len(jobs) + 1}, self.fixtures.SHA)
            with self.assertRaises(ValueError):
                verify(run, {'jobs': jobs, 'total_count': len(jobs), 'complete': False}, self.fixtures.SHA)
