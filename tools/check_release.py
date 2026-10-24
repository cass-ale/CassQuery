#!/usr/bin/env python3
"""Cheap release-input checks: run before expensive native packaging."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
}


REQUIRED_COMMANDS = {
    'Locked dependencies': '''npm ci
python -m pip install --require-hashes --only-binary=:all: -r packaging/requirements-macos.lock
python -m pip check''',
    'Source identity snapshot': 'python tools/release_evidence.py snapshot --output evidence/source-before.json',
    'Cheap fail-fast preflight': 'python tools/preflight.py --require-clean --source-before evidence/source-before.json --output evidence/preflight.json',
    'Native worker freeze': 'npm run build:worker',
    'Frozen real-CAD build, watcher and failure retention': 'python tools/worker_smoke.py --worker dist/cassquery-worker/cassquery-worker --output evidence/worker-${{ matrix.arch }}.json',
    'Assemble native app without publishing': 'npx electron-builder --config packaging/electron-builder.json --mac --dir --${{ matrix.arch }} --publish never',
    'Render the actual bundled application': '''APP=$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)
python tools/macos_smoke.py --app "$APP" --output evidence/mac-${{ matrix.arch }}''',
    'Package checked app': '''npx electron-builder --config packaging/electron-builder.json --mac --${{ matrix.arch }} --prepackaged "$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)" --publish never''',
    'Verify and launch the final ZIP': '''ARCH=${{ matrix.arch }}
if [ "$ARCH" = x64 ]; then ARCH=x86_64; fi
python tools/zip_smoke.py --zip "$(printf '%s\\n' dist/mac-app/*.zip)" --arch "$ARCH" --output evidence/zip-${{ matrix.arch }}''',
    'Verify and launch the final DMG': '''ARCH=${{ matrix.arch }}
if [ "$ARCH" = x64 ]; then ARCH=x86_64; fi
python tools/dmg_smoke.py --dmg "$(printf '%s\\n' dist/mac-app/*.dmg)" --arch "$ARCH" --output evidence/dmg-${{ matrix.arch }}''',
    'Candidate identity and hashes': '''python tools/sbom.py --output evidence/sbom.json
python tools/release_evidence.py stage --arch ${{ matrix.arch }} --output dist/candidate-${{ matrix.arch }}''',
}


def check_python_runtime(version, arches, manifest):
    """Reject a provider pin without a native download for every advertised arch."""
    assert re.fullmatch(r"3\.12\.\d+", version), "review Python ABI changes with the native dependency graph"
    release = next((entry for entry in manifest if entry.get("version") == version), None)
    assert release and release.get("stable") is True, f"Python {version} has no reviewed stable provider release"
    files = release.get("files", [])
    for arch in arches:
        expected = f"python-{version}-darwin-{arch}.tar.gz"
        assert any(file.get("platform") == "darwin" and file.get("arch") == arch
                   and file.get("filename") == expected
                   and file.get("download_url", "").startswith("https://github.com/actions/python-versions/releases/download/")
                   and file["download_url"].endswith("/" + expected)
                   for file in files), f"Python {version} has no macOS {arch} provider binary"


def check(root=ROOT, python_manifest=None):
    package = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", package["version"]), "invalid release version"
    assert lock["version"] == package["version"] == lock["packages"][""]["version"], "version drift"
    for name, version in package["devDependencies"].items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"unbounded dependency: {name}"
        assert lock["packages"][f"node_modules/{name}"]["version"] == version, f"lock drift: {name}"
    builder = json.loads((root / "packaging/electron-builder.json").read_text())
    assert builder["publish"] is None, "first candidate must not auto-publish"
    assert builder["mac"]["icon"] == "assets/icons/cassquery.icns", "default app icon is not a branded candidate"
    assert (root / builder['mac']['icon']).is_file(), "brand icon missing"
    assert builder["mac"]["identity"] == "-", "candidate must explicitly ad-hoc sign"
    assert builder["mac"]["hardenedRuntime"] is False, "hardened Developer ID mode requires separate configuration"
    assert builder["files"] == ["shell/**", "package.json"], "bundle allowlist changed"
    wheel_manifest = json.loads((root / 'packaging/wheel-manifest.json').read_text())
    assert wheel_manifest.get('schema') == 1 and wheel_manifest.get('python_abi') == 'cp312', 'invalid native wheel manifest'
    import hashlib
    assert hashlib.sha256((root / 'requirements-build.txt').read_bytes()).hexdigest() == wheel_manifest['requirements_sha256'], 'wheel manifest source graph drift'
    wheel_lock = (root / 'packaging/requirements-macos.lock').read_text()
    for name, version in [line.split('==') for line in (root / 'requirements-build.txt').read_text().splitlines() if line and not line.startswith('#')]:
        normalized = re.sub(r'[-_.]+', '-', name).lower()
        files = [wheel for wheel in wheel_manifest['wheels'] if wheel['name'] == normalized and wheel['version'] == version]
        assert files and f'{normalized}=={version}' in wheel_lock, f'missing locked dependency: {name}'
        for wheel in files:
            assert re.fullmatch(r'[0-9a-f]{64}', wheel['sha256']) and '--hash=sha256:' + wheel['sha256'] in wheel_lock, 'missing wheel content hash'
    import importlib.util
    validator_spec = importlib.util.spec_from_file_location('native_wheel_lock', Path(__file__).with_name('wheel_lock.py'))
    validator = importlib.util.module_from_spec(validator_spec)
    validator_spec.loader.exec_module(validator)
    validator.validate(root)
    workflow = (root / ".github/workflows/macos-candidate.yml").read_text()
    import yaml
    document = yaml.safe_load(workflow)
    assert document['permissions'] == {'contents': 'read'}, 'excess workflow permissions'
    job = document['jobs']['candidate']
    assert job['env'].get('CSC_FOR_PULL_REQUEST') == 'true', 'PR ad-hoc sealing must not be skipped'
    assert job['env'].get('CASSQUERY_ANGLE') == 'metal' and 'CASSQUERY_SOFTWARE_GL' not in job['env'], 'macOS CI must use explicit ANGLE Metal, not forced Vulkan SwiftShader'
    assert job['env'].get('CSC_IDENTITY_AUTO_DISCOVERY') == 'false', 'candidate must not discover owner signing identity'
    assert job.get('continue-on-error', False) is False and 'if' not in job, 'candidate job can bypass gates'
    assert job.get('permissions', document['permissions']) == {'contents': 'read'}, 'candidate permissions exceed read-only'
    entries = job['strategy']['matrix']['include']
    assert {(x['runner'], x['arch']) for x in entries} == {('macos-15', 'arm64'), ('macos-15-intel', 'x64')}
    required = list(REQUIRED_COMMANDS)
    steps = job['steps']
    python_steps = [step for step in steps if step.get('uses', '').startswith('actions/setup-python@')]
    assert len(python_steps) == 1, 'exactly one native Python setup is required'
    python_step = python_steps[0]
    assert 'if' not in python_step and not python_step.get('continue-on-error', False), 'Python setup cannot be bypassed'
    assert python_step['with'].get('architecture', '${{ matrix.arch }}') == '${{ matrix.arch }}', 'Python architecture must match the native matrix'
    if python_manifest is None:
        python_manifest = json.loads((root / 'packaging/python-runtime.json').read_text())
    check_python_runtime(python_step['with']['python-version'], {entry['arch'] for entry in entries}, python_manifest)
    names = [step.get('name') for step in steps]
    assert names.count('Record runner context') == 1, 'early failure diagnostics are required'
    context = steps[names.index('Record runner context')]
    assert 'if' not in context and not context.get('continue-on-error', False), 'runner context cannot be skipped'
    context_script = '''mkdir -p evidence
printf 'commit=%s\\nrunner=%s\\narch=%s\\nrun=%s\\n' "$GITHUB_SHA" '${{ matrix.runner }}' '${{ matrix.arch }}' "$GITHUB_RUN_ID" > evidence/runner-context.txt
sw_vers >> evidence/runner-context.txt
uname -m >> evidence/runner-context.txt'''
    assert context.get('run', '').strip() == context_script, 'runner diagnostics must execute the reviewed output-producing commands'
    setups = [step for step in steps if step.get('uses', '').startswith(('actions/setup-python@', 'actions/setup-node@'))]
    assert len(setups) == 2 and all(names.index('Record runner context') < steps.index(step) for step in setups), 'diagnostics must precede both tool setup actions'
    uploads = [step for step in steps if step.get('uses', '').startswith('actions/upload-artifact@')]
    assert len(uploads) == 1 and uploads[0].get('if') == 'always()', 'diagnostics must survive failed setup'
    assert uploads[0]['with']['path'] == 'dist/candidate-${{ matrix.arch }}/' and uploads[0]['with']['if-no-files-found'] == 'error', 'candidate artifact root must match verified staging'
    diagnostic = [step for step in steps if step.get('name') == 'Retain failure diagnostics']
    assert len(diagnostic) == 1 and diagnostic[0].get('if') == 'always()', 'early failures must retain nonqualifying diagnostics'
    checkouts = [step for step in steps if step.get('uses', '').startswith('actions/checkout@')]
    assert len(checkouts) == 1 and checkouts[0].get('with') == {'ref': '${{ github.sha }}', 'persist-credentials': False}, 'candidate must checkout exact CI SHA without persisted credentials'
    positions = []
    for name in required:
        assert names.count(name) == 1, f'missing/duplicate gate: {name}'
        step = steps[names.index(name)]
        assert 'if' not in step, f'gate can be skipped: {name}'
        assert step.get('continue-on-error', False) is False, f'gate failure ignored: {name}'
        assert step.get('run', '').strip() == REQUIRED_COMMANDS[name], f'gate command was changed or replaced: {name}'
        positions.append(names.index(name))
    assert positions == sorted(positions), 'release gates out of order'
    used = dict(re.findall(r"uses:\s*([\w/-]+)@([0-9a-f]{40})", workflow))
    assert used == ACTION_PINS, "review action pin changes together with their contract"
    assert "--publish never" in package["scripts"]["build:mac"], "build can publish"
    assert "macos-15-intel" in workflow and "macos-15" in workflow, "native architecture coverage"
    assert "tools/macos_smoke.py" in workflow and "tools/worker_smoke.py" in workflow
    assert "contents: read" in workflow and "contents: write" not in workflow
    assert "workflow_dispatch:" in workflow and "pull_request:" in workflow
    # Published third-party renderer must match the exact vendored revision.
    core = (root / "viewer/vendor/three.core.js").read_text()
    assert re.search(r"REVISION\s*=\s*['\"]186['\"]", core), "vendored renderer revision changed"
    return {"pass": True, "version": package["version"], "checks": [
        "version/lock parity", "exact build tools", "reviewed action pins",
        "two native macOS architectures", "native Python provider availability",
        "early failure diagnostics", "frozen worker and real renderer smoke gates",
        "no publish permission", "bundle allowlist", "vendored renderer revision"]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-manifest', type=Path,
                        help='fresh actions/python-versions manifest; default is the reviewed native snapshot')
    args = parser.parse_args()
    try:
        manifest = json.loads(args.python_manifest.read_text()) if args.python_manifest else None
        print(json.dumps(check(python_manifest=manifest), indent=2))
    except (AssertionError, KeyError, ValueError, OSError) as error:
        print(f"Release preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1)
