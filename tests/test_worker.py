"""Independent worker regressions. Run: python3 -m unittest discover -s tests -v.

Only the optional NumPy/CadQuery/websocket integration classes need dependencies.
All generated files and models live in temporary directories, never fixtures.
"""
import asyncio
import base64
import contextlib
import copy
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import py_compile
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest import mock
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def make_project(root, **changes):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "entry.py").write_text("def build():\n    return {}\n", encoding="utf-8")
    manifest = {"name": "TEST", "entry": "entry.py:build", "live": True}
    manifest.update(changes)
    (root / "viewer.json").write_text(json.dumps(manifest), encoding="utf-8")
    return worker.Project(str(root))


def prepared(p, marker="one", passed=True, tier="certified"):
    raw = json.dumps({"marker": marker}).encode()
    vh = hashlib.sha256(raw).hexdigest()[:16]
    h = hashlib.sha256(vh.encode()).hexdigest()[:16]
    gates = {"pass": passed, "checks": [], "diagnostics": [] if passed else ["deliberate failure"]} if tier == "certified" else None
    meta = {"hash": vh, "url": f"/payload/{p.name}-{tier}-neutral-{vh}.json", "tris": 1, "bytes": len(raw)}
    index = {"project": p.name, "tier": tier, "gen": p.gen, "hash": h, "gates": gates,
             "variants": {"neutral": meta}, "built_at": 1}
    return h, {"seconds": 0, "tris": 1, "bytes": len(raw), "gates": gates,
               "index": index, "variant_files": {"neutral": {**meta, "raw": raw}},
               "truth": {"marker": marker}, "sources": worker.snapshot([p.root]), "manifest": copy.deepcopy(p.manifest)}


class TempWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = make_project(self.root / "project")
        self.payloads = self.root / "payloads"
        self.shots = self.root / "shots"
        self.payloads.mkdir()
        self.shots.mkdir()
        self.events = []
        patches = [mock.patch.object(worker, "PAYLOAD_DIR", str(self.payloads)),
                   mock.patch.object(worker, "SHOT_DIR", str(self.shots)),
                   mock.patch.object(worker, "PROJECTS", {self.p.name: self.p}),
                   mock.patch.object(worker, "LOAD_ERRORS", []),
                   mock.patch.object(worker, "SHOTS_PENDING", {}),
                   mock.patch.object(worker, "broadcast", side_effect=lambda m: self.events.append(copy.deepcopy(m)))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_prepared(self, data, tier="certified"):
        with mock.patch.object(self.p, "build", return_value=data):
            return worker.run_build(self.p, tier)


class ManifestTests(TempWorkerTest):
    def test_bundled_manifest_and_build_only_contract(self):
        p = worker.Project(str(ROOT / "testpart"))
        self.assertEqual(p.name, "TESTPLATE")
        self.assertEqual(p.manifest["entry"], "part_entry.py:build")
        self.assertEqual([s["id"] for s in p.manifest["steps"]], ["assembled", "apart"])
        # The public entry exposes build-only shapes, without requiring CAD to read it.
        import ast
        tree = ast.parse((ROOT / "testpart" / "part_entry.py").read_text())
        self.assertEqual([n.name for n in tree.body if isinstance(n, ast.FunctionDef)], ["build"])

    def test_manifest_validation(self):
        cases = [{"name": "../escape"}, {"name": "bad\\path"}, {"name": "bad?query"},
                 {"name": []}, {"entry": "../entry.py:build"}, {"entry": "entry.py:f:bad"},
                 {"entry": "missing.py:build"}, {"poses": "../poses.py"},
                 {"unit_scale": 0}, {"unit_scale": -1}, {"unit_scale": float("nan")},
                 {"live": "false"}, {"weld_epsilon": -0.1}, {"materials": []},
                 {"materials": {"part": {"material": []}}}, {"explode": {"part": [1, 2]}},
                 {"tessellation": {"fine": [0, 1]}}, {"steps": [{"camera": {"position": [1, 2, 3]}}]},
                 {"chips": "not an array"}, {"extra": float("inf")}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises((ValueError, OSError)):
                make_project(self.root / "invalid", **changes)
        (self.root / "invalid" / "viewer.json").write_text("[]")
        with self.assertRaises(ValueError):
            worker.Project(str(self.root / "invalid"))

    def test_symlink_source_escape(self):
        outside = self.root / "outside.py"
        outside.write_text("pass")
        link = Path(self.p.root) / "linked.py"
        try:
            link.symlink_to(outside)
        except OSError as e:
            self.skipTest(str(e))
        with self.assertRaises(ValueError):
            worker._source_path(self.p.root, "linked.py")

    def test_source_reload_ignores_timestamp_pyc_and_supports_dataclasses(self):
        source = Path(self.p.root) / "entry.py"
        source.write_text("VALUE = 1\n")
        stamp = source.stat().st_mtime_ns
        py_compile.compile(str(source), doraise=True)
        source.write_text("VALUE = 2\n")
        os.utime(source, ns=(stamp, stamp))
        with worker._project_imports(self.p.root):
            self.assertEqual(self.p.load_module("entry.py").VALUE, 2)
        source.write_text("from dataclasses import dataclass\n@dataclass\nclass Part:\n    count: int = 3\n")
        with worker._project_imports(self.p.root):
            self.assertEqual(self.p.load_module("entry.py").Part().count, 3)

    def test_local_helper_imports_are_fresh_and_do_not_leak_across_projects(self):
        source, helper = Path(self.p.root) / "entry.py", Path(self.p.root) / "cq_test_helper.py"
        source.write_text("def build():\n    import cq_test_helper\n    return cq_test_helper.VALUE\n")
        helper.write_text("VALUE = 1\n")
        stamp = helper.stat().st_mtime_ns
        py_compile.compile(str(helper), doraise=True)
        before_path, before_meta = list(sys.path), list(sys.meta_path)
        with worker._project_imports(self.p.root):
            self.assertEqual(self.p.load_module("entry.py").build(), 1)
        helper.write_text("VALUE = 2\n")
        os.utime(helper, ns=(stamp, stamp))
        with worker._project_imports(self.p.root):
            self.assertEqual(self.p.load_module("entry.py").build(), 2)
        self.assertNotIn("cq_test_helper", sys.modules)
        self.assertEqual(sys.path, before_path)
        self.assertEqual(sys.meta_path, before_meta)

    def test_invalid_reload_blocks_old_manifest_build_and_recovers(self):
        path = Path(self.p.root) / "viewer.json"
        old = copy.deepcopy(self.p.manifest)
        path.write_text("{")
        worker._apply_changes([str(path)])
        self.assertIsNotNone(self.p.manifest_error)
        self.assertEqual(self.p.manifest, old)
        self.assertFalse(self.p.preview_dirty)
        with self.assertRaises(ValueError):
            self.p.build("certified")
        path.write_text(json.dumps(old))
        worker._apply_changes([str(path)])
        self.assertIsNone(self.p.manifest_error)
        self.assertTrue(self.p.preview_dirty)

    def test_manifest_save_before_watcher_poll_is_loaded_before_build(self):
        path = Path(self.p.root) / "viewer.json"
        path.write_text(json.dumps({**self.p.manifest, "unit_scale": 2}))
        # This fixture's build result is deliberately invalid, before any OCP import.
        with self.assertRaises(ValueError):
            self.p.build("certified")
        self.assertEqual(self.p.manifest["unit_scale"], 2)
        self.assertEqual(self.p.gen, 1)

    def test_reload_cannot_rename_running_project(self):
        path = Path(self.p.root) / "viewer.json"
        path.write_text(json.dumps({**self.p.manifest, "name": "RENAMED"}))
        worker._apply_changes([str(path)])
        self.assertEqual(self.p.name, "TEST")
        self.assertIn("restart", self.p.manifest_error)

    def test_path_prefix_siblings_do_not_trigger_project(self):
        sibling = self.root / "project-other"
        sibling.mkdir()
        changed = sibling / "entry.py"
        changed.write_text("pass")
        worker._apply_changes([str(changed)])
        self.assertEqual(self.p.gen, 0)

    def test_status_is_a_detached_snapshot(self):
        self.p.hashes["certified"] = "good"
        self.p.log("certified", "okay")
        status = self.p.status()
        status["hashes"]["certified"] = "changed"
        status["history"][0]["detail"] = "changed"
        self.assertEqual(self.p.hashes["certified"], "good")
        self.assertEqual(self.p.history[0]["detail"], "okay")

    def test_snapshot_prunes_dependencies_and_detects_delete_same_mtime(self):
        p = Path(self.p.root)
        for d in (".venv", "node_modules", "__pycache__"):
            (p / d).mkdir()
            (p / d / "unused.py").write_text("ignored")
        first = worker.snapshot([self.p.root])
        self.assertFalse(any("unused.py" in name for name in first))
        file = p / "entry.py"
        stamp = file.stat().st_mtime_ns
        file.write_text("longer contents than before")
        os.utime(file, ns=(stamp, stamp))
        self.assertNotEqual(first, worker.snapshot([self.p.root]))
        file.unlink()
        self.assertNotIn(str(file), worker.snapshot([self.p.root]))


class BuildPublicationTests(TempWorkerTest):
    def test_success_immutable_variants_preserve_old_lazy_fetch(self):
        first = prepared(self.p, "first")
        self.assertTrue(self.run_prepared(first))
        old_index = json.loads((self.payloads / "TEST-certified.json").read_bytes())
        old_file = self.payloads / unquote(old_index["variants"]["neutral"]["url"].rsplit("/", 1)[1])
        old_raw = old_file.read_bytes()
        self.assertTrue(self.run_prepared(prepared(self.p, "second")))
        new_index = json.loads((self.payloads / "TEST-certified.json").read_bytes())
        self.assertNotEqual(new_index["hash"], old_index["hash"])
        self.assertEqual(old_file.read_bytes(), old_raw)
        self.assertEqual(self.p.hashes["certified"], new_index["hash"])
        self.assertEqual(self.p.truth, {"marker": "second"})

    def test_failed_gates_keep_good_files_and_truth_and_announce_nothing(self):
        self.assertTrue(self.run_prepared(prepared(self.p, "good")))
        before = (self.payloads / "TEST-certified.json").read_bytes()
        good_hash, good_truth = self.p.hashes["certified"], copy.deepcopy(self.p.truth)
        self.events.clear()
        self.assertFalse(self.run_prepared(prepared(self.p, "bad", passed=False)))
        self.assertEqual((self.payloads / "TEST-certified.json").read_bytes(), before)
        self.assertEqual(self.p.hashes["certified"], good_hash)
        self.assertEqual(self.p.truth, good_truth)
        self.assertEqual(self.p.state, "gate-failed")
        self.assertFalse(any(m["type"] == "announce" for m in self.events))
        self.assertTrue((self.payloads / "TEST-certified-failed.json").exists())

    def test_stale_generation_is_not_published(self):
        data = prepared(self.p)
        self.p.gen += 1
        self.assertFalse(self.run_prepared(data))
        self.assertFalse(list(self.payloads.iterdir()))
        self.assertFalse(self.p.hashes)
        self.assertTrue(self.p.preview_dirty)
        self.assertEqual(self.p.state, "stale")

    def test_source_save_before_watcher_poll_is_not_published(self):
        data = prepared(self.p)
        (Path(self.p.root) / "entry.py").write_text("changed before watcher poll")
        self.assertFalse(self.run_prepared(data))
        self.assertFalse(list(self.payloads.iterdir()))

    def test_captured_new_file_snapshot_cannot_publish_old_manifest(self):
        path = Path(self.p.root) / "viewer.json"
        path.write_text(json.dumps({**self.p.manifest, "unit_scale": 2}))
        # Snapshot now matches disk, but the prepared build used old metadata.
        data = prepared(self.p)
        self.assertFalse(self.run_prepared(data))
        self.assertFalse(list(self.payloads.iterdir()))

    def test_source_change_during_variant_write_keeps_last_index(self):
        self.assertTrue(self.run_prepared(prepared(self.p, "good")))
        before = (self.payloads / "TEST-certified.json").read_bytes()
        data = prepared(self.p, "new")
        write = worker._atomic_write
        def change(path, raw):
            write(path, raw)
            (Path(self.p.root) / "entry.py").write_text("changed during write")
        with mock.patch.object(worker, "_atomic_write", side_effect=change):
            self.assertFalse(self.run_prepared(data))
        self.assertEqual((self.payloads / "TEST-certified.json").read_bytes(), before)
        self.assertEqual(self.p.truth, {"marker": "good"})

    def test_source_change_during_index_write_does_not_commit_stale_index(self):
        self.assertTrue(self.run_prepared(prepared(self.p, "good")))
        before = (self.payloads / "TEST-certified.json").read_bytes()
        data = prepared(self.p, "new")
        write = worker._atomic_write
        def change(path, raw, **kwargs):
            if path.endswith("TEST-certified.json"):
                (Path(self.p.root) / "entry.py").write_text("changed before index replace")
            write(path, raw, **kwargs)
        with mock.patch.object(worker, "_atomic_write", side_effect=change):
            self.assertFalse(self.run_prepared(data))
        self.assertEqual((self.payloads / "TEST-certified.json").read_bytes(), before)
        self.assertFalse(any(p.name.startswith(".cq-") for p in self.payloads.iterdir()))

    def test_write_error_keeps_last_certified_and_announces_no_success(self):
        self.assertTrue(self.run_prepared(prepared(self.p, "good")))
        before = (self.payloads / "TEST-certified.json").read_bytes()
        self.events.clear()
        with mock.patch.object(worker, "_atomic_write", side_effect=OSError("disk full")):
            self.assertFalse(self.run_prepared(prepared(self.p, "new")))
        self.assertEqual((self.payloads / "TEST-certified.json").read_bytes(), before)
        self.assertFalse(any(m["type"] == "announce" for m in self.events))
        self.assertEqual(self.p.state, "error")

    def test_project_system_exit_does_not_kill_build_worker(self):
        with mock.patch.object(self.p, "build", side_effect=SystemExit(7)):
            self.assertFalse(worker.run_build(self.p, "preview"))
        self.assertEqual(self.p.state, "error")
        self.assertTrue(self.run_prepared(prepared(self.p)))

    def test_dirty_event_during_preview_is_not_lost_or_certified_prematurely(self):
        self.p.preview_dirty = self.p.certified_due = True
        def build(p, tier):
            self.assertEqual(tier, "preview")
            with p.lock:
                p.gen += 1
                p.preview_dirty = True
            return True
        with mock.patch.object(worker, "run_build", side_effect=build) as run:
            worker._build_pending(self.p)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(self.p.preview_dirty)
        self.assertTrue(self.p.certified_due)

    def test_both_pending_builds_run_in_order(self):
        self.p.preview_dirty = self.p.certified_due = True
        with mock.patch.object(worker, "run_build", return_value=True) as run:
            self.assertTrue(worker._build_pending(self.p))
        self.assertEqual([call.args[1] for call in run.call_args_list], ["preview", "certified"])

    def test_atomic_write_failure_keeps_old_file_and_cleans_temp(self):
        file = self.payloads / "old.json"
        file.write_bytes(b"old")
        with mock.patch.object(worker.os, "replace", side_effect=OSError("failed rename")):
            with self.assertRaises(OSError):
                worker._atomic_write(str(file), b"new")
        self.assertEqual(file.read_bytes(), b"old")
        self.assertEqual([p.name for p in self.payloads.iterdir()], ["old.json"])


class BoundaryTests(unittest.TestCase):
    def headers(self, host="127.0.0.1:3939", origin=None):
        headers = Message()
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        return headers

    def test_native_and_same_worker_browser_are_allowed(self):
        for host in ("127.0.0.1:3939", "localhost:3939"):
            self.assertIsNone(worker._boundary_error(self.headers(host), 3939, 3939))
            self.assertIsNone(worker._boundary_error(self.headers(host, "http://localhost:3939"), 3939, 3939))
        self.assertIsNone(worker._boundary_error(self.headers("localhost:4939", "http://localhost:3939"), 4939, 3939))

    def test_dns_rebinding_and_foreign_origins_are_rejected(self):
        for host in (None, "evil.test:3939", "127.0.0.1:80", "127.0.0.1:bad", "user@127.0.0.1:3939", "localhost:3939/path", "[::1]:3939"):
            with self.subTest(host=host):
                self.assertIsNotNone(worker._boundary_error(self.headers(host), 3939, 3939))
        for origin in ("null", "https://localhost:3939", "http://evil.test:3939", "http://localhost:4939", "http://localhost:3939/path"):
            with self.subTest(origin=origin):
                self.assertIsNotNone(worker._boundary_error(self.headers(origin=origin), 3939, 3939))

    def test_duplicate_headers_rejected_and_ws_get_all_signature_supported(self):
        headers = self.headers()
        headers["Host"] = "evil.test:3939"
        self.assertIsNotNone(worker._boundary_error(headers, 3939, 3939))
        headers = self.headers(origin="http://localhost:3939")
        headers["Origin"] = "http://localhost:3939"
        self.assertIsNotNone(worker._boundary_error(headers, 3939, 3939))
        class WSHeaders:
            def get_all(self, key):  # websockets.Headers does not accept a default argument.
                if key == "Host":
                    return ["127.0.0.1:4939"]
                raise KeyError(key)
        self.assertIsNone(worker._boundary_error(WSHeaders(), 4939, 3939))


class HTTPTests(TempWorkerTest):
    def setUp(self):
        super().setUp()
        self.viewer = self.root / "viewer"
        self.viewer.mkdir()
        (self.viewer / "index.html").write_text("public viewer")
        (self.root / "secret.txt").write_text("SECRET")
        (self.payloads / "fixture.json").write_text('{"okay":true}')
        patch = mock.patch.object(worker, "VIEWER_DIR", str(self.viewer))
        patch.start()
        self.addCleanup(patch.stop)
        self.server = worker.ThreadingHTTPServer(("127.0.0.1", 0), worker.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            method = "GET" if body is None else "POST"
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def raw(self, headers, body=b"", method="POST", path="/api/rebuild"):
        with socket.create_connection(self.server.server_address, timeout=3) as sock:
            request = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
            sock.sendall(request.encode() + body)
            sock.shutdown(socket.SHUT_WR)
            received = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        return int(received.split(b" ", 2)[1]), received

    def test_existing_status_api_shapes_and_static_files(self):
        code, headers, raw = self.request("/api/status")
        result = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual(set(result), {"api_version", "projects", "load_errors"})
        self.assertEqual(result["api_version"], 2)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIsInstance(json.loads(self.request("/api/projects")[2]), list)
        self.assertEqual(json.loads(self.request("/api/status?project=TEST")[2])["name"], "TEST")
        self.assertEqual(self.request("/")[2], b"public viewer")
        self.assertEqual(self.request("/payload/fixture.json")[0], 200)

    def test_http_host_origin_protection_applies_to_reads_and_writes(self):
        for path, body in (("/api/status", None), ("/api/rebuild", {"project": "TEST"})):
            self.assertEqual(self.request(path, body, {"Host": "evil.test:3939"})[0], 403)
            self.assertEqual(self.request(path, body, {"Origin": "http://evil.test"})[0], 403)
            self.assertEqual(self.request(path, body, {"Origin": f"http://localhost:{self.server.server_port}"})[0], 200 if body is None else 202)

    def test_traversal_raw_encoded_symlinks_and_payload_aliases_fail(self):
        try:
            (self.viewer / "link.txt").symlink_to(self.root / "secret.txt")
            (self.payloads / "link.json").symlink_to(self.root / "secret.txt")
        except OSError:
            pass
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/%2e%2e%2fsecret.txt",
                     "/..%5csecret.txt", "/link.txt", "/payload/link.json", "/payload/a/fixture.json", "/%00.txt", "/payload/%00.json"):
            with self.subTest(path=path):
                code, _, raw = self.request(path)
                self.assertEqual(code, 404)
                self.assertNotIn(b"SECRET", raw)

    def test_invalid_json_and_types_return_400_not_thread_crash(self):
        for body in (b"{", b"[]", b"null", b'{"project":NaN}', b'"string"', b"\xff"):
            with self.subTest(body=body):
                self.assertEqual(self.request("/api/rebuild", body)[0], 400)
        for body in ({"project": []}, {"project": {}}, {"project": "TEST", "tier": "bad"}, {"project": "TEST", "tier": []}):
            self.assertEqual(self.request("/api/rebuild", body)[0], 400)
        self.assertFalse(self.p.preview_dirty or self.p.certified_due)
        self.assertEqual(self.request("/api/rebuild", {"project": "TEST", "tier": "both"})[0], 202)
        self.assertTrue(self.p.preview_dirty and self.p.certified_due)

    def test_length_transfer_encoding_and_duplicate_headers_fail_closed(self):
        host = f"Host: 127.0.0.1:{self.server.server_port}"
        cases = [([host], 400), ([host, "Content-Length: -1"], 400),
                 ([host, "Content-Length: x"], 400), ([host, "Content-Length: 17000000"], 413),
                 ([host, "Content-Length: 0", "Content-Length: 0"], 400),
                 ([host, "Transfer-Encoding: chunked", "Content-Length: 0"], 400),
                 ([host, "Content-Length: 100"], 400),
                 ([host, host, "Content-Length: 0"], 403), (["Content-Length: 0"], 403)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                code, raw = self.raw(headers)
                self.assertEqual(code, expected, raw)

    def test_view_validation_and_supported_command_shape(self):
        for body in ({"camera": []}, {"camera": {"position": [1, 2]}}, {"explode": "1"},
                     {"xray": "false"}, {"step": []}, {"variant": 5}, {"project": []},
                     {"explode": 10 ** 400}, {"camera": {"position": [10 ** 400, 0, 0]}}):
            self.assertEqual(self.request("/api/view", body)[0], 400)
        body = {"project": "TEST", "step": "apart", "variant": "neutral", "explode": 0.5,
                "camera": {"position": [1, 2, 3], "target": [0, 0, 0]}, "xray": True}
        self.assertEqual(self.request("/api/view", body)[0], 202)
        self.assertEqual(self.events[-1], {"type": "command", **body})
        self.assertEqual(self.request("/api/view", {"explode": 0})[0], 202)

    def answer_shot(self, msg):
        if msg["type"] != "screenshot":
            return
        code, _, raw = self.request("/api/shot", {"id": msg["id"], "project": msg["project"],
                                               "data": "data:image/png;base64," + base64.b64encode(PNG).decode()})
        self.assertEqual(code, 200, raw)

    def test_simultaneous_screenshots_unique_and_cleaned_after_success(self):
        ids = []
        lock = threading.Lock()
        def answer(msg):
            if msg["type"] == "screenshot":
                with lock:
                    ids.append(msg["id"])
                self.answer_shot(msg)
        with mock.patch.object(worker, "broadcast", side_effect=answer), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request("/api/screenshot", {"project": "TEST"}), range(2)))
        self.assertEqual([r[0] for r in results], [200, 200])
        self.assertEqual(len(set(ids)), 2)
        self.assertFalse(worker.SHOTS_PENDING)
        self.assertEqual(len(list(self.shots.iterdir())), 2)
        for file in self.shots.iterdir():
            self.assertEqual(file.read_bytes(), PNG)

    def test_shot_rejects_wrong_project_bad_data_duplicates_and_late_reply(self):
        ev = threading.Event()
        worker.SHOTS_PENDING["safe-id"] = (ev, {}, "TEST")
        good = "data:image/png;base64," + base64.b64encode(PNG).decode()
        for body in ({"id": [], "data": good}, {"id": "safe-id", "data": []},
                     {"id": "safe-id", "data": "data:image/png;base64,!!!!", "project": "TEST"},
                     {"id": "safe-id", "data": good, "project": "OTHER"},
                     {"id": "safe-id", "data": "data:image/png;base64,AA==", "project": "TEST"}):
            self.assertEqual(self.request("/api/shot", body)[0], 400)
        body = {"id": "safe-id", "data": good, "project": "TEST"}
        self.assertEqual(self.request("/api/shot", body)[0], 200)
        self.assertTrue(ev.is_set())
        self.assertEqual(self.request("/api/shot", body)[0], 400)
        worker.SHOTS_PENDING.clear()
        self.assertEqual(self.request("/api/shot", body)[0], 400)

    def test_screenshot_timeout_cleanup_without_waiting_25_seconds(self):
        event = threading.Event()
        event.wait = mock.Mock(return_value=False)
        handler = worker.Handler.__new__(worker.Handler)
        handler.path = "/api/screenshot"
        with mock.patch.object(handler, "_guard", return_value=True), \
                mock.patch.object(handler, "_body", return_value={"project": "TEST"}), \
                mock.patch.object(handler, "_project", return_value=self.p), \
                mock.patch.object(handler, "_send") as send, \
                mock.patch.object(worker.threading, "Event", return_value=event):
            handler.do_POST()
        self.assertEqual(send.call_args.args[0], 504)
        self.assertFalse(worker.SHOTS_PENDING)


class CoverageMathTests(unittest.TestCase):
    def test_adjacent_edges_and_a_midpoint_crossing_do_not_cover_a_hole(self):
        p, q = (0, 0, 0), (10, 0, 0)
        adjacent = [((0, 0, 0), (0, 10, 0)), ((10, 0, 0), (10, 10, 0))]
        self.assertFalse(worker._edge_covered(p, q, adjacent, 0.01))
        self.assertFalse(worker._edge_covered(p, q, [((5, -10, 0), (5, 10, 0))], 0.01))

    def test_full_coverage_by_split_reverse_parallel_segments(self):
        p, q = (0, 0, 0), (10, 0, 0)
        segments = [((3, 0.01, 0), (0, 0.01, 0)), ((10, 0.01, 0), (3, 0.01, 0))]
        self.assertTrue(worker._edge_covered(p, q, segments, 0.02))
        self.assertFalse(worker._edge_covered(p, q, segments, 0.005))
        self.assertFalse(worker._edge_covered(p, q, [((0, 0, 0), (4, 0, 0)), ((6, 0, 0), (10, 0, 0))], 0.1))

    def test_capsule_intervals_against_independent_point_distance_oracle(self):
        rng = random.Random(4423)
        for _ in range(100):
            p, q, a, b = [tuple(rng.uniform(-2, 2) for _ in range(3)) for _ in range(4)]
            eps = rng.uniform(0.05, 1)
            intervals = worker._capsule_intervals(p, q, a, b, eps)
            ab = [b[i] - a[i] for i in range(3)]
            ab2 = sum(x * x for x in ab)
            for j in range(101):
                t = j / 100
                x = [p[i] + t * (q[i] - p[i]) for i in range(3)]
                u = min(1, max(0, sum((x[i] - a[i]) * ab[i] for i in range(3)) / ab2))
                distance2 = sum((x[i] - a[i] - u * ab[i]) ** 2 for i in range(3))
                self.assertEqual(any(lo <= t <= hi for lo, hi in intervals), distance2 <= eps * eps)


class RuntimeTests(unittest.TestCase):
    def test_import_does_not_preflight_or_create_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = str(Path(tmp) / "must-not-be-created")
            env = {**os.environ, "CASSQUERY_DATA_DIR": data}
            result = subprocess.run([sys.executable, "-c", "import worker"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(Path(data).exists())

    def test_help_and_bad_ports_work_without_geometry_dependencies(self):
        for args, code in ((["--help"], 0), (["--port", "0"], 2), (["--port", "64477"], 2), (["--port", "3.5"], 2)):
            result = subprocess.run([sys.executable, str(ROOT / "worker.py"), *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, code, result.stderr)

    def test_preflight_checks_the_actual_websocket_api(self):
        def check(name):
            if name == "websockets.asyncio.server":
                raise ImportError("old websocket runtime")
            return object()
        with mock.patch.object(worker.importlib, "import_module", side_effect=check), contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as exc:
                worker._preflight()
        self.assertEqual(exc.exception.code, 1)
        self.assertIn("websockets.asyncio.server", error.getvalue())

    def test_frozen_resources_and_writable_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            resources, data = str(Path(tmp) / "resources"), str(Path(tmp) / "data")
            code = ("import sys; sys.frozen=True; sys._MEIPASS=" + repr(resources) +
                    "; import worker,json; print(json.dumps([worker.HERE,worker.VIEWER_DIR,worker.PAYLOAD_DIR,worker.SHOT_DIR]))")
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                    env={**os.environ, "CASSQUERY_DATA_DIR": data}, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            here, viewer, payload, shots = json.loads(result.stdout)
            self.assertEqual(here, resources)
            self.assertEqual(viewer, str(Path(resources) / "viewer"))
            self.assertEqual(payload, str(Path(data) / "payloads"))
            self.assertEqual(shots, str(Path(data) / "evidence" / "shots"))
            self.assertFalse(Path(data).exists())
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(worker._data_dir(), os.path.expanduser("~/Library/Application Support/CassQuery"))

    def test_port_validation(self):
        for bad in (0, -1, 64477, 65535, True, 1.5, "text", None):
            with self.subTest(port=bad), self.assertRaises(ValueError):
                worker._port(bad)
        self.assertEqual(worker._port("3939"), 3939)
        self.assertEqual(worker._port(64476), 64476)

    def test_occupied_ws_port_scans_pair_and_releases_failed_http_socket(self):
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", 0))
        blocked = blocker.getsockname()[1]
        if not 1001 <= blocked <= 65476:
            self.skipTest("ephemeral port outside scan range")
        blocker.listen()
        start = blocked - 1000
        http, ws = worker.bind_servers(start)
        self.addCleanup(http.server_close)
        self.addCleanup(ws.close)
        self.assertGreater(http.server_port, start)
        self.assertEqual(ws.getsockname()[1], http.server_port + 1000)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", start))

    def test_registry_is_written_only_after_both_listeners_are_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp) / "project", live=False)
            registry = Path(tmp) / "owned.json"
            payload, shots = Path(tmp) / "payload", Path(tmp) / "shots"
            seen = []
            def fake_ws(sock, ready, errors, stop):
                self.assertFalse(registry.exists())
                self.assertGreater(sock.getsockname()[1], 1000)
                ready.set()
            def handle(http):
                data = json.loads(registry.read_text())
                self.assertEqual(data["pid"], os.getpid())
                self.assertEqual(data["port"], http.server_port)
                seen.append(data)
                raise KeyboardInterrupt
            with mock.patch.object(worker, "_preflight"), \
                    mock.patch.object(worker, "PROJECTS", {}), mock.patch.object(worker, "LOAD_ERRORS", []), \
                    mock.patch.object(worker, "PAYLOAD_DIR", str(payload)), mock.patch.object(worker, "SHOT_DIR", str(shots)), \
                    mock.patch.object(worker, "ws_thread", side_effect=fake_ws), \
                    mock.patch.object(worker, "build_thread"), mock.patch.object(worker, "watcher"), \
                    mock.patch.object(worker.ThreadingHTTPServer, "handle_request", autospec=True, side_effect=handle), \
                    contextlib.redirect_stdout(io.StringIO()):
                worker.main([project.root, "--registry-file", str(registry), "--port", "30339"])
            self.assertEqual(len(seen), 1)
            self.assertFalse(registry.exists())

    def test_failed_ws_startup_never_overwrites_readiness_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp) / "project", live=False)
            registry = Path(tmp) / "owned.json"
            registry.write_text("sentinel")
            def fake_ws(sock, ready, errors, stop):
                errors.append(RuntimeError("deliberate websocket bind failure"))
                ready.set()
            with mock.patch.object(worker, "_preflight"), \
                    mock.patch.object(worker, "PROJECTS", {}), mock.patch.object(worker, "LOAD_ERRORS", []), \
                    mock.patch.object(worker, "PAYLOAD_DIR", str(Path(tmp) / "payload")), \
                    mock.patch.object(worker, "SHOT_DIR", str(Path(tmp) / "shots")), \
                    mock.patch.object(worker, "ws_thread", side_effect=fake_ws), \
                    self.assertRaisesRegex(RuntimeError, "websocket startup failed"):
                worker.main([project.root, "--registry-file", str(registry), "--port", "31339"])
            self.assertEqual(registry.read_text(), "sentinel")

    def test_scan_exhaustion_closes_every_allocated_socket(self):
        http = mock.Mock()
        ws = mock.Mock()
        ws.bind.side_effect = OSError("occupied")
        with mock.patch.object(worker, "ThreadingHTTPServer", return_value=http), mock.patch.object(socket, "socket", return_value=ws):
            with self.assertRaises(RuntimeError):
                worker.bind_servers(3939)
        self.assertEqual(ws.close.call_count, 60)
        self.assertEqual(http.server_close.call_count, 60)


