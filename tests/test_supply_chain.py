import copy
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SupplyChainTests(unittest.TestCase):
    def test_all_locked_versions_have_exact_wheel_hashes(self):
        import hashlib
        manifest = json.loads((ROOT / 'packaging/wheel-manifest.json').read_text())
        self.assertEqual(manifest['requirements_sha256'], hashlib.sha256((ROOT / 'requirements-build.txt').read_bytes()).hexdigest())
        lock = (ROOT / 'packaging/requirements-macos.lock').read_text()
        packages = [line for line in (ROOT / 'requirements-build.txt').read_text().splitlines() if line and not line.startswith('#')]
        self.assertEqual(len(packages), 51)
        for package in packages:
            name, version = package.split('==')
            name = re.sub(r'[-_.]+', '-', name).lower()
            wheels = [file for file in manifest['wheels'] if file['name'] == name]
            self.assertTrue(wheels, name)
            self.assertIn(f'{name}=={version}', lock)
            for file in wheels:
                self.assertEqual(file['version'], version)
                self.assertIn('--hash=sha256:' + file['sha256'], lock)
                self.assertRegex(file['sha256'], r'^[0-9a-f]{64}$')
                self.assertTrue(file['url'].startswith('https://files.pythonhosted.org/'))

    def test_architecture_tag_compatibility(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('wheel_validator', ROOT / 'tools/wheel_lock.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.compatible('pyinstaller-6.20.0-py3-none-macosx_10_13_universal2.whl', 'arm64'))
        for name in ('pkg-1-cp314-abi3-macosx_14_0_arm64.whl', 'pkg-1-cp313-none-any.whl', 'pkg-1-py2-none-any.whl'):
            self.assertFalse(module.compatible(name, 'arm64'), name)

    def test_missing_intel_wheel_and_extra_package_rejected(self):
        import importlib.util
        import tempfile
        import shutil
        spec = importlib.util.spec_from_file_location('wheel_validator', ROOT / 'tools/wheel_lock.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'packaging').mkdir()
            for name in ('requirements-build.txt', 'packaging/requirements-macos.lock', 'packaging/wheel-manifest.json'):
                shutil.copyfile(ROOT / name, root / name)
            module.validate(root)
            manifest = json.loads((root / 'packaging/wheel-manifest.json').read_text())
            removed = next(file for file in manifest['wheels'] if file['name'] == 'cadquery-ocp' and 'x86_64' in file['filename'])
            manifest['wheels'].remove(removed)
            (root / 'packaging/wheel-manifest.json').write_text(json.dumps(manifest))
            lock = (root / 'packaging/requirements-macos.lock').read_text().replace(' \\\n    --hash=sha256:' + removed['sha256'], '').replace('--hash=sha256:' + removed['sha256'] + ' \\\n    ', '')
            (root / 'packaging/requirements-macos.lock').write_text(lock)
            with self.assertRaises(ValueError):
                module.validate(root)
            shutil.copyfile(ROOT / 'packaging/wheel-manifest.json', root / 'packaging/wheel-manifest.json')
            (root / 'packaging/requirements-macos.lock').write_text((ROOT / 'packaging/requirements-macos.lock').read_text() + 'unreviewed==1.0 --hash=sha256:' + 'a' * 64 + '\n')
            with self.assertRaises(ValueError):
                module.validate(root)

    def test_only_native_or_universal_python_wheels_recorded(self):
        manifest = json.loads((ROOT / 'packaging/wheel-manifest.json').read_text())
        for file in manifest['wheels']:
            name = file['filename']
            self.assertTrue(name.endswith('.whl'))
            self.assertTrue('macosx_' in name or name.endswith('-none-any.whl'), name)
            if 'macosx_' in name:
                self.assertTrue(any(arch in name for arch in ('arm64', 'x86_64', 'universal2')), name)
