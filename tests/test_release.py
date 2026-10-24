import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check', ROOT / 'tools/check_release.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseContractTests(unittest.TestCase):
    def test_current_contract(self):
        self.assertTrue(module.check()['pass'])

    def mutation(self, old, new):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in ('package.json', 'package-lock.json',
                             'packaging/electron-builder.json',
                             '.github/workflows/macos-candidate.yml',
                             'viewer/vendor/three.core.js', 'assets/icons/cassquery.icns'):
                target = root / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / filename, target)
            workflow = root / '.github/workflows/macos-candidate.yml'
            source = workflow.read_text()
            self.assertIn(old, source)
            workflow.write_text(source.replace(old, new, 1))
            with self.assertRaises(AssertionError):
                module.check(root)

    def test_skipped_native_gate_is_not_green(self):
        self.mutation('      - name: Render the actual bundled application\n',
                      '      - name: Render the actual bundled application\n        if: false\n')

    def test_ignored_failure_is_not_green(self):
        self.mutation('      - name: Package checked app\n',
                      '      - name: Package checked app\n        continue-on-error: true\n')

    def test_missing_architecture_is_not_green(self):
        self.mutation('arch: x64', 'arch: arm64')

    def test_skipped_final_dmg_gate_is_not_green(self):
        self.mutation('      - name: Verify and launch the final DMG\n',
                      '      - name: Verify and launch the final DMG\n        if: false\n')

    def test_pr_signing_cannot_be_silently_skipped(self):
        self.mutation("CSC_FOR_PULL_REQUEST: 'true'", "CSC_FOR_PULL_REQUEST: 'false'")

    def test_missing_freeze_is_not_green(self):
        self.mutation('name: Native worker freeze', 'name: optional freeze')
