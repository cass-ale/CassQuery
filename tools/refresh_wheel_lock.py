#!/usr/bin/env python3
"""Refresh native wheel hashes from explicit downloaded wheels and PyPI metadata."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import ssl
import urllib.request
import zipfile


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel-dir', type=Path, action='append', required=True)
    parser.add_argument('--requirements', type=Path, default=Path('requirements-build.txt'))
    parser.add_argument('--output', type=Path, default=Path('packaging/requirements-macos.lock'))
    parser.add_argument('--manifest', type=Path, default=Path('packaging/wheel-manifest.json'))
    parser.add_argument('--ca-file', type=Path)
    args = parser.parse_args()
    expected = {}
    for line in args.requirements.read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            name, version = line.strip().split('==')
            expected[canonical(name)] = version
    wheels = {}
    for directory in args.wheel_dir:
        for file in directory.glob('*.whl'):
            with zipfile.ZipFile(file) as archive:
                metadata = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA') and len(Path(name).parts) == 2]
                if len(metadata) != 1:
                    raise ValueError(f'invalid wheel metadata: {file.name}')
                info = BytesParser().parsebytes(archive.read(metadata[0]))
            name, version = canonical(info['Name']), info['Version']
            if expected.get(name) != version:
                raise ValueError(f'wheel not in exact dependency graph: {file.name}')
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            if file.name in wheels and wheels[file.name]['sha256'] != digest:
                raise ValueError(f'inconsistent downloaded wheel bytes: {file.name}')
            wheels[file.name] = {'name': name, 'version': version, 'filename': file.name,
                                'sha256': digest, 'bytes': file.stat().st_size}
    context = ssl.create_default_context(cafile=str(args.ca_file) if args.ca_file else None)
    def release(item):
        name, version = item
        with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json', context=context, timeout=30) as response:
            value = json.load(response)
        return name, {file['filename']: file for file in value['urls']}
    with ThreadPoolExecutor(max_workers=6) as executor:
        releases = dict(executor.map(release, expected.items()))
    for wheel in wheels.values():
        provider = releases[wheel['name']].get(wheel['filename'])
        if not provider or provider['digests']['sha256'] != wheel['sha256'] or provider['size'] != wheel['bytes']:
            raise ValueError(f'downloaded bytes disagree with PyPI: {wheel["filename"]}')
        if provider.get('yanked'):
            raise ValueError(f'refuse yanked wheel: {wheel["filename"]}')
        wheel['url'] = provider['url']
    for name in expected:
        files = [wheel for wheel in wheels.values() if wheel['name'] == name]
        from wheel_lock import compatible
        for arch in ('arm64', 'x64'):
            if not any(compatible(wheel['filename'], arch) for wheel in files):
                raise ValueError(f'missing compatible {arch} wheel: {name}')
    lines = ['# Exact cp312 macOS wheels verified against PyPI; ARM64 and x64.',
             '# Refresh with tools/refresh_wheel_lock.py, then verify both native candidates.']
    for name, version in sorted(expected.items()):
        hashes = sorted({wheel['sha256'] for wheel in wheels.values() if wheel['name'] == name})
        if not hashes:
            raise ValueError(f'missing wheels: {name}')
        lines.append(f'{name}=={version} \\\n    ' + ' \\\n    '.join('--hash=sha256:' + digest for digest in hashes))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('\n'.join(lines) + '\n')
    args.manifest.write_text(json.dumps({'schema': 1, 'python_abi': 'cp312',
        'requirements_sha256': hashlib.sha256(args.requirements.read_bytes()).hexdigest(),
        'wheels': sorted(wheels.values(), key=lambda wheel: wheel['filename'])}, indent=2) + '\n')
    print(f'Locked {len(expected)} packages / {len(wheels)} verified wheel files')


if __name__ == '__main__':
    main()
