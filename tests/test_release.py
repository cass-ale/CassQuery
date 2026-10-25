import importlib.util
import json
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

    def copy_inputs(self, root):
        sources = json.loads((ROOT / 'package.json').read_text())['cassquerySources']
        for filename in ('package.json', 'package-lock.json',
                         'packaging/electron-builder.json', 'packaging/python-runtime.json',
                         'packaging/wheel-manifest.json', 'packaging/requirements-macos.lock', 'packaging/worker.spec',
                         'requirements-build.txt', '.github/workflows/macos-candidate.yml', '.github/workflows/integration.yml',
                         'viewer/vendor/three.core.js', 'assets/icons/cassquery.icns',
                         *sources['shell'], *sources['python']):
            target = root / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / filename, target)
        self.assertTrue(module.check(root)['pass'])  # Mutations must not pass due to missing prerequisites.

    def mutation(self, old, new):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.copy_inputs(root)
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

    def test_explicit_packaged_software_renderer_is_required(self):
        self.mutation("CASSQUERY_ANGLE: 'swiftshader'", "CASSQUERY_ANGLE: 'metal'")

    def test_whole_candidate_budget_is_explicit_and_bounded(self):
        for value in ('60', '0', '360'):
            with self.subTest(value=value):
                self.mutation('timeout-minutes: 90', 'timeout-minutes: ' + value)
        self.mutation('    timeout-minutes: 90\n', '')

    def test_job_level_skip_is_rejected(self):
        self.mutation('  candidate:\n', '  candidate:\n    if: false\n')

    def test_moving_checkout_ref_is_rejected(self):
        self.mutation('ref: ${{ github.sha }}', 'ref: main')

    def test_native_features_precede_network_transport_and_candidate_staging(self):
        import yaml
        document = yaml.safe_load((ROOT / '.github/workflows/macos-candidate.yml').read_text())
        names = [step.get('name') for step in document['jobs']['candidate']['steps']]
        gates = ['Native workbench editor and file dialogs', 'Native update installation and rollback',
                 'Verify GitHub update channel transport', 'Candidate identity and hashes']
        self.assertEqual([name for name in names if name in gates], gates)
        self.assertTrue(module.check()['pass'])

    def test_network_transport_before_native_features_is_rejected(self):
        source = (ROOT / '.github/workflows/macos-candidate.yml').read_text()
        start = source.index('      - name: Native workbench editor and file dialogs\n')
        network = source.index('      - name: Verify GitHub update channel transport\n')
        stage = source.index('      - name: Candidate identity and hashes\n')
        self.mutation(source[start:stage], source[network:stage] + source[start:network])

    def test_network_transport_after_candidate_staging_is_rejected(self):
        source = (ROOT / '.github/workflows/macos-candidate.yml').read_text()
        network = source.index('      - name: Verify GitHub update channel transport\n')
        stage = source.index('      - name: Candidate identity and hashes\n')
        diagnostics = source.index('      - name: Retain failure diagnostics\n')
        self.mutation(source[network:diagnostics], source[stage:diagnostics] + source[network:stage])

    def test_network_gate_remains_mandatory_and_cannot_fail_open(self):
        gate = '      - name: Verify GitHub update channel transport\n'
        for bypass in ('        if: false\n', '        continue-on-error: true\n'):
            with self.subTest(bypass=bypass):
                self.mutation(gate, gate + bypass)
        self.mutation(gate, '      - name: Optional network check\n')

    def test_missing_freeze_is_not_green(self):
        self.mutation('name: Native worker freeze', 'name: optional freeze')
