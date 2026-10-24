"""Promotion qualification regressions: synthetic API context, real entrypoint/ZIP bytes.

These fixtures prove the release decision edges, not a native build, human approval,
or a GitHub write. The existing native conformance fixture remains Agent9-owned.
"""
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_release_publish as base

m = base.m


class QualificationAPI(base.FakeAPI):
    def __init__(self):
        super().__init__()
        repo = copy.deepcopy(self.run["repository"])
        self.integration = {"id": 101, "run_number": 11, "run_attempt": 1,
                            "repository": repo, "head_repository": copy.deepcopy(repo),
                            "head_sha": base.SHA, "head_branch": "main", "event": "push",
                            "workflow_id": 7, "path": m.INTEGRATION_WORKFLOW,
                            "status": "completed", "conclusion": "success"}
        self.integration_runs = None  # Default inventory reflects the current run.
        self.integration_total = None
        self.integration_jobs = [{"id": 31, "run_id": 101, "head_sha": base.SHA,
                                  "run_attempt": 1, "head_branch": "main", "name": "integration",
                                  "status": "completed", "conclusion": "success",
                                  "steps": [{"name": name, "status": "completed", "conclusion": "success"}
                                            for name in base.evidence.INTEGRATION_STEPS]}]
        self.integration_job_total = 1
        self.team = {"id": 800, "slug": "release-team", "organization": {"login": m.OWNER}}
        self.members = [{"id": 900, "login": "reviewer", "type": "User"}]
        self.team_denied = False

    def json(self, path, *, method="GET", payload=None, missing=False):
        location = urllib.parse.urlsplit(path)
        tail = location.path.removeprefix(m.PREFIX)
        integration_path = tail in ("/actions/workflows/integration.yml", "/actions/workflows/7/runs", "/actions/runs/101")
        integration_path = integration_path or tail.startswith("/actions/runs/101/attempts/")
        team_path = location.path.startswith(f"/orgs/{m.OWNER}/teams/")
        if not integration_path and not team_path:
            return super().json(path, method=method, payload=payload, missing=missing)
        self.calls.append((method, path))
        if self.on_call:
            self.on_call(method, location.path)
        if team_path:
            if self.team_denied:
                raise m.ReleaseError("GitHub GET failed (HTTP 403)")
            if location.path.endswith("/members"):
                page = int(urllib.parse.parse_qs(location.query).get("page", ["1"])[0])
                return copy.deepcopy(self.members[(page - 1) * 100:page * 100])
            return copy.deepcopy(self.team)
        if tail == "/actions/workflows/integration.yml":
            return {"id": 7, "path": m.INTEGRATION_WORKFLOW}
        if tail == "/actions/workflows/7/runs":
            rows = self.integration_runs if self.integration_runs is not None else [self.integration]
            total = len(rows) if self.integration_total is None else self.integration_total
            page = int(urllib.parse.parse_qs(location.query).get("page", ["1"])[0])
            return {"total_count": total, "workflow_runs": copy.deepcopy(rows[(page - 1) * 100:page * 100])}
        if tail == "/actions/runs/101":
            return copy.deepcopy(self.integration)
        return {"total_count": self.integration_job_total, "jobs": copy.deepcopy(self.integration_jobs)}