@unittest.skipUnless(worker.np is not None, "NumPy is not installed")
class MeshTests(unittest.TestCase):
    def tetrahedron(self):
        np = worker.np
        return np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float), np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], np.int64)

    def test_empty_and_nonmanifold_meshes_fail_closure(self):
        np = worker.np
        self.assertFalse(worker._closure_check(np.empty((0, 3)), np.empty((0, 3), dtype=np.int64), 0)[0])
        verts, tris = self.tetrahedron()
        self.assertTrue(worker._closure_check(verts, tris, 0)[0])
        self.assertFalse(worker._closure_check(verts, np.vstack([tris, tris[0]]), 100, worker._mesh_area(verts, tris), True)[0])
        bad = tris.copy()
        bad[0] = bad[0, ::-1]
        self.assertFalse(worker._closure_check(verts, bad, 0)[0])

    def test_tiny_hole_does_not_pass_area_boundary_gap_fallback(self):
        np = worker.np
        # Many intact closed components put one removed face below the existing
        # 1% area / 2% boundary thresholds. Adjacent edges still meet at zero distance.
        vertices, triangles = [], []
        for i in range(100):
            v, t = self.tetrahedron()
            vertices.extend(v + [3 * i, 0, 0])
            triangles.extend(t + 4 * i if i else t[1:])
        v, t = np.array(vertices), np.array(triangles)
        area = worker._mesh_area(v, t)
        ok, method, details = worker._closure_check(v, t, 0.01, brep_area=area + 0.5, brep_closed=True)
        self.assertFalse(ok, details)
        self.assertEqual(method, "brep-closed+area")
        self.assertEqual(details.get("note"), "uncovered boundary edge")

    def test_pack_uses_unsigned_little_endian_and_roundtrips_indices(self):
        np = worker.np
        v, t = self.tetrahedron()
        v = v * 40 - 20
        packed = worker._pack_solid(v, t)
        q = struct.unpack("<" + "H" * (len(v) * 3), base64.b64decode(packed["positions"]))
        self.assertEqual(max(q), 65535)
        decoded = np.array(q).reshape(-1, 3) * packed["qscale"] + packed["qoffset"]
        self.assertLessEqual(float(np.abs(decoded - v).max()), packed["qscale"])
        self.assertEqual(struct.unpack("<" + "H" * t.size, base64.b64decode(packed["indices"])), tuple(t.ravel()))

    def test_u32_indices_and_invalid_mesh_fail_closed(self):
        np = worker.np
        v = np.column_stack((np.arange(65536, dtype=float), np.zeros(65536), np.ones(65536)))
        t = np.array([[0, 1, 65535]], np.int64)
        packed = worker._pack_solid(v, t)
        self.assertEqual(packed["itype"], "u32")
        self.assertEqual(struct.unpack("<III", base64.b64decode(packed["indices"])), (0, 1, 65535))
        good_v, good_t = self.tetrahedron()
        for vv, tt in ((good_v * float("nan"), good_t), (good_v, good_t - 5), (good_v, good_t + 5), (good_v, np.array([[0, 0, 1]]))):
            with self.assertRaises(ValueError):
                worker._pack_solid(vv, tt)

    def test_volume_stable_far_from_origin(self):
        v, t = self.tetrahedron()
        self.assertAlmostEqual(worker._mesh_volume(v + 1e9, t), 1 / 6, places=8)


