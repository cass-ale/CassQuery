#!/usr/bin/env python3
"""Verify final DMG identity, mounted/copied launch parity and successful detach."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile

from macos_smoke import (ROOT, architecture, checked_bundle_inventory, checkpoint,
                         file_evidence, inventory, require, run_smoke, sha256,
                         verify_bundle, write_report)


def check_launch(app, output, arch, version, baseline):
    require(inventory(app) == baseline, 'Extracted/copied bundle differs from original package')
    report = run_smoke(app, output, arch)
    identity = report['bundle']
    require(report.get('schema') == 1 and report.get('pass') is True and report.get('architecture') == architecture(arch)
            and report.get('version') == version and report.get('source_sha') == (os.environ.get('GITHUB_SHA') or None), 'Native package launch failed/identity mismatch')
    require(inventory(app) == baseline, 'Package launch mutated bundle')
    return {'bundle': identity, 'native_report': file_evidence(output / 'renderer.json'),
            'images': report['images'], 'matrix': report['matrix'], 'checked': report['checked']}


def mounted_device(attached, mount):
    """Select only the device reported for this exact, physically owned mount."""
    entities = plistlib.loads(attached).get('system-entities', [])
    require(isinstance(entities, list) and all(isinstance(entity, dict) for entity in entities), 'Invalid attach device inventory')
    owned = Path(mount)
    require(owned.is_absolute() and not owned.is_symlink() and owned.resolve() == owned,
            'Owned mount path was redirected after attach')
    matches = [entity for entity in entities if isinstance(entity.get('mount-point'), str)
               and Path(entity['mount-point']).is_absolute() and Path(entity['mount-point']).resolve() == owned]
    require(len(matches) == 1, 'Attach did not report exactly one requested owned mount')
    device = matches[0].get('dev-entry')
    require(isinstance(device, str) and re.fullmatch(r'/dev/disk\d+(?:s\d+)*', device), 'Attach owned mount is missing a valid device')
    return device


def validate_readonly_mount(data, mount, device):
    """Bind diskutil's actual writable flags to the selected owned device/path."""
    require(isinstance(data, dict), 'Invalid diskutil mount info')
    owned = Path(mount)
    require(owned.is_absolute() and not owned.is_symlink() and owned.resolve() == owned,
            'Owned mount path redirected during read-only verification')
    require(isinstance(device, str) and device and isinstance(data.get('DeviceNode'), str)
            and data['DeviceNode'] == device, 'Read-only mount info device differs from owned device')
    point = data.get('MountPoint')
    require(isinstance(point, str) and Path(point).is_absolute() and Path(point).resolve() == owned,
            'Read-only mount info path differs from owned mount')
    require(data.get('WritableVolume') is False, 'Mounted DMG volume is not verifiably read-only')
    # Writable/WritableMedia describe other layers and may remain true for a
    # read-only volume. Retain raw info, but do not infer volume state from them.


