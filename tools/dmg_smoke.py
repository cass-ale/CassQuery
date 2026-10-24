#!/usr/bin/env python3
"""Verify final DMG identity, mounted/copied launch parity and successful detach."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

from macos_smoke import (ROOT, architecture, checkpoint, file_evidence, inventory,
                         require, run_smoke, sha256, verify_bundle, write_report)


def check_launch(app, output, arch, version, baseline):
    require(inventory(app) == baseline, 'Extracted/copied bundle differs from original package')
    report = run_smoke(app, output, arch)
    identity = report['bundle']
    require(report.get('schema') == 1 and report.get('pass') is True and report.get('architecture') == architecture(arch)
            and report.get('version') == version and report.get('source_sha') == (os.environ.get('GITHUB_SHA') or None), 'Native package launch failed/identity mismatch')
    require(inventory(app) == baseline, 'Package launch mutated bundle')
    return {'bundle': identity, 'native_report': file_evidence(output / 'renderer.json'),
            'images': report['images'], 'matrix': report['matrix'], 'checked': report['checked']}


def run_package(dmg, output, arch):
    dmg, output, arch = Path(dmg).resolve(), Path(output).resolve(), architecture(arch)
    version = json.loads((ROOT / 'package.json').read_text())['version']
    output.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'pass': False, 'source_sha': os.environ.get('GITHUB_SHA') or None,
              'version': version, 'architecture': arch, 'checks': [], 'checked': [],
              'not_checked': ['Developer ID/notarization/Gatekeeper acceptance', 'human visual approval',
                              'clean/quarantined Finder launch', 'minimum macOS version'],
              'checkpoints': [checkpoint('package-start')], 'detach_attempted': False, 'detach_completed': False,
              'images': [], 'launches': {}}
    error = None
    device, mount = None, None
    scratch = Path(tempfile.mkdtemp(prefix='cq-dmg-'))
    write_report(output / 'package.json', report)
    try:
        require(dmg.is_file() and dmg.stat().st_size > 0, 'Missing/empty final DMG')
        report['asset'] = file_evidence(dmg)
        subprocess.run(['hdiutil', 'verify', str(dmg)], check=True, timeout=120)
        mount = scratch / 'mounted'
        mount.mkdir()
        # Parse exact device identity; detach this mount, never another volume.
        attached = subprocess.check_output(['hdiutil', 'attach', '-readonly', '-nobrowse', '-plist',
                                            '-mountpoint', str(mount), str(dmg)], timeout=120)
        (output / 'attach.plist').write_bytes(attached)
        data = plistlib.loads(attached)
        entities = data.get('system-entities', [])
        device = next((e.get('dev-entry') for e in entities if e.get('mount-point') == str(mount)), None)
        # Keep cleanup ownership even if a malformed attach reply omits its mount point.
        fallback_device = next((e.get('dev-entry') for e in entities if e.get('dev-entry')), None)
        device = device or fallback_device
        require(device and any(e.get('mount-point') == str(mount) for e in entities), 'Attach did not report the requested owned mount/device')
        report['mount'] = {'path': str(mount), 'device': device, 'read_only_requested': True,
                           'attach_evidence': file_evidence(output / 'attach.plist')}
        disk = subprocess.check_output(['diskutil', 'info', '-plist', str(mount)], timeout=30)
        (output / 'mount-info.plist').write_bytes(disk)
        require(plistlib.loads(disk).get('ReadOnlyVolume') is True, 'Mounted DMG volume is not read-only')
        report['mount']['read_only_verified'] = True
        report['mount']['info_evidence'] = file_evidence(output / 'mount-info.plist')
        report['checkpoints'].append(checkpoint('mounted'))
        app = mount / 'CassQuery.app'
        _, baseline = verify_bundle(app, arch, version)
        write_report(output / 'bundle-inventory.json', baseline)
        report['bundle_inventory'] = file_evidence(output / 'bundle-inventory.json')
        copied = scratch / 'copied/CassQuery.app'
        copied.parent.mkdir()
        subprocess.run(['ditto', str(app), str(copied)], check=True, timeout=120)
        require(inventory(copied) == baseline, 'DMG copy changed bundle bytes/modes/symlinks')
        for label, candidate in (('mounted', app), ('copied', copied)):
            result = check_launch(candidate, output / label, arch, version, baseline)
            report['launches'][label] = result
            report['images'].extend(result['images'])
            report['checkpoints'].append(checkpoint(f'{label}-launch-complete'))
        require(sha256(dmg) == report['asset']['sha256'], 'Final DMG bytes changed during smoke')
        report['checks'] = ['DMG verified', 'bundle ID/version/native architecture', 'bundle seal', 'mounted launch', 'copied launch']
        report['checked'] = report['checks'] + ['exact final DMG SHA256', 'all embedded Mach-O and source/icon/resource parity',
                                               'immutable mounted/copied byte-mode-symlink parity']
    except BaseException as exc:
        error = f'{type(exc).__name__}: {exc}'
    finally:
        if mount is not None:
            report['detach_attempted'] = True
            try:
                # No force/alternate mounting route: host denial remains a failed gate.
                subprocess.run(['hdiutil', 'detach', device or str(mount)], check=True, timeout=60)
                report['detach_completed'] = True
                report['checked'].append('owned DMG detached before pass commit')
                report['checkpoints'].append(checkpoint('detached'))
            except Exception as exc:
                error = '; '.join(filter(None, [error, f'Detach failed: {type(exc).__name__}: {exc}']))
        try:
            # Never traverse/delete a volume whose detach failed; retain its exact
            # owned mount path for the operator rather than attempting a bypass.
            if mount is None or report['detach_completed']:
                shutil.rmtree(scratch)
            report['temp_root_removed'] = not scratch.exists()
            if scratch.exists():
                report['retained_temp_root'] = str(scratch)
        except Exception as exc:
            error = '; '.join(filter(None, [error, f'Cleanup failed: {exc}']))
            report['temp_root_removed'] = False
        if not report.get('temp_root_removed'):
            error = '; '.join(filter(None, [error, 'Temporary root still exists']))
        report['pass'] = error is None and report['detach_completed'] and set(report['launches']) == {'mounted', 'copied'}
        if error:
            report['error'] = error
        report['visual_review'] = 'required'
        report['checkpoints'].append(checkpoint('package-complete' if report['pass'] else 'package-failed'))
        write_report(output / 'package.json', report)
    require(report['pass'], report.get('error', 'Incomplete final DMG assurance'))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dmg', required=True, type=Path)
    parser.add_argument('--arch', required=True, choices=('arm64', 'x86_64', 'x64'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run_package(args.dmg, args.output, args.arch), indent=2))


if __name__ == '__main__':
    main()