class QualificationTests(unittest.TestCase):
    set_event = base.PromotionTests.set_event
    prepare = base.PromotionTests.prepare
    execute = base.PromotionTests.execute
    writes = base.PromotionTests.writes

    def setUp(self):
        base.PromotionTests.setUp(self)
        self.api = QualificationAPI()

    def test_actual_main_blocks_failed_integration_before_any_write_or_candidate_download(self):
        self.api.integration["conclusion"] = "failure"
        self.set_event("publish")
        argv = ["release_publish.py", "--candidate-run-id", str(base.RUN_ID), "--tag", base.TAG,
                "--source-sha", base.SHA, "--allow-ad-hoc", "--mode", "publish", "--execute"]
        output, error = io.StringIO(), io.StringIO()
        with patch.object(m, "GitHub", return_value=self.api), patch.object(sys, "argv", argv), \
             patch.dict(os.environ, {**self.env, "GITHUB_TOKEN": "offline-only"}, clear=True), \
             patch.object(sys, "stdout", output), patch.object(sys, "stderr", error):
            self.assertEqual(m.main(), 1)
        self.assertIn("integration", error.getvalue().lower())
        self.assertTrue(any("/actions/runs/101" in path for method, path in self.api.calls))
        self.assertFalse(self.writes())
        self.assertFalse(any(method == "DOWNLOAD" for method, path in self.api.calls))

    def test_latest_failed_or_in_progress_run_never_falls_back_to_older_green(self):
        old = {**self.api.integration, "id": 90, "run_number": 10}
        for status, conclusion in (("completed", "failure"), ("in_progress", None), ("completed", "cancelled")):
            with self.subTest(status=status, conclusion=conclusion):
                self.api.integration.update(status=status, conclusion=conclusion)
                self.api.integration_runs = [old, self.api.integration]
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_missing_duplicate_excessive_or_truncated_integration_inventory_blocks(self):
        changes = [lambda: setattr(self.api, "integration_runs", []),
                   lambda: setattr(self.api, "integration_runs", [self.api.integration, self.api.integration]),
                   lambda: setattr(self.api, "integration_total", 201),
                   lambda: setattr(self.api, "integration_total", 2),
                   lambda: setattr(self.api, "integration_job_total", 2)]
        for change in changes:
            with self.subTest(change=change):
                self.api = QualificationAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_every_integration_step_and_actual_job_source_identity_are_mandatory(self):
        changes = [lambda: self.api.integration.update(head_sha="d" * 40),
                   lambda: self.api.integration.update(event="workflow_dispatch"),
                   lambda: self.api.integration.update(head_branch="dev"),
                   lambda: self.api.integration.update(workflow_id=99),
                   lambda: self.api.integration["head_repository"].update(full_name="fork/CassQuery"),
                   lambda: self.api.integration_jobs[0].update(run_id=999),
                   lambda: self.api.integration_jobs[0].update(head_sha="d" * 40),
                   lambda: self.api.integration_jobs[0].update(run_attempt=2),
                   lambda: self.api.integration_jobs[0].update(conclusion="failure")]
        for step in base.evidence.INTEGRATION_STEPS:
            changes.append(lambda step=step: next(row for row in self.api.integration_jobs[0]["steps"]
                                                  if row["name"] == step).update(conclusion="skipped"))
        for change in changes:
            with self.subTest(change=change):
                self.api = QualificationAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_rerun_starting_while_attempt_jobs_are_read_blocks(self):
        def rerun(method, path):
            if "/actions/runs/101/attempts/1/jobs" in path:
                self.api.integration["run_attempt"] = 2
        self.api.on_call = rerun
        with self.assertRaisesRegex(ValueError, "attempt changed"):
            self.prepare()
        self.assertFalse(self.writes())

    def test_latest_run_or_attempt_change_after_preparation_blocks_all_writes(self):
        plan = self.prepare()
        self.api.integration.update(run_attempt=2)
        self.api.integration_jobs[0]["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "integration run/attempt changed"):
            self.execute(plan, "publish")
        self.assertFalse(self.writes())

    def test_integration_failure_after_draft_or_upload_leaves_draft_unpublished(self):
        for boundary in ("draft", "upload"):
            with self.subTest(boundary=boundary):
                self.api = QualificationAPI()
                plan = self.prepare()
                if boundary == "draft":
                    def fail_after_draft(method, path):
                        if method == "POST":
                            self.api.integration["conclusion"] = "failure"
                    self.api.on_call = fail_after_draft
                else:
                    original = self.api.upload
                    def fail_after_upload(*args):
                        value = original(*args)
                        self.api.integration["conclusion"] = "failure"
                        return value
                    self.api.upload = fail_after_upload
                with self.assertRaises(ValueError):
                    self.execute(plan, "publish")
                self.assertTrue(self.api.releases[0]["draft"])
                self.assertFalse(any(method == "PATCH" for method, path in self.writes()))
                self.assertEqual(len(self.api.assets), 0 if boundary == "draft" else 1)

    def test_each_contents_write_has_a_fresh_native_and_integration_check(self):
        result = self.execute(self.prepare(), "publish")
        self.assertFalse(result["draft"])
        self.assertEqual(result["integration"]["run_id"], 101)
        previous = 0
        for index, (method, path) in enumerate(self.api.calls):
            if method not in ("POST", "PATCH", "UPLOAD"):
                continue
            between = self.api.calls[previous:index]
            self.assertTrue(any("/actions/runs/101/attempts/1/jobs" in endpoint for _, endpoint in between))
            self.assertTrue(any("/actions/runs/100/attempts/1/jobs" in endpoint for _, endpoint in between))
            self.assertEqual(between[-1], ("GET", m.PREFIX + "/actions/runs/101"))
            previous = index + 1

    def owner_policy(self, prevent):
        rule = self.api.environment["protection_rules"][0]
        owner_id = 111076541  # Actual public owner identity in the captured environment/run API.
        rule.update(prevent_self_review=prevent, reviewers=[{"type": "User", "reviewer": {"id": owner_id}}])
        self.api.reviews[0]["user"].update(id=owner_id, login=m.OWNER)
        self.api.current_run["actor"]["id"] = owner_id
        self.api.current_run["triggering_actor"]["id"] = owner_id

    def test_owner_approval_requires_explicit_permitted_current_reviewer_policy(self):
        self.owner_policy(False)
        self.assertTrue(self.execute(self.prepare())["draft"])
        self.api = QualificationAPI()
        self.owner_policy(True)
        with self.assertRaisesRegex(ValueError, "protected environment approval"):
            self.execute(self.prepare())
        self.assertFalse(self.writes())

    def test_owner_approval_identity_must_match_both_server_actor_ids(self):
        for actor in ("actor", "triggering_actor"):
            with self.subTest(actor=actor):
                self.api = QualificationAPI()
                self.owner_policy(False)
                plan = self.prepare()
                self.api.current_run[actor]["id"] = 901
                with self.assertRaisesRegex(ValueError, "protected environment approval"):
                    self.execute(plan, "publish")
                self.assertFalse(self.writes())

    def test_missing_or_unauthorized_approval_and_current_reviewer_replacement_block(self):
        changes = [lambda: self.api.reviews.clear(),
                   lambda: self.api.reviews[0]["user"].update(id=901),
                   lambda: self.api.environment["protection_rules"][0]["reviewers"][0]["reviewer"].update(id=901)]
        for change in changes:
            with self.subTest(change=change):
                self.api = QualificationAPI()
                plan = self.prepare()
                change()
                with self.assertRaises(ValueError):
                    self.execute(plan, "publish")
                self.assertFalse(self.writes())

    def test_changed_self_review_policy_before_next_upload_blocks_owner(self):
        self.owner_policy(False)
        plan = self.prepare()
        original = self.api.upload
        def change_policy(*args):
            result = original(*args)
            self.api.environment["protection_rules"][0]["prevent_self_review"] = True
            return result
        self.api.upload = change_policy
        with self.assertRaisesRegex(ValueError, "protected environment approval"):
            self.execute(plan, "publish")
        self.assertEqual(len(self.api.assets), 1)
        self.assertFalse(any(method == "PATCH" for method, path in self.writes()))

    def test_team_approval_uses_bound_team_id_and_paginated_current_member_id(self):
        self.api.environment["protection_rules"][0]["reviewers"] = [{"type": "Team", "reviewer": {"id": 800, "slug": "release-team"}}]
        self.api.members = [{"id": 9000 + i, "login": f"member-{i}", "type": "User"} for i in range(100)] + self.api.members
        self.assertTrue(self.execute(self.prepare())["draft"])
        self.assertTrue(any("/teams/release-team/members?per_page=100&page=2" in path for _, path in self.api.calls))

    def test_team_denial_wrong_identity_or_removed_membership_never_falls_back(self):
        changes = [lambda: setattr(self.api, "team_denied", True), lambda: self.api.team.update(id=801),
                   lambda: setattr(self.api, "members", []),
                   lambda: self.api.members[0].update(id=901)]
        for change in changes:
            with self.subTest(change=change):
                self.api = QualificationAPI()
                self.api.environment["protection_rules"][0]["reviewers"] = [{"type": "Team", "reviewer": {"id": 800, "slug": "release-team"}}]
                plan = self.prepare()
                change()
                with self.assertRaises(ValueError):
                    self.execute(plan, "publish")
                self.assertFalse(self.writes())


class QualificationNetworkTests(unittest.TestCase):
    def test_team_routes_are_owner_scoped_read_only_and_inventory_bounded(self):
        for path, method in (("/orgs/other/teams/release-team", "GET"),
                             (f"/orgs/{m.OWNER}/teams/release-team", "POST")):
            opener = base.Opener([])
            with self.assertRaises(ValueError):
                m.GitHub("offline-only", opener).json(path, method=method)
            self.assertFalse(opener.requests)
        opener = base.Opener([base.Response(200, b'{"total_count":201,"workflow_runs":[]}')])
        with self.assertRaisesRegex(ValueError, "total_count"):
            m.GitHub("offline-only", opener).items(m.PREFIX + "/actions/workflows/7/runs", "workflow_runs", limit=200)
        self.assertEqual(len(opener.requests), 1)


if __name__ == "__main__":
    unittest.main()
