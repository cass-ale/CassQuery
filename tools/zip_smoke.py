#!/usr/bin/env python3
"""Verify final ZIP extraction/copy/native-launch parity without trusting archive paths."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import subprocess
import tempfile
import zipfile

from macos_smoke import (ROOT, architecture, checkpoint, file_evidence, inventory,
                         require, sha256, verify_bundle, write_report)
from dmg_smoke import check_launch


def validate_archive(file):
    with zipfile.ZipFile(file) as archive:
        members = archive.infolist()
        require(0 < len(members) <= 10000 and sum(m.file_size for m in members) <= 4 * 1024**3, 'ZIP exceeds reviewed app inventory bounds')
        seen, links = set(), {}
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            require(name and not path.is_absolute() and '..' not in path.parts and '\\' not in name
                    and ':' not in name and path.parts[0] in ('CassQuery.app', '__MACOSX'), 'Unsafe ZIP member path')
            canonical = path.as_posix()
            require(name.rstrip('/') == canonical and canonical.casefold() not in seen, 'Duplicate/noncanonical ZIP member')
            seen.add(canonical.casefold())
            mode = member.external_attr >> 16
            require(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK), 'Unsupported ZIP entry type')
            if stat.S_ISLNK(mode):
                require(member.file_size <= 4096, 'Oversized ZIP symlink')
                target = archive.read(member).decode('utf-8')
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                require(target and not target.startswith('/') and '\\' not in target and ':' not in target
                        and (resolved == 'CassQuery.app' or resolved.startswith('CassQuery.app/')), 'ZIP symlink escapes app')
                links[canonical.casefold()] = target
        for member in members:
            parent = PurePosixPath(member.filename).parent
            require(not any(p.as_posix().casefold() in links for p in (parent, *parent.parents)), 'ZIP writes through a symlink ancestor')
        require('cassquery.app/contents/info.plist' in seen and 'cassquery.app/contents/macos/cassquery' in seen, 'ZIP lacks required app')
        require(archive.testzip() is None, 'ZIP CRC/integrity failure')
        return {'entries': len(members), 'uncompressed_bytes': sum(m.file_size for m in members)}


def run_package(file, output, arch):
    file, output, arch = Path(file).resolve(), Path(output).resolve(), architecture(arch)
    version = json.loads((ROOT / 'package.json').read_text())['version']
    output.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'pass': False, 'source_sha': os.environ.get('GITHUB_SHA') or None,
              'version': version, 'architecture': arch, 'checks': [], 'checked': [],
              'not_checked': ['Developer ID/notarization/Gatekeeper acceptance', 'human visual approval',
                              'clean/quarantined Finder launch', 'minimum macOS version'],
              'checkpoints': [checkpoint('package-start')], 'images': [], 'launches': {}}
    write_report(output / 'package.json', report)
    try:
        require(file.is_file() and file.stat().st_size > 0, 'Missing/empty final ZIP')
        report['asset'] = file_evidence(file)
        report['archive'] = validate_archive(file)
        with tempfile.TemporaryDirectory(prefix='cq-zip-') as scratch:
            extracted = Path(scratch) / 'extracted'
            extracted.mkdir()
            subprocess.run(['ditto', '-x', '-k', str(file), str(extracted)], check=True, timeout=120)
            app = extracted / 'CassQuery.app'
            _, baseline = verify_bundle(app, arch, version)
            write_report(output / 'bundle-inventory.json', baseline)
            report['bundle_inventory'] = file_evidence(output / 'bundle-inventory.json')
            copied = Path(scratch) / 'copied/CassQuery.app'
            copied.parent.mkdir()
            subprocess.run(['ditto', str(app), str(copied)], check=True, timeout=120)
            require(inventory(copied) == baseline, 'ZIP copy changed bundle bytes/modes/symlinks')
            for label, candidate in (('extracted', app), ('copied', copied)):
                result = check_launch(candidate, output / label, arch, version, baseline)
                report['launches'][label] = result
                report['images'].extend(result['images'])
                report['checkpoints'].append(checkpoint(f'{label}-launch-complete'))
        report['temp_root_removed'] = not Path(scratch).exists()
        require(report['temp_root_removed'], 'ZIP temporary extraction still exists')
        require(sha256(file) == report['asset']['sha256'], 'Final ZIP bytes changed during smoke')
        report['checks'] = ['ZIP paths/CRC verified', 'bundle ID/version/native architecture', 'bundle seal', 'extracted launch', 'copied launch']
        report['checked'] = report['checks'] + ['exact final ZIP SHA256', 'all embedded Mach-O and source/icon/resource parity',
                                               'immutable extracted/copied byte-mode-symlink parity', 'temporary extraction removed']
        report['pass'] = True
        report['visual_review'] = 'required'
        report['checkpoints'].append(checkpoint('package-complete'))
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        report['checkpoints'].append(checkpoint('package-failed'))
        raise
    finally:
        write_report(output / 'package.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', required=True, type=Path)
    parser.add_argument('--arch', required=True, choices=('arm64', 'x86_64', 'x64'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run_package(args.zip, args.output, args.arch), indent=2))


if __name__ == '__main__':
    main()
