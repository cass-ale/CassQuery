#!/usr/bin/env python3
"""Build a CycloneDX dependency inventory from the actual locked runtime graph."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import re

from release_evidence import digest, source_snapshot


def canonical(value):
    return re.sub(r'[-_.]+', '-', value).lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('evidence/sbom.json'))
    args = parser.parse_args()
    root = args.root.resolve()
    source = source_snapshot(root)
    wheel_manifest = json.loads((root / 'packaging/wheel-manifest.json').read_text())
    components = []
    for line in (root / 'requirements-build.txt').read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, version = line.split('==')
        require_version = importlib.metadata.version(name)
        if require_version != version:
            raise ValueError(f'installed Python dependency differs from lock: {name}')
        wheel_hashes = sorted({wheel['sha256'] for wheel in wheel_manifest['wheels'] if wheel['name'] == canonical(name)})
        components.append({'type': 'library', 'name': canonical(name), 'version': version,
                           'purl': f'pkg:pypi/{canonical(name)}@{version}',
                           'hashes': [{'alg': 'SHA-256', 'content': sha} for sha in wheel_hashes]})
    lock = json.loads((root / 'package-lock.json').read_text())
    seen_npm = set()
    for name, item in sorted(lock['packages'].items()):
        if not name or not item.get('version'):
            continue
        package = name.rsplit('node_modules/', 1)[-1]
        key = (package, item['version'])
        if key in seen_npm:
            continue
        seen_npm.add(key)
        components.append({'type': 'library', 'name': package, 'version': item['version'],
                           'purl': f'pkg:npm/{package.replace("@", "%40")}@{item["version"]}',
                           'properties': [{'name': 'npm:integrity', 'value': item.get('integrity', '')}]})
    output = {'bomFormat': 'CycloneDX', 'specVersion': '1.5', 'version': 1,
              'metadata': {'component': {'type': 'application', 'name': 'CassQuery',
                  'version': json.loads((root / 'package.json').read_text())['version']},
                  'properties': [{'name': 'source_sha', 'value': source['source_sha']},
                                 {'name': 'python-wheel-lock-sha256', 'value': digest(root / 'packaging/requirements-macos.lock')},
                                 {'name': 'npm-lock-sha256', 'value': digest(root / 'package-lock.json')}]},
              'components': components}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + '\n')
    print(f'Recorded {len(components)} dependency components with exact source/lock identity')


if __name__ == '__main__':
    main()
