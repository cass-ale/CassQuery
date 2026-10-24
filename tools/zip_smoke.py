#!/usr/bin/env python3
"""Verify final ZIP extraction/copy/native-launch parity against the checked app."""
import argparse
from collections import deque
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import subprocess
import tempfile
import unicodedata
import zipfile

from macos_smoke import (ROOT, architecture, checked_bundle_inventory, checkpoint,
                         file_evidence, inventory, require, sha256, verify_bundle,
                         write_report)
from dmg_smoke import check_launch


def archive_key(name):
    # Portable across case-insensitive and Unicode-normalizing Mac volumes.
    # Casefold can decompose again (e.g. Greek iota with dialytika/tonos).
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', name).casefold())


def validate_archive(file):
    with zipfile.ZipFile(file) as archive:
        members = archive.infolist()
        require(0 < len(members) <= 10000 and sum(m.file_size for m in members) <= 4 * 1024**3, 'ZIP exceeds reviewed app inventory bounds')
        seen, links, directories = set(), {}, {}
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            require(name and not path.is_absolute() and '..' not in path.parts and '\\' not in name
                    and ':' not in name and '\0' not in name and path.parts[0] in ('CassQuery.app', '__MACOSX'), 'Unsafe ZIP member path')
            canonical = path.as_posix()
            key = archive_key(canonical)
            require(name.rstrip('/') == canonical and key not in seen, 'Duplicate/noncanonical ZIP member')
            seen.add(key)
            mode = member.external_attr >> 16
            kind = stat.S_IFMT(mode)
            require(kind in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK), 'Unsupported ZIP entry type')
            require(kind == 0 or member.is_dir() == stat.S_ISDIR(mode), 'ZIP path/type mismatch')
            require(not member.is_dir() or member.file_size == 0, 'ZIP directory has file payload')
            directories[key] = member.is_dir()
            if stat.S_ISLNK(mode):
                require(member.file_size <= 4096, 'Oversized ZIP symlink')
                target = archive.read(member).decode('utf-8')
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                require(target and not target.startswith('/') and '\\' not in target and ':' not in target and '\0' not in target
                        and (resolved == 'CassQuery.app' or resolved.startswith('CassQuery.app/')), 'ZIP symlink escapes app')
                links[key] = target
        for member in members:
            parent = PurePosixPath(member.filename).parent
            require(not any(archive_key(p.as_posix()) in links for p in (parent, *parent.parents)), 'ZIP writes through a symlink ancestor')
            require(all(directories.get(archive_key(p.as_posix()), True) for p in (parent, *parent.parents)), 'ZIP writes through a nondirectory ancestor')
            if archive_key(PurePosixPath(member.filename).as_posix()) not in links:
                continue
            # Lexical normalization alone misses a/../b when a is a symlink.
            pending, resolved, expansions = deque(PurePosixPath(member.filename).parts), [], 0
            while pending:
                part = pending.popleft()
                if part in ('', '.'):
                    continue
                if part == '..':
                    require(len(resolved) > 1, 'Resolved ZIP symlink escapes app')
                    resolved.pop()
                    continue
                target = links.get(archive_key('/'.join([*resolved, part])))
                if target is None:
                    resolved.append(part)
                else:
                    expansions += 1
                    require(expansions <= 64, 'ZIP symlink cycle/depth exceeds bound')
                    pending.extendleft(reversed(PurePosixPath(target).parts))
            require(resolved and resolved[0] == 'CassQuery.app', 'Resolved ZIP symlink escapes app')
        require('cassquery.app/contents/info.plist' in seen and 'cassquery.app/contents/macos/cassquery' in seen, 'ZIP lacks required app')
        require(archive.testzip() is None, 'ZIP CRC/integrity failure')
        return {'entries': len(members), 'uncompressed_bytes': sum(m.file_size for m in members)}


def run_package(file, output, arch, *, checked_app, checked_inventory):
    file, output, arch = Path(file).resolve(), Path(output).resolve(), architecture(arch)
    checked_app, checked_inventory = Path(checked_app), Path(checked_inventory)
    require(not output.is_relative_to(checked_app.resolve()), 'Evidence must be outside checked app')
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
        baseline, report['checked_inventory'] = checked_bundle_inventory(checked_app, checked_inventory, output)
        report['checked_bundle_sha256'] = baseline['sha256']
        report['archive'] = validate_archive(file)
        with tempfile.TemporaryDirectory(prefix='cq-zip-') as scratch:
            extracted = Path(scratch) / 'extracted'
            extracted.mkdir()
            subprocess.run(['ditto', '-x', '-k', str(file), str(extracted)], check=True, timeout=120)
            app = extracted / 'CassQuery.app'
            _, extracted_inventory = verify_bundle(app, arch, version)
            require(extracted_inventory == baseline, 'ZIP app differs from source-checked prepackaged inventory')
            write_report(output / 'bundle-inventory.json', extracted_inventory)
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
        require(inventory(checked_app) == baseline and sha256(checked_inventory) == report['checked_inventory']['sha256'], 'Source-checked app/inventory changed during package smoke')
        require(sha256(file) == report['asset']['sha256'], 'Final ZIP bytes changed during smoke')
        report['checks'] = ['ZIP paths/CRC verified', 'bundle ID/version/native architecture', 'bundle seal', 'extracted launch', 'copied launch']
        report['checked'] = report['checks'] + ['exact final ZIP SHA256', 'source-checked prepackaged inventory parity',
                                               'all embedded Mach-O and source/icon/resource parity',
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
    parser.add_argument('--checked-app', required=True, type=Path)
    parser.add_argument('--checked-inventory', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run_package(args.zip, args.output, args.arch,
                                 checked_app=args.checked_app, checked_inventory=args.checked_inventory), indent=2))


if __name__ == '__main__':
    main()
