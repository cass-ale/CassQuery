#!/usr/bin/env python3
"""Run the built macOS app with isolated state and check its rendered demo."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signed", action="store_true")
    args = parser.parse_args()
    app = args.app.resolve()
    output = args.output.resolve()
    binary = app / "Contents" / "MacOS" / "CassQuery"
    if not binary.is_file():
        parser.error(f"no app executable at {binary}")
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    version = json.loads((Path(__file__).resolve().parents[1] / 'package.json').read_text())["version"]
    assert info["CFBundleIdentifier"] == "com.cassquery.desktop", "wrong bundle identity"
    assert info["CFBundleShortVersionString"] == version, "wrong bundle version"
    for executable in (binary, app / "Contents/Resources/worker/cassquery-worker"):
        architectures = subprocess.check_output(["lipo", "-archs", str(executable)], text=True).split()
        assert platform.machine() in architectures, f"wrong executable architecture: {executable}"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True, timeout=120)
    output.mkdir(parents=True, exist_ok=True)
    if args.signed:
        for command in (["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
                        ["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)],
                        ["xcrun", "stapler", "validate", str(app)]):
            subprocess.run(command, check=True, timeout=120)
    with tempfile.TemporaryDirectory(prefix="cassquery-mac-smoke-") as scratch:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("CASSQUERY_") and key not in ("PYTHONHOME", "PYTHONPATH")}
        project = Path(scratch) / 'demo'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'testpart', project)
        challenge = uuid.uuid4().hex
        env.update({"CASSQUERY_PROJECTS": str(project),
                    "CASSQUERY_SMOKE_CHALLENGE": challenge,
                    "CASSQUERY_USER_DATA": str(Path(scratch) / "user"),
                    "CASSQUERY_DATA_DIR": str(Path(scratch) / "worker"),
                    "CASSQUERY_SMOKE_DIR": str(output),
                    "HOME": scratch, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        expected_images = [output / 'assembled.png', output / 'apart.png']
        for image in expected_images:
            image.unlink(missing_ok=True)
        report_file = output / "renderer.json"
        report_file.unlink(missing_ok=True)
        with (output / "app.log").open("w") as log:
            process = subprocess.Popen([str(binary)], env=env, cwd=scratch,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                code = process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                # App might exit before a stalled worker; reap the owned group too.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise RuntimeError("Packaged app did not finish renderer smoke") from None
        if not report_file.is_file():
            detail = (output / 'app.log').read_text(errors='replace')[-4000:]
            raise RuntimeError(f'App exited {code} without renderer evidence. Native log:\n{detail}')
        report = json.loads(report_file.read_text())
        if code or report.get("pass") is not True:
            raise RuntimeError(f"Packaged renderer failed: {report}")
        if report.get("challenge") != challenge or report.get("app_pid") != process.pid:
            raise RuntimeError("Stale or unrelated renderer evidence")
        if not isinstance(report.get("worker_pid"), int) or report["worker_pid"] <= 0:
            raise RuntimeError("Missing worker process evidence")
        required = {'certified demo', 'gate pass', 'assembled step', 'exploded step', 'reset view',
                    'save updates rendered model', 'camera preserved on rebuild',
                    'failed gate restores certified model', 'no renderer diagnostics'}
        if set(report.get('checks', [])) != required or len(report.get('screenshots', [])) != 2:
            raise RuntimeError("Incomplete renderer evidence")
        if [Path(image).resolve() for image in report['screenshots']] != expected_images:
            raise RuntimeError('Screenshots must be the two distinct expected files in this run directory')
        from PIL import Image
        for image in expected_images:
            if not image.is_file() or image.stat().st_size < 1000:
                raise RuntimeError(f"missing rendered evidence: {image}")
            with Image.open(image) as picture:
                if picture.format != 'PNG' or min(picture.size) < 100:
                    raise RuntimeError(f'invalid captured PNG: {image}')
                picture.verify()
        pid = report.get("worker_pid")
        if pid:
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("App quit left its worker running")
        report["native_checks"] = ["bundled Python (no PATH interpreter)",
                                   "isolated writable state", "native app launch", "worker shutdown"]
        report["signed_checks"] = args.signed
        # Image files are evidence to inspect, not a substitute for visual approval.
        report["visual_review"] = "required"
        report_file.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
