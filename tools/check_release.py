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
npx --no-install install-electron
python -m pip install --require-hashes --only-binary=:all: -r packaging/requirements-macos.lock
python -m pip check''',
    'Source identity snapshot': 'python tools/release_evidence.py snapshot --output evidence/source-before.json',
    'Cheap fail-fast preflight': 'python tools/preflight.py --require-clean --source-before evidence/source-before.json --output evidence/preflight.json',
    'Native worker freeze': 'npm run build:worker',
    'Frozen real-CAD build, watcher and failure retention': 'python tools/worker_smoke.py --worker dist/cassquery-worker/cassquery-worker --output evidence/worker-${{ matrix.arch }}.json',
    'Assemble native app without publishing': 'npx electron-builder --config packaging/electron-builder.json --mac --dir --${{ matrix.arch }} --publish never',
    'Render the actual bundled application': '''APP=$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)
python tools/macos_smoke.py --app "$APP" --output evidence/mac-${{ matrix.arch }}''',
    'Package checked app': '''python - <<'PY'
import json, os, re
from pathlib import Path
version = json.loads(Path('package.json').read_text())['version']
arch = os.environ['CASSQUERY_ARCH']
if not re.fullmatch(r'\\d+\\.\\d+\\.\\d+', version) or arch not in ('arm64', 'x64'):
    raise RuntimeError('Invalid owned package name')
directory = Path('dist/mac-app')
if directory.resolve() != Path.cwd() / directory:
    raise RuntimeError('Package output is redirected outside its owned path')
files = [directory / f'CassQuery-{version}-mac-{arch}.{extension}'
         for extension in ('zip', 'dmg', 'zip.blockmap', 'dmg.blockmap')]
if any(file.is_symlink() or (file.exists() and not file.is_file()) for file in files):
    raise RuntimeError('Refusing to remove a nonregular package output')
for file in files:
    file.unlink(missing_ok=True)
PY
npx electron-builder --config packaging/electron-builder.json --mac --${{ matrix.arch }} --prepackaged "$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)" --publish never''',
    'Verify and launch the final ZIP': '''ARCH=${{ matrix.arch }}
if [ "$ARCH" = x64 ]; then ARCH=x86_64; fi
APP=$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)
python tools/zip_smoke.py --zip "$(printf '%s\\n' dist/mac-app/*.zip)" --arch "$ARCH" --output evidence/zip-${{ matrix.arch }} --checked-app "$APP" --checked-inventory evidence/mac-${{ matrix.arch }}/bundle-before.json''',
    'Verify and launch the final DMG': '''ARCH=${{ matrix.arch }}
if [ "$ARCH" = x64 ]; then ARCH=x86_64; fi
APP=$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)
python tools/dmg_smoke.py --dmg "$(printf '%s\\n' dist/mac-app/*.dmg)" --arch "$ARCH" --output evidence/dmg-${{ matrix.arch }} --checked-app "$APP" --checked-inventory evidence/mac-${{ matrix.arch }}/bundle-before.json''',
    'Native workbench editor and file dialogs': '''APP=$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)
python tools/workbench_native_smoke.py --app "$APP" --arch ${{ matrix.arch }} --output evidence/workbench-${{ matrix.arch }}''',
    'Native update installation and rollback': '''APP=$(printf '%s\\n' dist/mac-app/mac*/CassQuery.app)
