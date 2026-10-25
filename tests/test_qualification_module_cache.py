"""Trusted-local producer compilation cache controls; no native qualification claim."""
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import release_evidence as evidence


class QualificationLoaderTests(unittest.TestCase):
    def setUp(self):
        evidence._compile_qualification_module.cache_clear()
        self.addCleanup(evidence._compile_qualification_module.cache_clear)
        self.temp = tempfile.TemporaryDirectory(prefix='trusted-module-source-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'workbench_native_smoke.py'
        self.path.write_bytes(b'MARKER = "a"\n')
        (self.root / 'update_smoke.py').write_bytes(b'MARKER = "update"\n')
        self.location = patch.object(evidence, '__file__', str(self.root / 'release_evidence.py'))
        self.location.start(); self.addCleanup(self.location.stop)

    def test_exact_source_is_reused_but_actual_local_bytes_are_read_each_time(self):
        with patch.object(evidence.os, 'open', wraps=os.open) as opened:
            first = evidence.load_qualification_module('workbench')
            second = evidence.load_qualification_module('workbench')
        self.assertIs(first, second)
        self.assertEqual(opened.call_count, 2)
        info = evidence._compile_qualification_module.cache_info()
        self.assertEqual((info.hits, info.misses, info.maxsize), (1, 1, 2))

    def test_same_size_preserved_mtime_source_change_recompiles_exact_new_bytes(self):
        first = evidence.load_qualification_module('workbench')
        before = self.path.stat()
        self.path.write_bytes(b'MARKER = "b"\n')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = evidence.load_qualification_module('workbench')
        self.assertIsNot(first, second)
        self.assertEqual(second.MARKER, 'b')
        self.assertEqual(self.path.stat().st_size, before.st_size)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_compiler_executes_captured_bytes_not_a_later_file_read_or_pyc(self):
        captured = self.path.read_bytes()
        sha = hashlib.sha256(captured).hexdigest()
        self.path.write_bytes(b'raise AssertionError("should not execute reopened source")\n')
        module = evidence._compile_qualification_module(str(self.path), sha, captured)
        self.assertEqual(module.MARKER, 'a')

    def test_missing_symlink_oversized_and_unknown_paths_do_not_reuse_warm_source(self):
        evidence.load_qualification_module('workbench')
        self.path.unlink()
        with self.assertRaises((OSError, ValueError)):
            evidence.load_qualification_module('workbench')
        outside = self.root / 'other.py'; outside.write_bytes(b'MARKER = "a"\n')
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError):
            evidence.load_qualification_module('workbench')
        self.path.unlink(); self.path.write_bytes(b'#' * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            evidence.load_qualification_module('workbench')
        for name in ('../other.py', str(outside), 'renderer'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                evidence.load_qualification_module(name)

    def test_failed_new_code_does_not_fall_back_and_cache_retains_at_most_two_modules(self):
        evidence.load_qualification_module('workbench')
        self.path.write_bytes(b'raise RuntimeError("new trusted source is broken")\n')
        with self.assertRaisesRegex(RuntimeError, 'new trusted source'):
            evidence.load_qualification_module('workbench')
        for marker in ('b', 'c', 'd'):
            self.path.write_bytes(f'MARKER = "{marker}"\n'.encode())
            self.assertEqual(evidence.load_qualification_module('workbench').MARKER, marker)
        evidence.load_qualification_module('update')
        self.assertEqual(evidence._compile_qualification_module.cache_info().currsize, 2)


class CachedAdmissionTests(unittest.TestCase):
    def test_warm_module_reuses_only_pixel_analysis_and_still_verifies_every_file(self):
        import test_release_publish as fixtures
        evidence._compile_qualification_module.cache_clear()
        self.addCleanup(evidence._compile_qualification_module.cache_clear)
        archive, _ = fixtures.candidate_bytes('arm64')
        with tempfile.TemporaryDirectory(prefix='cached-admission-PROTOCOL-') as directory:
            root = Path(directory).resolve(); zipfile.ZipFile(io.BytesIO(archive)).extractall(root)
            kwargs = dict(expected_sha=fixtures.SHA, expected_run_id=fixtures.RUN_ID,
                          expected_arch='arm64', expected_attempt=1, expected_version=fixtures.VERSION)
            with patch.object(evidence, 'file_record', wraps=evidence.file_record) as file_records:
                evidence.verify_candidate(root, **kwargs)
                first_count = file_records.call_count
                module = evidence.load_qualification_module('workbench')
                first_pixel = module._canvas_pixels.cache_info()
                evidence.verify_candidate(root, **kwargs)
                second_count = file_records.call_count - first_count
                self.assertEqual(second_count, first_count)
                self.assertGreater(first_count, 1000)
                self.assertIs(module, evidence.load_qualification_module('workbench'))
                second_pixel = module._canvas_pixels.cache_info()
                self.assertEqual(second_pixel.misses, first_pixel.misses)
                self.assertGreater(second_pixel.hits, first_pixel.hits)
                file = root / 'evidence/source-before.json'
                raw = file.read_bytes(); file.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
                with self.assertRaises(ValueError):
                    evidence.verify_candidate(root, **kwargs)
                file.write_bytes(raw)
                evidence.verify_candidate(root, **kwargs)

    def test_shared_imports_and_per_call_validator_state_remain_live(self):
        evidence._compile_qualification_module.cache_clear()
        self.addCleanup(evidence._compile_qualification_module.cache_clear)
        module = evidence.load_qualification_module('workbench')
        # The helper is imported at validation time: a warm producer does not cache
        # a report result or suppress a current shared helper failure.
        with patch.object(evidence, 'validate_preload_proof', side_effect=ValueError('fresh shared check')) as current:
            import test_workbench_contract as models
            with tempfile.TemporaryDirectory(prefix='module-shared-check-PROTOCOL-') as directory:
                root = Path(directory).resolve(); report = models.native_qualification_model(root)
                with self.assertRaisesRegex(ValueError, 'fresh shared check'):
                    module.validate_qualification(report, root, expected_sha=report['source_sha'], expected_arch=report['architecture'])
            current.assert_called_once()


if __name__ == '__main__':
    unittest.main()
