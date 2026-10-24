"""Package trust boundaries, cleanup and stale-pass regressions (no OS mounts)."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / 'package.json').read_text())['version']
sys.path.insert(0, str(ROOT / 'tools'))
import macos_smoke as native
import dmg_smoke as dmg
import zip_smoke as zipped
import worker_smoke as worker

# Exact Intel CI attach reply at 8006753, not a fabricated mount response.
INTEL_ATTACH_PLIST = b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>system-entities</key>
\t<array>
\t\t<dict>
\t\t\t<key>content-hint</key>
\t\t\t<string>Apple_HFS</string>
\t\t\t<key>dev-entry</key>
\t\t\t<string>/dev/disk2s1</string>
\t\t\t<key>mount-point</key>
\t\t\t<string>/private/var/folders/20/jp1_0n3n7kndh6rnbqb5344m0000gn/T/cq-dmg-2kizoopy/mounted</string>
\t\t\t<key>potentially-mountable</key>
\t\t\t<true/>
\t\t\t<key>unmapped-content-hint</key>
\t\t\t<string>48465300-0000-11AA-AA11-00306543ECAC</string>
\t\t\t<key>volume-kind</key>
\t\t\t<string>hfs</string>
\t\t</dict>
\t\t<dict>
\t\t\t<key>content-hint</key>
\t\t\t<string>GUID_partition_scheme</string>
\t\t\t<key>dev-entry</key>
\t\t\t<string>/dev/disk2</string>
\t\t\t<key>potentially-mountable</key>
\t\t\t<false/>
\t\t\t<key>unmapped-content-hint</key>
\t\t\t<string>GUID_partition_scheme</string>
\t\t</dict>
\t</array>
</dict>
</plist>
'''


class PackageAssuranceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cq-package-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checked_app = self.root / 'source/CassQuery.app'
        contents = self.checked_app / 'Contents'
        contents.mkdir(parents=True)
        (contents / 'Info.plist').write_bytes(plistlib.dumps({}))
        binary = contents / 'MacOS/CassQuery'
        binary.parent.mkdir()
        binary.write_bytes(b'checked fixture binary')
        binary.chmod(0o755)
        self.baseline = native.inventory(self.checked_app)
        self.checked_file = self.root / 'checked-before.json'
        native.write_report(self.checked_file, self.baseline)
        self.checked = {'checked_app': self.checked_app, 'checked_inventory': self.checked_file}

    def test_inventory_root_mode_and_containment_precede_every_hash(self):
        original = native.inventory(self.checked_app)
        self.assertEqual(next(r for r in original['records'] if r['path'] == '.'),
                         {'path': '.', 'kind': 'directory', 'mode': self.checked_app.stat().st_mode & 0o7777})
        mode = self.checked_app.stat().st_mode & 0o7777
        self.checked_app.chmod(0o700 if mode != 0o700 else 0o755)
        self.assertNotEqual(native.inventory(self.checked_app), original)
        self.checked_app.chmod(mode)
        outside = self.root / 'public-fixture'; outside.write_bytes(b'public fixture')
        (self.checked_app / 'Contents/escape').symlink_to(outside)
        with patch.object(native, 'sha256') as hashed:
            with self.assertRaisesRegex(RuntimeError, 'symlink escapes'):
                native.verify_bundle(self.checked_app, 'arm64', VERSION)
        hashed.assert_not_called()

    def test_vulkan_alias_requires_exact_internal_relative_driver(self):
        repo, app, packed = self.fake_bundle()
        libraries = app / 'Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries'
        loader = libraries / 'libvulkan.dylib'
        with patch.object(native, 'ROOT', repo), patch.object(native, 'source_files', return_value=[]), \
             patch.object(native, 'asar_contents', return_value=packed), patch.object(native.subprocess, 'run'), \
             patch.object(native.subprocess, 'check_output', return_value='arm64'):
            native.verify_bundle(app, 'arm64', VERSION)
            loader.unlink()
            with self.assertRaisesRegex(RuntimeError, 'Vulkan discovery alias'):
                native.verify_bundle(app, 'arm64', VERSION)
            loader.write_bytes((libraries / 'libvk_swiftshader.dylib').read_bytes())
            with self.assertRaisesRegex(RuntimeError, 'Vulkan discovery alias'):
                native.verify_bundle(app, 'arm64', VERSION)
            loader.unlink(); loader.symlink_to('../Libraries/libvk_swiftshader.dylib')
            with self.assertRaisesRegex(RuntimeError, 'Vulkan discovery alias'):
                native.verify_bundle(app, 'arm64', VERSION)

    def test_checked_app_inventory_preserves_exact_bytes_and_rejects_mutation(self):
        output = self.root / 'binding'; output.mkdir()
        # JSON whitespace is not part of the semantic tree, but retained evidence
        # must preserve the exact native-before file, not regenerate its bytes.
        self.checked_file.write_text(json.dumps(self.baseline) + '\n\n')
        baseline, record = native.checked_bundle_inventory(self.checked_app, self.checked_file, output)
        self.assertEqual(baseline, self.baseline)
        self.assertEqual((output / 'checked-inventory.json').read_bytes(), self.checked_file.read_bytes())
        self.assertEqual(record['sha256'], native.sha256(self.checked_file))
        (self.checked_app / 'Contents/MacOS/CassQuery').write_bytes(b'different checked binary')
        with self.assertRaisesRegex(RuntimeError, 'changed since'):
            native.checked_bundle_inventory(self.checked_app, self.checked_file, output)

    def test_zip_mac_normalization_types_link_graph_and_cycles(self):
        bad = [
            [('CassQuery.app/Contents/caf\u00e9', b'one', stat.S_IFREG),
             ('CassQuery.app/Contents/cafe\u0301', b'two', stat.S_IFREG)],
            [('CassQuery.app/Contents/\u0390', b'one', stat.S_IFREG),
             ('CassQuery.app/Contents/\u03aa\u0301', b'two', stat.S_IFREG)],
            [('CassQuery.app/Contents/CASE', b'one', stat.S_IFREG),
             ('CassQuery.app/Contents/case', b'two', stat.S_IFREG)],
            [('CassQuery.app/Contents/file/', b'payload', stat.S_IFREG)],
            [('CassQuery.app/Contents/dir', b'', stat.S_IFDIR)],
            [('CassQuery.app/Contents/file', b'one', stat.S_IFREG),
             ('CassQuery.app/Contents/file/child', b'two', stat.S_IFREG)],
            [('CassQuery.app/Contents/deep', b'..', stat.S_IFLNK),
             ('CassQuery.app/Contents/escape', b'deep/../outside', stat.S_IFLNK)],
            [('CassQuery.app/Contents/a', b'b', stat.S_IFLNK),
             ('CassQuery.app/Contents/b', b'a', stat.S_IFLNK)],
            [('CassQuery.app/Contents/link', b'MacOS', stat.S_IFLNK),
             ('CassQuery.app/Contents/link/child', b'two', stat.S_IFREG)],
        ]
        for extras in bad:
            with self.subTest(extras=extras), self.assertRaises(RuntimeError):
                zipped.validate_archive(self.make_zip(extras))
        valid = [('CassQuery.app/Contents/a', b'b', stat.S_IFLNK),
                 ('CassQuery.app/Contents/b', b'MacOS/CassQuery', stat.S_IFLNK)]
        self.assertEqual(zipped.validate_archive(self.make_zip(valid))['entries'], 4)

    @unittest.skipUnless(sys.platform == 'darwin', 'requires actual Mac filename normalization')
    def test_greek_casefold_collision_rejected_before_real_ditto_overwrite(self):
        import subprocess
        first, second = '\u0390', '\u03aa\u0301'
        self.assertEqual(zipped.archive_key(first), zipped.archive_key(second))
        file = self.make_zip([('CassQuery.app/Contents/' + first, b'first', stat.S_IFREG | 0o644),
                              ('CassQuery.app/Contents/' + second, b'second', stat.S_IFREG | 0o644)])
        with self.assertRaisesRegex(RuntimeError, 'Duplicate/noncanonical'):
            zipped.validate_archive(file)
        # Deliberate tiny unsafe-name fixture only: independently demonstrate
        # the actual consumer collision without launching any app or mounting.
        destination = self.root / 'greek'; destination.mkdir()
        subprocess.run(['ditto', '-x', '-k', str(file), str(destination)], check=True, timeout=20)
        contents = destination / 'CassQuery.app/Contents'
        self.assertTrue(os.path.samefile(contents / first, contents / second))
        self.assertEqual((contents / first).read_bytes(), b'second')

    @unittest.skipUnless(sys.platform == 'darwin', 'requires real unprivileged Mac ditto')
    def test_real_zip_extraction_rejects_stale_app_before_any_launch(self):
        import subprocess
        for stale in (True, False):
            file = self.root / f'real-{stale}.zip'
            with zipfile.ZipFile(file, 'w') as archive:
                for relative in ('Contents/Info.plist', 'Contents/MacOS/CassQuery'):
                    source = self.checked_app / relative
                    member = zipfile.ZipInfo('CassQuery.app/' + relative)
                    member.create_system = 3
                    member.external_attr = source.stat().st_mode << 16
                    data = b'stale archive binary' if stale and relative.endswith('/CassQuery') else source.read_bytes()
                    archive.writestr(member, data)
            self.assertEqual(zipped.validate_archive(file)['entries'], 2)
            output = self.root / f'real-out-{stale}'
            apps = []
            def launch(app, directory, *_args):
                # Only archive/copy I/O is real here; no simulated native result
                # may be treated as real-device launch/signing evidence.
                self.assertEqual(native.inventory(app), self.baseline)
                apps.append(app)
                return {'native_report': {}, 'images': [], 'matrix': [], 'checked': []}
            with patch.object(zipped, 'verify_bundle', side_effect=lambda app, *_args: ({}, native.inventory(app))), \
                 patch.object(zipped, 'check_launch', side_effect=launch):
                if stale:
                    with self.assertRaisesRegex(RuntimeError, 'source-checked prepackaged'):
                        zipped.run_package(file, output, 'arm64', **self.checked)
                else:
                    result = zipped.run_package(file, output, 'arm64', **self.checked)
                    self.assertEqual(result['checked_bundle_sha256'], self.baseline['sha256'])
                    self.assertEqual((output / 'checked-inventory.json').read_bytes(), self.checked_file.read_bytes())
            self.assertEqual(len(apps), 0 if stale else 2)
            if apps:
                self.assertTrue(all(not app.exists() for app in apps))
            self.assertIs(json.loads((output / 'package.json').read_text())['pass'], not stale)

    def test_dmg_report_write_failure_allocates_no_scratch(self):
        output = self.root / 'bad-report'; (output / 'package.json').mkdir(parents=True)
        with patch.object(dmg.tempfile, 'mkdtemp') as allocated, patch.object(dmg.subprocess, 'run') as executed:
            with self.assertRaises(IsADirectoryError):
                dmg.run_package(self.root / 'missing.dmg', output, 'arm64', **self.checked)
        allocated.assert_not_called()
        executed.assert_not_called()

    def test_workflow_cleanup_removes_only_exact_regular_owned_outputs(self):
        import subprocess
        import textwrap
        text = (ROOT / '.github/workflows/macos-candidate.yml').read_text()
        code = textwrap.dedent(text.split("python - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])
        (self.root / 'package.json').write_text(json.dumps({'version': VERSION}))
        directory = self.root / 'dist/mac-app'; directory.mkdir(parents=True)
        owned = [directory / f'CassQuery-{VERSION}-mac-arm64.{ext}' for ext in ('zip', 'dmg', 'zip.blockmap', 'dmg.blockmap')]
        other = directory / f'CassQuery-{VERSION}-mac-x64.zip'; other.write_bytes(b'keep other arch')
        env = {**os.environ, 'CASSQUERY_ARCH': 'arm64'}
        for file in owned: file.write_bytes(b'old package')
        subprocess.run([sys.executable, '-c', code], cwd=self.root, env=env, check=True, timeout=20)
        self.assertTrue(all(not file.exists() for file in owned))
        self.assertEqual(other.read_bytes(), b'keep other arch')
        for file in owned[1:]: file.write_bytes(b'keep on refused cleanup')
        owned[0].symlink_to(other)
        result = subprocess.run([sys.executable, '-c', code], cwd=self.root, env=env, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(owned[0].is_symlink())
        self.assertTrue(all(file.exists() for file in owned[1:]))
        self.assertEqual(other.read_bytes(), b'keep other arch')

    def test_architecture_normalization_and_optimized_python_fail_closed(self):
        self.assertEqual(native.architecture('x86_64'), 'x64')
        self.assertEqual(native.architecture('aarch64'), 'arm64')
        for bad in ('universal', 'i386', ''):
            with self.assertRaises(RuntimeError):
                native.architecture(bad)
        with self.assertRaises(RuntimeError):
            native.require(False, 'not an optimizable assert')

    def test_bundle_inventory_detects_bytes_mode_and_symlink_changes(self):
        app = self.root / 'CassQuery.app'
        contents = app / 'Contents'
        contents.mkdir(parents=True)
        (contents / 'Info.plist').write_bytes(plistlib.dumps({}))
        binary = contents / 'binary'; binary.write_bytes(b'original'); binary.chmod(0o755)
        link = contents / 'link'; link.symlink_to('binary')
        original = native.inventory(app)
        binary.write_bytes(b'changed')
        self.assertNotEqual(native.inventory(app), original)
        binary.write_bytes(b'original'); binary.chmod(0o644)
        self.assertNotEqual(native.inventory(app), original)
        binary.chmod(0o755); self.assertEqual(native.inventory(app), original)
        link.unlink(); link.symlink_to('../../outside')
        with self.assertRaises(RuntimeError):
            native.inventory(app)

    def fake_bundle(self):
        repo = self.root / 'repo'; repo.mkdir()
        for name in ('assets/icons/cassquery.icns', 'worker.py', 'shell/main.js', 'shell/runtime.cjs', 'shell/smoke.cjs', 'shell/renderer_config.cjs'):
            file = repo / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(name.encode())
        app = self.root / 'CassQuery.app'
        resources = app / 'Contents/Resources'; resources.mkdir(parents=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.cassquery.desktop',
            'CFBundleExecutable': 'CassQuery', 'CFBundleShortVersionString': VERSION, 'CFBundleVersion': VERSION, 'CFBundleIconFile': 'icon.icns'}))
        (resources / 'icon.icns').write_bytes((repo / 'assets/icons/cassquery.icns').read_bytes())
        (resources / 'source').mkdir()
        (resources / 'source/worker.py').write_bytes((repo / 'worker.py').read_bytes())
        for name in ('Contents/MacOS/CassQuery', 'Contents/Resources/worker/cassquery-worker', 'Contents/Resources/worker/_internal/lazy-extension.so',
                     'Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/libvk_swiftshader.dylib'):
            file = app / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(bytes.fromhex('cffaedfe') + b'native'); file.chmod(0o755)
        libraries = app / 'Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries'
        (libraries / 'libvulkan.dylib').symlink_to('libvk_swiftshader.dylib')
        packed = {'package.json': json.dumps({'version': VERSION}).encode()}
        packed.update({name: (repo / name).read_bytes() for name in ('shell/main.js', 'shell/runtime.cjs', 'shell/smoke.cjs', 'shell/renderer_config.cjs')})
        return repo, app, packed

    def test_all_macho_including_lazy_dependency_architecture_is_checked(self):
        repo, app, packed = self.fake_bundle()
        observed = []
        def lipo(command, **_kwargs):
            observed.append(command[-1]); return 'x86_64' if command[-1].endswith('.so') else 'arm64'
        with patch.object(native, 'ROOT', repo), patch.object(native, 'source_files', return_value=[]), \
             patch.object(native, 'asar_contents', return_value=packed), patch.object(native.subprocess, 'run'), \
             patch.object(native.subprocess, 'check_output', side_effect=lipo):
            with self.assertRaisesRegex(RuntimeError, 'embedded Mach-O'):
                native.verify_bundle(app, 'arm64', VERSION)
        self.assertTrue(any(name.endswith('lazy-extension.so') for name in observed))
        with patch.object(native, 'ROOT', repo), patch.object(native, 'source_files', return_value=[]), \
             patch.object(native, 'asar_contents', return_value=packed), patch.object(native.subprocess, 'run'), \
             patch.object(native.subprocess, 'check_output', return_value='arm64'):
            identity, _ = native.verify_bundle(app, 'arm64', VERSION)
        self.assertEqual(len(identity['machos']), 4)
        (app / 'Contents/Resources/icon.icns').write_bytes(b'wrong')
        with patch.object(native, 'ROOT', repo), self.assertRaisesRegex(RuntimeError, 'icon'):
            native.verify_bundle(app, 'arm64', VERSION)

    def make_zip(self, extras=()):
        file = self.root / 'candidate.zip'
        with zipfile.ZipFile(file, 'w') as archive:
            archive.writestr('CassQuery.app/Contents/Info.plist', b'plist')
            archive.writestr('CassQuery.app/Contents/MacOS/CassQuery', b'native')
            for name, data, mode in extras:
                member = zipfile.ZipInfo(name); member.external_attr = mode << 16
                archive.writestr(member, data)
        return file

    def test_zip_crc_paths_symlink_and_inventory_bounds(self):
        self.assertEqual(zipped.validate_archive(self.make_zip())['entries'], 2)
        for extras in ([('../escaped', b'x', stat.S_IFREG)],
                       [('/absolute', b'x', stat.S_IFREG)],
                       [('CassQuery.app/Contents/link', b'../../../escaped', stat.S_IFLNK)],
                       [('CassQuery.app/Contents/link', b'/absolute', stat.S_IFLNK)],
                       [('CassQuery.app/Contents/fifo', b'x', stat.S_IFIFO)],
                       [('CassQuery.app/Contents/Info.plist', b'duplicate', stat.S_IFREG)]):
            with self.subTest(extras=extras), self.assertRaises(RuntimeError):
                zipped.validate_archive(self.make_zip(extras))
        file = self.make_zip([('CassQuery.app/Contents/link', b'MacOS/CassQuery', stat.S_IFLNK)])
        self.assertEqual(zipped.validate_archive(file)['entries'], 3)
        damaged = bytearray(file.read_bytes())
        offset = damaged.index(b'plist'); damaged[offset] ^= 1; file.write_bytes(damaged)
        with self.assertRaises((RuntimeError, zipfile.BadZipFile)):
            zipped.validate_archive(file)

    def test_portable_report_evidence_paths_are_relative_to_each_report(self):
        directory = self.root / 'evidence'; directory.mkdir()
        file = directory / 'renderer.json'
        native.write_report(file, {'schema': 1, 'pass': True, 'images': [{'path': str(directory / 'bundled/a.png')}],
                                  'screenshots': [str(directory / 'a.png')], 'runtime_executable': '/gone/temp/CassQuery',
                                  'native_report': {'path': str(directory / 'bundled/renderer.json')}})
        report = json.loads(file.read_text())
        self.assertEqual(report['images'][0]['path'], 'bundled/a.png')
        self.assertEqual(report['native_report']['path'], 'bundled/renderer.json')
        self.assertEqual(report['screenshots'], ['a.png'])
        self.assertEqual(report['runtime_executable'], '/gone/temp/CassQuery')

    def test_every_failed_gate_replaces_prior_pass_before_work(self):
        for tool, name, argument in ((dmg, 'dmg', 'missing.dmg'), (zipped, 'zip', 'missing.zip')):
            output = self.root / name; output.mkdir()
            (output / 'package.json').write_text('{"schema":1,"pass":true}')
            with self.assertRaises(RuntimeError):
                tool.run_package(self.root / argument, output, 'arm64', **self.checked)
            report = json.loads((output / 'package.json').read_text())
            self.assertIs(report['pass'], False); self.assertIn('error', report)
        output = self.root / 'worker.json'; output.write_text('{"schema":1,"pass":true}')
        with self.assertRaises(OSError):
            worker.run_smoke(self.root / 'missing-worker', output)
        self.assertIs(json.loads(output.read_text())['pass'], False)

    def test_actual_intel_attach_plist_selects_same_unique_canonical_entity(self):
        self.assertEqual(hashlib.sha256(INTEL_ATTACH_PLIST).hexdigest(),
                         '1304fa7f824af2e4263793df595d0981f807dd72a5e2cf258d796b94616d9cd8')
        data = plistlib.loads(INTEL_ATTACH_PLIST)
        canonical = Path(data['system-entities'][0]['mount-point']).resolve()
        self.assertEqual(dmg.mounted_device(INTEL_ATTACH_PLIST, canonical), '/dev/disk2s1')
        if sys.platform == 'darwin':
            reported_alias = INTEL_ATTACH_PLIST.replace(b'/private/var/', b'/var/', 1)
            self.assertEqual(dmg.mounted_device(reported_alias, canonical), '/dev/disk2s1')
        for case in ('foreign', 'missing-device', 'duplicate', 'invalid-device'):
            bad = copy.deepcopy(data)
            if case == 'foreign':
                bad['system-entities'][0]['mount-point'] += '-foreign'
            elif case == 'missing-device':
                bad['system-entities'][0].pop('dev-entry')
                bad['system-entities'][1]['dev-entry'] = '/dev/disk999'
            elif case == 'duplicate':
                bad['system-entities'].append(copy.deepcopy(bad['system-entities'][0]))
            else:
                bad['system-entities'][0]['dev-entry'] = '/dev/disk../bad'
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                dmg.mounted_device(plistlib.dumps(bad), canonical)

    def test_attach_selector_rejects_expected_mount_retarget(self):
        mount, foreign = self.root.resolve() / 'owned-mount', self.root.resolve() / 'foreign'
        mount.mkdir(); foreign.mkdir()
        mount.rmdir(); mount.symlink_to(foreign, target_is_directory=True)
        data = plistlib.loads(INTEL_ATTACH_PLIST)
        data['system-entities'][0]['mount-point'] = str(foreign)
        with self.assertRaisesRegex(RuntimeError, 'redirected'):
            dmg.mounted_device(plistlib.dumps(data), mount)

    def exercise_dmg(self, detach_error=False, launch_error=False, cancelled=False, attach_case=None):
        file = self.root / 'candidate.dmg'; file.write_bytes(b'final exact dmg bytes')
        output = self.root / 'dmg'; output.mkdir()
        (output / 'package.json').write_text('{"schema":1,"pass":true}')
        baseline = self.baseline
        order = []
        self.dmg_commands = []
        physical = self.root.resolve() / 'physical'
        physical.mkdir(exist_ok=True)
        owned = physical / 'scratch'; owned.mkdir()
        logical = self.root / 'logical'
        if not logical.is_symlink(): logical.symlink_to(physical, target_is_directory=True)
        def run(command, **_kwargs):
            self.dmg_commands.append(command)
            order.append(command[0] + ':' + command[1])
            if command[:2] == ['hdiutil', 'verify'] and attach_case == 'root-before-attach':
                foreign = self.root.resolve() / 'foreign'; foreign.mkdir(exist_ok=True)
                (foreign / 'keep').write_bytes(b'public negative fixture')
                owned.rename(physical / 'banked-scratch')
                owned.symlink_to(foreign, target_is_directory=True)
            if command[:2] == ['hdiutil', 'detach'] and detach_error:
                raise RuntimeError('host detach denied')
        real_mkdir = Path.mkdir
        def mkdir(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            if path == owned / 'mounted' and attach_case == 'mount-before-attach':
                foreign = self.root.resolve() / 'foreign'; real_mkdir(foreign, exist_ok=True)
                (foreign / 'keep').write_bytes(b'public negative fixture')
                path.rename(owned / 'banked-mount')
                path.symlink_to(foreign, target_is_directory=True)
        def capture(command, **_kwargs):
            self.dmg_commands.append(command)
            if command[0] == 'diskutil':
                return plistlib.dumps({'ReadOnlyVolume': True})
            mount = Path(command[command.index('-mountpoint') + 1])
            self.assertEqual(mount, owned / 'mounted')  # Canonical before OS call, not an echoed alias.
            data = plistlib.loads(INTEL_ATTACH_PLIST)
            data['system-entities'][0]['mount-point'] = str(mount)
            if attach_case == 'foreign':
                data['system-entities'][0]['mount-point'] = str(self.root.resolve() / 'foreign')
                data['system-entities'][0]['dev-entry'] = '/dev/disk999'
            elif attach_case == 'missing-device':
                data['system-entities'][0].pop('dev-entry')
                data['system-entities'][1]['dev-entry'] = '/dev/disk999'
            elif attach_case == 'duplicate':
                data['system-entities'].append(copy.deepcopy(data['system-entities'][0]))
            elif attach_case == 'retarget':
                foreign = self.root.resolve() / 'foreign'; foreign.mkdir()
                (foreign / 'keep').write_bytes(b'public negative fixture')
                mount.rmdir(); mount.symlink_to(foreign, target_is_directory=True)
                data['system-entities'][0]['mount-point'] = str(foreign)
                data['system-entities'][0]['dev-entry'] = '/dev/disk999'
            return plistlib.dumps(data)
        def verify(*_args):
            order.append('bundle-verification')
            return {}, baseline
        def launch(_app, directory, _arch, _version, _baseline):
            self.assertIs(json.loads((output / 'package.json').read_text())['pass'], False)
            order.append('launch:' + directory.name)
            if cancelled:
                raise KeyboardInterrupt('cancelled')
            if launch_error:
                raise RuntimeError('native failure')
            return {'native_report': {'path': str(directory / 'renderer.json'), 'sha256': 'a' * 64, 'bytes': 1}, 'images': [], 'matrix': [], 'checked': []}
        with patch.object(dmg.tempfile, 'mkdtemp', return_value=str(logical / 'scratch')), \
             patch.object(Path, 'mkdir', new=mkdir), \
             patch.object(dmg.subprocess, 'run', side_effect=run), patch.object(dmg.subprocess, 'check_output', side_effect=capture), \
             patch.object(dmg, 'verify_bundle', side_effect=verify), patch.object(dmg, 'inventory', return_value=baseline), \
             patch.object(dmg, 'check_launch', side_effect=launch):
            if detach_error or launch_error or cancelled or attach_case:
                with self.assertRaises(RuntimeError):
                    dmg.run_package(file, output, 'arm64', **self.checked)
            else:
                dmg.run_package(file, output, 'arm64', **self.checked)
        report = json.loads((output / 'package.json').read_text())
        if report.get('retained_temp_root'):
            retained = Path(report['retained_temp_root'])
            if retained.is_symlink(): retained.unlink()
            else: shutil.rmtree(retained)  # Mock mount only, never a real volume.
        elif owned.is_symlink():
            owned.unlink()  # Mock pre-attach root retarget, no real mount was attempted.
        return report, order

    def test_foreign_missing_duplicate_attach_never_detach_an_unproven_device(self):
        for case in ('foreign', 'missing-device', 'duplicate'):
            with self.subTest(case=case):
                report, order = self.exercise_dmg(attach_case=case)
                self.assertIs(report['pass'], False)
                self.assertNotIn('bundle-verification', order)
                self.assertFalse(any(item.startswith('launch:') for item in order))
                attached = next(c for c in self.dmg_commands if c[:2] == ['hdiutil', 'attach'])
                owned = attached[attached.index('-mountpoint') + 1]
                detached = [c for c in self.dmg_commands if c[:2] == ['hdiutil', 'detach']]
                self.assertEqual(detached, [['hdiutil', 'detach', owned]])
                self.assertFalse(any('/dev/disk999' in c for c in self.dmg_commands))
                self.assertIs(report['detach_completed'], True)  # Mock OS result, never a real mount.
                shutil.rmtree(self.root / 'dmg')

    def test_attach_retarget_refuses_detach_and_retains_owned_scratch(self):
        report, order = self.exercise_dmg(attach_case='retarget')
        self.assertIs(report['pass'], False)
        self.assertNotIn('bundle-verification', order)
        self.assertFalse(any(c[:2] == ['hdiutil', 'detach'] for c in self.dmg_commands))
        self.assertIs(report['detach_completed'], False)
        self.assertIs(report['temp_root_removed'], False)
        self.assertEqual((self.root / 'foreign/keep').read_bytes(), b'public negative fixture')

    def test_pre_attach_retarget_stops_before_mkdir_or_unowned_attach(self):
        for case in ('root-before-attach', 'mount-before-attach'):
            with self.subTest(case=case):
                report, order = self.exercise_dmg(attach_case=case)
                self.assertIs(report['pass'], False)
                self.assertNotIn('bundle-verification', order)
                self.assertFalse(any(c[:2] == ['hdiutil', 'attach'] for c in self.dmg_commands))
                self.assertFalse(any(c[:2] == ['hdiutil', 'detach'] for c in self.dmg_commands))
                self.assertFalse((self.root / 'foreign/mounted').exists())
                self.assertEqual((self.root / 'foreign/keep').read_bytes(), b'public negative fixture')
                self.assertIs(report['temp_root_removed'], False)
                self.assertEqual(report['retained_temp_root'], str(self.root.resolve() / 'physical/scratch'))
                shutil.rmtree(self.root / 'dmg')

    def test_dmg_pass_commits_only_after_detach_with_exact_asset_hash(self):
        report, order = self.exercise_dmg()
        self.assertIs(report['pass'], True); self.assertIs(report['detach_completed'], True)
        self.assertEqual(order[-1], 'hdiutil:detach')
        self.assertEqual(report['asset']['sha256'], native.sha256(self.root / 'candidate.dmg'))
        self.assertIs(report['temp_root_removed'], True)

    def test_dmg_detach_failure_retains_owned_mount_and_never_passes(self):
        report, _ = self.exercise_dmg(detach_error=True)
        self.assertIs(report['pass'], False); self.assertIs(report['detach_completed'], False)
        self.assertIs(report['temp_root_removed'], False)
        self.assertIn('host detach denied', report['error'])

    def test_dmg_launch_failure_and_cancellation_still_detach_and_fail(self):
        report, _ = self.exercise_dmg(launch_error=True)
        self.assertIs(report['pass'], False); self.assertIs(report['detach_completed'], True)
        # Separate test scratch avoids output naming collisions.
        shutil.rmtree(self.root / 'dmg')
        report, _ = self.exercise_dmg(cancelled=True)
        self.assertIs(report['pass'], False); self.assertIs(report['detach_completed'], True)

    def test_zip_extraction_copy_launch_parity_and_final_hash_fail_closed(self):
        file = self.make_zip()
        baseline = self.baseline
        for failure in (None, 'copy', 'launch', 'changed-asset'):
            output = self.root / ('zip-' + str(failure)); output.mkdir()
            labels = []
            def launch(_app, directory, *_args):
                self.assertIs(json.loads((output / 'package.json').read_text())['pass'], False)
                labels.append(directory.name)
                if failure == 'launch':
                    raise RuntimeError('native launch failed')
                if failure == 'changed-asset':
                    file.write_bytes(b'changed package after extraction')
                return {'native_report': {'path': str(directory / 'renderer.json'), 'sha256': 'a' * 64, 'bytes': 1},
                        'images': [], 'matrix': [], 'checked': []}
            with patch.object(zipped.subprocess, 'run'), patch.object(zipped, 'verify_bundle', return_value=({}, baseline)), \
                 patch.object(zipped, 'inventory', return_value=({**baseline, 'sha256': 'different'} if failure == 'copy' else baseline)), \
                 patch.object(zipped, 'check_launch', side_effect=launch):
                if failure:
                    with self.assertRaises(RuntimeError):
                        zipped.run_package(file, output, 'x86_64', **self.checked)
                else:
                    zipped.run_package(file, output, 'x86_64', **self.checked)
            report = json.loads((output / 'package.json').read_text())
            self.assertIs(report['pass'], failure is None)
            self.assertEqual(report['architecture'], 'x64')
            if failure is None:
                self.assertEqual(labels, ['extracted', 'copied'])
                self.assertIs(report['temp_root_removed'], True)
            if failure == 'copy':
                self.assertEqual(labels, [])

    def test_worker_eight_gate_names_and_statuses_are_not_a_count_check(self):
        ids = ['units', 'watertight', 'volume', 'thin-extent', 'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes']
        report = {'pass': True, 'checks': [{'id': name, 'status': 'pass'} for name in ids]}
        worker.certified_gates(report)
        for change in ('duplicate', 'failed', 'missing'):
            bad = copy.deepcopy(report)
            if change == 'duplicate': bad['checks'][0]['id'] = ids[1]
            elif change == 'failed': bad['checks'][0]['status'] = 'fail'
            else: bad['checks'].pop()
            with self.assertRaises(RuntimeError):
                worker.certified_gates(bad)


if __name__ == '__main__':
    unittest.main()
