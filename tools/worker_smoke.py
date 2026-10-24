#!/usr/bin/env python3
"""Check the actual worker subprocess, real CAD, save watcher and gate retention."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def get(port, endpoint):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{endpoint}", timeout=5) as response:
        return json.load(response)


def until(check, message, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise RuntimeError(f"Timed out: {message}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--python", type=Path, help="interpreter for an unfrozen worker.py")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    checks = []
    with tempfile.TemporaryDirectory(prefix="cq-worker-smoke-") as scratch:
        root = Path(scratch)
        project = root / "demo"
        shutil.copytree(ROOT / "testpart", project)
        registry = root / "registry.json"
        # Exercise busy HTTP and WS port selection using real sockets, not mocks.
        # Reserve a real HTTP collision and the next candidate's WS collision.
        for _ in range(100):
            busy_http = socket.socket()
            busy_ws = socket.socket()
            busy_http.bind(("127.0.0.1", 0))
            start = busy_http.getsockname()[1]
            try:
                if start > 64476:
                    raise OSError("port too high")
                busy_ws.bind(("127.0.0.1", start + 1001))
                break
            except OSError:
                busy_http.close()
                busy_ws.close()
        else:
            raise RuntimeError("could not reserve collision test sockets")
        try:
            env = {key: value for key, value in os.environ.items() if not key.startswith("CASSQUERY_")}
            env.update({"HOME": scratch, "CASSQUERY_DATA_DIR": str(root / "data")})
            command = ([str(args.python.absolute())] if args.python else []) + [str(args.worker.resolve()),
                str(project), "--port", str(start), "--registry-file", str(registry)]
            with (root / "worker.log").open("w") as log:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    until(lambda: registry.exists() or process.poll() is not None, "worker readiness")
                    if process.poll() is not None:
                        raise RuntimeError((root / "worker.log").read_text())
                    info = json.loads(registry.read_text())
                    assert info["pid"] == process.pid
                    port = info["port"]
                    assert port >= start + 2, "busy websocket port was not skipped"
                    checks.append("paired port collision handling and owned readiness")
                    until(lambda: get(port, "/api/status")["projects"][0]["state"] == "certified", "initial certified CAD build")
                    status = get(port, "/api/status")
                    assert status["api_version"] == 2 and not status["load_errors"]
                    gates = get(port, "/api/gates?project=TESTPLATE")
                    assert gates["pass"] and len(gates["checks"]) == 8, gates
                    checks.append("real CAD demo passes all eight certified gates")
                    original = get(port, "/payload/TESTPLATE-certified.json")
                    entry = project / "part_entry.py"
                    entry.write_text(entry.read_text().replace(".circle(4)", ".circle(5)"))
                    changed = until(lambda: (value if (value := get(port, "/payload/TESTPLATE-certified.json"))["hash"] != original["hash"] else None), "save-triggered preview and certified rebuild")
                    assert get(port, "/api/status")["projects"][0]["hashes"].get("preview")
                    checks.append("real save -> preview -> certified geometry change")
                    config = project / "viewer.json"
                    manifest = json.loads(config.read_text())
                    manifest["unit_scale"] = 2
                    config.write_text(json.dumps(manifest))
                    until(lambda: get(port, "/api/status")["projects"][0]["state"] == "gate-failed", "deliberate units failure")
                    assert not get(port, "/api/gates?project=TESTPLATE")["pass"]
                    assert get(port, "/payload/TESTPLATE-certified.json")["hash"] == changed["hash"]
                    for metadata in changed["variants"].values():
                        assert get(port, metadata["url"])["parts"], "prior geometry unavailable"
                    checks.append("failed certified gate retains prior certified index and geometry")
                finally:
                    process.terminate()
                    process.wait(timeout=10)
        finally:
            busy_http.close()
            busy_ws.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"pass": True, "checks": checks}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