def run_package(dmg, output, arch, *, checked_app, checked_inventory):
    dmg, output, arch = Path(dmg).resolve(), Path(output).resolve(), architecture(arch)
    checked_app, checked_inventory = Path(checked_app), Path(checked_inventory)
    require(not output.is_relative_to(checked_app.resolve()), 'Evidence must be outside checked app')
    version = json.loads((ROOT / 'package.json').read_text())['version']
    output.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'pass': False, 'source_sha': os.environ.get('GITHUB_SHA') or None,
              'version': version, 'architecture': arch, 'checks': [], 'checked': [],
              'not_checked': ['Developer ID/notarization/Gatekeeper acceptance', 'human visual approval',
                              'clean/quarantined Finder launch', 'minimum macOS version'],
              'checkpoints': [checkpoint('package-start')], 'detach_attempted': False, 'detach_completed': False,
              'images': [], 'launches': {}}
    error = None
    device, mount, scratch = None, None, None
    write_report(output / 'package.json', report)
    try:
        require(dmg.is_file() and dmg.stat().st_size > 0, 'Missing/empty final DMG')
        report['asset'] = file_evidence(dmg)
        baseline, report['checked_inventory'] = checked_bundle_inventory(checked_app, checked_inventory, output)
        report['checked_bundle_sha256'] = baseline['sha256']
        # mkdtemp may spell the Mac /var alias while hdiutil reports /private/var.
        # Bind the physical owned root before creating/passing its mount path.
        scratch = Path(tempfile.mkdtemp(prefix='cq-dmg-')).resolve()
        subprocess.run(['hdiutil', 'verify', str(dmg)], check=True, timeout=120)
        require(not scratch.is_symlink() and scratch.resolve() == scratch, 'Owned scratch root redirected before mount creation')
        mount = scratch / 'mounted'
        mount.mkdir()
        require(not mount.is_symlink() and mount.resolve() == mount, 'Owned mount path redirected before attach')
        # Parse exact device identity; detach this mount, never another volume.
        attached = subprocess.check_output(['hdiutil', 'attach', '-readonly', '-nobrowse', '-plist',
                                            '-mountpoint', str(mount), str(dmg)], timeout=120)
        (output / 'attach.plist').write_bytes(attached)
        # Never borrow a container/foreign device when the owned entity is absent.
        # On parse/identity failure cleanup falls back only to our owned mount path.
        device = mounted_device(attached, mount)
        report['mount'] = {'path': str(mount), 'device': device, 'read_only_requested': True,
                           'attach_evidence': file_evidence(output / 'attach.plist')}
        disk = subprocess.check_output(['diskutil', 'info', '-plist', str(mount)], timeout=30)
        (output / 'mount-info.plist').write_bytes(disk)
        report['mount']['info_evidence'] = file_evidence(output / 'mount-info.plist')
        validate_readonly_mount(plistlib.loads(disk), mount, device)
        report['mount']['read_only_verified'] = True
        report['checkpoints'].append(checkpoint('mounted'))
        app = mount / 'CassQuery.app'
        _, mounted_inventory = verify_bundle(app, arch, version)
        require(mounted_inventory == baseline, 'DMG app differs from source-checked prepackaged inventory')
        write_report(output / 'bundle-inventory.json', mounted_inventory)
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
        require(inventory(checked_app) == baseline and sha256(checked_inventory) == report['checked_inventory']['sha256'], 'Source-checked app/inventory changed during package smoke')
        require(sha256(dmg) == report['asset']['sha256'], 'Final DMG bytes changed during smoke')
        report['checks'] = ['DMG verified', 'bundle ID/version/native architecture', 'bundle seal', 'mounted launch', 'copied launch']
        report['checked'] = report['checks'] + ['exact final DMG SHA256', 'source-checked prepackaged inventory parity',
                                               'all embedded Mach-O and source/icon/resource parity',
                                               'immutable mounted/copied byte-mode-symlink parity']
    except BaseException as exc:
        error = f'{type(exc).__name__}: {exc}'
    finally:
        if mount is not None:
            report['detach_attempted'] = True
            try:
                # No force/alternate route or cleanup through a retargeted link.
                require(not mount.is_symlink() and mount.resolve() == mount, 'Owned mount path redirected; refusing detach')
                subprocess.run(['hdiutil', 'detach', device or str(mount)], check=True, timeout=60)
                report['detach_completed'] = True
                report['checked'].append('owned DMG detached before pass commit')
                report['checkpoints'].append(checkpoint('detached'))
            except Exception as exc:
                error = '; '.join(filter(None, [error, f'Detach failed: {type(exc).__name__}: {exc}']))
        try:
            # Never traverse/delete a volume whose detach failed; retain its exact
            # owned mount path for the operator rather than attempting a bypass.
            if scratch is not None and (mount is None or report['detach_completed']):
                require(not scratch.is_symlink() and scratch.resolve() == scratch, 'Owned scratch root redirected; refusing cleanup')
                shutil.rmtree(scratch)
            report['temp_root_removed'] = scratch is None or not scratch.exists()
            if scratch is not None and scratch.exists():
                report['retained_temp_root'] = str(scratch)
        except Exception as exc:
            error = '; '.join(filter(None, [error, f'Cleanup failed: {exc}']))
            report['temp_root_removed'] = False
            if scratch is not None:
                report['retained_temp_root'] = str(scratch)
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
    parser.add_argument('--checked-app', required=True, type=Path)
    parser.add_argument('--checked-inventory', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run_package(args.dmg, args.output, args.arch,
                                 checked_app=args.checked_app, checked_inventory=args.checked_inventory), indent=2))


if __name__ == '__main__':
    main()
