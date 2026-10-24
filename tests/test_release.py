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
                             'packaging/electron-builder.json', 'packaging/python-runtime.json',
                             'packaging/wheel-manifest.json', 'packaging/requirements-macos.lock',
                             'requirements-build.txt',
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

    def test_unavailable_python_pin_is_rejected(self):
        self.mutation("python-version: '3.12.10'", "python-version: '3.12.13'")

    def test_native_python_provider_requires_both_architectures(self):
        import json
        manifest = json.loads((ROOT / 'packaging/python-runtime.json').read_text())
        module.check_python_runtime('3.12.10', {'arm64', 'x64'}, manifest)
        manifest[0]['files'] = [file for file in manifest[0]['files'] if file['arch'] == 'arm64']
        with self.assertRaisesRegex(AssertionError, 'macOS x64'):
            module.check_python_runtime('3.12.10', {'arm64', 'x64'}, manifest)

    def test_floating_python_pin_is_rejected(self):
        self.mutation("python-version: '3.12.10'", "python-version: '3.12'")

    def test_python_setup_cannot_be_skipped(self):
        self.mutation('      - uses: actions/setup-python@',
                      '      - if: false\n        uses: actions/setup-python@')

    def test_python_setup_failure_cannot_be_ignored(self):
        self.mutation('      - uses: actions/setup-python@',
                      '      - continue-on-error: true\n        uses: actions/setup-python@')

    def test_wrong_python_architecture_is_rejected(self):
        self.mutation("          python-version: '3.12.10'",
                      "          architecture: x64\n          python-version: '3.12.10'")

    def test_noop_runner_context_is_rejected(self):
        source = (ROOT / '.github/workflows/macos-candidate.yml').read_text()
        start = source.index('        run: |', source.index('name: Record runner context'))
        end = source.index('      - uses: actions/setup-node@', start)
        self.mutation(source[start:end], '        run: |\n          : # evidence/runner-context.txt\n')

    def test_context_must_precede_node_setup(self):
        source = (ROOT / '.github/workflows/macos-candidate.yml').read_text()
        start = source.index('      - name: Record runner context')
        end = source.index('      - uses: actions/setup-node@', start)
        node_end = source.index('      - uses: actions/setup-python@', end)
        self.mutation(source[start:node_end], source[end:node_end] + source[start:end])

    def test_early_diagnostics_cannot_be_removed(self):
        self.mutation('name: Record runner context', 'name: Optional diagnostics')

    def test_early_diagnostics_cannot_be_skipped(self):
        self.mutation('      - name: Record runner context\n',
                      '      - name: Record runner context\n        if: false\n')

    def test_failed_setup_cannot_skip_diagnostic_upload(self):
        self.mutation('        if: always()', '        if: success()')

    def test_each_gate_rejects_noop_command(self):
        workflow = (ROOT / '.github/workflows/macos-candidate.yml').read_text()
        for name in module.REQUIRED_COMMANDS:
            start = workflow.index('        run:', workflow.index('name: ' + name))
            end = workflow.find('      - ', start)
            if end == -1:
                end = len(workflow)
            with self.subTest(gate=name):
                self.mutation(workflow[start:end], '        run: echo green\n')

    def test_forced_swiftshader_cannot_replace_native_metal(self):
        self.mutation("CASSQUERY_ANGLE: 'metal'", "CASSQUERY_SOFTWARE_GL: '1'")

    def test_job_level_skip_is_rejected(self):
        self.mutation('  candidate:\n', '  candidate:\n    if: false\n')

    def test_moving_checkout_ref_is_rejected(self):
        self.mutation('ref: ${{ github.sha }}', 'ref: main')

    def test_missing_freeze_is_not_green(self):
        self.mutation('name: Native worker freeze', 'name: optional freeze')