python tools/update_smoke.py --app "$APP" --arch ${{ matrix.arch }} --output evidence/update-${{ matrix.arch }}''',
    'Verify GitHub update channel transport': 'node tools/update_check_smoke.cjs --arch ${{ matrix.arch }} --output evidence/updatecheck-${{ matrix.arch }}.json',
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


def check_executable_sources(root, package, builder):
    """Fail before packaging when any executable is undeclared, absent or redirected."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('release_source_contract', Path(__file__).with_name('release_evidence.py'))
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    try:
        sources = contract.source_manifest(package)
    except ValueError as error:
        raise AssertionError(str(error)) from error
    assert package.get('main') == 'shell/main.js', 'unexpected application entry point'
    assert {'shell/preload.cjs', 'shell/desktop.cjs', 'shell/updater.cjs', 'shell/update_install.cjs'}.issubset(sources['shell']), 'workbench/updater source closure incomplete'
    assert {'worker.py', 'workbench.py', 'cad_io.py'}.issubset(sources['python']), 'frozen workbench/CADIO source closure incomplete'
    assert builder['files'] == sources['shell'] + ['package.json'], 'bundle allowlist differs from executable source manifest'
    assert builder.get('asar') is True, 'application code must remain packed'
    assert builder.get('electronFuses', {}).get('runAsNode') is True, 'owned install helper requires explicit packaged Node mode'
    shell = root / 'shell'
    assert shell.is_dir() and not shell.is_symlink(), 'missing/redirected shell source directory'
    entries = list(shell.iterdir())
    assert len(entries) <= 64 and not any(file.is_dir() or file.is_symlink() for file in entries), 'nested/redirected shell source is outside the reviewed flat closure'
    actual_shell = {file.relative_to(root).as_posix() for file in entries if file.suffix in ('.js', '.cjs', '.mjs')}
    actual_python = {file.name for file in root.iterdir() if file.suffix == '.py'}
    assert actual_shell == set(sources['shell']) and actual_python == set(sources['python']), 'undeclared executable source'
    for name in sources['shell'] + sources['python']:
        file = root / name
        assert file.is_file() and not file.is_symlink() and 0 < file.stat().st_size <= 1024 * 1024, 'missing/unsafe executable source: ' + name
    expected = [{'from': 'dist/cassquery-worker', 'to': 'worker'}]
    expected += [{'from': name, 'to': 'source/' + name} for name in sources['python']]
    expected += [{'from': name, 'to': 'source/' + name} for name in ('viewer', 'testpart')]
    expected.append({'from': 'LICENSE', 'to': 'LICENSE'})
    assert builder['extraResources'] == expected, 'frozen/public source resource mapping differs from declared closure'
    assert (root / 'packaging/worker.spec').is_file(), 'missing frozen worker specification'
    return sources


INTEGRATION_COMMANDS = {
    'Record integration context': '''mkdir -p evidence
printf 'commit=%s\\nrun=%s\\n' "$GITHUB_SHA" "$GITHUB_RUN_ID" > evidence/integration-context.txt
uname -a >> evidence/integration-context.txt''',
    'Source integration dependencies': '''npm ci