CAD_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("numpy", "cadquery", "OCP"))


@unittest.skipUnless(CAD_AVAILABLE, "CadQuery/OCP/NumPy runtime is not installed")
class CADTests(TempWorkerTest):
    def setUp(self):
        super().setUp()
        import cadquery as cq
        self.cq = cq

    def test_bundled_demo_certifies_and_payloads_roundtrip(self):
        demo = self.root / "demo"
        shutil.copytree(ROOT / "testpart", demo)
        p = worker.Project(str(demo))
        self.assertTrue(worker.run_build(p, "certified"), p.history)
        self.assertEqual(len(p.gates["checks"]), 8)
        self.assertTrue(all(c["status"] == "pass" for c in p.gates["checks"]))
        index = json.loads((self.payloads / "TESTPLATE-certified.json").read_bytes())
        meta = index["variants"]["neutral"]
        raw = (self.payloads / unquote(meta["url"].rsplit("/", 1)[1])).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest()[:16], meta["hash"])
        self.assertEqual(len(raw), meta["bytes"])
        payload = json.loads(raw)
        self.assertEqual([part["name"] for part in payload["parts"]], ["Base", "Post", "Cap"])
        self.assertEqual(payload["bounds"]["min"], [-20.0, -15.0, 0.0])
        self.assertEqual(payload["bounds"]["max"], [20.0, 15.0, 32.0])
        for part in payload["parts"]:
            for s in part["solids"]:
                np = worker.np
                q = np.frombuffer(base64.b64decode(s["positions"]), dtype="<u2").reshape(-1, 3)
                v = q * s["qscale"] + s["qoffset"]
                t = np.frombuffer(base64.b64decode(s["indices"]), dtype="<u2" if s["itype"] == "u16" else "<u4").reshape(-1, 3)
                self.assertTrue(np.isfinite(v).all())
                self.assertLess(t.max(), len(v))
        self.assertAlmostEqual(p.truth["Post"]["volume"], 3.141592653589793 * 4 * 4 * 22, places=5)

    def test_unit_scale_mismatch_fails_nominal_mm_gates(self):
        entry = Path(self.p.root) / "entry.py"
        entry.write_text("import cadquery as cq\ndef build():\n    s = cq.Workplane('XY').box(2, 3, 4).val()\n    return {'names':['Box'], 'parts':{'Box':s}}\n")
        path = Path(self.p.root) / "viewer.json"
        path.write_text(json.dumps({**self.p.manifest, "unit_scale": 2}))
        worker._apply_changes([str(path)])
        self.assertFalse(worker.run_build(self.p, "certified"), self.p.history)
        checks = {c["id"]: c["status"] for c in self.p.gates["checks"]}
        self.assertEqual(checks["units"], "fail")
        self.assertEqual(checks["volume"], "fail")
        self.assertEqual(checks["thin-extent"], "fail")
        self.assertFalse((self.payloads / "TEST-certified.json").exists())
        self.assertIsNone(self.p.truth)

    def test_corrupt_packed_indices_fail_gate_and_preserve_last_certified(self):
        demo = self.root / "demo"
        shutil.copytree(ROOT / "testpart", demo)
        p = worker.Project(str(demo))
        self.assertTrue(worker.run_build(p, "certified"))
        before = (self.payloads / "TESTPLATE-certified.json").read_bytes()
        pack = worker._pack_solid
        def corrupt(v, t):
            s = pack(v, t)
            data = bytearray(base64.b64decode(s["indices"]))
            data[0] ^= 1
            s["indices"] = base64.b64encode(data).decode()
            return s
        with mock.patch.object(worker, "_pack_solid", side_effect=corrupt):
            self.assertFalse(worker.run_build(p, "certified"))
        self.assertEqual((self.payloads / "TESTPLATE-certified.json").read_bytes(), before)
        check = next(c for c in p.gates["checks"] if c["id"] == "payload-roundtrip")
        self.assertEqual(check["status"], "fail")

    def test_empty_build_and_reserved_pose_fail_without_publishing(self):
        entry = Path(self.p.root) / "entry.py"
        entry.write_text("def build():\n    return {'names':[], 'parts':{}}\n")
        self.assertFalse(worker.run_build(self.p, "certified"))
        entry.write_text("import cadquery as cq\ndef build():\n    return {'names':['Box'], 'parts':{'Box':cq.Workplane('XY').box(2,3,4).val()}}\n")
        poses = Path(self.p.root) / "poses.py"
        poses.write_text("POSES = {'neutral': lambda r:r['parts']}\n")
        path = Path(self.p.root) / "viewer.json"
        path.write_text(json.dumps({**self.p.manifest, "poses": "poses.py"}))
        worker._apply_changes([str(path)])
        self.assertFalse(worker.run_build(self.p, "certified"))
        self.assertFalse(list(self.payloads.iterdir()))


