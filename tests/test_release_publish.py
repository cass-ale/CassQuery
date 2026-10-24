"""Bounded real-byte fixtures for the protected publication boundary (no GitHub writes)."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import struct
import sys
import zlib
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_evidence as evidence
spec = importlib.util.spec_from_file_location("release_publish", ROOT / "tools/release_publish.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SHA = "a" * 40
TAG_SHA = "b" * 40
LOCKED_INPUTS = {name: (ROOT / name).read_bytes() for name in
                 ("package.json", "requirements-build.txt", "package-lock.json", "packaging/requirements-macos.lock", "packaging/wheel-manifest.json")}
SOURCE_FILES = [{"path": name, "bytes": len(data), "executable": False, "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(LOCKED_INPUTS.items())]
FINGERPRINT = hashlib.sha256(json.dumps({"source_sha": SHA, "files": SOURCE_FILES}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
VERSION = json.loads(LOCKED_INPUTS["package.json"])["version"]
TAG = "v" + VERSION
major, minor, patch_version = map(int, VERSION.split("."))
OTHER_VERSION = f"{major}.{minor}.{patch_version + 1}"
OTHER_TAG = "v" + OTHER_VERSION
RUN_ID = 100
PNG_BYTES = {}


def record(name, data):
    return {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def candidate_bytes(arch, mutate=None, extra=None):
    # These are complete protocol fixtures, not claims of a rendered native app.
    files = dict(LOCKED_INPUTS)
    identity = {"schema": 1, "pass": True, "source_sha": SHA, "architecture": arch, "version": VERSION}
    for suffix in ("zip", "dmg"):
        files[f"dist/mac-app/CassQuery-{VERSION}-mac-{arch}.{suffix}"] = f"original-{arch}-{suffix}".encode()

    def put(name, value):
        files[name] = json.dumps(value).encode()
        return record(name, files[name])

    def linked(name, report_dir):
        return {**record(name, files[name]), "path": str(Path(name).relative_to(report_dir))}

    def png(index):
        if index in PNG_BYTES:
            return PNG_BYTES[index]
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        raw = b"".join(b"\x00" + bytes((x * 37 + y * 83 + index) % 256 for x in range(300)) for y in range(100))
        PNG_BYTES[index] = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 2, 0, 0, 0))
                            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        return PNG_BYTES[index]

    matrix = [{"theme": theme, "width": width, "state": state, "status": "checked",
               "vertices": 10, "meshes": 1, "clipped": 0, "nonfinite": 0, "overflow": False,
               "bad": [], "overlaps": [], "errors": [], "realized_theme": theme, "controls_checked": 3,
               "gates_pass": True, "tier": "certified", "viewport": [width, 700], "diag": "",
               "background": "black" if theme == "dark" else "white",
               "step_pressed": ["true", "false"] if state == "assembled" else ["false", "true"],
               "explode": "0%" if state == "assembled" else "100%",
               "canvas": {"width": width, "height": 700, "client_width": width, "client_height": 700}}
              for theme in ("dark", "light") for width in (450, 700, 1000, 1320) for state in ("assembled", "apart")]

    def renderer(directory):
        images = []
        for i in range(18):
            path = directory + f"/frame-{i}.png"
            files[path] = png(i)
            images.append({**linked(path, directory), "width": 100, "height": 100})
        native = {**identity, "matrix": copy.deepcopy(matrix), "images": images,
                  "lifecycle": {"pass": True, "close_reopen": True, "secondary_exit": 0, "secondary_pid": 12,
                                "second_instance_events": 1, "worker_pid_before": 11, "worker_pid_after": 11},
                  "worker_pid": 11, "app_pid": 10, "packaged": True, "bundled_worker": True,
                  "worker_shutdown": True, "challenge": "fixture", "errors": []}
        put(directory + "/renderer.json", native)
        return native

    renderer(f"evidence/mac-{arch}")
    for extension in ("zip", "dmg"):
        directory = f"evidence/{extension}-{arch}"
        launches = {}
        for launch in (("mounted", "copied") if extension == "dmg" else ("extracted", "copied")):
            native = renderer(directory + "/" + launch)
            launches[launch] = {"matrix": copy.deepcopy(matrix),
                                "native_report": linked(directory + "/" + launch + "/renderer.json", directory)}
        asset = f"dist/mac-app/CassQuery-{VERSION}-mac-{arch}.{extension}"
        put(directory + "/package.json", {**identity, "asset": record(asset, files[asset]),
                                         "launches": launches, "temp_root_removed": True, "detach_completed": True})
    stages = []
    for stage in ("syntax", "node", "python", "release-contract", "brand-exports"):
        path = "evidence/" + stage + ".log"
        files[path] = b"bounded successful protocol fixture, not actual CI\n"
        stages.append({"name": stage, "exit": 0, **linked(path, "evidence")})
    put("evidence/preflight.json", {**identity, "source_fingerprint": FINGERPRINT, "after_fingerprint": FINGERPRINT,
                                    "clean": True, "failures": 0, "skipped": 0, "tests": 20, "stages": stages,
                                    "started_at": 1, "completed_at": 2})
    runtime_path = f"evidence/worker-{arch}-runtime.json"
    runtime = {"pid": 11, "challenge": "fixture", "frozen": True}
    put(runtime_path, runtime)
    ids = ('units', 'watertight', 'volume', 'thin-extent', 'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes')
    put(f"evidence/worker-{arch}.json", {**identity, "gates": {"pass": True, "checks": [{"id": gate, "status": "pass"} for gate in ids]},
                                         "worker_pid": 11, "challenge": "fixture", "worker_runtime": runtime,
                                         "worker_shutdown": True, "runtime_evidence": linked(runtime_path, "evidence")})
    components = []
    wheels = json.loads(LOCKED_INPUTS["packaging/wheel-manifest.json"])["wheels"]
    for line in LOCKED_INPUTS["requirements-build.txt"].decode().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        canonical = m.re.sub(r"[-_.]+", "-", name).lower()
        components.append({"type": "library", "name": canonical, "version": version,
                           "purl": f"pkg:pypi/{canonical}@{version}",
                           "hashes": [{"alg": "SHA-256", "content": sha} for sha in sorted(
                               {wheel["sha256"] for wheel in wheels if wheel["name"] == canonical})]})
    packages = json.loads(LOCKED_INPUTS["package-lock.json"])["packages"]
    npm = {}
    for path, item in packages.items():
        if path and item.get("version"):
            name = path.rsplit("node_modules/", 1)[-1]
            version = item["version"]
            npm[name, version] = {"type": "library", "name": name, "version": version,
                                  "purl": f'pkg:npm/{name.replace("@", "%40")}@{version}',
                                  "properties": [{"name": "npm:integrity", "value": item.get("integrity", "")}]}
    components.extend(npm.values())
    put("evidence/sbom.json", {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": components,
                                "metadata": {"properties": [{"name": "source_sha", "value": SHA},
                                                            {"name": "python-wheel-lock-sha256", "value": hashlib.sha256(LOCKED_INPUTS["packaging/requirements-macos.lock"]).hexdigest()},
                                                            {"name": "npm-lock-sha256", "value": hashlib.sha256(LOCKED_INPUTS["package-lock.json"]).hexdigest()}]}})
    put("evidence/source-before.json", {"schema": 1, "clean": True, "source_sha": SHA,
                                         "fingerprint": FINGERPRINT, "files": SOURCE_FILES})
    files["evidence/runner-context.txt"] = b"bounded native CI protocol fixture\n"
    records = [record(name, data) for name, data in sorted(files.items())]
    by_name = {x["path"]: x for x in records}
    manifest = {"schema": 1, "repository": m.REPOSITORY, "source_sha": SHA,
                "source_fingerprint": FINGERPRINT, "version": VERSION, "architecture": arch,
                "run_id": RUN_ID, "run_attempt": 1, "distribution": "adhoc", "files": records,
                "assets": [by_name[f"dist/mac-app/CassQuery-{VERSION}-mac-{arch}.{suffix}"] for suffix in ("zip", "dmg")],
                "gates": {gate: by_name[template.format(arch=arch)] for gate, template in evidence.GATES.items()}}
    if mutate:
        mutate(manifest, files)
    files["candidate.json"] = json.dumps(manifest, indent=2).encode()
    if extra:
        files.update(extra)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue(), files


class FakeAPI:
    """Endpoint-level fixture; runs the production paginator and evidence verifier."""
    items = m.GitHub.items

    def __init__(self):
        self.calls = []
        self.environment = {"id": 700, "name": "releases", "protection_rules": [{"type": "required_reviewers",
                            "prevent_self_review": True, "reviewers": [{"type": "User", "reviewer": {"id": 900}}]}],
                            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        self.policies = [{"id": 701, "name": "v*", "type": "tag"}]
        repo = {"id": 1, "full_name": m.REPOSITORY}
        self.run = {"id": RUN_ID, "run_attempt": 1, "repository": repo, "head_repository": repo,
                    "head_sha": SHA, "event": "push", "head_branch": "main", "status": "completed",
                    "conclusion": "success", "path": m.CANDIDATE_WORKFLOW, "workflow_id": 5}
        self.jobs = [{"id": 20 + i, "name": name, "status": "completed", "conclusion": "success",
                      "steps": [{"name": step, "status": "completed", "conclusion": "success"}
                                for step in evidence.REQUIRED_STEPS]}
                     for i, name in enumerate(("candidate (macos-15, arm64)", "candidate (macos-15-intel, x64)"))]
        self.archives = {}
        self.originals = {}
        self.artifacts = []
        for i, arch in enumerate(m.ARCHES):
            data, files = candidate_bytes(arch)
            self.archives[300 + i] = data
            self.originals[arch] = files
            self.artifacts.append({"id": 300 + i, "name": f"CassQuery-mac-{arch}-{SHA}", "expired": False,
                                   "size_in_bytes": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                                   "workflow_run": {"id": RUN_ID, "head_sha": SHA, "head_branch": "main",
                                                    "repository_id": 1, "head_repository_id": 1}})
        self.releases = []
        self.latest_override = "automatic"
        self.asset_data = {}
        self.assets = []
        self.current_run = {"id": 200, "run_attempt": 1, "event": "workflow_dispatch", "head_branch": TAG,
                            "head_sha": SHA, "status": "in_progress", "actor": {"login": m.OWNER},
                            "triggering_actor": {"login": m.OWNER}, "repository": repo, "head_repository": repo,
                            "path": m.WORKFLOW, "workflow_id": 6}
        self.approval_jobs = [{"name": name, "status": "completed", "conclusion": "success",
                               "steps": [{"name": step, "status": "completed", "conclusion": "success"}]}
                              for name, step in (("prepare", "Verify candidate and protection"),
                                                 ("approve", "Recheck protected approval boundary"))]
        self.reviews = [{"state": "approved", "user": {"id": 900, "login": "reviewer", "type": "User"},
                         "environments": [{"id": 700, "name": "releases"}]}]
        self.tag_type = "tag"
        self.tag_sha = TAG_SHA
        self.tag_commit = SHA
        self.failures = {}
        self.on_call = None

    def json(self, path, *, method="GET", payload=None, missing=False):
        self.calls.append((method, path))
        base = path.split("?", 1)[0]
        if self.on_call:
            self.on_call(method, base)
        if base in self.failures:
            raise m.ReleaseError(self.failures[base])
        tail = base.removeprefix(m.PREFIX)
        if method == "POST" and tail == "/releases":
            result = {"id": 400, **payload}
            self.releases.append(result)
            return copy.deepcopy(result)
        if method == "PATCH" and tail == "/releases/400":
            self.releases[-1].update(payload)
            return copy.deepcopy(self.releases[-1])
        if tail == "/environments/releases":
            return copy.deepcopy(self.environment)
        if tail == "/environments/releases/deployment-branch-policies":
            return {"branch_policies": copy.deepcopy(self.policies), "total_count": len(self.policies)}
        if tail == "/git/ref/tags/" + TAG:
            return {"ref": "refs/tags/" + TAG, "object": {"type": self.tag_type, "sha": self.tag_sha}}
        if tail.startswith("/git/tags/"):
            return {"sha": self.tag_sha, "tag": TAG, "object": {"type": "commit", "sha": self.tag_commit}}
        if tail == "/actions/workflows/macos-candidate.yml":
            return {"id": 5, "path": m.CANDIDATE_WORKFLOW}
        if tail == "/actions/workflows/release.yml":
            return {"id": 6, "path": m.WORKFLOW}
        if tail == f"/actions/runs/{RUN_ID}":
            return copy.deepcopy(self.run)
        if tail == f"/actions/runs/{RUN_ID}/attempts/{self.run['run_attempt']}/jobs":
            return {"jobs": copy.deepcopy(self.jobs), "total_count": len(self.jobs)}
        if tail == f"/actions/runs/{RUN_ID}/artifacts":
            return {"artifacts": copy.deepcopy(self.artifacts), "total_count": len(self.artifacts)}
        if tail == "/actions/runs/200":
            return copy.deepcopy(self.current_run)
        if tail == "/actions/runs/200/attempts/1/jobs":
            return {"jobs": copy.deepcopy(self.approval_jobs), "total_count": len(self.approval_jobs)}
        if tail == "/actions/runs/200/approvals":
            return copy.deepcopy(self.reviews)
        if tail == "/releases/latest":
            if self.latest_override != "automatic":
                return copy.deepcopy(self.latest_override)
            public = [x for x in self.releases if not x["draft"]]
            return copy.deepcopy(public[-1]) if public else None
        if tail == "/releases":
            return copy.deepcopy(self.releases)
        if tail == "/releases/400":
            return copy.deepcopy(self.releases[-1])
        if tail == "/releases/400/assets":
            return copy.deepcopy(self.assets)
        raise AssertionError((method, path, payload))

    def download(self, path, output, *, limit=m.MAX_ARCHIVE):
        self.calls.append(("DOWNLOAD", path))
        if "/actions/artifacts/" in path:
            data = self.archives[int(path.split("/")[-2])]
        else:
            data = self.asset_data[int(path.split("/")[-1])]
        m.require(len(data) <= limit, "download exceeds size limit")
        with output.open("xb") as stream:
            stream.write(data)

    def upload(self, release_id, name, path):
        self.calls.append(("UPLOAD", name))
        asset = {"id": 500 + len(self.assets), "name": name, "state": "uploaded", "size": path.stat().st_size}
        self.assets.append(asset)
        self.asset_data[asset["id"]] = path.read_bytes()
        return copy.deepcopy(asset)

    def rewrite_candidate(self, arch, mutate=None, extra=None):
        data, files = candidate_bytes(arch, mutate, extra)
        i = m.ARCHES.index(arch)
        self.archives[300 + i] = data
        self.originals[arch] = files
        self.artifacts[i].update(size_in_bytes=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.api = FakeAPI()
        self.trusted_source = {"schema": 1, "source_sha": SHA, "clean": True, "fingerprint": FINGERPRINT, "files": SOURCE_FILES}
        # Mock the Git filesystem boundary only; candidate/run verifiers remain real.
        snapshot = patch.object(evidence, "source_snapshot", side_effect=lambda root: copy.deepcopy(self.trusted_source))
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.event = self.work / "event.json"
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REPOSITORY": m.REPOSITORY, "GITHUB_REPOSITORY_OWNER": m.OWNER,
                    "GITHUB_REF": f"refs/tags/{TAG}", "GITHUB_SHA": SHA,
                    "GITHUB_WORKFLOW_REF": f"{m.REPOSITORY}/{m.WORKFLOW}@refs/tags/{TAG}",
                    "GITHUB_ACTOR": m.OWNER, "GITHUB_TRIGGERING_ACTOR": m.OWNER,
                    "GITHUB_JOB": "publish", "GITHUB_RUN_ID": "200", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_EVENT_PATH": str(self.event)}
        self.set_event("draft")

    def set_event(self, mode):
        self.event.write_text(json.dumps({"inputs": {"candidate_run_id": str(RUN_ID), "tag": TAG,
                                                    "allow_ad_hoc": "true", "mode": mode}}))

    def prepare(self, **kwargs):
        directory = self.work / f"prepare-{len(list(self.work.iterdir()))}"
        directory.mkdir()
        return m.prepare(self.api, directory, run_id=RUN_ID, tag=kwargs.get("tag", TAG), expected_sha=SHA, allow_ad_hoc=kwargs.get("allow_ad_hoc", True))

    def execute(self, plan, mode="draft"):
        self.set_event(mode)
        directory = self.work / f"execute-{len(list(self.work.iterdir()))}"
        directory.mkdir()
        return m.execute(self.api, plan, directory, mode=mode, env=self.env)

    def writes(self):
        return [x for x in self.api.calls if x[0] in ("POST", "PATCH", "UPLOAD", "DELETE")]

    def test_dry_run_uses_real_common_verifier_and_writes_nothing(self):
        plan = self.prepare()
        self.assertEqual(len(plan["inventory"]), 7)
        self.assertFalse(self.writes())
        self.assertEqual(plan["inventory"]["candidate-arm64.json"]["path"].read_bytes(), self.api.originals["arm64"]["candidate.json"])

    def test_tag_version_must_match_trusted_package_before_any_api(self):
        with self.assertRaisesRegex(ValueError, "trusted source package.json"):
            self.prepare(tag=OTHER_TAG)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.writes())

    def test_dirty_or_different_publication_source_blocks_before_network(self):
        for change in ({"clean": False}, {"source_sha": "d" * 40}, {"fingerprint": "f" * 64}):
            with self.subTest(change=change):
                saved = copy.deepcopy(self.trusted_source)
                self.trusted_source.update(change)
                with self.assertRaisesRegex(ValueError, "source|fingerprints"):
                    self.prepare()
                self.assertFalse(self.writes())
                self.trusted_source = saved

    def test_explicit_ad_hoc_opt_in_is_required(self):
        with self.assertRaisesRegex(ValueError, "ad-hoc"):
            self.prepare(allow_ad_hoc=False)
        self.assertFalse(self.writes())

    def test_first_release_creates_only_draft_and_downloads_all_assets(self):
        plan = self.prepare()
        result = self.execute(plan)
        self.assertTrue(result["draft"])
        self.assertTrue(self.api.releases[0]["draft"])
        self.assertEqual(len(self.api.assets), 7)
        self.assertFalse(any(method == "PATCH" for method, path in self.writes()))
        downloaded = [path for method, path in self.api.calls if method == "DOWNLOAD" and "/releases/assets/" in path]
        self.assertEqual(len(downloaded), 7)
        for asset in self.api.assets:
            self.assertEqual(self.api.asset_data[asset["id"]], plan["inventory"][asset["name"]]["path"].read_bytes())

    def test_publish_happens_after_all_downloaded_hashes_and_approval_recheck(self):
        plan = self.prepare()
        result = self.execute(plan, "publish")
        self.assertFalse(result["draft"])
        operations = [method for method, path in self.api.calls]
        public = next(i for i, (method, path) in enumerate(self.api.calls) if method == "PATCH")
        self.assertEqual(sum(method == "DOWNLOAD" and "/releases/assets/" in path for method, path in self.api.calls[:public]), 7)
        self.assertGreater(public, max(i for i, x in enumerate(operations) if x == "UPLOAD"))
        self.assertFalse(self.api.releases[0]["draft"])

    def test_resume_matching_draft_never_overwrites_existing_assets(self):
        plan = self.prepare()
        self.execute(plan)
        original_count = len(self.writes())
        self.execute(self.prepare())
        self.assertEqual(len(self.writes()), original_count)

    def test_same_size_existing_draft_corruption_blocks_before_any_write(self):
        self.execute(self.prepare())
        asset = self.api.assets[0]
        self.api.asset_data[asset["id"]] = b"X" * asset["size"]
        self.api.calls.clear()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.execute(self.prepare(), "publish")
        self.assertFalse(self.writes())

    def test_same_size_server_corruption_after_upload_blocks_publish(self):
        original = self.api.upload
        def corrupt(*args):
            result = original(*args)
            self.api.asset_data[result["id"]] = b"X" * result["size"]
            return result
        self.api.upload = corrupt
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.execute(self.prepare(), "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))
        self.assertTrue(self.api.releases[0]["draft"])

    def test_unknown_or_duplicate_draft_assets_block(self):
        self.execute(self.prepare())
        for change in (lambda assets: assets.append({"id": 900, "name": "unexpected", "size": 1, "state": "uploaded"}),
                       lambda assets: assets.append(copy.deepcopy(assets[0]))):
            with self.subTest(change=change):
                saved = copy.deepcopy(self.api.assets)
                change(self.api.assets)
                self.api.calls.clear()
                with self.assertRaisesRegex(ValueError, "inventory"):
                    self.execute(self.prepare())
                self.assertFalse(self.writes())
                self.api.assets = saved

    def test_published_same_tag_or_nonmonotonic_version_never_writes(self):
        for version in (TAG, "v999.0.0", OTHER_TAG, "garbage", "v01.0.0"):
            with self.subTest(version=version):
                self.api.releases = [{"id": 400, "draft": False, "prerelease": False, "tag_name": version}]
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_first_release_404_must_agree_with_enumerated_history(self):
        self.api.latest_override = None
        self.api.releases = [{"id": 399, "draft": False, "prerelease": False, "tag_name": "v0.9.0"}]
        with self.assertRaisesRegex(ValueError, "404 contradicts"):
            self.prepare()

    def test_missing_unprotected_self_approval_or_wrong_tag_policy_blocks(self):
        changes = [lambda: setattr(self.api, "environment", None),
                   lambda: self.api.environment.update(protection_rules=[]),
                   lambda: self.api.environment["protection_rules"][0].update(reviewers=[]),
                   lambda: self.api.environment["protection_rules"][0].update(prevent_self_review=False),
                   lambda: self.api.environment.update(deployment_branch_policy=None),
                   lambda: self.api.policies[0].update(name="*"),
                   lambda: self.api.policies[0].update(type="branch"),
                   lambda: self.api.policies.append({"id": 702, "name": "dev", "type": "branch"})]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())
                self.assertFalse(any("/actions/artifacts/" in path for method, path in self.api.calls))

    def test_run_fail_stale_wrong_source_fork_pr_manual_and_skipped_native_steps(self):
        changes = [lambda: self.api.run.update(status="in_progress"), lambda: self.api.run.update(conclusion="failure"),
                   lambda: self.api.run.update(head_sha="d" * 40), lambda: self.api.run.update(event="pull_request"),
                   lambda: self.api.run.update(event="workflow_dispatch"), lambda: self.api.run.update(head_branch="dev"),
                   lambda: self.api.run["head_repository"].update(full_name="fork/CassQuery"),
                   lambda: self.api.run.update(workflow_id=99), lambda: self.api.run.update(run_attempt=2),
                   lambda: self.api.jobs.pop(), lambda: self.api.jobs[0].update(conclusion="failure")]
        for step in evidence.REQUIRED_STEPS:
            changes.append(lambda step=step: next(x for x in self.api.jobs[0]["steps"] if x["name"] == step).update(conclusion="skipped"))
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_lightweight_wrong_target_or_moved_tag_blocks(self):
        for field, value in (("tag_type", "commit"), ("tag_commit", "d" * 40)):
            with self.subTest(field=field):
                self.api = FakeAPI()
                setattr(self.api, field, value)
                with self.assertRaises(ValueError):
                    self.prepare()
        self.api = FakeAPI()
        plan = self.prepare()
        self.api.tag_sha = "e" * 40
        with self.assertRaisesRegex(ValueError, "tag moved"):
            self.execute(plan)
        self.assertFalse(self.writes())

    def test_tag_moved_during_upload_leaves_draft_unpublished(self):
        original = self.api.upload
        def move(*args):
            result = original(*args)
            self.api.tag_sha = "e" * 40
            return result
        self.api.upload = move
        with self.assertRaisesRegex(ValueError, "tag moved"):
            self.execute(self.prepare(), "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))

    def test_expired_wrong_identity_duplicate_or_corrupted_api_artifact_blocks(self):
        changes = [lambda: self.api.artifacts[0].update(expired=True),
                   lambda: self.api.artifacts[0].update(digest="sha256:" + "0" * 64),
                   lambda: self.api.artifacts[0].update(name="wrong"),
                   lambda: self.api.artifacts[0]["workflow_run"].update(head_sha="d" * 40),
                   lambda: self.api.artifacts[0]["workflow_run"].update(repository_id=99),
                   lambda: self.api.artifacts[0].update(id=self.api.artifacts[1]["id"]),
                   lambda: self.api.artifacts.pop(), lambda: self.api.artifacts.append(copy.deepcopy(self.api.artifacts[0]))]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                change()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_real_manifest_mismatch_missing_gate_corruption_and_signing_do_not_pass(self):
        def stale_report(manifest, files):
            name = evidence.GATES["preflight"]
            report = json.loads(files[name])
            report.update(source_sha="d" * 40)
            files[name] = json.dumps(report).encode()
            item = record(name, files[name])
            manifest["files"] = [item if x["path"] == name else x for x in manifest["files"]]
            manifest["gates"]["preflight"] = item
        changes = [lambda manifest, files: manifest.update(source_sha="d" * 40),
                   lambda manifest, files: manifest.update(run_attempt=2),
                   lambda manifest, files: manifest.update(version=OTHER_VERSION),
                   lambda manifest, files: manifest.update(distribution="developer-id"),
                   lambda manifest, files: manifest["gates"].pop("dmg"),
                   lambda manifest, files: files.update({manifest["assets"][0]["path"]: b"corrupt"}),
                   stale_report]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                self.api.rewrite_candidate("arm64", change)
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.writes())

    def test_unlisted_artifact_file_is_not_promoted(self):
        self.api.rewrite_candidate("arm64", extra={"dist/mac-app/unreviewed.zip": b"not reviewed"})
        with self.assertRaisesRegex(ValueError, "unlisted"):
            self.prepare()
        self.assertFalse(self.writes())

    def test_different_architecture_source_fingerprints_are_not_same_candidate(self):
        def mismatch(manifest, files):
            before_path = "evidence/source-before.json"
            before = json.loads(files[before_path])
            before["files"].append({"path": "extra-source.txt", "bytes": 1, "executable": False, "sha256": "d" * 64})
            before["fingerprint"] = hashlib.sha256(json.dumps({"source_sha": SHA, "files": before["files"]},
                                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            files[before_path] = json.dumps(before).encode()
            manifest["files"] = [record(before_path, files[before_path]) if x["path"] == before_path else x for x in manifest["files"]]
            manifest["source_fingerprint"] = before["fingerprint"]
            name = evidence.GATES["preflight"]
            report = json.loads(files[name])
            report["source_fingerprint"] = manifest["source_fingerprint"]
            report["after_fingerprint"] = manifest["source_fingerprint"]
            files[name] = json.dumps(report).encode()
            item = record(name, files[name])
            manifest["files"] = [item if x["path"] == name else x for x in manifest["files"]]
            manifest["gates"]["preflight"] = item
        self.api.rewrite_candidate("x64", mismatch)
        with self.assertRaisesRegex(ValueError, "fingerprints"):
            self.prepare()

    def test_tag_dispatch_keeps_candidate_main_push_only(self):
        plan = self.prepare()
        self.assertEqual(self.env["GITHUB_REF"], "refs/tags/" + TAG)
        self.assertEqual(self.api.current_run["head_branch"], TAG)
        self.assertEqual(self.api.run["head_branch"], "main")
        self.assertTrue(self.execute(plan)["draft"])
        self.api = FakeAPI()
        self.api.run["head_branch"] = TAG
        with self.assertRaisesRegex(ValueError, "main push"):
            self.prepare()
        self.assertFalse(self.writes())

    def test_wrong_dispatch_tag_or_main_branch_blocks_all_writes(self):
        plan = self.prepare()
        for key, value in (("GITHUB_REF", "refs/heads/main"), ("GITHUB_REF", "refs/tags/" + OTHER_TAG),
                           ("GITHUB_WORKFLOW_REF", f"{m.REPOSITORY}/{m.WORKFLOW}@refs/tags/{OTHER_TAG}")):
            with self.subTest(key=key, value=value):
                saved = self.env[key]
                self.env[key] = value
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.env[key] = saved
                self.assertFalse(self.writes())
        for head_branch in ("main", OTHER_TAG, "refs/tags/" + TAG):
            with self.subTest(server_head=head_branch):
                self.api.current_run["head_branch"] = head_branch
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.assertFalse(self.writes())

    def test_reported_tag_policy_without_reviewers_blocks_before_download_or_write(self):
        self.api.environment["protection_rules"] = [{"type": "branch_policy"}]
        with self.assertRaisesRegex(ValueError, "independent review"):
            self.prepare()
        self.assertFalse(self.writes())
        self.assertFalse(any(method == "DOWNLOAD" for method, path in self.api.calls))

    def test_execute_guard_rejects_local_nonowner_wrong_branch_wrong_sha_and_rerun(self):
        plan = self.prepare()
        for key, value in (("GITHUB_ACTIONS", "false"), ("GITHUB_ACTOR", "other"), ("GITHUB_TRIGGERING_ACTOR", "other"),
                           ("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/dev"), ("GITHUB_SHA", "d" * 40),
                           ("GITHUB_JOB", "other"), ("GITHUB_RUN_ATTEMPT", "2"),
                           ("GITHUB_WORKFLOW_REF", f"{m.REPOSITORY}/{m.WORKFLOW}@refs/heads/main")):
            with self.subTest(key=key):
                saved = self.env[key]
                self.env[key] = value
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.env[key] = saved
                self.assertFalse(self.writes())

    def test_server_context_missing_stale_failed_skipped_or_self_review_blocks_write(self):
        changes = [lambda: self.api.current_run.update(head_sha="d" * 40),
                   lambda: self.api.current_run.update(workflow_id=999),
                   lambda: self.api.current_run.update(run_attempt=2),
                   lambda: self.api.approval_jobs[0].update(conclusion="failure"),
                   lambda: self.api.approval_jobs[1]["steps"][0].update(conclusion="skipped"),
                   lambda: self.api.reviews.clear(), lambda: self.api.reviews[0].update(state="rejected"),
                   lambda: self.api.reviews[0]["user"].update(login=m.OWNER),
                   lambda: self.api.reviews[0]["environments"][0].update(id=999)]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeAPI()
                plan = self.prepare()
                change()
                with self.assertRaises(ValueError):
                    self.execute(plan)
                self.assertFalse(self.writes())

    def test_api_errors_in_authorization_never_become_bootstrap(self):
        for endpoint in ("/releases/latest", "/releases", "/environments/releases", f"/actions/runs/{RUN_ID}"):
            for status in (401, 403, 429, 500):
                with self.subTest(endpoint=endpoint, status=status):
                    self.api = FakeAPI()
                    self.api.failures[m.PREFIX + endpoint] = f"HTTP {status}"
                    with self.assertRaises(ValueError):
                        self.prepare()
                    self.assertFalse(self.writes())

    def test_candidate_rerun_during_promotion_blocks_before_publication(self):
        plan = self.prepare()
        def change(method, path):
            if method == "POST":
                self.api.run["run_attempt"] = 2
        self.api.on_call = change
        with self.assertRaises(ValueError):
            self.execute(plan, "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))

    def test_removed_environment_after_upload_does_not_publish(self):
        plan = self.prepare()
        original = self.api.upload
        def remove(*args):
            result = original(*args)
            self.api.environment = None
            return result
        self.api.upload = remove
        with self.assertRaises(ValueError):
            self.execute(plan, "publish")
        self.assertFalse(any(x[0] == "PATCH" for x in self.writes()))


class Response(io.BytesIO):
    def __init__(self, status, data=b"", headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class NetworkTests(unittest.TestCase):
    def test_only_get_explicit_404_is_missing_never_other_errors(self):
        for status in (401, 403, 429, 500, 302):
            with self.subTest(status=status):
                api = m.GitHub("secret", Opener([Response(status, b'{"message":"Not Found"}')]))
                with self.assertRaisesRegex(ValueError, f"HTTP {status}"):
                    api.json(m.PREFIX + "/releases/latest", missing=True)
        api = m.GitHub("secret", Opener([Response(404, b'{"message":"Not Found"}')]))
        self.assertIsNone(api.json(m.PREFIX + "/releases/latest", missing=True))
        for method in ("GET", "POST", "PATCH"):
            with self.subTest(method=method):
                api = m.GitHub("secret", Opener([Response(404)]))
                with self.assertRaises(ValueError):
                    api.json(m.PREFIX + "/releases", method=method, missing=method != "GET")

    def test_bad_duplicate_or_oversized_json_is_rejected(self):
        for payload in (b"not-json", b'{"id":1,"id":2}', b'{"value":NaN}', b"x" * (m.MAX_JSON + 1)):
            api = m.GitHub("secret", Opener([Response(200, payload)]))
            with self.assertRaises(ValueError):
                api.json(m.PREFIX + "/releases/latest")

    def test_redirect_drops_authorization_and_streams_exact_bytes(self):
        opener = Opener([Response(302, headers={"Location": "https://release-assets.githubusercontent.com/file?sig=private"}),
                         Response(200, b"bytes", {"Content-Length": "5"})])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "file"
            m.GitHub("secret", opener).download(m.PREFIX + "/releases/assets/5", output, limit=5)
            self.assertEqual(output.read_bytes(), b"bytes")
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer secret")
        self.assertIsNone(opener.requests[1].get_header("Authorization"))

    def test_untrusted_nested_or_plaintext_redirect_never_receives_credentials(self):
        for url in ("http://release-assets.githubusercontent.com/x", "https://evil.example/x", "https://api.github.com@evil.example/x",
                    "https://release-assets.githubusercontent.com.evil.example/x", "https://release-assets.githubusercontent.com:444/x",
                    "https://user@release-assets.githubusercontent.com/x"):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                opener = Opener([Response(302, headers={"Location": url})])
                with self.assertRaises(ValueError):
                    m.GitHub("secret", opener).download(m.PREFIX + "/releases/assets/5", Path(directory) / "file")
                self.assertEqual(len(opener.requests), 1)
        with tempfile.TemporaryDirectory() as directory:
            opener = Opener([Response(302, headers={"Location": "https://release-assets.githubusercontent.com/x"}),
                             Response(302, headers={"Location": "https://evil.example/x"})])
            with self.assertRaises(ValueError):
                m.GitHub("secret", opener).download(m.PREFIX + "/releases/assets/5", Path(directory) / "file")
            self.assertEqual(len(opener.requests), 2)

    def test_truncated_oversized_or_failed_binary_download_blocks(self):
        for response in (Response(200, b"short", {"Content-Length": "10"}), Response(200, b"too-long"),
                         Response(200, b"x", {"Content-Length": "100"}), Response(403)):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    m.GitHub("secret", Opener([response])).download(m.PREFIX + "/releases/assets/5", Path(directory) / "file", limit=6)

    def test_paginated_inventory_enumerates_second_page_and_rejects_truncation(self):
        first = [{"id": i} for i in range(100)]
        second = [{"id": 100}]
        opener = Opener([Response(200, json.dumps(first).encode()), Response(200, json.dumps(second).encode())])
        rows = m.GitHub("secret", opener).items(m.PREFIX + "/releases")
        self.assertEqual(len(rows), 101)
        self.assertIn("page=2", opener.requests[1].full_url)
        payload = {"jobs": first[:1], "total_count": 2}
        with self.assertRaisesRegex(ValueError, "truncated"):
            m.GitHub("secret", Opener([Response(200, json.dumps(payload).encode())])).items(m.PREFIX + "/actions/runs/100/jobs", "jobs")

    def test_authentication_error_body_is_not_printed_or_retried(self):
        opener = Opener([urllib.error.HTTPError("https://api.github.com/x", 403, "secret-body", {}, io.BytesIO(b"secret-body"))])
        with self.assertRaisesRegex(ValueError, "HTTP 403") as caught:
            m.GitHub("secret", opener).json(m.PREFIX + "/releases")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(opener.requests), 1)


class ArchiveTests(unittest.TestCase):
    def extract(self, entries):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "candidate.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
                for name, data in entries:
                    target.writestr(name, data)
            root = Path(directory) / "out"
            m.extract_candidate(archive, root)
            return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_safe_wrapper_preserves_exact_bytes(self):
        self.assertEqual(self.extract([("candidate.json", b"{}"), ("evidence/test.json", b"evidence")]),
                         {"candidate.json": b"{}", "evidence/test.json": b"evidence"})

    def test_traversal_absolute_backslash_controls_symlink_and_collision(self):
        paths = ["../x", "/x", "a/../../x", "a\\x", "a//x", "a/./x", "C:/x", "a\nx"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.extract([(path, b"x")])
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.extract([(link, b"../target")])
        for entries in ([("a", b"x"), ("a/b", b"x")], [("a", b"x"), ("A", b"x")]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.extract(entries)

    def test_zip_bomb_file_count_and_corrupt_archive_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "bomb"):
            self.extract([("bomb", b"x" * (2 * 1024 * 1024))])
        with patch.object(m, "MAX_FILES", 1), self.assertRaisesRegex(ValueError, "count"):
            self.extract([("a", b"a"), ("b", b"b")])
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            archive.write_bytes(b"bad")
            with self.assertRaises((m.ReleaseError, zipfile.BadZipFile)):
                m.extract_candidate(archive, Path(directory) / "out")

    def test_zip_metadata_limits_reject_before_zipinfo_allocation(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for i in range(10000):
                archive.writestr(f"entry-{i}", b"")
        raw = bytearray(buffer.getvalue())
        end = raw.rfind(b"PK\x05\x06")
        fixtures = [(bytes(raw), "file count")]
        forged_count = bytearray(raw)
        struct.pack_into("<HH", forged_count, end + 8, 1, 1)
        fixtures.append((bytes(forged_count), "actual file count"))
        huge_directory = bytearray(raw)
        struct.pack_into("<HH", huge_directory, end + 8, 1, 1)
        struct.pack_into("<L", huge_directory, end + 12, m.MAX_DIRECTORY + 1)
        fixtures.append((bytes(huge_directory), "central directory exceeds"))
        zip64 = bytearray(raw)
        struct.pack_into("<HH", zip64, end + 8, 65535, 65535)
        fixtures.append((bytes(zip64), "ZIP64"))
        for data, message in fixtures:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "wrapper.zip"
                path.write_bytes(data)
                with patch.object(zipfile.ZipFile, "_RealGetContents", side_effect=AssertionError("unbounded metadata allocation")) as allocation:
                    with self.assertRaisesRegex(ValueError, message):
                        m.extract_candidate(path, Path(directory) / "out")
                    allocation.assert_not_called()

    def test_zip64_locator_in_ordinary_entry_comment_is_rejected_before_allocation(self):
        # Ordinary EOCD claims one entry; CPython selects a hidden 10,001-entry
        # ZIP64 directory because its locator occupies EOCD-20 inside a comment.
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            for i in range(10000):
                archive.writestr(f"entry-{i}", b"")
        raw = inner.getvalue()
        end = raw.rfind(b"PK\x05\x06")
        hidden_size, hidden_offset = struct.unpack_from("<LL", raw, end + 12)
        hidden = raw[hidden_offset:hidden_offset + hidden_size]
        outer = io.BytesIO()
        entry = zipfile.ZipInfo("outer")
        entry.comment = b"X" * 76
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr(entry, hidden)
        polyglot = bytearray(outer.getvalue())
        end = polyglot.rfind(b"PK\x05\x06")
        directory_size, directory_offset = struct.unpack_from("<LL", polyglot, end + 12)
        header_size = directory_size - 76
        start = directory_offset - len(hidden)
        zip64_start = end - 76
        zip64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 10001, 10001,
                            len(hidden) + header_size, start)
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_start, 1)
        polyglot[zip64_start:end] = zip64 + locator
        self.assertEqual(struct.unpack_from("<H", polyglot, end + 10)[0], 1)
        with zipfile.ZipFile(io.BytesIO(polyglot)) as different_parser:
            self.assertEqual(len(different_parser.infolist()), 10001)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrapper.zip"
            path.write_bytes(polyglot)
            with patch.object(zipfile.ZipFile, "_RealGetContents", side_effect=AssertionError("metadata allocation")) as allocation:
                with self.assertRaisesRegex(ValueError, "ZIP64 locator"):
                    m.extract_candidate(path, Path(directory) / "out")
                allocation.assert_not_called()

    def test_zip64_entry_extra_rejected_before_zipinfo_allocation(self):
        buffer = io.BytesIO()
        entry = zipfile.ZipInfo("file")
        entry.extra = struct.pack("<HHQQ", 1, 16, 1, 1)
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(entry, b"x")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrapper.zip"
            path.write_bytes(buffer.getvalue())
            with patch.object(zipfile.ZipFile, "_RealGetContents", side_effect=AssertionError("metadata allocation")) as allocation:
                with self.assertRaisesRegex(ValueError, "ZIP64"):
                    m.extract_candidate(path, Path(directory) / "out")
                allocation.assert_not_called()


class WorkflowTests(unittest.TestCase):
    def document(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())

    def assert_workflow(self, document):
        trigger = document.get("on", document.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        inputs = trigger["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["mode"]["default"], "draft")
        self.assertFalse(inputs["allow_ad_hoc"]["default"])
        self.assertEqual(set(inputs), {"candidate_run_id", "tag", "mode", "allow_ad_hoc"})
        self.assertEqual(document["permissions"], {})
        self.assertFalse(document["concurrency"]["cancel-in-progress"])
        jobs = document["jobs"]
        self.assertEqual(set(jobs), {"prepare", "approve", "publish"})
        self.assertEqual(jobs["approve"]["needs"], "prepare")
        self.assertEqual(jobs["publish"]["needs"], ["prepare", "approve"])
        self.assertNotIn("environment", jobs["prepare"])
        self.assertEqual(jobs["approve"]["environment"], "releases")
        self.assertNotIn("environment", jobs["publish"])
        self.assertEqual(jobs["prepare"]["if"],
                         "github.repository == 'cass-ale/CassQuery' && github.ref == format('refs/tags/{0}', inputs.tag) && "
                         "startsWith(github.ref, 'refs/tags/v') && github.event_name == 'workflow_dispatch' && "
                         "github.actor == github.repository_owner && "
                         "github.triggering_actor == github.repository_owner && github.run_attempt == 1")
        for name, job in jobs.items():
            self.assertEqual(job["permissions"], {"contents": "write" if name == "publish" else "read", "actions": "read"})
            self.assertNotIn("continue-on-error", job)
            self.assertNotIn("if", job if name != "prepare" else {})
            self.assertEqual(len(job["steps"]), 2)
            checkout, step = job["steps"]
            self.assertEqual(checkout["uses"], "actions/checkout@11d5960a326750d5838078e36cf38b85af677262")
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertFalse(checkout["with"]["persist-credentials"])
            self.assertNotIn("if", step)
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("${{", step["run"])
            self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', step["run"])
            self.assertEqual("--execute" in step["run"], name == "publish")
            self.assertNotIn("build", step["run"])
            self.assertNotIn("GITHUB_OUTPUT", step["run"])
            invocation = 'python3 tools/release_publish.py --candidate-run-id "$CANDIDATE_RUN_ID" --tag "$EXISTING_TAG" --source-sha "$GITHUB_SHA"'
            if name == "publish":
                invocation += ' --mode "$RELEASE_MODE" --execute'
            invocation += ' "${flags[@]}"'
            expected = ('set -euo pipefail\n'
                        'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"\n'
                        'flags=()\n'
                        'if [ "$ALLOW_AD_HOC" = true ]; then flags+=(--allow-ad-hoc); fi\n'
                        + invocation + '\n')
            self.assertEqual(step["run"], expected)

    def test_semantic_dispatch_permissions_and_environment_chain(self):
        self.assert_workflow(self.document())

    def test_dispatch_guard_trust_predicate_and_disjunction_mutations_fail(self):
        document = self.document()
        expected = document["jobs"]["prepare"]["if"]
        mutations = ["true || " + expected, expected + " || true"]
        terms = expected.split(" && ")
        mutations.extend(" && ".join(terms[:i] + terms[i + 1:]) for i in range(len(terms)))
        for mutated in mutations:
            with self.subTest(guard=mutated):
                copy_doc = copy.deepcopy(document)
                copy_doc["jobs"]["prepare"]["if"] = mutated
                with self.assertRaises(AssertionError):
                    self.assert_workflow(copy_doc)

    def test_workflow_skip_permissions_bypass_and_noop_mutations_fail(self):
        changes = [lambda doc: doc["jobs"]["approve"]["steps"][1].update({"if": "false"}),
                   lambda doc: doc["jobs"]["approve"].update({"continue-on-error": True}),
                   lambda doc: doc["jobs"]["publish"].update(needs=["prepare"]),
                   lambda doc: doc["jobs"]["prepare"].update(environment="releases"),
                   lambda doc: doc["jobs"]["prepare"]["permissions"].update(contents="write"),
                   lambda doc: doc["jobs"]["publish"]["steps"][0]["with"].update(ref="main"),
                   lambda doc: doc["jobs"]["prepare"]["steps"][1].update(run=doc["jobs"]["prepare"]["steps"][1]["run"].replace("python3 tools/", "echo python3 tools/"))]
        for change in changes:
            with self.subTest(change=change):
                document = self.document()
                change(document)
                with self.assertRaises(AssertionError):
                    self.assert_workflow(document)


if __name__ == "__main__":
    unittest.main()
