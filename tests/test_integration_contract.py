"""Fail-closed report contracts. No CAD/browser or native packaging substitutes here."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tools/integration_suite.cjs"
EXPECTED = {
    "live-truth": ["default-demo", "eight-cad-gates", "dom-rebuild-http",
                   "save-preview-certified", "camera-direction",
                   "failed-gates-retain-bytes-and-model"],
    "interaction-order": ["step-buttons-keyboard", "rapid-aba-variant",
                          "xray-materials", "explode-transforms", "pointer-pick-hide-show",
                          "http-view-screenshot"],
    "isolation-reconnect": ["malformed-manifest-isolation", "syntax-error-no-model",
                            "error-recovery", "project-switch-stale-response",
                            "reconnect-hello-snapshot"],
    "boundary": ["malformed-json", "host-origin", "static-traversal",
                 "websocket-origin", "mutation-witness"],
}


def png_chunk(kind, content):
    return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))


def png_fixture(width=1320, height=900, raw=None, compressed=None):
    """Decodable RGB parser fixture, explicitly not a captured browser frame."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    if compressed is None:
        compressed = zlib.compress(raw if raw is not None else bytes((width * 3 + 1) * height))
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", compressed) + png_chunk(b"IEND", b"")


def parser_evidence():
    """Explicit typed parser fixtures, not executed production-path evidence."""
    def model(extent=42, tier="certified", name="TESTPLATE", key="b"):
        return {"project": name, "tier": tier, "hash": key * 16,
                "decoded_vertices": 120, "decoded_extent": [extent, 30, 32],
                "parts": [{"name": name, "visible": [True]} for name in ("Base", "Post", "Cap")],
                "variant": "neutral", "badge": "certified · gates passed"}
    gates = {"pass": True, "checks": [{"id": gate, "status": "pass"} for gate in
             ("units", "watertight", "volume", "thin-extent", "triangulation",
              "payload-roundtrip", "budget-tris", "budget-bytes")]}
    preview = model(tier="preview", key="c")
    preview["badge"] = "preview · not gate-checked"
    failed = model()
    failed["badge"] = "gate FAILED — showing last certified"
    failed_gates = copy.deepcopy(gates)
    failed_gates["pass"] = False
    failed_gates["checks"][0]["status"] = "fail"
    files = {name: "a" * 64 for name in ("index.json", "neutral.json", "lifted.json")}
    parts = [{"name": name, "opacity": 1 if name == "Cap" else 0.2,
              "transparent": name != "Cap", "depthWrite": name == "Cap", "positions": [[0, 0, z]]}
             for name, z in (("Base", 0), ("Post", 22), ("Cap", 48))]
    hidden = model()
    hidden["parts"][2]["visible"] = [False]
    rebuilt_hidden = copy.deepcopy(hidden)
    rebuilt_hidden["hash"] = "e" * 16
    wire_command = {"project": "TESTPLATE", "step": "apart", "variant": "lifted", "explode": 0.5, "xray": True,
                    "camera": {"position": [150, -170, 110], "target": [0, 0, 24]}}
    wire = model()
    wire.update(variant="lifted", camera=wire_command["camera"]["position"], target=wire_command["camera"]["target"])
    wire["parts"] = [{**part, "visible": [True], "positions": [[0, 0, {"Base": 0, "Post": 11, "Cap": 24}[part["name"]]]]}
                     for part in parts]
    shot_id = "TESTPLATE-" + "a" * 32
    mutation = {"detected": True, "check": "host-origin", "baseline_status": 403,
                "actual": 200, "expected": 403, "isolated_copy": True,
                "production_source_unchanged": True, "original_sha256": "a" * 64,
                "mutant_sha256": "b" * 64}
    return {
        "default-demo": {"no_project_arguments": True, "name": "TESTPLATE", "root_is_temp": True},
        "eight-cad-gates": {"gates": gates, "model": model(40)},
        "dom-rebuild-http": {"status": 202, "body": {"project": "TESTPLATE", "tier": "both"},
                             "prior_built_at": 1, "rebuilt_at": 2, "visible_built_at": 2,
                             "worker_frames": [{"type": "status", "state": "building-certified"},
                                               {"type": "announce", "tier": "certified"}]},
        "save-preview-certified": {"before": model(40, key="a"), "preview": preview,
                                   "after": model(), "preview_tools_disabled": True,
                                   "announcements": [{"tier": "preview", "hash": "c" * 16},
                                                     {"tier": "certified", "hash": "b" * 16}]},
        "camera-direction": {"before": [1, 0, 0], "after": [1, 0, 0], "direction_error": 0},
        "failed-gates-retain-bytes-and-model": {"files": files, "after_files": dict(files),
                                              "failed": failed, "retained_hash": "b" * 16,
                                              "gates": failed_gates, "certified_announcements": 0},
        "step-buttons-keyboard": {"buttons": 3, "keyboard_step": "Take it apart", "explode": "100%"},
        "rapid-aba-variant": {"delayed_real_variant": True, "final": model(),
                              "selected_step": "Assembled", "pending": 0, "loading_line": ""},
        "xray-materials": {"parts": parts, "off_parts": [{**p, "opacity": 1, "transparent": False, "depthWrite": True} for p in parts]},
        "explode-transforms": {"parts": parts, "keyboard_range_input": "End"},
        "pointer-pick-hide-show": {"actual_pointer": {"x": 300, "y": 200}, "picked": "Cap",
                                   "hidden": hidden, "shown": model(), "prior_hidden_hash": "b" * 16,
                                   "rebuilt_hidden": rebuilt_hidden, "show_all_reachable": True},
        "http-view-screenshot": {"statuses": [202, 200, 200], "rejected_statuses": [400, 400],
                                 "held_actual_variant": True, "renderer_posts_before_release": 0,
                                 "request": wire_command, "visible": wire, "canvas": {"width": 1320, "height": 900},
                                 "shot": {"project": "TESTPLATE", "file": "09-protocol-shot.png", "id": shot_id,
                                          "renderer_id": shot_id, "sha256": "a" * 64, "renderer_sha256": "a" * 64,
                                          "width": 1320, "height": 900},
                                 "worker_frames": [{"type": "command", **wire_command},
                                                   {"type": "screenshot", "project": "TESTPLATE", "id": shot_id}]},
        "malformed-manifest-isolation": {"api_version": 2, "projects": ["TESTPLATE", "SECONDPLATE"],
                                         "errors": [{"error": "JSONDecodeError: fixture"}]},
        "syntax-error-no-model": {"visible": {"project": None, "tier": None, "hash": None,
                                              "diagnostic": "SyntaxError: fixture", "badge": "no certified model"},
                                  "certified_index_absent": True},
        "error-recovery": {"recovered": model(24, name="SECONDPLATE")},
        "project-switch-stale-response": {"delayed_real_response": True, "visible": model()},
        "reconnect-hello-snapshot": {"previous_hash": "a" * 16, "snapshot_hash": "d" * 16,
                                    "missed_announcements": True, "actual_browser_network_offline": True,
                                    "reconnected": model(44, key="d"), "original_port": 41000,
                                    "reconnected_port": 41000,
                                    "hello": {"projects": [{"name": "TESTPLATE", "hashes": {"certified": "d" * 16}}]}},
        "malformed-json": {"bodies": ["syntax", "array", "non-finite number"], "statuses": [400] * 3},
        "host-origin": {"statuses": [403] * 2, "real_listener": 41000},
        "static-traversal": {"statuses": [404] * 4, "worker_source_not_served": True},
        "websocket-origin": {"handshake": "HTTP/1.1 403 Forbidden"}, "mutation-witness": mutation,
    }


class IntegrationReportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get("CASSQUERY_NODE") or shutil.which("node")
        if not cls.node:
            raise RuntimeError("Node is required: integration contracts cannot silently skip")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        # Report-parser fixture only, never passed off as a rendered browser screenshot.
        png = png_fixture()
        self.png = png
        shots = ["01-default-certified.png", "02-live-preview.png", "03-live-certified.png",
                 "04-unit-failure-retained.png", "05-xray.png", "06-exploded.png",
                 "07-syntax-no-model.png", "08-reconnect-snapshot.png", "09-protocol-shot.png"]
        for shot in shots:
            (self.output / shot).write_bytes(png)
        evidence = parser_evidence()
        evidence["http-view-screenshot"]["shot"].update(sha256=hashlib.sha256(png).hexdigest(), renderer_sha256=hashlib.sha256(png).hexdigest())
        self.report = {
            "schema": 1, "platform": "linux-source-browser-worker", "pass": True,
            "error": None, "cleanup": {"pass": True, "process_groups_gone": True,
                                       "ports_closed": True, "errors": []}, "browser_errors": [],
            "scenarios": [{"id": name, "pass": True,
                           "checks": [{"id": check, "pass": True, "evidence": evidence[check]}
                                      for check in checks]} for name, checks in EXPECTED.items()],
            "mutation": evidence["mutation-witness"],
            "source": {"worker_sha256": "a" * 64, "viewer_sha256": "d" * 64, "chromium_sandbox": True,
                       "python_sources": [{"path": name, "bytes": 100, "sha256": "a" * 64}
                                          for name in ("worker.py", "workbench.py", "cad_io.py")]},
            "screenshots": [{"file": shot, "description": "Parser fixture, not browser evidence",
                             "sha256": hashlib.sha256(png).hexdigest(), "width": 1320, "height": 900} for shot in shots],
        }

    def validate(self, report):
        file = self.output / "report.json"
        file.write_text(json.dumps(report))
        return subprocess.run([self.node, str(SUITE), "--validate-report", str(file)],
                              capture_output=True, text=True, timeout=10, check=False)

    def test_python_source_closure_is_complete_unique_and_typed(self):
        mutations = [
            lambda r: r["source"].pop("python_sources"),
            lambda r: r["source"]["python_sources"].pop(),
            lambda r: r["source"]["python_sources"].append(dict(r["source"]["python_sources"][0])),
            lambda r: r["source"]["python_sources"][0].update(sha256="b" * 64),
            lambda r: r["source"]["python_sources"][1].update(path="../workbench.py"),
            lambda r: r["source"]["python_sources"][1].update(bytes=0),
            lambda r: r["source"]["python_sources"][1].update(bytes="100"),
            lambda r: r["source"]["python_sources"][1].update(sha256="invalid"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                report = copy.deepcopy(self.report)
                mutate(report)
                self.assertNotEqual(self.validate(report).returncode, 0)

    def test_declared_product_invariants_do_not_drift(self):
        result = subprocess.run(
            [self.node, "-e", "const s=require(process.argv[1]); console.log(JSON.stringify({checks:s.REQUIRED_CHECKS,gates:s.GATES}))",
             str(SUITE)], capture_output=True, text=True, timeout=10, check=True)
        declared = json.loads(result.stdout)
        self.assertEqual(declared["checks"], EXPECTED)
        self.assertEqual(declared["gates"], ["units", "watertight", "volume", "thin-extent",
                                             "triangulation", "payload-roundtrip", "budget-tris", "budget-bytes"])
        source = SUITE.read_text()
        self.assertNotIn(".__cq.goStep(", source)
        self.assertNotIn("route.fulfill(", source)
        self.assertIn("route.continue()", source)
        self.assertIn("Network.emulateNetworkConditions", source)
        self.assertIn("chromiumSandbox: true", source)
        self.assertNotIn("--no-sandbox", source)

    def test_valid_parser_contract(self):
        result = self.validate(self.report)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_omitted_or_duplicate_product_invariants_fail(self):
        for kind in ("scenario", "check", "duplicate-scenario", "duplicate-check"):
            with self.subTest(kind=kind):
                report = copy.deepcopy(self.report)
                if kind == "scenario":
                    report["scenarios"].pop()
                elif kind == "check":
                    report["scenarios"][0]["checks"].pop()
                elif kind == "duplicate-scenario":
                    report["scenarios"][0] = report["scenarios"][1]
                else:
                    report["scenarios"][0]["checks"][0] = report["scenarios"][0]["checks"][1]
                self.assertNotEqual(self.validate(report).returncode, 0)

    def test_red_errors_cleanup_and_mutation_cannot_be_green(self):
        mutations = [
            lambda r: r.update(pass_=False),
            lambda r: r.update(error={"message": "deadline exceeded"}),
            lambda r: r["cleanup"].update(process_groups_gone=False),
            lambda r: r["cleanup"].update(ports_closed=False),
            lambda r: r["mutation"].update(detected=False),
            lambda r: r["mutation"].update(check="unrelated-startup-failure"),
            lambda r: r["mutation"].update(mutant_sha256="a" * 64),
            lambda r: r["scenarios"][0]["checks"][0].update(pass_=False),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                report = copy.deepcopy(self.report)
                mutate(report)
                # Python keyword spelling cannot be used for JSON's 'pass'.
                if "pass_" in report:
                    report["pass"] = report.pop("pass_")
                for scenario in report["scenarios"]:
                    for check in scenario["checks"]:
                        if "pass_" in check:
                            check["pass"] = check.pop("pass_")
                self.assertNotEqual(self.validate(report).returncode, 0)

    def test_evidence_missing_tampered_or_outside_output_fails(self):
        for kind in ("missing", "tampered", "outside", "none", "duplicate", "bad-source-digest"):
            with self.subTest(kind=kind):
                report = copy.deepcopy(self.report)
                if kind == "missing":
                    report["screenshots"][0]["file"] = "absent.png"
                elif kind == "tampered":
                    report["screenshots"][0]["sha256"] = "0" * 64
                elif kind == "outside":
                    report["screenshots"][0]["file"] = str(SUITE)
                elif kind == "none":
                    report["screenshots"] = []
                elif kind == "duplicate":
                    report["screenshots"][0] = report["screenshots"][1]
                else:
                    report["source"]["worker_sha256"] = "unknown"
                self.assertNotEqual(self.validate(report).returncode, 0)

    def test_pass_labels_cannot_hide_contradictory_observations(self):
        def evidence(report, name):
            return next(c["evidence"] for s in report["scenarios"] for c in s["checks"] if c["id"] == name)
        mutations = [
            lambda r: evidence(r, "eight-cad-gates")["gates"]["checks"][0].update(status="fail"),
            lambda r: evidence(r, "eight-cad-gates")["model"].update(decoded_vertices=0),
            lambda r: evidence(r, "eight-cad-gates")["model"].update(decoded_vertices=0.5),
            lambda r: evidence(r, "eight-cad-gates")["model"].update(decoded_vertices="120"),
            lambda r: evidence(r, "eight-cad-gates")["model"].update(decoded_extent=[40, -30, -32]),
            lambda r: evidence(r, "eight-cad-gates")["model"].update(decoded_extent=[40]),
            lambda r: evidence(r, "error-recovery")["recovered"].update(decoded_extent=[24, -30, -32]),
            lambda r: evidence(r, "dom-rebuild-http").update(rebuilt_at="2", visible_built_at="2"),
            lambda r: evidence(r, "xray-materials").update(parts=[{"name": "Base", "opacity": 0.2, "depthWrite": False}] * 3),
            lambda r: evidence(r, "explode-transforms").update(parts=[{"name": name, "positions": []} for name in ("Base", "Post", "Cap")]),
            lambda r: evidence(r, "pointer-pick-hide-show")["shown"].update(parts=[{"name": name, "visible": []} for name in ("Base", "Post", "Cap")]),
            lambda r: evidence(r, "save-preview-certified")["preview"].update(tier="certified"),
            lambda r: evidence(r, "save-preview-certified")["after"].update(hash="a" * 16),
            lambda r: evidence(r, "save-preview-certified").update(announcements=[]),
            lambda r: evidence(r, "camera-direction").update(after=[0, 1, 0], direction_error=1),
            lambda r: evidence(r, "dom-rebuild-http").update(rebuilt_at=1, visible_built_at=1),
            lambda r: evidence(r, "dom-rebuild-http").update(worker_frames=[]),
            lambda r: evidence(r, "failed-gates-retain-bytes-and-model").update(after_files={}),
            lambda r: evidence(r, "rapid-aba-variant").update(loading_line="loading lifted view"),
            lambda r: evidence(r, "host-origin").update(statuses=[200, 403]),
            lambda r: evidence(r, "websocket-origin").update(handshake="HTTP/1.1 101 Switching Protocols"),
            lambda r: r["mutation"].update(actual=403),
            lambda r: r["mutation"].update(production_source_unchanged=False),
            lambda r: r["browser_errors"].append("uncaught browser error"),
            lambda r: r["cleanup"]["errors"].append("worker still alive"),
            lambda r: evidence(r, "reconnect-hello-snapshot").update(reconnected_port=41001),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                report = copy.deepcopy(self.report)
                mutate(report)
                self.assertNotEqual(self.validate(report).returncode, 0)
        for empty in ({}, [], {"fixture": True}):
            for scenario in self.report["scenarios"]:
                for check in scenario["checks"]:
                    with self.subTest(empty=empty, check=check["id"]):
                        report = copy.deepcopy(self.report)
                        evidence(report, check["id"]).clear()
                        for candidate in report["scenarios"]:
                            for item in candidate["checks"]:
                                if item["id"] == check["id"]:
                                    item["evidence"] = copy.deepcopy(empty)
                        self.assertNotEqual(self.validate(report).returncode, 0)

    def test_png_signature_is_not_decoded_evidence(self):
        header = struct.pack(">IIBBBBB", 1320, 900, 8, 2, 0, 0, 0)
        raw = bytes((1320 * 3 + 1) * 900)
        compressed = zlib.compress(raw)
        signature = b"\x89PNG\r\n\x1a\n"
        cases = {
            "old-31-byte-fixture": signature + b"parser contract fixture",
            "truncated": self.png[:-5],
            "missing-end": self.png[:-12],
            "checksum": self.png[:-1] + bytes([self.png[-1] ^ 1]),
            "invalid-deflate": png_fixture(compressed=b"not-deflate"),
            "trailing-deflate": png_fixture(compressed=compressed + b"unused"),
            "decoded-length": png_fixture(raw=raw[:-1]),
            "row-filter": png_fixture(raw=b"\x05" + raw[1:]),
            "zero-width": png_fixture(width=0),
            "duplicate-header": signature + png_chunk(b"IHDR", header) + self.png[8:],
            "critical-chunk": signature + png_chunk(b"IHDR", header) + png_chunk(b"ABCD", b"") + self.png[33:],
            "noncontiguous-data": signature + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", compressed[:4])
                                  + png_chunk(b"tEXt", b"x\0y") + png_chunk(b"IDAT", compressed[4:]) + png_chunk(b"IEND", b""),
            "high-bit-type": signature + png_chunk(b"\xc9HDR", header) + self.png[33:],
        }
        for kind, png in cases.items():
            with self.subTest(kind=kind):
                report = copy.deepcopy(self.report)
                (self.output / report["screenshots"][0]["file"]).write_bytes(png)
                report["screenshots"][0]["sha256"] = hashlib.sha256(png).hexdigest()
                self.assertNotEqual(self.validate(report).returncode, 0, kind)
        (self.output / self.report["screenshots"][0]["file"]).write_bytes(self.png)
        for key, value in (("width", 1), ("height", 1), ("width", "1320")):
            report = copy.deepcopy(self.report)
            report["screenshots"][0][key] = value
            self.assertNotEqual(self.validate(report).returncode, 0)

    def test_wire_command_and_shot_observations_fail_closed(self):
        def evidence(report):
            return next(c["evidence"] for s in report["scenarios"] for c in s["checks"] if c["id"] == "http-view-screenshot")
        rounded = copy.deepcopy(self.report)
        evidence(rounded)["visible"]["camera"] = [149.99999999999994, -169.99999999999955, 109.99999999999974]
        self.assertEqual(self.validate(rounded).returncode, 0)
        mutations = [
            lambda e: e.update(statuses=[202, 504, 200]),
            lambda e: e.update(renderer_posts_before_release=1),
            lambda e: e.update(held_actual_variant=False),
            lambda e: e.update(worker_frames=[]),
            lambda e: e["visible"].update(variant="neutral"),
            lambda e: e["visible"].update(camera=[1, 2, 3]),
            lambda e: e["visible"]["parts"][1].update(positions=[[0, 0, 22]]),
            lambda e: e["shot"].update(renderer_id="unrelated"),
            lambda e: e["shot"].update(renderer_sha256="0" * 64),
            lambda e: e["shot"].update(width=1),
            lambda e: e.update(rejected_statuses=[202, 400]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                report = copy.deepcopy(self.report)
                mutate(evidence(report))
                self.assertNotEqual(self.validate(report).returncode, 0)

    def test_missing_worker_setup_is_red_and_cleans_fixture(self):
        # Execute a real copy of the runner with absent source, no mocks or browser required.
        broken = self.output / "broken"
        (broken / "tools").mkdir(parents=True)
        shutil.copyfile(SUITE, broken / "tools/integration_suite.cjs")
        scratch = self.output / "scratch"
        scratch.mkdir()
        output = self.output / "startup-evidence"
        env = dict(os.environ, TMPDIR=str(scratch), CASSQUERY_INTEGRATION_OUTPUT=str(output))
        result = subprocess.run([self.node, str(broken / "tools/integration_suite.cjs")],
                                env=env, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads((output / "integration-results.json").read_text())
        self.assertFalse(report["pass"])
        if sys.platform.startswith("linux"):
            self.assertIn("ENOENT", report["error"]["message"])
        else:
            self.assertIn("This suite proves Linux source behavior only", report["error"]["message"])
        self.assertEqual(report["scenarios"], [])
        self.assertTrue(report["cleanup"]["pass"])
        self.assertEqual(list(scratch.iterdir()), [])

    def test_unknown_cli_option_fails_instead_of_selecting_partial_suite(self):
        result = subprocess.run([self.node, str(SUITE), "--skip-cad"], capture_output=True,
                                text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