@unittest.skipUnless(importlib.util.find_spec("websockets") is not None, "websockets runtime is not installed")
class WebsocketTests(unittest.TestCase):
    def test_real_ws_handshake_enforces_host_origin_and_serves_hello(self):
        http, sock = worker.bind_servers(32339)
        ready, stop, errors = threading.Event(), threading.Event(), []
        thread = threading.Thread(target=worker.ws_thread, args=(sock, ready, errors, stop), daemon=True)
        with mock.patch.object(worker, "PROJECTS", {}):
            try:
                thread.start()
                self.assertTrue(ready.wait(5))
                self.assertFalse(errors, errors)
                port = sock.getsockname()[1]
                for host, origin, expected in ((f"127.0.0.1:{port}", f"http://localhost:{http.server_port}", 101),
                                                (f"127.0.0.1:{port}", "http://evil.test", 403),
                                                (f"evil.test:{port}", None, 403),
                                                (f"localhost:{port}", None, 101)):
                    with self.subTest(host=host, origin=origin), socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                        headers = ["GET / HTTP/1.1", "Host: " + host, "Upgrade: websocket", "Connection: Upgrade",
                                   "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg=="]
                        if origin is not None:
                            headers.append("Origin: " + origin)
                        client.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
                        with client.makefile("rb") as response:
                            status = response.readline()
                            self.assertEqual(int(status.split()[1]), expected, status)
                            while response.readline() != b"\r\n":
                                pass
                            if expected == 101:
                                op, size = response.read(2)
                                self.assertEqual(op, 0x81)
                                if size == 126:
                                    size = struct.unpack("!H", response.read(2))[0]
                                hello = json.loads(response.read(size))
                                self.assertEqual(hello, {"type": "hello", "projects": []})
                                # An empty masked close frame ends the connection cleanly.
                                client.sendall(b"\x88\x80\0\0\0\0")
            finally:
                stop.set()
                thread.join(5)
                sock.close()
                http.server_close()
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()
