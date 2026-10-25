"""Independent v0.2 trust-boundary regressions; no GUI, listener or install actor.

These exercise actual worker/workbench interfaces. They are source proof only;
passing them does not qualify native dialogs, frozen runtime or update installs.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from email.message import Message
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker
import workbench


def digest(data):
    return hashlib.sha256(data).hexdigest()


class WorkbenchSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cq-security-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "viewer.json").write_text(json.dumps({"name": "Security", "entry": "entry.py:build", "live": True}))
        self.file = self.root / "entry.py"
        self.file.write_text("original\n")
        self.project = worker.Project(str(self.root))
        self.projects = mock.patch.object(worker, "PROJECTS", {self.project.name: self.project})
        self.projects.start()
        self.addCleanup(self.projects.stop)
        self.broadcast = mock.patch.object(worker, "broadcast")
        self.events = self.broadcast.start()
        self.addCleanup(self.broadcast.stop)
        self.api = workbench.Workbench(worker)

    def save(self, text, expected):
        return self.api.dispatch({"action": "save", "project": self.project.name,
                                  "path": "entry.py", "text": text,
                                  "expected_sha256": expected, "auto_build": False})

    def test_capability_requires_one_exact_header_without_default_authority(self):
        token = "a" * 64
        with mock.patch.dict(os.environ, {"CASSQUERY_WORKBENCH_TOKEN": token}):
            for values in ([], ["b" * 64], [token, token]):
                with self.subTest(count=len(values)):
                    headers = Message()
                    for value in values:
                        headers["X-CassQuery-Workbench"] = value
                    with self.assertRaises(workbench.WorkbenchError) as caught:
                        workbench.authorize(headers)
                    self.assertEqual((caught.exception.status, caught.exception.code), (403, "CAPABILITY_REQUIRED"))
            headers = Message()
            headers["X-CassQuery-Workbench"] = token
            workbench.authorize(headers)
        with mock.patch.dict(os.environ, {"CASSQUERY_WORKBENCH_TOKEN": ""}):
            with self.assertRaises(workbench.WorkbenchError):
                workbench.authorize(headers)

    def test_competing_workbench_writers_have_one_commit_and_one_conflict(self):
        expected = digest(self.file.read_bytes())
        barrier, outcomes = threading.Barrier(2), []
        def writer(text):
            barrier.wait(timeout=3)
            try:
                result = self.save(text, expected)
                outcomes.append((200, result["sha256"]))
            except workbench.WorkbenchError as error:
                outcomes.append((error.status, error.code))
        threads = [threading.Thread(target=writer, args=(text,)) for text in ("writer one\n", "writer two\n")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(status for status, _ in outcomes), [200, 409])
        self.assertIn(self.file.read_text(), ("writer one\n", "writer two\n"))

    def test_exchange_conflict_retains_external_revision_and_blocks_build_export(self):
        expected, original_exchange = digest(self.file.read_bytes()), workbench._exchange_files
        external, editor = b"external committed revision\n", "editor draft\n"
        def external_at_exchange(directory, left, right):
            self.file.write_bytes(external)
            return original_exchange(directory, left, right)
        with mock.patch.object(workbench, "_exchange_files", side_effect=external_at_exchange):
            with self.assertRaises(workbench.WorkbenchError) as caught:
                self.save(editor, expected)
        error = caught.exception
        self.assertEqual((error.status, error.code), (409, "CONFLICT"))
        self.assertTrue(error.details["active_file_changed"])
        self.assertEqual(self.file.read_text(), editor)
        self.assertIn(external, [(self.root / r["path"]).read_bytes() for r in error.details["recovery_paths"]])
        self.assertIsNotNone(self.project.workbench_conflict)
        self.assertFalse(worker.run_build(self.project, "certified"))
        with self.assertRaises(workbench.WorkbenchError) as export_error:
            with worker.certified_record(self.project.name):
                self.fail("Unresolved conflict yielded an export certificate")
        self.assertEqual(export_error.exception.status, 409)

    def test_post_exchange_io_error_preserves_revisions_and_marks_unresolved_transaction(self):
        original, editor = self.file.read_bytes(), "editor draft\n"
        with mock.patch.object(workbench, "_revision_at", side_effect=PermissionError("controlled displaced-revision read denied")):
            with self.assertRaises(workbench.WorkbenchError) as caught:
                self.save(editor, digest(original))
        error = caught.exception
        self.assertEqual(error.status, 409)
        self.assertTrue(error.details["active_file_changed"])
        self.assertEqual(self.file.read_text(), editor)
        self.assertIn(original, [(self.root / r["path"]).read_bytes() for r in error.details["recovery_paths"]])
        self.assertIsNotNone(self.project.workbench_conflict)
        self.assertGreater(self.project.gen, 0)
        self.assertFalse(worker.run_build(self.project, "certified"))

    def test_interrupted_exchange_journal_blocks_restart_until_explicit_resolution(self):
        original = self.file.read_bytes()
        marker = self.root / "executed.txt"
        editor = "from pathlib import Path\ndef build():\n    Path(__file__).with_name('executed.txt').write_text('executed')\n    raise RuntimeError('should not execute an unresolved draft')\n"
        with mock.patch.object(workbench, "_revision_at", side_effect=PermissionError("controlled post-exchange denial")):
            with self.assertRaises(workbench.WorkbenchError) as caught:
                self.save(editor, digest(original))
        recovery = [(self.root / item["path"]) for item in caught.exception.details["recovery_paths"]]
        self.assertIn(original, [path.read_bytes() for path in recovery])
        self.assertTrue((self.root / workbench.JOURNAL).is_file())
        fresh = worker.Project(str(self.root))
        self.assertIsNotNone(fresh.workbench_conflict)
        self.assertEqual(fresh.state, "conflict")
        with mock.patch.object(worker, "PROJECTS", {}):
            registered = worker.register_project(str(self.root))
            self.assertFalse(registered.preview_dirty)
            self.assertFalse(registered.certified_due)
            worker._apply_changes([str(self.file)])
            self.assertFalse(registered.preview_dirty)
            self.assertFalse(registered.certified_due)
            self.assertFalse(worker.run_build(registered, "certified"))
            self.assertFalse(marker.exists())
            with self.assertRaises(workbench.WorkbenchError):
                with worker.certified_record(registered.name):
                    self.fail("Restart admitted an unresolved certificate")
            resolved = "def build():\n    return {}\n"
            result = workbench.Workbench(worker).dispatch({
                "action": "save", "project": registered.name, "path": "entry.py",
                "text": resolved, "expected_sha256": digest(self.file.read_bytes()),
                "auto_build": False, "resolve_conflict": True,
            })
            self.assertEqual(result["sha256"], digest(resolved.encode()))
            self.assertIsNone(registered.workbench_conflict)
            self.assertFalse((self.root / workbench.JOURNAL).exists())
            self.assertIn(original, [path.read_bytes() for path in recovery])
            self.assertFalse(marker.exists())

    def test_unsupported_exchange_fails_closed_without_replacing_source(self):
        original = self.file.read_bytes()
        unavailable = workbench.WorkbenchError(501, "ATOMIC_SAVE_UNAVAILABLE", "controlled unsupported platform")
        with mock.patch.object(workbench, "_exchange_files", side_effect=unavailable):
            with self.assertRaises(workbench.WorkbenchError) as caught:
                self.save("new draft\n", digest(original))
        self.assertEqual((caught.exception.status, caught.exception.code), (501, "ATOMIC_SAVE_UNAVAILABLE"))
        self.assertEqual(self.file.read_bytes(), original)
        self.assertFalse((self.root / workbench.JOURNAL).exists())
        self.assertEqual(list(self.root.glob(".cq-workbench-recovery-*")), [])

    def test_path_symlink_encoding_and_size_rejections_preserve_source(self):
        original = self.file.read_bytes()
        outside = self.root.parent / (self.root.name + "-outside.py")
        outside.write_bytes(b"outside\n")
        self.addCleanup(outside.unlink)
        (self.root / "linked.py").symlink_to(outside)
        for name in ("../entry.py", "entry.py\0", "linked.py", "entry.txt"):
            with self.subTest(path=name):
                # The HTTP boundary maps OS errors to typed 400s. On Darwin
                # O_NOFOLLOW raises ELOOP=62 rather than Linux's 40; byte safety
                # must hold without hardcoding the OS errno in this helper test.
                with self.assertRaises((workbench.WorkbenchError, OSError)) as caught:
                    workbench.save_text(self.root, name, "replacement\n", digest(original))
                if isinstance(caught.exception, OSError):
                    self.assertEqual(name, "linked.py")
                    self.assertEqual(caught.exception.errno, errno.ELOOP)
        for text in ("\ud800", "x" * (workbench.TEXT_LIMIT + 1)):
            with self.assertRaises(workbench.WorkbenchError):
                workbench.save_text(self.root, "entry.py", text, digest(original))
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(outside.read_bytes(), b"outside\n")

    def test_private_capability_is_redacted_from_build_status_history_and_events(self):
        token = "0123456789abcdef" * 4  # Synthetic token, never print/persist it.
        self.file.write_text("import os\ndef build():\n    raise RuntimeError('useful diagnostic ' + os.environ['CASSQUERY_WORKBENCH_TOKEN'])\n")
        with mock.patch.dict(os.environ, {"CASSQUERY_WORKBENCH_TOKEN": token}):
            self.assertFalse(worker.run_build(self.project, "certified"))
            for value in (self.project.status(), self.project.history, self.events.call_args_list):
                self.assertNotIn(token, str(value))
            self.assertIn("useful diagnostic", str(self.project.status()))
        self.assertEqual(self.project.state, "error")


if __name__ == "__main__":
    unittest.main()
