"""Project input freshness regressions; all models and assets are generated scratch.

Fixtures use canonical roots, matching main's loaded Project.root namespace.
"""
import copy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker

HAS_CAD = all(importlib.util.find_spec(name) for name in ("numpy", "cadquery", "OCP"))


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.payloads = self.project / "runtime" / "payloads"
        self.shots = self.project / "runtime" / "evidence" / "shots"
        self.payloads.mkdir(parents=True)
        self.shots.mkdir(parents=True)
        for name, value in (("PAYLOAD_DIR", str(self.payloads)), ("SHOT_DIR", str(self.shots))):
            patch = mock.patch.object(worker, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_regular_project_input_has_a_content_digest(self):
        names = ("entry.py", "viewer.json", "part.step", "part.stp", "profile.dxf",
                 "part.brep", "part.bin", "dimensions.yaml", "custom.data")
        for name in names:
            (self.project / name).write_bytes(b"first")
        snapshot = worker.snapshot([str(self.project)])
        self.assertEqual(set(snapshot), {str(self.project / name) for name in names})
        for value in snapshot.values():
            self.assertEqual(value[-1], hashlib.sha256(b"first").hexdigest())
        path = self.project / "part.step"
        stamp = path.stat().st_mtime_ns
        path.write_bytes(b"other")  # Same size and restored mtime aren't a content identity.
        os.utime(path, ns=(stamp, stamp))
        changed = worker.snapshot([str(self.project)])
        self.assertNotEqual(snapshot[str(path)][-1], changed[str(path)][-1])
        self.assertEqual(changed[str(path)][-1], hashlib.sha256(b"other").hexdigest())

    def test_owned_outputs_and_dependency_cache_trees_are_not_inputs(self):
        (self.project / "entry.py").write_text("pass")
        for output in (self.payloads, self.shots):
            (output / "generated.json").write_text("output")
        for name in (".git", ".venv", "venv", "node_modules", "__pycache__"):
            directory = self.project / name
            directory.mkdir()
            (directory / "ignored.data").write_text("dependency/cache")
        self.assertEqual(set(worker.snapshot([str(self.project)])), {str(self.project / "entry.py")})

    def test_non_regular_and_external_linked_inputs_do_not_block_snapshot(self):
        outside = self.root / "outside.step"
        outside.write_text("external")
        (self.project / "external.step").symlink_to(outside)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.project / "pipe.step")
        self.assertEqual(worker.snapshot([str(self.project)]), {})

    def test_deleted_asset_is_a_watched_change(self):
        (self.project / "entry.py").write_text("def build(): return {}")
        (self.project / "viewer.json").write_text(json.dumps({"name": "INPUT", "entry": "entry.py:build", "live": True}))
        asset = self.project / "part.step"
        asset.write_text("asset")
        before = worker.snapshot([str(self.project)])
        project = worker.Project(str(self.project))
        asset.unlink()
        after = worker.snapshot([str(self.project)])
        changed = [path for path in set(before) | set(after) if before.get(path) != after.get(path)]
        self.assertEqual(changed, [str(asset)])
        with mock.patch.object(worker, "PROJECTS", {project.name: project}), mock.patch.object(worker, "broadcast"):
            worker._apply_changes(changed)
        self.assertEqual(project.state, "stale")
        self.assertTrue(project.preview_dirty)
        self.assertEqual(project.gen, 1)

    def test_removed_regular_input_keeps_ownership_after_external_symlink_replacement(self):
        (self.project / "entry.py").write_text("def build(): return {}")
        (self.project / "viewer.json").write_text(json.dumps({"name": "INPUT", "entry": "entry.py:build", "live": True}))
        asset = self.project / "part.step"
        asset.write_text("original input")
        before = worker.snapshot([str(self.project)])
        project = worker.Project(str(self.project))
        project.state = "certified"
        outside = self.root / "external.step"
        outside.write_text("external input")
        asset.unlink()
        asset.symlink_to(outside)
        after = worker.snapshot([str(self.project)])
        self.assertNotIn(str(asset), after)
        changed = [path for path in set(before) | set(after) if before.get(path) != after.get(path)]
        with mock.patch.object(worker, "PROJECTS", {project.name: project}), mock.patch.object(worker, "broadcast"):
            worker._apply_changes(changed)
        self.assertEqual(project.state, "stale")
        self.assertEqual(project.gen, 1)
        self.assertTrue(project.preview_dirty)
        outside.write_text("later external change")
        self.assertEqual(worker.snapshot([str(self.project)]), after)

    def test_in_tree_alias_of_owned_output_is_not_an_input(self):
        (self.project / "entry.py").write_text("pass")
        output = self.payloads / "generated.json"
        output.write_text("first output")
        alias = self.project / "latest.json"
        alias.symlink_to(output)
        before = worker.snapshot([str(self.project)])
        self.assertNotIn(str(alias), before)
        output.write_text("changed output")
        self.assertEqual(worker.snapshot([str(self.project)]), before)

    @unittest.skipUnless(HAS_CAD, "requires the real NumPy/CadQuery/OCP runtime")
    def test_certified_output_alias_does_not_feed_back_into_real_cad_builds(self):
        (self.project / "viewer.json").write_text(json.dumps({"name": "INPUT", "entry": "entry.py:build", "live": True}))
        (self.project / "entry.py").write_text("import cadquery as cq\ndef build():\n    return {'names':['Box'], 'parts':{'Box':cq.Workplane('XY').box(2,3,4).val()}}\n")
        project = worker.Project(str(self.project))
        with mock.patch.object(worker, "broadcast"):
            self.assertTrue(worker.run_build(project, "certified"), project.history)
            (self.project / "latest.json").symlink_to(self.payloads / "INPUT-certified.json")
            before = worker.snapshot([str(self.project)])
            for _ in range(2):
                self.assertTrue(worker.run_build(project, "certified"), project.history)
                self.assertTrue(project.gates["pass"])
                self.assertEqual(len(project.gates["checks"]), 8)
                after = worker.snapshot([str(self.project)])
                self.assertEqual(after, before)
                changed = [path for path in set(before) | set(after) if before.get(path) != after.get(path)]
                with mock.patch.object(worker, "PROJECTS", {project.name: project}):
                    worker._apply_changes(changed)
                self.assertEqual(project.state, "certified")
                self.assertEqual(project.gen, 0)
                self.assertFalse(project.preview_dirty)

    def test_input_mutation_during_digest_is_rejected(self):
        asset = self.project / "part.step"
        asset.write_bytes(b"a" * (1024 * 1024 + 1))
        real_open = open
        class ChangingReader:
            def __init__(self, file):
                self.file, self.changed = file, False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def fileno(self):
                return self.file.fileno()
            def read(self, size):
                data = self.file.read(size)
                if not self.changed:
                    self.changed = True
                    with real_open(asset, "ab") as output:
                        output.write(b"new")
                return data
        with mock.patch.object(worker, "open", side_effect=lambda *args: ChangingReader(real_open(*args)), create=True):
            with self.assertRaises(worker.StaleBuild):
                worker.snapshot([str(self.project)])


