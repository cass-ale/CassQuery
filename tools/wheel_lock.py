"""Exact native Python wheel lock validation shared by refresh and preflight."""
import json
from pathlib import Path
import re


def canonical(value):
    return re.sub(r'[-_.]+', '-', value).lower()


def compatible(filename, arch):
    if not filename.endswith('.whl'):
        return False
    try:
        _, interpreter, abi, platform = filename[:-4].rsplit('-', 3)
    except ValueError:
        return False
    interpreters = interpreter.split('.')
    platforms = platform.split('.')
    valid_python = any(value in ('py3', 'cp312') for value in interpreters)
    if abi == 'abi3':
        valid_python = any(re.fullmatch(r'cp3\d+', value) and int(value[2:]) <= 312 for value in interpreters)
    elif abi not in ('none', 'cp312'):
        return False
    if not valid_python:
        return False
    return any(value == 'any' or (value.startswith('macosx_') and
               ('universal2' in value or value.endswith('_arm64' if arch == 'arm64' else '_x86_64')))
               for value in platforms)


def validate(root):
    root = Path(root)
    expected = {}
    for line in (root / 'requirements-build.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            name, version = line.split('==')
            expected[canonical(name)] = version
    manifest = json.loads((root / 'packaging/wheel-manifest.json').read_text())
    lock = (root / 'packaging/requirements-macos.lock').read_text()
    entries = {}
    logical = lock.replace('\\\n', ' ')
    for line in logical.splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([a-z0-9-]+)==([^ ]+)\s+((?:--hash=sha256:[0-9a-f]{64}\s*)+)', line)
        if not match or match[1] in entries:
            raise ValueError('invalid/duplicate wheel lock requirement')
        entries[match[1]] = (match[2], set(re.findall(r'--hash=sha256:([0-9a-f]{64})', match[3])))
    if {name: value[0] for name, value in entries.items()} != expected:
        raise ValueError('wheel lock dependency graph differs from requirements')
    wheels = manifest['wheels']
    if {file['name'] for file in wheels} != set(expected):
        raise ValueError('wheel manifest dependency graph differs from requirements')
    if len({file['filename'] for file in wheels}) != len(wheels):
        raise ValueError('duplicate wheel manifest filenames')
    for name, version in expected.items():
        files = [file for file in wheels if file['name'] == name]
        if any(file['version'] != version or not re.fullmatch(r'[0-9a-f]{64}', file['sha256']) for file in files):
            raise ValueError('wheel version or hash mismatch')
        if entries[name][1] != {file['sha256'] for file in files}:
            raise ValueError(f'wheel hash set differs from manifest: {name}')
        for arch in ('arm64', 'x64'):
            if not any(compatible(file['filename'], arch) for file in files):
                raise ValueError(f'missing compatible {arch} wheel: {name}')
    return manifest
