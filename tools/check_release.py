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


def check(root=ROOT):
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
    workflow = (root / ".github/workflows/macos-candidate.yml").read_text()
    import yaml
    document = yaml.safe_load(workflow)
    assert document['permissions'] == {'contents': 'read'}, 'excess workflow permissions'
    job = document['jobs']['candidate']
    assert job['env'].get('CSC_FOR_PULL_REQUEST') == 'true', 'PR ad-hoc sealing must not be skipped'
    assert job['env'].get('CSC_IDENTITY_AUTO_DISCOVERY') == 'false', 'candidate must not discover owner signing identity'
    assert job.get('continue-on-error', False) is False, 'candidate can ignore failure'
    entries = job['strategy']['matrix']['include']
    assert {(x['runner'], x['arch']) for x in entries} == {('macos-15', 'arm64'), ('macos-15-intel', 'x64')}
    required = [
        'Locked dependencies', 'Cheap fail-fast preflight', 'Native worker freeze',
        'Frozen real-CAD build, watcher and failure retention',
        'Assemble native app without publishing', 'Render the actual bundled application',
        'Package checked app', 'Verify and launch the final DMG', 'Candidate identity and hashes',
    ]
    steps = job['steps']
    names = [step.get('name') for step in steps]
    positions = []
    for name in required:
        assert names.count(name) == 1, f'missing/duplicate gate: {name}'
        step = steps[names.index(name)]
        assert 'if' not in step, f'gate can be skipped: {name}'
        assert step.get('continue-on-error', False) is False, f'gate failure ignored: {name}'
        assert step.get('run', '').strip(), f'gate has no command: {name}'
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
        "two native macOS architectures", "frozen worker and real renderer smoke gates",
        "no publish permission", "bundle allowlist", "vendored renderer revision"]}


if __name__ == "__main__":
    try:
        print(json.dumps(check(), indent=2))
    except (AssertionError, KeyError, ValueError, OSError) as error:
        print(f"Release preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1)
