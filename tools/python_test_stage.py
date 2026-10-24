#!/usr/bin/env python3
"""Execute source-bound unittest discovery in a separately time-bounded process."""
import json
import os
from pathlib import Path
import sys
import unittest


def python_sources(root, pinned):
    """Check the small tests tree before unittest can import package/test source."""
    from release_evidence import require
    base = root / 'tests'
    require(base.is_dir() and not base.is_symlink(), 'Python tests directory missing or symlinked')
    sources, entries = [], 0
    for directory, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != '__pycache__')
        entries += len(dirs) + len(files)
        require(entries <= 10000, 'Python test discovery exceeds 10000 entries')
        require(all(not (Path(directory) / name).is_symlink() for name in dirs), 'Python test directory symlink')
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            file = Path(directory) / name
            relative = file.relative_to(root).as_posix()
            require(not file.is_symlink() and relative in pinned, f'Unpinned Python test source: {relative}')
            sources.append(relative)
    require(any(Path(name).name.startswith('test_') for name in sources), 'Python test collection is empty')
    return sorted(sources)


def main():
    root, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    output.unlink(missing_ok=True)
    sys.path.insert(0, str(root / 'tools'))
    from release_evidence import source_snapshot, require
    pinned = {item['path'] for item in source_snapshot(root)['files']}
    sources = python_sources(root, pinned)
    sys.path.insert(0, str(root / 'tests'))
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_*.py', top_level_dir=str(root / 'tests'))
    collected = suite.countTestCases()
    require(collected > 0, 'Python test collection is empty')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    counts = {'schema': 1, 'tests': result.testsRun, 'collected': collected, 'files': sources,
              'failures': len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses),
              'skipped': len(result.skipped) + len(result.expectedFailures)}
    output.write_text(json.dumps(counts) + '\n')
    raise SystemExit(0 if result.wasSuccessful() and counts['tests'] == collected and counts['skipped'] == 0 else 1)


if __name__ == '__main__':
    main()
