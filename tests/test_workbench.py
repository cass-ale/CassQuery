"""Workbench trust-boundary, source-CAS and real BREP regressions (owned scratch)."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker
import workbench

HAS_CAD = all(importlib.util.find_spec(name) for name in ("numpy", "cadquery", "OCP"))
CODE = "VALUE = 1\n\ndef build():\n    return {}\n"


class WorkbenchCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        (self.project_root / "entry.py").write_text(CODE)
        (self.project_root / "viewer.json").write_text(json.dumps({"name": "BOX", "entry": "entry.py:build", "live": True}))
        self.project = worker.Project(str(self.project_root))
        self.payloads = self.root / "data" / "payloads"
        self.payloads.mkdir(parents=True)
        self.events = []
        for patch in (mock.patch.object(worker, "PROJECTS", {"BOX": self.project}),
                      mock.patch.object(worker, "PAYLOAD_DIR", str(self.payloads)),
                      mock.patch.object(worker, "SHOT_DIR", str(self.root / "shots")),
                      mock.patch.object(worker, "broadcast", side_effect=lambda event: self.events.append(event))):
            patch.start()
            self.addCleanup(patch.stop)
        self.bench = workbench.Workbench(worker)

    def action(self, action, **values):
        return self.bench.dispatch({"action": action, "project": "BOX", **values})

    def revision(self, path="entry.py"):
        return workbench.read_text(self.project_root, path)["sha256"]

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(workbench.WorkbenchError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception


class FileBoundaryTests(WorkbenchCase):
    def test_read_save_and_external_conflict_keep_draft_and_actual_bytes(self):
        source = self.action("read", path="entry.py")
        saved = self.action("save", path="entry.py", text=CODE.replace("1", "2"), expected_sha256=source["sha256"])
        self.assertEqual(saved["sha256"], self.revision())
        external = CODE.replace("1", "3")
        (self.project_root / "entry.py").write_text(external)
        error = self.error("CONFLICT", self.action, "save", path="entry.py", text="DRAFT = 4\n", expected_sha256=saved["sha256"])
        self.assertEqual(error.status, 409)
        self.assertEqual((self.project_root / "entry.py").read_text(), external)

    def test_competing_cas_writers_commit_exactly_once(self):
        expected = self.revision()
        barrier = threading.Barrier(2)
        def save(number):
            barrier.wait(timeout=3)
            try:
                return self.action("save", path="entry.py", text=f"VALUE = {number}\n", expected_sha256=expected)["sha256"]
            except workbench.WorkbenchError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, (2, 3)))
        self.assertEqual(results.count("CONFLICT"), 1)
        self.assertIn(self.revision(), results)

    def test_atomic_external_replacement_in_commit_gap_is_retained_and_blocks_build(self):
        expected = self.revision()
        exchange = workbench._exchange_files
        external = b"VALUE = 99\n"
        def concurrent(directory, left, right):
            file = self.root / "external-save.py"
            file.write_bytes(external)
            os.replace(file, self.project_root / right)
            exchange(directory, left, right)
        with mock.patch.object(workbench, "_exchange_files", side_effect=concurrent):
            error = self.error("CONFLICT", self.action, "save", path="entry.py", text="VALUE = 2\n", expected_sha256=expected)
        self.assertTrue(error.details["active_file_changed"])
        self.assertEqual((self.project_root / "entry.py").read_text(), "VALUE = 2\n")
        recovery = error.details["recovery_paths"][0]
        self.assertEqual((self.project_root / recovery["path"]).read_bytes(), external)
        self.assertEqual(recovery["sha256"], hashlib.sha256(external).hexdigest())
        self.assertEqual(self.project.state, "conflict")
        self.assertFalse(worker.run_build(self.project, "certified"))
        self.assertFalse(worker._build_pending(self.project))
        self.error("STALE_CERTIFICATE", lambda: worker.certified_record("BOX").__enter__())
        self.error("UNRESOLVED_CONFLICT", self.action, "save", path="entry.py", text="VALUE = 3\n", expected_sha256=self.revision())
        resolved = self.action("save", path="entry.py", text=external.decode(), expected_sha256=self.revision(), resolve_conflict=True)
        self.assertEqual(resolved["sha256"], hashlib.sha256(external).hexdigest())
        self.assertIsNone(self.project.workbench_conflict)
        self.assertTrue((self.project_root / recovery["path"]).exists())

    def test_post_exchange_io_fault_is_typed_and_restart_remains_blocked(self):
        before = (self.project_root / "entry.py").read_bytes()
        with mock.patch.object(workbench, "_revision_at", side_effect=PermissionError("injected post-exchange read failure")):
            error = self.error("CONFLICT", self.action, "save", path="entry.py", text="VALUE = 2\n", expected_sha256=self.revision())
        self.assertEqual(error.status, 409)
        self.assertTrue(error.details["active_file_changed"])
        self.assertTrue((self.project_root / workbench.JOURNAL).is_file())
        self.assertEqual((self.project_root / error.details["recovery_paths"][0]["path"]).read_bytes(), before)
        restarted = worker.Project(str(self.project_root))
        self.assertEqual(restarted.state, "conflict")
        self.assertIsNotNone(restarted.workbench_conflict)
        self.assertFalse(worker.run_build(restarted, "certified"))
        self.assertEqual((self.project_root / "entry.py").read_text(), "VALUE = 2\n")

    def test_malformed_journal_objects_fail_closed_without_running_code(self):
        for value in ("null", "[]", "1", '{"schema":1}'):
            with self.subTest(journal=value):
                (self.project_root / workbench.JOURNAL).write_text(value)
                project = worker.Project(str(self.project_root))
                self.assertEqual(project.state, "conflict")
                self.assertIsNotNone(project.workbench_conflict)
                with mock.patch.object(project, "build", side_effect=AssertionError("must not execute")):
                    self.assertFalse(worker.run_build(project, "certified"))

    def test_prepared_journal_blocks_registration_before_python_execution(self):
        recovery = ".cq-workbench-recovery-test.py"
        (self.project_root / recovery).write_text("VALUE = 2\n")
        journal = {"schema":1,"path":"entry.py","phase":"prepared","recovery_paths":[{"path":recovery,"sha256":None,"bytes":None}]}
        (self.project_root / workbench.JOURNAL).write_text(json.dumps(journal))
        with mock.patch.object(worker, "PROJECTS", {}):
            registered = worker.register_project(str(self.project_root))
            self.assertEqual(registered.state, "conflict")
            self.assertFalse(registered.preview_dirty or registered.certified_due)
            with mock.patch.object(registered, "build", side_effect=AssertionError("must not execute")):
                self.assertFalse(worker.run_build(registered, "certified"))

    def test_no_blind_restore_over_later_external_revision(self):
        expected = self.revision()
        exchange = workbench._exchange_files
        first, later = b"VALUE = 99\n", b"VALUE = 100\n"
        calls = []
        def concurrent(directory, left, right):
            calls.append(right)
            file = self.root / "external-save.py"
            file.write_bytes(first)
            os.replace(file, self.project_root / right)
            exchange(directory, left, right)
            file.write_bytes(later)
            os.replace(file, self.project_root / right)
        with mock.patch.object(workbench, "_exchange_files", side_effect=concurrent):
            error = self.error("CONFLICT", self.action, "save", path="entry.py", text="VALUE = 2\n", expected_sha256=expected)
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.project_root / "entry.py").read_bytes(), later)
        self.assertEqual((self.project_root / error.details["recovery_paths"][0]["path"]).read_bytes(), first)
        self.assertEqual(error.details["draft_sha256"], hashlib.sha256(b"VALUE = 2\n").hexdigest())

    def test_unsupported_exchange_fails_closed(self):
        before = (self.project_root / "entry.py").read_bytes()
        unavailable = workbench.WorkbenchError(501, "ATOMIC_SAVE_UNAVAILABLE", "unsupported filesystem")
        with mock.patch.object(workbench, "_exchange_files", side_effect=unavailable):
            self.error("ATOMIC_SAVE_UNAVAILABLE", self.action, "save", path="entry.py", text="VALUE = 2\n", expected_sha256=self.revision())
        self.assertEqual((self.project_root / "entry.py").read_bytes(), before)
        self.assertFalse(any(p.name.startswith(".cq-workbench-") for p in self.project_root.iterdir()))

    def test_new_file_creation_is_explicit_and_create_only(self):
        self.error("INVALID_REVISION", self.action, "save", path="new.py", text="VALUE = 1\n")
        self.action("save", path="new.py", text="VALUE = 1\n", expected_sha256=None)
        self.error("CONFLICT", self.action, "save", path="new.py", text="VALUE = 2\n", expected_sha256=None)
        self.assertEqual((self.project_root / "new.py").read_text(), "VALUE = 1\n")

    def test_bad_paths_encodings_and_sizes_never_mutate_source(self):
        before = (self.project_root / "entry.py").read_bytes()
        for path in ("../entry.py", "/entry.py", "a/../entry.py", "entry.py\0", "a\\entry.py", ".git/entry.py", "shape.step"):
            with self.subTest(path=path), self.assertRaises(workbench.WorkbenchError):
                self.action("save", path=path, text="DRAFT\n", expected_sha256=self.revision())
        self.error("INVALID_ENCODING", self.action, "save", path="entry.py", text="\ud800", expected_sha256=self.revision())
        self.error("DOCUMENT_TOO_LARGE", self.action, "save", path="entry.py", text="x" * (workbench.TEXT_LIMIT + 1), expected_sha256=self.revision())
        self.assertEqual((self.project_root / "entry.py").read_bytes(), before)
        (self.project_root / "invalid.py").write_bytes(b"\xff")
        self.error("INVALID_ENCODING", self.action, "read", path="invalid.py")

    def test_symlink_leaf_and_ancestor_escape_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "entry.py").write_text("OUTSIDE = 1\n")
        (self.project_root / "link.py").symlink_to(outside / "entry.py")
        (self.project_root / "linked").symlink_to(outside)
        for path in ("link.py", "linked/entry.py"):
            self.error("SYMLINK_PATH", self.action, "read", path=path)
        self.assertEqual((outside / "entry.py").read_text(), "OUTSIDE = 1\n")

    def test_directory_replacement_during_save_is_not_silent(self):
        original = os.fsync
        moved = self.root / "moved"
        def relocate(fd):
            original(fd)
            self.project_root.rename(moved)
            self.project_root.mkdir()
            (self.project_root / "entry.py").write_text("REPLACEMENT = 1\n")
        with mock.patch.object(os, "fsync", side_effect=relocate):
            self.error("CONFLICT", workbench.save_text, self.project_root, "entry.py", "DRAFT = 1\n", self.revision())
        self.assertEqual((moved / "entry.py").read_text(), CODE)
        self.assertEqual((self.project_root / "entry.py").read_text(), "REPLACEMENT = 1\n")

    def test_bundled_demo_is_read_only(self):
        demo = worker.Project(str(ROOT / "testpart"))
        before = (ROOT / "testpart/part_entry.py").read_bytes()
        with mock.patch.object(worker, "PROJECTS", {demo.name: demo}):
            self.assertTrue(self.bench.list(demo)["read_only"])
            self.error("READ_ONLY", self.bench.dispatch, {"action": "save", "project": demo.name, "path": "part_entry.py", "text": "DRAFT\n", "expected_sha256": hashlib.sha256(before).hexdigest()})
        self.assertEqual((ROOT / "testpart/part_entry.py").read_bytes(), before)

    def test_save_copy_preserves_original_and_active_entry(self):
        target = self.root / "copy.py"
        expected = self.revision()
        result = self.action("saveAs", path="entry.py", destination=str(target), text="DRAFT = 9\n")
        self.assertTrue(result["copy"])
        self.assertFalse(result["active_project_changed"])
        self.assertEqual(self.revision(), expected)
        self.assertEqual(target.read_text(), "DRAFT = 9\n")
        self.error("CONFLICT", self.action, "saveAs", path="entry.py", destination=str(target), text="DRAFT = 10\n")
        self.assertEqual(target.read_text(), "DRAFT = 9\n")

    def test_cad_io_typed_output_conflict_details_are_preserved_without_source_block(self):
        class IOError(ValueError):
            def __init__(self):
                super().__init__("output conflict")
                self.status, self.code = 409, "OUTPUT_CHANGED"
                self.details = {"active_file_changed": True, "output_path": "/selected/output.step", "recovery_paths": [{"path": "/selected/recovery.step"}]}
        error = IOError()
        fake = type("IO", (), {"CadIOError": IOError, "export_geometry": staticmethod(lambda *args, **kwargs: (_ for _ in ()).throw(error))})
        with mock.patch.dict(sys.modules, {"cad_io": fake}), mock.patch.object(worker, "certified_record", return_value=mock.MagicMock(__enter__=lambda _: {}, __exit__=lambda *args: False)):
            captured = self.error("OUTPUT_CHANGED", self.action, "export", path="/selected/output.step", format="step")
        self.assertEqual(captured.status, 409)
        self.assertEqual(captured.details, error.details)
        self.assertIsNone(self.project.workbench_conflict)

    def test_auto_build_false_pauses_watch_updates_but_explicit_rebuild_is_possible(self):
        self.action("save", path="entry.py", text="VALUE = 2\n", expected_sha256=self.revision(), auto_build=False)
        self.assertEqual(self.project.state, "stale")
        self.assertFalse(self.project.preview_dirty or self.project.certified_due)
        worker._apply_changes([str(self.project_root / "entry.py")])
        self.assertFalse(self.project.preview_dirty or self.project.certified_due)


class ParameterTests(WorkbenchCase):
    def test_unicode_byte_spans_and_comments_are_preserved(self):
        text = 'LABEL = "☃"; WIDTH = 1.0  # mm\r\n\ndef build():\r\n    return {}\r\n'
        (self.project_root / "entry.py").write_bytes(text.encode())
        value = self.action("parameters", path="entry.py")
        self.assertEqual({p["name"] for p in value["parameters"]}, {"LABEL", "WIDTH"})
        self.action("setParameter", path="entry.py", name="WIDTH", value=2, expected_sha256=value["sha256"])
        self.assertEqual((self.project_root / "entry.py").read_bytes(), text.replace("WIDTH = 1.0", "WIDTH = 2.0").encode())

    def test_discovery_never_executes_source(self):
        sentinel = self.root / "should-not-exist"
        text = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\nWIDTH = 1.0\n"
        (self.project_root / "entry.py").write_text(text)
        self.assertEqual(self.action("parameters", path="entry.py")["parameters"][0]["name"], "WIDTH")
        self.assertFalse(sentinel.exists())

    def test_ambiguous_bindings_expressions_and_function_locals(self):
        for source in ("x=1\nx,y=(2,3)\n", "x=1\nif True: x=2\n", "x=1\nfor x in [2]: pass\n", "x=1\nx+=2\n", "x=1\nimport os as x\n", "x=1\ndef f():\n global x\n x=2\n", "x=y=1\n", "x=1+2\n"):
            self.assertEqual(workbench.parameter_spans(source), [], source)
        values = workbench.parameter_spans("x=1\ndef f():\n x=2\n")
        self.assertEqual([x[0] for x in values], ["x"])

    def test_parameter_types_are_preserved(self):
        for text, value in (("X=1.0\n", "oops"), ("X=1\n", True), ("X=True\n", 1), ("X='a'\n", 1), ("X=1\n", 1.5)):
            self.error("INVALID_PARAMETER_TYPE", workbench.replace_parameter, text, "X", value)
        self.error("INVALID_PARAMETER", workbench.replace_parameter, "X=1.0\n", "X", float("inf"))


class HTTPTests(WorkbenchCase):
    def setUp(self):
        super().setUp()
        self.token = "c" * 64
        patch = mock.patch.dict(os.environ, {"CASSQUERY_WORKBENCH_TOKEN": self.token})
        patch.start()
        self.addCleanup(patch.stop)
        self.server = worker.ThreadingHTTPServer(("127.0.0.1", 0), worker.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, body, token=None, duplicate=False, endpoint="/api/workbench"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            data = json.dumps(body).encode()
            connection.putrequest("POST", endpoint)
            connection.putheader("Content-Length", str(len(data)))
            if token is not None:
                connection.putheader("X-CassQuery-Workbench", token)
            if duplicate:
                connection.putheader("X-CassQuery-Workbench", token)
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_missing_wrong_duplicate_capability_rejected_before_dispatch(self):
        with mock.patch.object(workbench.Workbench, "dispatch") as dispatch:
            for token, duplicate in ((None, False), ("wrong", False), (self.token, True)):
                status, result = self.request({"action": "list", "project": "BOX"}, token, duplicate)
                self.assertEqual(status, 403)
                self.assertEqual(result["code"], "CAPABILITY_REQUIRED")
            dispatch.assert_not_called()

    def test_real_authorized_read_save_and_conflict(self):
        status, source = self.request({"action": "read", "project": "BOX", "path": "entry.py"}, self.token)
        self.assertEqual(status, 200)
        status, saved = self.request({"action": "save", "project": "BOX", "path": "entry.py", "text": "VALUE = 2\n", "expected_sha256": source["sha256"]}, self.token)
        self.assertEqual(status, 200)
        status, failed = self.request({"action": "save", "project": "BOX", "path": "entry.py", "text": "DRAFT = 3\n", "expected_sha256": source["sha256"]}, self.token)
        self.assertEqual(status, 409)
        self.assertEqual(failed["code"], "CONFLICT")

    def test_existing_agent_api_still_works_without_capability(self):
        status, result = self.request({"project": "BOX", "tier": "certified"}, endpoint="/api/rebuild")
        self.assertEqual(status, 202)
        self.assertTrue(result["accepted"])

    def test_owned_error_history_broadcast_and_http_sinks_redact_token(self):
        (self.project_root / "entry.py").write_text("import os\ndef build():\n    raise RuntimeError('diagnostic '+os.environ['CASSQUERY_WORKBENCH_TOKEN'])\n")
        self.assertFalse(worker.run_build(self.project, "certified"))
        serialized = json.dumps([self.project.status(), self.events])
        self.assertNotIn(self.token, serialized)
        self.assertIn("[redacted]", serialized)
        self.project.log("direct", self.token)
        self.assertNotIn(self.token, json.dumps(self.project.history))


class RegistryTests(WorkbenchCase):
    def test_explicit_create_and_register_preserve_unique_names_and_source(self):
        result = self.bench.dispatch({"action": "create", "directory": str(self.root), "name": "NEW"})
        self.assertEqual(result["project"], "NEW")
        self.assertEqual(result["root"], str(self.root / "NEW"))
        self.assertIn("WIDTH = 20.0", (self.root / "NEW/part_entry.py").read_text())
        self.error("PROJECT_EXISTS", self.bench.dispatch, {"action": "create", "directory": str(self.root), "name": "NEW"})

    def test_plain_python_requires_explicit_sidecar_consent_and_preserves_files(self):
        p = self.root / "plain"
        p.mkdir()
        code = b"result = None\n"
        (p / "plain.py").write_bytes(code)
        (p / "helper.py").write_bytes(b"VALUE = 2\n")
        self.error("ADAPTER_REQUIRED", self.bench.dispatch, {"action": "open", "path": str(p / "plain.py")})
        self.assertFalse((p / "viewer.json").exists())
        result = self.bench.dispatch({"action": "open", "path": str(p / "plain.py"), "adapt": True})
        self.assertEqual(result["source"], "plain.py")
        self.assertEqual((p / "plain.py").read_bytes(), code)
        self.assertEqual((p / "helper.py").read_bytes(), b"VALUE = 2\n")

    def test_registry_iteration_is_snapshot_based_under_concurrent_registration(self):
        def register(number):
            p = self.root / f"p{number}"
            p.mkdir()
            (p / "entry.py").write_text(CODE)
            (p / "viewer.json").write_text(json.dumps({"name": f"P{number}", "entry": "entry.py:build"}))
            return worker.register_project(str(p))
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(register, i) for i in range(20)]
            for _ in range(200):
                tuple(p.status() for p in worker.project_values())
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(len(worker.project_values()), 21)


@unittest.skipUnless(HAS_CAD, "requires existing CadQuery/OCP")
class CADTests(WorkbenchCase):
    def setUp(self):
        super().setUp()
        self.source = "import cadquery as cq\nWIDTH=2.0\ndef build():\n    return {'names':['Body'], 'parts':{'Body':cq.Workplane('XY').box(WIDTH,3,4).val()}}\n"
        (self.project_root / "entry.py").write_text(self.source)
        self.assertTrue(worker.run_build(self.project, "certified"), self.project.history)

    def test_brep_reference_bounds_do_not_inherit_tessellation_padding(self):
        project = worker.Project(str(ROOT / "testpart"))
        with worker.GEOMETRY_LOCK, worker._project_imports(project.root):
            shape = project.load_module("part_entry.py").build()["parts"]["Base"]
            before = worker._brep_bounds(shape)
            expected = [before.xmin, before.ymin, before.zmin, before.xmax, before.ymax, before.zmax]
            worker._tessellate_solid(shape, *worker.FINE)
            after = worker._brep_bounds(shape)
        actual = [after.xmin, after.ymin, after.zmin, after.xmax, after.ymax, after.zmax]
        for value, reference in zip(actual, [-20, -15, 0, 20, 15, 6]):
            self.assertAlmostEqual(value, reference, delta=1e-10)
        self.assertEqual(actual, expected)
        self.assertTrue(worker.run_build(project, "certified"), project.history)
        facts = project.certified["variant_truth"]["neutral"]["Base"]
        for value, reference in zip(facts["bbox"], [-20, -15, 0, 20, 15, 6]):
            self.assertAlmostEqual(value, reference, delta=1e-10)
        self.assertTrue(project.gates["pass"])
        self.assertEqual(len(project.gates["checks"]), 8)

    def test_true_transform_origin_order_and_pose_truth(self):
        (self.project_root / "poses.py").write_text("POSES={'lifted':lambda r:{n:s.translate((0,0,10)) for n,s in r['parts'].items()}}\n")
        manifest = json.loads((self.project_root / "viewer.json").read_text())
        manifest["poses"] = "poses.py"
        (self.project_root / "viewer.json").write_text(json.dumps(manifest))
        result = self.action("transform", part="Body", translation=[10,20,30], rotation=[0,0,90], scale=2, expected_sha256=self.revision("viewer.json"))
        self.assertEqual(result["previous"]["scale"], 1)
        self.assertEqual(result["manifest_sha256"], self.revision("viewer.json"))
        self.assertTrue(worker._build_pending(self.project), self.project.history)
        neutral = self.action("inspect", variant="neutral")
        lifted = self.action("inspect", variant="lifted")
        self.assertAlmostEqual(neutral["parts"][0]["volume"], 192)
        self.assertEqual([round(x,6) for x in neutral["parts"][0]["dimensions"]], [6,4,8])
        self.assertEqual([round(x,6) for x in neutral["parts"][0]["center"]], [10,20,30])
        self.assertEqual([round(x,6) for x in lifted["parts"][0]["center"]], [10,20,50])
        self.assertFalse(neutral["stale"])
        self.assertTrue(self.project.gates["pass"])
        self.assertEqual(len(self.project.gates["checks"]), 8)

    def test_preview_and_failed_gate_never_replace_certified_truth_or_shapes(self):
        retained = self.project.certified
        index = (self.payloads / "BOX-certified.json").read_bytes()
        (self.project_root / "entry.py").write_text(self.source.replace("WIDTH=2.0", "WIDTH=4.0"))
        self.assertTrue(worker.run_build(self.project, "preview"))
        inspected = self.action("inspect")
        self.assertAlmostEqual(inspected["parts"][0]["volume"], 24)
        self.assertTrue(inspected["stale"])
        self.assertIs(self.project.certified, retained)
        manifest = json.loads((self.project_root / "viewer.json").read_text())
        manifest["unit_scale"] = 2
        (self.project_root / "viewer.json").write_text(json.dumps(manifest))
        self.assertFalse(worker.run_build(self.project, "certified"))
        self.assertIs(self.project.certified, retained)
        self.assertEqual((self.payloads / "BOX-certified.json").read_bytes(), index)
        self.error("STALE_CERTIFICATE", lambda: worker.certified_record("BOX").__enter__())

    def test_late_watcher_keeps_already_certified_manual_inputs_current(self):
        manifest = json.loads((self.project_root / "viewer.json").read_text())
        manifest["live"] = False
        (self.project_root / "viewer.json").write_text(json.dumps(manifest))
        path = self.project_root / "entry.py"
        path.write_text(self.source.replace("WIDTH=2.0", "WIDTH=3.0"))
        self.assertTrue(worker.run_build(self.project, "certified"))
        generation = self.project.gen
        record = self.project.certified
        self.events.clear()
        worker._apply_changes([str(path)])  # Poll discovers the prior edit after manual completion.
        self.assertEqual(self.project.state, "certified")
        self.assertEqual(self.project.gen, generation)
        self.assertIs(self.project.certified, record)
        self.assertFalse(self.project.preview_dirty or self.project.certified_due)
        self.assertFalse(self.action("inspect")["stale"])
        self.assertFalse(any(e.get("state") == "stale" for e in self.events))
        with worker.certified_record("BOX") as current:
            current["assert_current"]()
            self.assertAlmostEqual(current["variants"]["neutral"]["Body"].Volume(), 36)
        path.write_text(self.source.replace("WIDTH=2.0", "WIDTH=4.0"))
        worker._apply_changes([str(path)])
        self.assertEqual(self.project.state, "stale")
        self.assertEqual(self.project.gen, generation + 1)
        self.assertTrue(self.action("inspect")["stale"])
        self.error("STALE_CERTIFICATE", lambda: worker.certified_record("BOX").__enter__())

    def test_comment_only_edit_requires_new_source_certificate_even_same_geometry_hash(self):
        before = self.project.certified
        self.action("save", path="entry.py", text=self.source+"# comment\n", expected_sha256=self.revision())
        self.assertTrue(worker._build_pending(self.project))
        self.assertEqual(self.project.certified["hash"], before["hash"])
        self.assertNotEqual(self.project.certified["source_fingerprint"], before["source_fingerprint"])
        self.assertGreater(self.project.certified["built_at"], before["built_at"])

    def test_export_record_is_retained_shape_and_source_guard_without_python_rerun(self):
        with mock.patch.object(self.project, "build", side_effect=AssertionError("must not rebuild for export")):
            with worker.certified_record("BOX") as record:
                self.assertAlmostEqual(record["variants"]["neutral"]["Body"].Volume(), 24)
                record["assert_current"]()
                self.action("save", path="entry.py", text=self.source+"# changed\n", expected_sha256=self.revision(), auto_build=False)
                self.error("STALE_CERTIFICATE", record["assert_current"])

    def test_geometry_lane_busy_is_bounded_and_status_lock_is_free(self):
        held, release = threading.Event(), threading.Event()
        def own_lane():
            with worker.GEOMETRY_LOCK:
                held.set()
                release.wait(timeout=3)
        thread = threading.Thread(target=own_lane)
        thread.start()
        self.assertTrue(held.wait(timeout=2))
        try:
            self.error("GEOMETRY_BUSY", lambda: worker.geometry_operation(timeout=.01).__enter__())
            self.assertEqual(self.project.status()["name"], "BOX")
        finally:
            release.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_original_adapter_executes_once_with_helpers_and_file_context(self):
        counter = self.root / "counter.txt"
        (self.project_root / "helper.py").write_text("WIDTH=2\n")
        (self.project_root / "dimensions.json").write_text('{"depth":3}')
        code = f"from pathlib import Path\nimport json, cadquery as cq\nimport helper\np=Path({str(counter)!r});p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\ndepth=json.loads(Path('dimensions.json').read_text())['depth']\nassert Path(__file__).name=='original.py'\nresult=cq.Workplane('XY').box(helper.WIDTH,depth,4)\nshow_object(result,name='Body')\n"
        path = self.project_root / "original.py"
        path.write_text(code)
        before = path.read_bytes()
        with worker.GEOMETRY_LOCK, worker._project_imports(str(self.project_root)):
            result = workbench.build_script(path)
        self.assertEqual(list(result["parts"]), ["Body"])
        self.assertAlmostEqual(result["parts"]["Body"].Volume(), 24)
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(path.read_bytes(), before)

    def test_adapter_collects_all_workplane_values_and_rejects_duplicate_construction(self):
        path = self.project_root / "original.py"
        path.write_text("import cadquery as cq\nresult=cq.Workplane('XY').newObject([cq.Workplane('XY').box(2,3,4).val(),cq.Workplane('XY').box(2,3,4).val().translate((10,0,0))])\n")
        with worker.GEOMETRY_LOCK, worker._project_imports(str(self.project_root)):
            result = workbench.build_script(path)
        self.assertEqual(len(result["parts"]["Result"].Solids()), 2)
        path.write_text("import cadquery as cq\ns=cq.Workplane('XY').box(2,3,4)\nshow_object(s,name='Same')\nshow_object(s,name='Same')\n")
        with self.assertRaises(ValueError), worker.GEOMETRY_LOCK, worker._project_imports(str(self.project_root)):
            workbench.build_script(path)
        path.write_text("import cadquery as cq\nresult=cq.Sketch().rect(2,3)\n")
        with self.assertRaises(ValueError), worker.GEOMETRY_LOCK, worker._project_imports(str(self.project_root)):
            workbench.build_script(path)


if __name__ == "__main__":
    unittest.main()