python -m pip install --only-binary=:all: -r requirements-build.txt
python -m pip check
google-chrome --version''',
    'Real CAD browser loop and mutation witness': 'node tools/integration_suite.cjs',
    'Validate independent report bytes': 'node tools/integration_suite.cjs --validate-report evidence/live-integration/integration-results.json',
    'Real workbench editor and CAD roundtrip': 'node tools/workbench_suite.cjs',
    'Validate workbench report bytes': 'node tools/workbench_suite.cjs --validate-report evidence/workbench-integration/workbench-results.json',
}


def check_integration_workflow(root):
    import yaml
    document = yaml.safe_load((root / '.github/workflows/integration.yml').read_text())
    assert document.get('permissions') == {'contents': 'read'}, 'source integration permissions exceed read-only'
    assert set(document.get('jobs', {})) == {'integration'}, 'source integration job graph changed'
    job = document['jobs']['integration']
    assert 'if' not in job and job.get('continue-on-error', False) is False, 'source integration job can bypass required cases'
    assert job.get('runs-on') == 'ubuntu-24.04' and job.get('timeout-minutes') == 20, 'source integration host/budget changed'
    assert job.get('permissions', document['permissions']) == {'contents': 'read'}, 'source integration job permissions exceed read-only'
    steps = job['steps']
    trigger = document.get('on', document.get(True))
    assert isinstance(trigger, dict) and set(trigger) == {'pull_request', 'push', 'workflow_dispatch'}
    assert trigger['push'] == {'branches': ['main']}, 'source integration must qualify trusted main pushes'
    for action, settings in (('actions/setup-node', {'node-version': '22.23.4', 'cache': 'npm'}),
                             ('actions/setup-python', {'python-version': '3.12.10', 'cache': 'pip', 'cache-dependency-path': 'requirements-build.txt'})):
        setup = [step for step in steps if step.get('uses', '').startswith(action + '@')]
        assert len(setup) == 1 and setup[0]['uses'] == action + '@' + ACTION_PINS[action]
        assert setup[0].get('with') == settings and 'if' not in setup[0] and setup[0].get('continue-on-error', False) is False, 'source integration runtime setup can bypass reviewed contract'
    names = [step.get('name') for step in steps]
    positions = []
    for name, command in INTEGRATION_COMMANDS.items():
        assert names.count(name) == 1, 'missing/duplicate source integration step: ' + name
        step = steps[names.index(name)]
        assert 'if' not in step and step.get('continue-on-error', False) is False, 'source integration step can skip/fail open: ' + name
        script = step.get('run')
        assert isinstance(script, str) and script.strip() == command, 'source integration command changed/no-op: ' + name
        positions.append(names.index(name))
    assert positions == sorted(positions), 'source integration cases out of order'
    basic_env = {'CASSQUERY_PYTHON': 'python', 'CASSQUERY_BROWSER': '/usr/bin/google-chrome'}
    assert steps[names.index('Real CAD browser loop and mutation witness')].get('env') == {**basic_env, 'CASSQUERY_INTEGRATION_OUTPUT': 'evidence/live-integration'}, 'legacy integration output/interpreter boundary changed'
    assert steps[names.index('Real workbench editor and CAD roundtrip')].get('env') == {**basic_env,
        'CASSQUERY_BASELINE_REPORT': 'evidence/live-integration/integration-results.json',
        'CASSQUERY_WORKBENCH_OUTPUT': 'evidence/workbench-integration'}, 'workbench integration must consume exact baseline/output'
    checkout = [step for step in steps if step.get('uses', '').startswith('actions/checkout@')]
    assert len(checkout) == 1 and checkout[0].get('uses') == 'actions/checkout@' + ACTION_PINS['actions/checkout']
    assert checkout[0].get('with') == {'ref': '${{ github.sha }}', 'persist-credentials': False}, 'source integration checkout must bind exact SHA'
    assert 'if' not in checkout[0] and checkout[0].get('continue-on-error', False) is False
    upload = [step for step in steps if step.get('uses', '').startswith('actions/upload-artifact@')]
    assert len(upload) == 1 and upload[0]['uses'] == 'actions/upload-artifact@' + ACTION_PINS['actions/upload-artifact']
    assert upload[0].get('name') == 'Keep integration evidence' and upload[0].get('if') == 'always()'
    assert upload[0].get('with') == {'name': 'CassQuery-integration-${{ github.sha }}', 'path': 'evidence/', 'if-no-files-found': 'error', 'retention-days': 14}, 'source integration evidence retention changed'
    return True


def check(root=ROOT, python_manifest=None):
    package = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    assert re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", package["version"]), "invalid canonical release version"
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
    check_executable_sources(root, package, builder)
    check_integration_workflow(root)
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
    assert type(job.get('timeout-minutes')) is int and job['timeout-minutes'] == 90, 'native candidate requires its reviewed bounded 90-minute whole-job budget'
    assert job['env'].get('CSC_FOR_PULL_REQUEST') == 'true', 'PR ad-hoc sealing must not be skipped'
    assert job['env'].get('CASSQUERY_ANGLE') == 'swiftshader' and 'CASSQUERY_SOFTWARE_GL' not in job['env'], 'macOS CI must explicitly exercise the bundled software renderer'
    assert builder.get('afterPack') == './tools/package_renderer.cjs', 'software renderer discovery closure must be installed before sealing'
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
    assert isinstance(context.get('run'), str) and context['run'].strip() == context_script, 'runner diagnostics must execute the reviewed output-producing commands'
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
        assert isinstance(step.get('run'), str) and step['run'].strip() == REQUIRED_COMMANDS[name], f'gate command was changed or replaced: {name}'
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
        "canonical version/lock parity", "complete executable source and frozen-helper closure", "mandatory source/workbench integration", "exact build tools", "reviewed action pins",
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
