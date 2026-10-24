#!/usr/bin/env python3
"""Verify and launch the final DMG, mounted read-only and copied to clean state."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


def verify_bundle(app, arch, version):
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == "com.cassquery.desktop", "wrong bundle ID"
    assert info["CFBundleShortVersionString"] == version, "wrong bundle version"
    executable = app / "Contents/MacOS/CassQuery"
    worker = app / "Contents/Resources/worker/cassquery-worker"
    for binary in (executable, worker):
        assert binary.is_file() and binary.stat().st_mode & 0o111, f"missing executable: {binary}"
        architectures = subprocess.check_output(["lipo", "-archs", str(binary)], text=True).split()
        assert arch in architectures, f"wrong architecture: {binary} {architectures}"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True, timeout=120)
    return {"bundle_id": info["CFBundleIdentifier"], "version": version, "architecture": arch}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dmg", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=("arm64", "x86_64"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    version = json.loads((repo / "package.json").read_text())["version"]
    dmg = args.dmg.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(["hdiutil", "verify", str(dmg)], check=True, timeout=120)
    with tempfile.TemporaryDirectory(prefix="cq-dmg-") as scratch:
        mount = Path(scratch) / "mounted"
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(dmg)],
                       check=True, timeout=120)
        try:
            app = mount / "CassQuery.app"
            report = verify_bundle(app, args.arch, version)
            for location, candidate in (("mounted", app), ("copied", Path(scratch) / "CassQuery.app")):
                if location == "copied":
                    subprocess.run(["ditto", str(app), str(candidate)], check=True, timeout=120)
                    verify_bundle(candidate, args.arch, version)
                subprocess.run([sys.executable, str(repo / "tools/macos_smoke.py"), "--app", str(candidate),
                                "--output", str(output / location)], check=True, timeout=150)
            report.update({"pass": True, "checks": ["DMG verified", "bundle ID/version/native architecture",
                                                   "bundle seal", "mounted launch", "copied launch"],
                           "gatekeeper": "not checked; ad-hoc candidate is not Developer ID/notarized",
                           "visual_review": "required"})
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=True, timeout=60)
    (output / "package.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
