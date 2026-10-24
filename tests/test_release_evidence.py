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
        run = {'repository': {'full_name': evidence.REPOSITORY}, 'head_repository': {'full_name': evidence.REPOSITORY}, 'head_sha': SHA,
               'event': 'push', 'head_branch': 'main', 'path': '.github/workflows/macos-candidate.yml',
               'status': 'completed', 'conclusion': 'success'}
        jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success',
                 'steps': [{'name': step, 'status': 'completed', 'conclusion': 'success'} for step in evidence.REQUIRED_STEPS]}
                for name in ('candidate (macos-15, arm64)', 'candidate (macos-15-intel, x64)')]
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
        image = next(value for name, value in files.items() if name.endswith('.png'))
        self.assertEqual(png.dimensions(image), (100, 100))
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
