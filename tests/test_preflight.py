import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class PreflightEvidenceTests(unittest.TestCase):
    def test_early_rejection_replaces_old_success(self):
        import sys
        sys.path.insert(0, str(ROOT / 'tools'))
        import preflight
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'preflight.json'
            output.write_text('{"pass":true}')
            with mock.patch.object(sys, 'argv', ['preflight', '--root', str(ROOT), '--output', str(output), '--require-clean']), \
                 mock.patch.object(preflight, 'source_snapshot', return_value={'clean': False}), \
                 self.assertRaises(ValueError):
                preflight.main()
            self.assertFalse(json.loads(output.read_text())['pass'])

    def test_png_real_capture_and_corruption(self):
        spec = importlib.util.spec_from_file_location('png_reader', ROOT / 'tools/png_evidence.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        image = (ROOT / 'docs/viewer.png').read_bytes()
        self.assertEqual(module.dimensions(image), (1320, 900))
        for data in (image[:33], image[:-4], image[:100] + b'X' + image[101:]):
            with self.assertRaises(ValueError):
                module.dimensions(data)