class SVGAssetHTTPTests(unittest.TestCase):
    def setUp(self):
        self.svg = (ROOT / "viewer" / "brand.svg").read_bytes()
        self.server = worker.ThreadingHTTPServer(("127.0.0.1", 0), worker.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive(), "owned HTTP server must exit")

    def request_svg(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", "/brand.svg")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(int(response.getheader("Content-Length")), len(self.svg))
            self.assertEqual(body, self.svg)
            self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(self.svg).hexdigest())
            return response.getheader("Content-Type")
        finally:
            connection.close()

    def test_canonical_svg_resource_has_image_media_type_and_nosniff(self):
        self.assertEqual(self.request_svg(), "image/svg+xml")

    def test_old_octet_stream_media_type_is_rejected_with_same_bytes_and_nosniff(self):
        serve_file = worker.Handler._file
        def old_media_type(handler, root, rel, media_type):
            return serve_file(handler, root, rel, "application/octet-stream")
        with mock.patch.object(worker.Handler, "_file", old_media_type):
            media_type = self.request_svg()
        self.assertEqual(media_type, "application/octet-stream")
        with self.assertRaisesRegex(AssertionError, r"image/svg\+xml"):
            self.assertEqual(media_type, "image/svg+xml")


@unittest.skipUnless(HAS_CAD, "requires the real NumPy/CadQuery/OCP runtime")
class ActualCADInputTests(unittest.TestCase):
    def setUp(self):
        import cadquery as cq
        self.cq = cq
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.controls = self.root / "controls"  # Build synchronization isn't project input.
        self.controls.mkdir()
        self.payloads = self.root / "payloads"
        self.payloads.mkdir()
        self.events = []
        (self.project / "viewer.json").write_text(json.dumps({"name": "INPUT", "entry": "entry.py:build", "live": True}))
        (self.project / "entry.py").write_text(
            "from pathlib import Path\nimport time\nimport cadquery as cq\n"
            f"CONTROLS = Path({str(self.controls)!r})\n"
            "def build():\n"
            "    shape = cq.importers.importStep(str(Path(__file__).parent / 'part.step')).val()\n"
            "    if (CONTROLS / 'hold').exists():\n"
            "        (CONTROLS / 'read').write_text(str(shape.Volume()))\n"
            "        deadline = time.monotonic() + 5\n"
            "        while (CONTROLS / 'hold').exists() and time.monotonic() < deadline:\n"
            "            time.sleep(.01)\n"
            "        if (CONTROLS / 'hold').exists(): raise RuntimeError('test synchronization timed out')\n"
            "    return {'names': ['Step'], 'parts': {'Step': shape}}\n")
        self.asset = self.project / "part.step"
        self.write_step(2)
        self.p = worker.Project(str(self.project))
        for patch in (mock.patch.object(worker, "PAYLOAD_DIR", str(self.payloads)),
                      mock.patch.object(worker, "SHOT_DIR", str(self.root / "shots")),
                      mock.patch.object(worker, "broadcast", side_effect=lambda event: self.events.append(copy.deepcopy(event)))):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertTrue(worker.run_build(self.p, "certified"), self.p.history)
        self.before = (self.payloads / "INPUT-certified.json").read_bytes()
        self.events.clear()

    def write_step(self, width):
        self.cq.exporters.export(self.cq.Workplane("XY").box(width, 3, 4), str(self.asset))

    def test_step_save_during_real_build_cannot_commit_old_certificate(self):
        (self.controls / "hold").write_text("hold")
        result = []
        thread = threading.Thread(target=lambda: result.append(worker.run_build(self.p, "certified")))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not (self.controls / "read").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.controls / "read").exists(), self.p.history)
            self.assertAlmostEqual(float((self.controls / "read").read_text()), 24)
            self.write_step(4)
        finally:
            (self.controls / "hold").unlink(missing_ok=True)
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "owned build thread must exit")
        self.assertEqual(result, [False])
        self.assertEqual(self.p.state, "stale")
        self.assertTrue(self.p.preview_dirty)
        self.assertEqual((self.payloads / "INPUT-certified.json").read_bytes(), self.before)
        self.assertFalse(any(event.get("type") == "announce" for event in self.events))
        self.assertAlmostEqual(self.p.truth["Step"]["volume"], 24)
        self.assertTrue(worker._build_pending(self.p))
        self.assertEqual(self.p.state, "certified")
        self.assertAlmostEqual(self.p.truth["Step"]["volume"], 48)
        self.assertTrue(self.p.gates["pass"])
        self.assertEqual(len(self.p.gates["checks"]), 8)

    def test_step_replaced_by_invalid_external_link_fails_closed(self):
        before = worker.snapshot([str(self.project)])
        outside = self.root / "invalid-external.step"
        outside.write_text("not a STEP file")
        self.asset.unlink()
        self.asset.symlink_to(outside)
        after = worker.snapshot([str(self.project)])
        changed = [path for path in set(before) | set(after) if before.get(path) != after.get(path)]
        with mock.patch.object(worker, "PROJECTS", {self.p.name: self.p}):
            worker._apply_changes(changed)
        self.assertEqual(self.p.state, "stale")
        self.assertEqual(self.p.gen, 1)
        self.assertTrue(worker._build_pending(self.p))
        self.assertEqual(self.p.state, "error")
        self.assertEqual((self.payloads / "INPUT-certified.json").read_bytes(), self.before)
        self.assertFalse(any(event.get("type") == "announce" for event in self.events))

    def test_missing_step_fails_closed_and_recovers_after_asset_save(self):
        before = worker.snapshot([str(self.project)])
        self.asset.unlink()
        after = worker.snapshot([str(self.project)])
        changed = [path for path in set(before) | set(after) if before.get(path) != after.get(path)]
        with mock.patch.object(worker, "PROJECTS", {self.p.name: self.p}):
            worker._apply_changes(changed)
        self.assertTrue(worker._build_pending(self.p))
        self.assertEqual(self.p.state, "error")
        self.assertEqual((self.payloads / "INPUT-certified.json").read_bytes(), self.before)
        self.assertFalse(any(event.get("type") == "announce" for event in self.events))
        self.write_step(4)
        current = worker.snapshot([str(self.project)])
        changed = [path for path in set(current) | set(after) if current.get(path) != after.get(path)]
        with mock.patch.object(worker, "PROJECTS", {self.p.name: self.p}):
            worker._apply_changes(changed)
        self.assertTrue(worker._build_pending(self.p))
        self.assertEqual(self.p.state, "certified")
        self.assertAlmostEqual(self.p.truth["Step"]["volume"], 48)


if __name__ == "__main__":
    unittest.main()
