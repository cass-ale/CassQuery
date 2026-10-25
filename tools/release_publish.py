#!/usr/bin/env python3
"""Verify/promote existing CassQuery candidate bytes; read-only unless --execute.

Only the owner-dispatched, exact-tag release workflow may execute. This tool
never builds, creates tags/environments, deletes assets, or changes a public release.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = "cass-ale/CassQuery"
OWNER = "cass-ale"
PREFIX = f"/repos/{REPOSITORY}"
WORKFLOW = ".github/workflows/release.yml"
CANDIDATE_WORKFLOW = ".github/workflows/macos-candidate.yml"
INTEGRATION_WORKFLOW = ".github/workflows/integration.yml"
MAX_QUALIFICATION_ROWS = 200
ARCHES = ("arm64", "x64")
MAX_JSON = 8 * 1024 * 1024
MAX_ARCHIVE = 2 * 1024 ** 3
MAX_EXPANDED = 4 * 1024 ** 3
MAX_FILES = 2048
MAX_DIRECTORY = 2 * 1024 * 1024  # Bound ZIP metadata before ZipInfo allocation.
CHUNK = 1024 * 1024


class ReleaseError(ValueError):
    """A failed authorization/integrity check; no fallback is permitted."""


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def positive_id(value):
    require(type(value) is int and value > 0, "invalid API identifier")
    return value


def sha40(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "full source SHA required")
    return value


def version_tuple(value):
    require(isinstance(value, str) and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value),
            "only stable canonical major.minor.patch versions are supported")
    return tuple(int(x) for x in value.split("."))


def digest(path):
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            sha.update(block)
            size += len(block)
    return {"bytes": size, "sha256": sha.hexdigest()}


def object_json(data):
    # Duplicate keys must not create two interpretations of an evidence record.
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ReleaseError("non-finite JSON value")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class GitHub:
    """Small REST client: constructed API paths, bounded IO, no credential redirects."""

    def __init__(self, token, opener=None):
        require(isinstance(token, str) and token and not any(x in token for x in "\r\n"), "GITHUB_TOKEN required")
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def _open(self, url, *, method="GET", data=None, binary=False, authenticated=True, size=None,
              accept="application/vnd.github+json"):
        headers = {"Accept": accept,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "CassQuery-release-verifier"}
        if authenticated:
            require(urllib.parse.urlsplit(url).hostname in ("api.github.com", "uploads.github.com"),
                    "credentials are restricted to GitHub API hosts")
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
        if size is not None:
            headers["Content-Length"] = str(size)
        try:
            return self.opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=60)
        except urllib.error.HTTPError as error:
            return error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # Do not print signed redirect URLs, request headers or token-bearing errors.
            raise ReleaseError("GitHub network request failed") from error

    def json(self, path, *, method="GET", payload=None, missing=False):
        team_path = re.fullmatch(rf"/orgs/{re.escape(OWNER)}/teams/[A-Za-z0-9-]+(?:/members)?(?:\?per_page=100&page=[0-9]+)?", path)
        require((path.startswith(PREFIX + "/") or (team_path and method == "GET" and payload is None))
                and not any(c in path for c in "\r\n#"), "invalid API path")
        with self._open("https://api.github.com" + path, method=method,
                        data=None if payload is None else json.dumps(payload).encode()) as response:
            if response.status == 404 and missing and method == "GET":
                return None
            require(response.status in ({200} if method == "GET" else {200, 201}),
                    f"GitHub {method} failed (HTTP {response.status})")
            data = response.read(MAX_JSON + 1)
            require(len(data) <= MAX_JSON, "GitHub JSON response exceeds limit")
            try:
                return object_json(data)
            except (ValueError, UnicodeError) as error:
                raise ReleaseError("invalid GitHub JSON") from error

    def items(self, path, key=None, *, limit=10000):
        """Enumerate bounded pages; never follow an API-supplied URL/Link."""
        require(type(limit) is int and 0 < limit <= 10000, "invalid API inventory limit")
        result = []
        total = None
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            response = self.json(f"{path}{separator}per_page=100&page={page}")
            rows = response if key is None else response.get(key)
            require(isinstance(rows, list) and len(rows) <= 100, "invalid API page")
            require(all(isinstance(x, dict) for x in rows), "invalid API row")
            if key is not None:
                count = response.get("total_count")
                require(type(count) is int and 0 <= count <= limit, "missing/excessive API total_count")
                require(total is None or total == count, "API inventory changed while paginating")
                total = count
            result.extend(rows)
            require(len(result) <= limit, "API inventory exceeds item limit")
            if len(rows) < 100 or (total is not None and len(result) == total):
                require(total is None or total == len(result), "truncated API inventory")
                return result
        raise ReleaseError("API inventory exceeds pagination limit")

    def download(self, path, output, *, limit=MAX_ARCHIVE):
        artifact = re.fullmatch(re.escape(PREFIX) + r'/actions/artifacts/[1-9][0-9]*/zip', path)
        asset = re.fullmatch(re.escape(PREFIX) + r'/releases/assets/[1-9][0-9]*', path)
        require(artifact or asset, "invalid download path")
        # Artifact archive endpoints use the REST media type and redirect;
        # release asset endpoints require octet-stream to request their bytes.
        accept = "application/octet-stream" if asset else "application/vnd.github+json"
        response = self._open("https://api.github.com" + path, binary=True, accept=accept)
        try:
            if response.status == 302:
                location = response.headers.get("Location", "")
                url = urllib.parse.urlsplit(location)
                require(url.scheme == "https" and not url.username and not url.password
                        and url.port in (None, 443) and not url.fragment
                        and url.hostname and (url.hostname.endswith(".githubusercontent.com")
                                             or url.hostname.endswith(".blob.core.windows.net")),
                        "untrusted GitHub download redirect")
                response.close()
                # Signed storage URLs never receive the GitHub bearer token.
                response = self._open(location, binary=True, authenticated=False, accept="application/octet-stream")
            require(response.status == 200, f"GitHub download failed (HTTP {response.status})")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                require(declared.isdigit() and int(declared) <= limit, "download exceeds size limit")
            count = 0
            with output.open("xb") as target:
                while block := response.read(CHUNK):
                    count += len(block)
                    require(count <= limit, "download exceeds size limit")
                    target.write(block)
            require(declared is None or count == int(declared), "truncated GitHub download")
        finally:
            response.close()

    def upload(self, release_id, name, path):
        positive_id(release_id)
        require(re.fullmatch(r"[A-Za-z0-9_.-]+", name), "invalid release asset name")
        url = f"https://uploads.github.com{PREFIX}/releases/{release_id}/assets?name={urllib.parse.quote(name, safe='')}"
        with path.open("rb") as source, self._open(url, method="POST", data=source, binary=True,
                                                  size=path.stat().st_size) as response:
            require(response.status == 201, f"GitHub asset upload failed (HTTP {response.status})")
            data = response.read(MAX_JSON + 1)
            require(len(data) <= MAX_JSON, "upload response exceeds limit")
            return object_json(data)


def check_zip_directory(archive):
    """Bound/scan ordinary ZIP metadata before zipfile allocates any ZipInfo objects.

    ZIP64 and split archives are deliberately unsupported for this <2 GiB wrapper.
    Both the claimed count and the actual bounded directory records are checked.
    """
    size = archive.stat().st_size
    require(22 <= size <= MAX_ARCHIVE, "candidate ZIP archive size invalid")
    with archive.open("rb") as source:
        tail_size = min(size, 65535 + 22)
        source.seek(size - tail_size)
        tail = source.read(tail_size)
        end = tail.rfind(b"PK\x05\x06")
        if end < 0 or len(tail) - end < 22:
            raise zipfile.BadZipFile("candidate ZIP end record missing")
        signature, disk, start_disk, disk_count, count, directory_size, offset, comment = struct.unpack_from("<4s4H2LH", tail, end)
        require(end + 22 + comment == len(tail), "invalid ZIP end/comment length")
        require(disk == start_disk == 0 and disk_count == count, "split ZIP rejected")
        require(count != 65535 and directory_size != 0xffffffff and offset != 0xffffffff, "ZIP64 wrapper rejected")
        require(0 < count <= MAX_FILES, "candidate ZIP file count exceeds limit")
        require(0 < directory_size <= MAX_DIRECTORY, "candidate ZIP central directory exceeds limit")
        end_offset = size - tail_size + end
        if end_offset >= 20:
            source.seek(end_offset - 20)
            require(source.read(4) != b"PK\x06\x07", "ZIP64 locator/polyglot wrapper rejected")
        require(offset + directory_size == end_offset, "ZIP64/overlapping ZIP central directory rejected")
        source.seek(offset)
        directory = source.read(directory_size)
        require(len(directory) == directory_size, "truncated ZIP central directory")
    cursor = 0
    actual_count = 0
    while cursor < directory_size:
        require(cursor + 46 <= directory_size and directory[cursor:cursor + 4] == b"PK\x01\x02", "invalid ZIP central directory record")
        compressed, expanded = struct.unpack_from("<LL", directory, cursor + 20)
        name_len, extra_len, comment_len, file_disk = struct.unpack_from("<4H", directory, cursor + 28)
        local_offset = struct.unpack_from("<L", directory, cursor + 42)[0]
        require(file_disk == 0 and all(value != 0xffffffff for value in (compressed, expanded, local_offset)), "ZIP64/split ZIP entry rejected")
        length = 46 + name_len + extra_len + comment_len
        require(cursor + length <= directory_size and name_len > 0, "truncated ZIP metadata record")
        extra = cursor + 46 + name_len
        extra_end = extra + extra_len
        while extra < extra_end:
            require(extra + 4 <= extra_end, "truncated ZIP extra record")
            kind, extra_size = struct.unpack_from("<HH", directory, extra)
            require(kind != 1, "ZIP64 wrapper rejected")
            extra += 4 + extra_size
            require(extra <= extra_end, "truncated ZIP extra record")
        cursor += length
        actual_count += 1
        require(actual_count <= MAX_FILES, "candidate ZIP actual file count exceeds limit")
    require(actual_count == count, "forged ZIP file count")


def extract_candidate(archive, root):
    """Untrusted Actions wrapper ZIP, not the app ZIP; never use extractall."""
    require(not root.exists(), "candidate extraction needs a fresh directory")
    check_zip_directory(archive)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        require(0 < len(entries) <= MAX_FILES, "candidate ZIP file count exceeds limit")
        seen = set()
        total = 0
        for item in entries:
            name = item.filename.rstrip("/")
            parts = name.split("/")
            require(name and not item.filename.startswith("/") and "\\" not in name
                    and all(p not in ("", ".", "..") and ":" not in p for p in parts)
                    and not any(ord(c) < 32 or ord(c) > 126 for c in name), "unsafe ZIP path")
            require(name.casefold() not in seen, "duplicate ZIP path")
            seen.add(name.casefold())
            kind = stat.S_IFMT(item.external_attr >> 16)
            require(kind in (0, stat.S_IFREG, stat.S_IFDIR)
                    and (kind != stat.S_IFDIR or item.is_dir()), "ZIP symlink/special file rejected")
            require(not item.flag_bits & 1 and item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    "unsupported candidate ZIP encoding")
            total += item.file_size
            require(item.file_size <= MAX_ARCHIVE and total <= MAX_EXPANDED
                    and item.file_size <= max(1024 * 1024, item.compress_size * 200), "candidate ZIP bomb rejected")
        # Detect file/directory collisions before creating anything.
        files = {x.filename.casefold() for x in entries if not x.is_dir()}
        for item in entries:
            require(not any(str(parent).casefold() in files for parent in PurePosixPath(item.filename).parents
                            if str(parent) != "."), "ZIP file/directory collision")
        root.mkdir()
        for item in entries:
            target = root / item.filename
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with source.open(item) as data, target.open("xb") as output:
                while block := data.read(CHUNK):
                    count += len(block)
                    require(count <= item.file_size, "ZIP expanded beyond declared size")
                    output.write(block)
            require(count == item.file_size, "truncated ZIP file")


def protected_environment(api):
    environment = api.json(PREFIX + "/environments/releases", missing=True)
    require(isinstance(environment, dict), "release environment is absent; owner must configure protection first")
    positive_id(environment.get("id"))
    require(environment.get("name") == "releases", "wrong release environment")
    rules = environment.get("protection_rules")
    require(isinstance(rules, list) and all(isinstance(x, dict) for x in rules), "unreadable environment protection")
    reviewers = [x for x in rules if x.get("type") == "required_reviewers"]
    require(len(reviewers) == 1 and type(reviewers[0].get("prevent_self_review")) is bool,
            "release environment requires configured reviewers and an explicit self-review policy")
    entries = reviewers[0].get("reviewers")
    require(isinstance(entries, list) and 1 <= len(entries) <= 6, "release environment has no required reviewers")
    for entry in entries:
        require(isinstance(entry, dict) and entry.get("type") in ("User", "Team")
                and isinstance(entry.get("reviewer"), dict), "invalid required reviewer")
        positive_id(entry["reviewer"].get("id"))
    # The wildcard permits version tags, not branch dispatches; the exact tag/SHA
    # is independently constrained by workflow_context and tag_identity.
    require(environment.get("deployment_branch_policy") == {"protected_branches": False, "custom_branch_policies": True},
            "releases environment must restrict deployment to version tags")
    policies = api.items(PREFIX + "/environments/releases/deployment-branch-policies", "branch_policies")
    require(len(policies) == 1 and policies[0].get("name") == "v*" and policies[0].get("type") == "tag",
            "releases environment must allow only v* tags")
    return environment


def tag_identity(api, tag, expected_sha):
    require(isinstance(tag, str) and tag.startswith("v"), "existing annotated vMAJOR.MINOR.PATCH tag required")
    version_tuple(tag[1:])
    ref = api.json(PREFIX + "/git/ref/tags/" + urllib.parse.quote(tag, safe=""))
    require(isinstance(ref, dict) and ref.get("ref") == "refs/tags/" + tag, "wrong tag reference")
    obj = ref.get("object", {})
    require(obj.get("type") == "tag", "lightweight tag rejected")
    tag_sha = sha40(obj.get("sha"))
    annotated = api.json(PREFIX + "/git/tags/" + tag_sha)
    require(annotated.get("sha") == tag_sha and annotated.get("tag") == tag
            and annotated.get("object", {}).get("type") == "commit"
            and annotated["object"].get("sha") == expected_sha, "annotated tag must directly target the exact source SHA")
    return tag_sha


def release_history(api, tag, expected_sha):
    """404 is first-release only; an authentication/server error never is."""
    latest = api.json(PREFIX + "/releases/latest", missing=True)
    releases = api.items(PREFIX + "/releases")
    ids = [positive_id(x.get("id")) for x in releases]
    require(len(ids) == len(set(ids)), "duplicate release inventory")
    for release in releases:
        require(type(release.get("draft")) is bool and type(release.get("prerelease")) is bool,
                "invalid release metadata")
    public = [x for x in releases if not x["draft"]]
    if latest is None:
        require(not public, "latest-release 404 contradicts published history")
    else:
        require(isinstance(latest, dict) and latest.get("draft") is False and latest.get("prerelease") is False
                and latest.get("id") in ids, "invalid latest-release metadata")
    current = version_tuple(tag[1:])
    for release in public:
        require(isinstance(release.get("tag_name"), str) and release["tag_name"].startswith("v"), "unreadable published version")
        require(current > version_tuple(release["tag_name"][1:]), "release version must exceed every published version")
    matching = [x for x in releases if x.get("tag_name") == tag]
    require(len(matching) <= 1, "duplicate release tag")
    if matching:
        require(matching[0]["draft"] is True and matching[0].get("target_commitish") == expected_sha,
                "refusing to change published or unrelated release")
        return matching[0]
    return None


def candidate_run(api, run_id, expected_sha):
    from release_evidence import verify_run
    run = api.json(PREFIX + f"/actions/runs/{positive_id(run_id)}")
    require(isinstance(run, dict) and run.get("id") == run_id
            and run.get("repository", {}).get("full_name") == REPOSITORY
            and run.get("head_repository", {}).get("full_name") == REPOSITORY, "wrong candidate run/repository")
    positive_id(run.get("repository", {}).get("id"))
    positive_id(run.get("head_repository", {}).get("id"))
    attempt = positive_id(run.get("run_attempt"))
    workflow = api.json(PREFIX + "/actions/workflows/macos-candidate.yml")
    require(workflow.get("path") == CANDIDATE_WORKFLOW and run.get("workflow_id") == positive_id(workflow.get("id")),
            "candidate must use the trusted workflow")
    jobs = api.items(PREFIX + f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs", limit=MAX_QUALIFICATION_ROWS)
    verify_run(run, jobs, expected_sha)
    return run


def integration_run(api, expected_sha, expected=None):
    """Select the newest exact-source push, never an older successful fallback."""
    from release_evidence import verify_integration_run
    sha40(expected_sha)
    workflow = api.json(PREFIX + "/actions/workflows/integration.yml")
    workflow_id = positive_id(workflow.get("id"))
    require(workflow.get("path") == INTEGRATION_WORKFLOW, "wrong trusted integration workflow")
    runs = api.items(PREFIX + f"/actions/workflows/{workflow_id}/runs?head_sha={expected_sha}&event=push",
                     "workflow_runs", limit=MAX_QUALIFICATION_ROWS)
    require(runs, "no exact-source integration run")
    identifiers = [positive_id(run.get("id")) for run in runs]
    numbers = [positive_id(run.get("run_number")) for run in runs]
    require(len(set(identifiers)) == len(runs) and len(set(numbers)) == len(runs),
            "duplicate integration run inventory")
    require(all(run.get("workflow_id") == workflow_id and run.get("path") == INTEGRATION_WORKFLOW
                and run.get("head_sha") == expected_sha and run.get("event") == "push"
                and run.get("head_branch") == "main" for run in runs), "untrusted integration run inventory")
    selected = runs[numbers.index(max(numbers))]
    run_id = selected["id"]
    run = api.json(PREFIX + f"/actions/runs/{run_id}")
    require(run.get("id") == run_id and run.get("workflow_id") == workflow_id
            and run.get("run_number") == selected["run_number"], "integration run identity changed")
    attempt = positive_id(run.get("run_attempt"))
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "latest integration run is not successful and terminal")
    jobs = api.items(PREFIX + f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs",
                     limit=MAX_QUALIFICATION_ROWS)
    verify_integration_run(run, jobs, expected_sha)
    # The API's current run must still name the attempt whose jobs were read.
    current = api.json(PREFIX + f"/actions/runs/{run_id}")
    require(current.get("id") == run_id and current.get("workflow_id") == workflow_id
            and current.get("run_number") == selected["run_number"]
            and current.get("run_attempt") == attempt, "integration attempt changed while verifying")
    verify_integration_run(current, jobs, expected_sha)
    identity = {"run_id": run_id, "run_attempt": attempt, "workflow_id": workflow_id,
                "repository_id": positive_id(current.get("repository", {}).get("id")),
                "head_repository_id": positive_id(current.get("head_repository", {}).get("id"))}
    require(expected is None or identity == expected, "latest integration run/attempt changed; fresh qualification required")
    return identity


def authorized_reviewer(api, environment, user):
    """Bind approval to a current reviewer ID, including verified team membership."""
    user_id = positive_id(user.get("id"))
    entries = next(rule["reviewers"] for rule in environment["protection_rules"]
                   if rule.get("type") == "required_reviewers")
    if any(entry["type"] == "User" and entry["reviewer"]["id"] == user_id for entry in entries):
        return True
    for entry in entries:
        if entry["type"] != "Team":
            continue
        reviewer = entry["reviewer"]
        slug = reviewer.get("slug")
        require(isinstance(slug, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug),
                "required team lacks a safe slug")
        path = f"/orgs/{OWNER}/teams/{slug}"
        team = api.json(path)
        require(team.get("id") == reviewer["id"] and team.get("slug") == slug
                and team.get("organization", {}).get("login") == OWNER, "required team identity mismatch")
        members = api.items(path + "/members", limit=MAX_QUALIFICATION_ROWS)
        member_ids = [positive_id(member.get("id")) for member in members]
        require(len(member_ids) == len(set(member_ids)), "duplicate required-team membership inventory")
        if any(member.get("type") == "User" and member.get("id") == user_id
               and member.get("login") == user.get("login") for member in members):
            return True
    return False


def prepare(api, work, *, run_id, tag, expected_sha, allow_ad_hoc=False):
    from release_evidence import source_snapshot, verify_candidate
    sha40(expected_sha)
    source = source_snapshot(Path(__file__).resolve().parents[1])
    require(source.get("clean") is True and source.get("source_sha") == expected_sha,
            "publication checkout must be clean at the exact source SHA")
    package = object_json((Path(__file__).resolve().parents[1] / "package.json").read_bytes())
    version_tuple(package.get("version"))
    require(tag == "v" + package["version"], "tag version differs from trusted source package.json")
    protected_environment(api)  # Must precede any contents write or environment job.
    tag_sha = tag_identity(api, tag, expected_sha)
    draft = release_history(api, tag, expected_sha)
    run = candidate_run(api, run_id, expected_sha)
    integration = integration_run(api, expected_sha)
    artifacts = api.items(PREFIX + f"/actions/runs/{run_id}/artifacts", "artifacts")
    require(len(artifacts) == len(ARCHES), "candidate requires exactly two architecture artifacts")
    names = [x.get("name") for x in artifacts]
    require(set(names) == {f"CassQuery-mac-{arch}-{expected_sha}" for arch in ARCHES}, "unexpected candidate artifacts")
    require(len({positive_id(x.get("id")) for x in artifacts}) == len(ARCHES), "duplicate candidate artifact id")
    inventory = {}
    source_fingerprint = source["fingerprint"]
    for arch in ARCHES:
        artifact = artifacts[names.index(f"CassQuery-mac-{arch}-{expected_sha}")]
        require(artifact.get("expired") is False and type(artifact.get("size_in_bytes")) is int
                and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE, "expired/oversized candidate artifact")
        identity = artifact.get("workflow_run", {})
        require(identity.get("id") == run_id and identity.get("head_sha") == expected_sha
                and identity.get("head_branch") == "main"
                and identity.get("repository_id") == run.get("repository", {}).get("id")
                and identity.get("head_repository_id") == run.get("head_repository", {}).get("id"),
                "candidate artifact source identity mismatch")
        archive = work / f"{arch}.zip"
        api.download(PREFIX + f"/actions/artifacts/{artifact['id']}/zip", archive)
        artifact_digest = artifact.get("digest")
        require(isinstance(artifact_digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest),
                "candidate artifact lacks authenticated API digest")
        require(digest(archive)["sha256"] == artifact_digest[7:], "candidate archive API digest mismatch")
        root = work / arch
        extract_candidate(archive, root)
        manifest_file = root / "candidate.json"
        require(manifest_file.is_file() and manifest_file.stat().st_size <= MAX_JSON, "candidate root manifest missing/oversized")
        manifest = object_json(manifest_file.read_bytes())
        verify_candidate(root, manifest, expected_sha=expected_sha, expected_run_id=run_id,
                         expected_arch=arch, expected_attempt=run["run_attempt"])
        require(manifest.get("version") == tag[1:], "tag/candidate version mismatch")
        require(manifest["source_fingerprint"] == source_fingerprint,
                "candidate source fingerprints differ from the trusted publication checkout")
        distribution = manifest.get("distribution")
        if distribution == "adhoc":
            require(allow_ad_hoc is True, "ad-hoc candidate needs explicit allow_ad_hoc=true; not Developer ID distribution")
        else:
            # Current common contract deliberately cannot attest Developer ID/notarization.
            raise ReleaseError("unsupported signing evidence; no unsigned fallback")
        for asset in manifest["assets"]:
            relative = asset["path"]
            require(isinstance(relative, str) and relative.startswith("dist/mac-app/"), "unsafe candidate asset path")
            name = PurePosixPath(relative).name
            require(relative == "dist/mac-app/" + name and re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                    and name not in inventory, "unsafe/duplicate candidate asset")
            path = root / relative
            require(path.is_file() and not path.is_symlink() and digest(path) == {"bytes": asset["bytes"], "sha256": asset["sha256"]},
                    "candidate asset bytes mismatch")
            inventory[name] = {"path": path, **digest(path)}
        inventory[f"candidate-{arch}.json"] = {"path": manifest_file, **digest(manifest_file)}
    sums = work / "SHA256SUMS.txt"
    sums.write_text("".join(f"{inventory[name]['sha256']}  {name}\n" for name in sorted(inventory)), encoding="ascii")
    inventory[sums.name] = {"path": sums, **digest(sums)}
    body = (f"Candidate run: https://github.com/{REPOSITORY}/actions/runs/{run_id}\n"
            f"Integration run: https://github.com/{REPOSITORY}/actions/runs/{integration['run_id']}\n"
            f"Integration attempt: {integration['run_attempt']}\n"
            f"Source: {expected_sha}\nCandidate attempt: {run['run_attempt']}\nAnnotated tag object: {tag_sha}\n"
            "Distribution: ad-hoc, non-hardened; no Developer ID, notarization or Gatekeeper claim.\n"
            "ZIP/DMG and candidate manifests are promoted unchanged, without rebuilding.\n")
    if draft is not None:
        require(draft.get("body") == body and draft.get("name") == tag, "existing draft belongs to different evidence")
    require(tag_identity(api, tag, expected_sha) == tag_sha, "tag moved during candidate verification")
    # Detect a rerun/superseding attempt or a changed conclusion during downloads.
    require(candidate_run(api, run_id, expected_sha)["run_attempt"] == run["run_attempt"], "candidate attempt changed")
    integration_run(api, expected_sha, integration)
    return {"run_id": run_id, "attempt": run["run_attempt"], "sha": expected_sha,
            "source_fingerprint": source_fingerprint, "integration": integration,
            "tag": tag, "tag_sha": tag_sha, "body": body, "inventory": inventory, "draft": draft}


def workflow_context(api, plan, env=None):
    """Owner input plus server-side context; local environment variables alone are not approval."""
    from release_evidence import source_snapshot
    source = source_snapshot(Path(__file__).resolve().parents[1])
    require(source.get("clean") is True and source.get("source_sha") == plan["sha"]
            and source.get("fingerprint") == plan["source_fingerprint"], "trusted publication source changed")
    env = os.environ if env is None else env
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and env.get("GITHUB_REPOSITORY") == REPOSITORY and env.get("GITHUB_REPOSITORY_OWNER") == OWNER
            and env.get("GITHUB_REF") == f"refs/tags/{plan['tag']}" and env.get("GITHUB_SHA") == plan["sha"]
            and env.get("GITHUB_WORKFLOW_REF") == f"{REPOSITORY}/{WORKFLOW}@refs/tags/{plan['tag']}"
            and env.get("GITHUB_ACTOR") == OWNER and env.get("GITHUB_TRIGGERING_ACTOR") == OWNER
            and env.get("GITHUB_JOB") == "publish", "--execute requires owner-invoked exact-tag publication job")
    require(env.get("GITHUB_RUN_ATTEMPT") == "1", "release reruns require a fresh dispatch and approval")
    event = object_json(Path(env["GITHUB_EVENT_PATH"]).read_bytes())
    inputs = event.get("inputs", {})
    require(inputs.get("candidate_run_id") == str(plan["run_id"]) and inputs.get("tag") == plan["tag"]
            and inputs.get("mode") in ("draft", "publish")
            and inputs.get("allow_ad_hoc") in (True, "true"), "dispatch inputs do not authorize this promotion")
    current_id = int(env.get("GITHUB_RUN_ID", "0"))
    current = api.json(PREFIX + f"/actions/runs/{positive_id(current_id)}")
    trusted = api.json(PREFIX + "/actions/workflows/release.yml")
    require(current.get("id") == current_id and current.get("event") == "workflow_dispatch"
            and current.get("head_branch") == plan["tag"] and current.get("head_sha") == plan["sha"]
            and current.get("status") == "in_progress" and current.get("run_attempt") == 1
            and current.get("actor", {}).get("login") == OWNER
            and current.get("triggering_actor", {}).get("login") == OWNER
            and current.get("repository", {}).get("full_name") == REPOSITORY
            and current.get("head_repository", {}).get("full_name") == REPOSITORY
            and current.get("path") == WORKFLOW and trusted.get("path") == WORKFLOW
            and current.get("workflow_id") == positive_id(trusted.get("id")), "untrusted publication workflow run")
    environment = protected_environment(api)
    jobs = api.items(PREFIX + f"/actions/runs/{current_id}/attempts/1/jobs", "jobs", limit=MAX_QUALIFICATION_ROWS)
    for name, step_name in (("prepare", "Verify candidate and protection"), ("approve", "Recheck protected approval boundary")):
        matching = [x for x in jobs if x.get("name") == name]
        require(len(matching) == 1 and matching[0].get("status") == "completed"
                and matching[0].get("conclusion") == "success", "preparation/approval job did not succeed")
        steps = [x for x in matching[0].get("steps", []) if x.get("name") == step_name]
        require(len(steps) == 1 and steps[0].get("status") == "completed" and steps[0].get("conclusion") == "success",
                "preparation/approval step was skipped or failed")
    reviews = api.json(PREFIX + f"/actions/runs/{current_id}/approvals")
    require(isinstance(reviews, list), "unreadable environment approval history")
    relevant = [review for review in reviews if isinstance(review, dict) and any(
        isinstance(x, dict) and x.get("id") == environment["id"] and x.get("name") == "releases"
        for x in review.get("environments", []))]
    prevent_self_review = next(rule["prevent_self_review"] for rule in environment["protection_rules"]
                               if rule.get("type") == "required_reviewers")
    require(relevant and all(review.get("state") == "approved" and review.get("user", {}).get("type") == "User"
                             and isinstance(review["user"].get("login"), str) and review["user"]["login"]
                             and (not prevent_self_review or review["user"]["login"] != OWNER)
                             and (review["user"]["login"] != OWNER or (
                                 type(current.get("actor", {}).get("id")) is int and current["actor"]["id"] > 0
                                 and review["user"].get("id") == current["actor"]["id"]
                                 and current.get("triggering_actor", {}).get("id") == current["actor"]["id"]))
                             and authorized_reviewer(api, environment, review["user"]) for review in relevant),
            "configured protected environment approval is required")
    require(candidate_run(api, plan["run_id"], plan["sha"])["run_attempt"] == plan["attempt"], "candidate attempt changed")
    integration_run(api, plan["sha"], plan["integration"])
    return inputs["mode"]


def draft_state(api, plan, release_id):
    require(tag_identity(api, plan["tag"], plan["sha"]) == plan["tag_sha"], "tag moved; publication refused")
    release = api.json(PREFIX + f"/releases/{positive_id(release_id)}")
    require(release.get("id") == release_id and release.get("draft") is True and release.get("prerelease") is False
            and release.get("tag_name") == plan["tag"] and release.get("target_commitish") == plan["sha"]
            and release.get("body") == plan["body"], "release is published or draft identity changed")
    return release


def draft_assets(api, plan, release_id, work, *, complete):
    assets = api.items(PREFIX + f"/releases/{positive_id(release_id)}/assets")
    expected = plan["inventory"]
    names = [x.get("name") for x in assets]
    require(len(names) == len(set(names)) and all(name in expected for name in names), "unexpected/duplicate draft asset inventory")
    require(len({positive_id(x.get("id")) for x in assets}) == len(assets), "duplicate draft asset identifier")
    if complete:
        require(set(names) == set(expected), "incomplete draft asset inventory")
    for asset in assets:
        record = expected[asset["name"]]
        require(asset.get("state") == "uploaded" and type(asset.get("size")) is int and asset["size"] == record["bytes"],
                "draft asset size/state mismatch")
        path = work / str(asset["id"])
        api.download(PREFIX + f"/releases/assets/{asset['id']}", path, limit=record["bytes"])
        require(digest(path) == {"bytes": record["bytes"], "sha256": record["sha256"]}, "draft downloaded-byte checksum mismatch")
    return set(names)


def execute(api, plan, work, *, mode, env=None):
    require(mode in ("draft", "publish") and workflow_context(api, plan, env) == mode, "requested mode differs from owner dispatch")
    require(candidate_run(api, plan["run_id"], plan["sha"])["run_attempt"] == plan["attempt"], "candidate attempt changed")
    require(tag_identity(api, plan["tag"], plan["sha"]) == plan["tag_sha"], "tag moved")
    draft = release_history(api, plan["tag"], plan["sha"])
    if draft is None:
        # All authorization/source/native/evidence checks happened before this first write.
        require(tag_identity(api, plan["tag"], plan["sha"]) == plan["tag_sha"], "tag moved before draft creation")
        require(workflow_context(api, plan, env) == mode, "authorization changed before draft creation")
        draft = api.json(PREFIX + "/releases", method="POST", payload={
            "tag_name": plan["tag"], "target_commitish": plan["sha"], "name": plan["tag"],
            "body": plan["body"], "draft": True, "prerelease": False, "generate_release_notes": False})
    release_id = positive_id(draft.get("id"))
    draft_state(api, plan, release_id)
    before = work / "draft-before"
    before.mkdir()
    existing = draft_assets(api, plan, release_id, before, complete=False)
    for name in sorted(set(plan["inventory"]) - existing):
        draft_state(api, plan, release_id)
        record = plan["inventory"][name]
        require(digest(record["path"]) == {"bytes": record["bytes"], "sha256": record["sha256"]}, "candidate changed before upload")
        require(workflow_context(api, plan, env) == mode, "authorization changed before asset upload")
        uploaded = api.upload(release_id, name, record["path"])
        require(uploaded.get("name") == name and uploaded.get("state") == "uploaded"
                and uploaded.get("size") == record["bytes"], "asset upload response mismatch")
        positive_id(uploaded.get("id"))
    checked = work / "draft-checked"
    checked.mkdir()
    draft_assets(api, plan, release_id, checked, complete=True)
    draft_state(api, plan, release_id)
    # Approval/env/run/version checks are repeated after uploads, before any public write.
    require(workflow_context(api, plan, env) == mode, "authorization changed")
    require(candidate_run(api, plan["run_id"], plan["sha"])["run_attempt"] == plan["attempt"], "candidate attempt changed")
    fresh = release_history(api, plan["tag"], plan["sha"])
    require(fresh is not None and fresh.get("id") == release_id, "draft changed")
    draft_state(api, plan, release_id)
    if mode == "publish":
        # No deletes, no overwrites, no rebuilds. A failure leaves a draft for inspection.
        require(workflow_context(api, plan, env) == mode, "authorization changed before publication")
        released = api.json(PREFIX + f"/releases/{release_id}", method="PATCH", payload={"draft": False, "make_latest": "true"})
        require(released.get("id") == release_id and released.get("draft") is False
                and released.get("tag_name") == plan["tag"], "publication response mismatch")
        require(tag_identity(api, plan["tag"], plan["sha"]) == plan["tag_sha"], "tag moved after publication")
    return {"release_id": release_id, "draft": mode == "draft", "source_sha": plan["sha"],
            "candidate_run_id": plan["run_id"], "candidate_run_attempt": plan["attempt"],
            "integration": plan["integration"], "verified_assets": sorted(plan["inventory"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-run-id", type=int, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--allow-ad-hoc", action="store_true", default=False)
    parser.add_argument("--mode", choices=("draft", "publish"), default="draft")
    parser.add_argument("--execute", action="store_true", help="owner-dispatched protected CI context only")
    args = parser.parse_args()
    try:
        api = GitHub(os.environ.get("GITHUB_TOKEN", ""))
        with tempfile.TemporaryDirectory(prefix="cassquery-promotion-") as directory:
            work = Path(directory)
            plan = prepare(api, work, run_id=args.candidate_run_id, tag=args.tag,
                           expected_sha=args.source_sha, allow_ad_hoc=args.allow_ad_hoc)
            if args.execute:
                result = execute(api, plan, work, mode=args.mode)
            else:
                result = {"verified_only": True, "source_sha": plan["sha"], "candidate_run_id": plan["run_id"],
                          "candidate_run_attempt": plan["attempt"], "integration": plan["integration"],
                          "assets": sorted(plan["inventory"]), "contents_writes": 0}
            print(json.dumps(result, indent=2))
    except (ReleaseError, AssertionError, ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as error:
        print(f"Release promotion blocked: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
