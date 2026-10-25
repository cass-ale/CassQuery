"""Capability-gated local source workbench; project Python remains trusted code.

Paths are project-relative for editing. New project/adaptation and export/copy
paths come from the native dialog broker, not a renderer filesystem bridge.
"""
import ast
from contextlib import contextmanager
import copy
import ctypes
import errno
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import stat
import sys
import uuid

TEXT_LIMIT = 2 * 1024 * 1024
FILE_LIMIT = 2048
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
TEXT_SUFFIXES = {".py": "python", ".json": "json"}


class WorkbenchError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code = status, code
        self.details = details or {}


def fail(status, code, message, details=None):
    raise WorkbenchError(status, code, message, details)


def authorize(headers):
    token = os.environ.get("CASSQUERY_WORKBENCH_TOKEN", "")
    values = headers.get_all("X-CassQuery-Workbench", [])
    if not token or len(values) != 1 or not hmac.compare_digest(values[0].encode("utf-8"), token.encode("utf-8")):
        fail(403, "CAPABILITY_REQUIRED", "A workbench capability is required")


def text_bytes(text):
    if not isinstance(text, str):
        fail(400, "INVALID_TEXT", "text must be a UTF-8 string")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        fail(400, "INVALID_ENCODING", "Text is not valid UTF-8")
    if len(raw) > TEXT_LIMIT:
        fail(413, "DOCUMENT_TOO_LARGE", "Text exceeds 2 MiB")
    return raw


def relative_text_path(value):
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        fail(400, "INVALID_PATH", "A root-relative text path is required")
    p = PurePosixPath(value)
    if p.is_absolute() or any(c in ("", ".", "..") for c in value.split("/")) or ":" in value or any(c in SKIP_DIRS for c in p.parts):
        fail(400, "INVALID_PATH", "Path must remain in the project input tree")
    if p.suffix.lower() not in TEXT_SUFFIXES:
        fail(400, "UNSUPPORTED_FILE", "Only Python and JSON text can be edited")
    return p


@contextmanager
def parent_fd(root, relative):
    """Anchor operations to directory descriptors; never follow a path symlink."""
    p = relative_text_path(relative)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        descriptor = os.open(str(root), flags)
        descriptors.append(descriptor)
        for part in p.parts[:-1]:
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        yield descriptor, p.name
    except FileNotFoundError:
        fail(404, "FILE_NOT_FOUND", "Project file or directory does not exist")
    except OSError as error:
        if error.errno in (errno.ENOTDIR, errno.ELOOP):
            fail(400, "SYMLINK_PATH", "Symlink or non-directory paths cannot be edited")
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_at(directory, name):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except FileNotFoundError:
        fail(404, "FILE_NOT_FOUND", "Project file does not exist")
    except OSError as error:
        if error.errno == errno.ELOOP:
            fail(400, "SYMLINK_PATH", "Symlink files cannot be edited")
        raise
    with os.fdopen(descriptor, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            fail(400, "UNSUPPORTED_FILE", "Only regular text files can be edited")
        if before.st_size > TEXT_LIMIT:
            fail(413, "DOCUMENT_TOO_LARGE", "Text exceeds 2 MiB")
        raw = file.read(TEXT_LIMIT + 1)
        after = os.fstat(file.fileno())
    if len(raw) > TEXT_LIMIT:
        fail(413, "DOCUMENT_TOO_LARGE", "Text exceeds 2 MiB")
    if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size):
        fail(409, "CONFLICT", "File changed while being read; keep the draft and reload")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        fail(400, "INVALID_ENCODING", "Project text is not valid UTF-8")
    return raw, text, after


def read_text(root, path):
    with parent_fd(root, path) as (directory, name):
        raw, text, info = read_at(directory, name)
    return {"path": path, "text": text, "sha256": hashlib.sha256(raw).hexdigest(), "read_only": not bool(info.st_mode & 0o222)}


def _exchange_files(directory, left, right):
    """Atomic entry exchange, preserving the exact displaced entry for inspection.

    Darwin SDK sys/stdio.h: RENAME_SWAP=0x2, renameatx_np available since 10.12.
    Linux renameat2: RENAME_EXCHANGE=0x2. No replace-only fallback is safe here.
    """
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameatx_np" if sys.platform == "darwin" else "renameat2", None) if sys.platform in ("darwin", "linux") else None
    if function is None:
        fail(501, "ATOMIC_SAVE_UNAVAILABLE", "This platform cannot safely exchange source revisions")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(directory, os.fsencode(left), directory, os.fsencode(right), 2) != 0:
        code = ctypes.get_errno()
        if code in (errno.ENOSYS, errno.ENOTSUP, errno.EINVAL):
            fail(501, "ATOMIC_SAVE_UNAVAILABLE", "This filesystem cannot safely exchange source revisions")
        raise OSError(code, os.strerror(code))


def _revision_at(directory, name):
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    result = {"bytes": info.st_size, "sha256": None, "kind": "regular" if stat.S_ISREG(info.st_mode) else "nonregular"}
    if stat.S_ISREG(info.st_mode) and info.st_size <= TEXT_LIMIT:
        descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        with os.fdopen(descriptor, "rb") as file:
            raw = file.read(TEXT_LIMIT + 1)
        if len(raw) <= TEXT_LIMIT:
            result["sha256"] = hashlib.sha256(raw).hexdigest()
    return result


JOURNAL = ".cq-workbench-journal.json"


def load_conflict(root):
    try:
        record = read_text(root, JOURNAL)
    except WorkbenchError as error:
        if error.code == "FILE_NOT_FOUND":
            return None
        return {"path": None, "active_file_changed": True, "recovery_paths": [], "journal_error": "Recovery journal cannot be read"}
    try:
        data = json.loads(record["text"])
        if not isinstance(data, dict) or data.get("schema") != 1 or len(record["text"]) > 16384:
            raise ValueError("invalid journal")
        relative_text_path(data["path"])
        for item in data["recovery_paths"]:
            relative_text_path(item["path"])
        data["active_file_changed"] = True  # Prepared transaction may have exchanged before interruption.
        return data
    except (ValueError, KeyError, TypeError, WorkbenchError):
        return {"path": None, "active_file_changed": True, "recovery_paths": [], "journal_error": "Recovery journal needs manual reconciliation"}


def _write_journal(directory, data, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if exclusive:
        flags |= os.O_EXCL
    descriptor = os.open(JOURNAL, flags, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            fail(409, "UNRESOLVED_CONFLICT", "Recovery journal is not a regular file")
        file.write(json.dumps(data, separators=(",", ":")).encode())
        file.truncate()
        file.flush()
        os.fsync(file.fileno())


def _prepare_journal(root, directory, path, temporary, draft_sha256, resolve):
    inherited = load_conflict(root)
    if inherited and not resolve:
        fail(409, "UNRESOLVED_CONFLICT", "An interrupted source save must be resolved", inherited)
    recovery = str(PurePosixPath(path).parent / temporary)
    paths = inherited.get("recovery_paths", []) if inherited else []
    data = {"schema": 1, "path": path, "draft_sha256": draft_sha256, "phase": "prepared", "active_file_changed": True,
            "recovery_paths": [*paths, {"path": recovery, "sha256": None, "bytes": None}]}
    _write_journal(directory, data, exclusive=not resolve)
    return data


def save_text(root, path, text, expected, *, resolve=False):
    raw = text_bytes(text)
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        fail(400, "INVALID_REVISION", "expected_sha256 must be a current SHA-256 or null for a new file")
    with parent_fd(root, path) as (directory, name), parent_fd(root, JOURNAL) as (root_directory, _):
        root_identity = os.fstat(root_directory)
        parent_identity = os.fstat(directory)
        def check_parent():
            try:
                current_root = os.stat(root, follow_symlinks=False)
                current_parent = os.stat(Path(root, *PurePosixPath(path).parts[:-1]), follow_symlinks=False)
            except FileNotFoundError:
                fail(409, "CONFLICT", "Project directory moved while saving; keep the draft")
            if (not stat.S_ISDIR(current_root.st_mode) or not stat.S_ISDIR(current_parent.st_mode)
                    or (root_identity.st_dev, root_identity.st_ino) != (current_root.st_dev, current_root.st_ino)
                    or (parent_identity.st_dev, parent_identity.st_ino) != (current_parent.st_dev, current_parent.st_ino)):
                fail(409, "CONFLICT", "Project directory changed while saving; keep the draft")
        def check_current():
            try:
                current, _, info = read_at(directory, name)
            except WorkbenchError as error:
                if error.code == "FILE_NOT_FOUND" and expected is None:
                    return 0o644
                if error.code == "FILE_NOT_FOUND":
                    fail(409, "CONFLICT", "File was removed; keep the draft")
                raise
            if expected is None or hashlib.sha256(current).hexdigest() != expected:
                fail(409, "CONFLICT", "File changed externally; keep the draft")
            if not info.st_mode & 0o222:
                fail(403, "READ_ONLY", "The source file is read-only")
            return stat.S_IMODE(info.st_mode)
        mode = check_current()
        temporary = ".cq-workbench-recovery-" + uuid.uuid4().hex + PurePosixPath(path).suffix
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        preserve = False
        journal, inherited = None, None
        exchange_attempted = False
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(raw)
                os.fchmod(file.fileno(), mode)
                file.flush()
                os.fsync(file.fileno())
            # Project lock serializes workbench writers. Exchange captures the
            # actual displaced entry; an external replacement cannot be deleted
            # unseen. On mismatch keep both versions, never race a blind restore.
            check_parent()
            check_current()
            if expected is None:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(temporary, dir_fd=directory)
            else:
                inherited = load_conflict(root)
                journal = _prepare_journal(root, root_directory, path, temporary, hashlib.sha256(raw).hexdigest(), resolve)
                exchange_attempted = True
                _exchange_files(directory, temporary, name)
                preserve = True
                displaced = _revision_at(directory, temporary)
                if displaced["sha256"] != expected:
                    recovery = str(PurePosixPath(path).parent / temporary)
                    active = _revision_at(directory, name)
                    fail(409, "CONFLICT", "A concurrent revision was displaced and retained; resolve the conflict before building",
                         {"path": path, "active_file_changed": True, "active_revision": active,
                          "draft_sha256": hashlib.sha256(raw).hexdigest(),
                          "recovery_paths": [*(inherited.get("recovery_paths", []) if inherited else []), {"path": recovery, **displaced}]})
                os.unlink(JOURNAL, dir_fd=root_directory)
                journal = None
                preserve = False
        except BaseException as error:
            if not exchange_attempted and isinstance(error, FileExistsError):
                fail(409, "CONFLICT", "Destination was created externally; keep the draft")
            if isinstance(error, WorkbenchError) and error.code == "ATOMIC_SAVE_UNAVAILABLE":
                if inherited:
                    _write_journal(root_directory, inherited)
                elif journal:
                    os.unlink(JOURNAL, dir_fd=root_directory)
                journal = None
                exchange_attempted = False
                raise
            if exchange_attempted:
                preserve = True
                if isinstance(error, WorkbenchError) and error.details.get("active_file_changed"):
                    raise
                recovery = str(PurePosixPath(path).parent / temporary)
                raise WorkbenchError(409, "CONFLICT", "Source exchange was attempted but verification failed; retain and resolve both revisions",
                                     {"path": path, "active_file_changed": True, "active_revision": {"sha256": None, "bytes": None},
                                      "draft_sha256": hashlib.sha256(raw).hexdigest(),
                                      "recovery_paths": journal["recovery_paths"] if journal else [{"path": recovery, "sha256": None, "bytes": None}]}) from error
            raise
        finally:
            if not preserve:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def scalar(value):
    return type(value) in (bool, int, str) or type(value) is float and math.isfinite(value)


def parameter_spans(text):
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        fail(400, "SYNTAX_ERROR", f"Python syntax error on line {error.lineno}")
    candidates, counts = [], {}
    class ScopeWrites(ast.NodeVisitor):
        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                counts[node.id] = counts.get(node.id, 0) + 1
        def visit_FunctionDef(self, node):
            counts[node.name] = counts.get(node.name, 0) + 1
            for declaration in ast.walk(node):
                if isinstance(declaration, (ast.Global, ast.Nonlocal)):
                    for name in declaration.names:
                        counts[name] = counts.get(name, 0) + 1
            for value in [*node.decorator_list, *node.args.defaults, *(v for v in node.args.kw_defaults if v)]:
                self.visit(value)
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_ClassDef(self, node):
            counts[node.name] = counts.get(node.name, 0) + 1
            for value in [*node.bases, *node.decorator_list]:
                self.visit(value)
            for declaration in ast.walk(node):
                if isinstance(declaration, ast.Global):
                    for name in declaration.names:
                        counts[name] = counts.get(name, 0) + 1
        def visit_alias(self, node):
            name = node.asname or node.name.split(".", 1)[0]
            counts[name] = counts.get(name, 0) + 1
        def visit_Lambda(self, node):
            pass  # Lambda-local bindings are not module parameters.
        def visit_comprehension(self, node):
            self.visit(node.iter)
            for condition in node.ifs:
                self.visit(condition)
    ScopeWrites().visit(tree)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if len(targets) != 1 or not isinstance(targets[0], ast.Name) or value is None:
            continue
        name = targets[0].id
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if not scalar(literal) or name.startswith("_"):
            continue
        candidates.append((name, literal, value))
    return [(name, value, node) for name, value, node in candidates if counts[name] == 1]


def replace_parameter(text, name, value):
    if not isinstance(name, str) or not name.isidentifier() or not scalar(value):
        fail(400, "INVALID_PARAMETER", "Parameter value must be a finite scalar literal")
    matches = [(n, old, node) for n, old, node in parameter_spans(text) if n == name]
    if len(matches) != 1:
        fail(400, "UNSUPPORTED_PARAMETER", "Parameter is not a unique top-level scalar assignment")
    old_value = matches[0][1]
    if type(old_value) is float and type(value) is int:
        value = float(value)  # JSON has no separate integer/float scalar type.
    if type(value) is not type(old_value) or not scalar(value):
        fail(400, "INVALID_PARAMETER_TYPE", "Parameter edits must preserve the scalar type")
    node = matches[0][2]
    lines = text.encode("utf-8").splitlines(keepends=True)
    start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    raw = text.encode("utf-8")
    return (raw[:start] + repr(value).encode("utf-8") + raw[end:]).decode("utf-8")


def validate_transforms(manifest):
    settings = manifest.get("workbench", {})
    if not isinstance(settings, dict) or not isinstance(settings.get("transforms", {}), dict):
        raise ValueError("workbench/transforms must be objects")
    for part, values in settings.get("transforms", {}).items():
        if not isinstance(part, str) or not part or not isinstance(values, dict):
            raise ValueError("each workbench transform must identify a named part")
        for key in ("translation", "rotation"):
            vector = values.get(key, [0, 0, 0])
            if not isinstance(vector, list) or len(vector) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
                raise ValueError(f"transform {key} must have three finite numbers")
        scale = values.get("scale", 1)
        if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
            raise ValueError("transform scale must be finite and positive")


def build_script(path):
    """Execute one consented original with its own relative file context."""
    previous = os.getcwd()
    try:
        os.chdir(Path(path).parent)
        return _build_script(path)
    finally:
        os.chdir(previous)


def _build_script(path):
    """Collect only explicit solid output; never execute for introspection/export."""
    import cadquery as cq
    outputs = []
    def show_object(shape, name=None, options=None, **kwargs):
        outputs.append((name, shape))
    namespace = runpy.run_path(str(path), init_globals={"show_object": show_object}, run_name="__cassquery_workbench__")
    builder = namespace.get("build")
    if callable(builder):
        result = builder()
        if isinstance(result, dict) and isinstance(result.get("parts"), dict):
            return result
        outputs = [("Result", result)]
    elif not outputs and "result" in namespace:
        outputs = [("Result", namespace["result"])]
    if not outputs:
        raise ValueError("Python project needs build(), explicit show_object solids, or a supported result")
    parts = {}
    for index, (name, value) in enumerate(outputs, 1):
        name = name or f"Part {index}"
        if not isinstance(name, str) or not name or name in parts:
            raise ValueError("show_object names must be nonempty and unique")
        if isinstance(value, cq.Workplane):
            values = value.vals()
            if not values or not all(isinstance(v, cq.Shape) for v in values):
                raise ValueError("Workplane output must contain only shapes")
            value = values[0] if len(values) == 1 else cq.Compound.makeCompound(values)
        if not isinstance(value, cq.Shape) or not value.Solids() or not value.isValid() or value.Volume() <= 0:
            raise ValueError("Project output must be a valid positive-volume BREP solid")
        parts[name] = value
    return {"names": list(parts), "parts": parts, "groups": {"ALL": list(parts)}, "axes": {}}


class Workbench:
    def __init__(self, backend):
        self.backend = backend

    def project(self, name):
        if not isinstance(name, str):
            fail(400, "INVALID_PROJECT", "project must be a name")
        project = self.backend.get_project(name)
        if project is None:
            fail(404, "PROJECT_NOT_FOUND", "Project is not registered")
        return project

    def read_only(self, project):
        return self.backend.resource_read_only(project.root) or not os.access(project.root, os.W_OK)

    def writable(self, project):
        if self.read_only(project):
            fail(403, "READ_ONLY", "Bundled/application source is read-only; create a workspace")

    def list(self, project):
        files = []
        for directory, dirs, names in os.walk(project.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(directory, d).is_symlink())
            if len(Path(directory).relative_to(project.root).parts) > 32:
                fail(413, "PROJECT_TOO_LARGE", "Project file tree exceeds the workbench bound")
            for name in sorted(names):
                p = Path(directory, name)
                if name.startswith(".cq-workbench-") or p.is_symlink() or not p.is_file():
                    continue
                if any(self.backend._inside(output, str(p)) for output in (self.backend.PAYLOAD_DIR, self.backend.SHOT_DIR)):
                    continue
                files.append({"path": p.relative_to(project.root).as_posix(), "kind": TEXT_SUFFIXES.get(p.suffix.lower(), "asset"), "bytes": p.stat().st_size})
                if len(files) > FILE_LIMIT:
                    fail(413, "PROJECT_TOO_LARGE", "Project has too many files for the workbench")
        with project.lock:
            entry = project.manifest["entry"].split(":", 1)[0]
            source = project.manifest.get("workbench", {}).get("source", entry)
        return {"project": project.name, "root": project.root, "entry": entry, "source": source, "files": files, "read_only": self.read_only(project)}

    def create(self, directory, name):
        if not isinstance(directory, str) or "\0" in directory or not Path(directory).is_absolute():
            fail(400, "INVALID_PATH", "Select an absolute parent directory")
        parent = Path(directory).expanduser().resolve()
        if not parent.is_dir() or self.backend.resource_read_only(str(parent)):
            fail(403, "READ_ONLY", "Select an existing writable parent outside application resources")
        self.backend._component(name, "project name")
        root = parent / name
        self.backend.check_project_name(name, str(root))
        try:
            root.mkdir()
        except FileExistsError:
            fail(409, "PROJECT_EXISTS", "New project directory already exists")
        code = "import cadquery as cq\n\nWIDTH = 20.0\nDEPTH = 12.0\nHEIGHT = 6.0\n\ndef build():\n    shape = cq.Workplane('XY').box(WIDTH, DEPTH, HEIGHT).val()\n    return {'names': ['Body'], 'parts': {'Body': shape}}\n"
        save_text(root, "part_entry.py", code, None)
        save_text(root, "viewer.json", json.dumps({"name": name, "entry": "part_entry.py:build", "live": True}, indent=2) + "\n", None)
        return self.backend.register_project(str(root))

    def open(self, body):
        path = body.get("path")
        if not isinstance(path, str) or "\0" in path or not Path(path).is_absolute():
            fail(400, "INVALID_PATH", "A native-selected absolute path is required")
        selected = Path(path).resolve()
        if Path(path).is_symlink():
            fail(400, "SYMLINK_PATH", "Select the actual project file or directory")
        if selected.suffix.lower() in (".step", ".stp") and selected.is_file():
            fail(400, "IMPORT_REQUIRED", "STEP files require the import action and a selected new workspace")
        root = selected if selected.is_dir() else selected.parent
        if root in (Path.home().resolve(), Path(root.anchor)):
            fail(400, "UNSAFE_PROJECT_ROOT", "Select a bounded project folder, not home or the filesystem root")
        if not (root / "viewer.json").is_file():
            if not selected.is_file() or selected.suffix.lower() != ".py":
                fail(400, "MANIFEST_REQUIRED", "Folder projects need viewer.json")
            if body.get("adapt") is not True:
                fail(409, "ADAPTER_REQUIRED", "Confirm creation of a manifest and adapter beside this trusted Python file")
            if self.backend.resource_read_only(str(root)) or root in (Path.home().resolve(), Path(root.anchor)):
                fail(403, "READ_ONLY", "Create a workspace before adapting this source location")
            # Consent is native-owned. These new sidecars preserve the original
            # script/helper/resource tree and never overwrite existing source.
            relative_text_path(selected.name)
            name = self.backend._component(selected.stem, "project name")
            self.backend.check_project_name(name, str(root))
            adapter = "cassquery_entry_" + uuid.uuid4().hex[:12] + ".py"
            code = f"from pathlib import Path\nfrom workbench import build_script\n\ndef build():\n    return build_script(Path(__file__).with_name({selected.name!r}))\n"
            save_text(root, adapter, code, None)
            try:
                save_text(root, "viewer.json", json.dumps({"name": name, "entry": adapter + ":build", "live": True, "workbench": {"source": selected.name}}, indent=2) + "\n", None)
            except BaseException:
                (root / adapter).unlink(missing_ok=True)
                raise
        project = self.backend.register_project(str(root))
        result = self.list(project)
        result["status"] = project.status()
        return result

    def inspect(self, project, variant="neutral"):
        with project.lock:
            record = project.certified
            if not isinstance(variant, str) or record and variant not in record["variant_truth"]:
                fail(400, "UNKNOWN_VARIANT", "Inspector must identify a certified variant")
            stale = not self.backend.certificate_current(project)
            manifest = copy.deepcopy(project.manifest)
            truth = copy.deepcopy(record["variant_truth"][variant]) if record else {}
            conflict = copy.deepcopy(project.workbench_conflict)
        revision = read_text(project.root, "viewer.json")["sha256"]
        return {"project": project.name, "variant": variant, "units": "mm", "parts": [{"name": name, **facts} for name, facts in truth.items()], "certified_hash": record["hash"] if record else None,
                "source_fingerprint": record["source_fingerprint"] if record else None, "built_at": record["built_at"] if record else None,
                "stale": stale, "conflict": conflict, "transforms": manifest.get("workbench", {}).get("transforms", {}), "manifest_sha256": revision}

    def write(self, project, path, text, expected, *, auto_build=None, force=False, resolve=False):
        conflict = project.workbench_conflict
        if PurePosixPath(path).name.startswith(".cq-workbench-"):
            fail(403, "RESERVED_PATH", "Workbench recovery files cannot be overwritten")
        if conflict and (not resolve or conflict["path"] != path):
            fail(409, "UNRESOLVED_CONFLICT", "Choose a retained revision before saving", conflict)
        try:
            result = save_text(project.root, path, text, expected, resolve=resolve)
        except WorkbenchError as error:
            if error.details.get("active_file_changed"):
                project.workbench_conflict = copy.deepcopy(error.details)
                project.preview_dirty = project.certified_due = False
                project.gen += 1
                self.backend.set_state(project, "conflict", "A concurrent revision was retained; choose a revision before building")
            raise
        if resolve:
            project.workbench_conflict = None
        self.backend.changed_project(project, str(Path(project.root, path)), auto_build=auto_build, force=force)
        return result

    def dispatch(self, body):
        action = body.get("action")
        if action == "open":
            return self.open(body)
        if action == "create":
            project = self.create(body.get("directory"), body.get("name"))
            return {**self.list(project), "status": project.status()}
        if action == "import":
            import cad_io
            try:
                with self.backend.geometry_operation():
                    result = cad_io.import_step(body.get("path"), body.get("directory"), body.get("name"))
            except cad_io.CadIOError as error:
                raise WorkbenchError(error.status, error.code, str(error), getattr(error, "details", {})) from error
            project = self.backend.register_project(result["root"])
            return {**result, **self.list(project), "status": project.status()}
        project = self.project(body.get("project"))
        if action == "list":
            return self.list(project)
        if action == "inspect":
            return self.inspect(project, body.get("variant", "neutral"))
        if action == "read":
            with project.lock:
                result = read_text(project.root, body.get("path"))
            token = os.environ.get("CASSQUERY_WORKBENCH_TOKEN", "")
            if token and token in result["text"]:
                fail(403, "SENSITIVE_SOURCE", "Source containing the active capability cannot be displayed")
            result["read_only"] = result["read_only"] or self.read_only(project)
            return result
        if action in ("save", "setParameter", "transform"):
            self.writable(project)
            if "expected_sha256" not in body:
                fail(400, "INVALID_REVISION", "A current expected_sha256 is required")
            with project.lock:
                path, text = body.get("path"), body.get("text")
                auto_build = body.get("auto_build", True)
                if type(auto_build) is not bool:
                    fail(400, "INVALID_SETTING", "auto_build must be boolean")
                previous = None
                if action == "setParameter":
                    source = read_text(project.root, path)
                    text = replace_parameter(source["text"], body.get("name"), body.get("value"))
                if action == "transform":
                    path = "viewer.json"
                    source = read_text(project.root, path)
                    manifest = json.loads(source["text"])
                    part = body.get("part")
                    if not project.certified or part not in project.certified["truth"]:
                        fail(400, "UNKNOWN_PART", "Transform must identify a certified named part")
                    previous = copy.deepcopy(manifest.get("workbench", {}).get("transforms", {}).get(part, {"translation": [0, 0, 0], "rotation": [0, 0, 0], "scale": 1}))
                    values = {"translation": body.get("translation", [0, 0, 0]), "rotation": body.get("rotation", [0, 0, 0]), "scale": body.get("scale", 1)}
                    manifest.setdefault("workbench", {}).setdefault("transforms", {})[part] = values
                    validate_transforms(manifest)
                    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
                resolve = body.get("resolve_conflict", False)
                if type(resolve) is not bool or resolve and action != "save":
                    fail(400, "INVALID_REQUEST", "Conflict resolution requires an explicit source save")
                result = self.write(project, path, text, body["expected_sha256"],
                                    auto_build=None if action == "transform" else auto_build,
                                    force=action == "transform", resolve=resolve)
            response = {"project": project.name, **result, "queued": action == "transform" or auto_build}
            if action == "transform":
                response.update(previous=previous, transform=values, manifest_sha256=result["sha256"])
            return response
        if action == "parameters":
            source = read_text(project.root, body.get("path"))
            if not str(body.get("path")).endswith(".py"):
                fail(400, "UNSUPPORTED_FILE", "Parameters require Python source")
            result = [{"name": name, "value": value, "kind": type(value).__name__, "line": node.lineno} for name, value, node in parameter_spans(source["text"])]
            return {"project": project.name, "path": source["path"], "sha256": source["sha256"], "parameters": result, "read_only": source["read_only"] or self.read_only(project)}
        if action == "saveAs":
            # Save a Copy: never repoint the active project or overwrite its source.
            relative_text_path(body.get("path"))
            destination = body.get("destination")
            if not isinstance(destination, str) or not Path(destination).is_absolute() or "\0" in destination:
                fail(400, "INVALID_PATH", "Select an absolute Save a Copy destination")
            target = Path(destination)
            if self.backend.resource_read_only(str(target)):
                fail(403, "READ_ONLY", "Application resources cannot be overwritten")
            if target.resolve() == Path(project.root, body["path"]).resolve():
                fail(409, "CONFLICT", "Use Save with a revision to overwrite the active source")
            expected = None
            if target.exists():
                if body.get("overwrite") is not True:
                    fail(409, "CONFLICT", "Save a Copy destination exists; native confirmation is required")
                expected = read_text(target.parent.resolve(), target.name)["sha256"]
            result = save_text(target.parent.resolve(), target.name, body.get("text"), expected)
            return {**result, "path": str(target), "source_path": body["path"], "copy": True, "active_project_changed": False}
        if action == "export":
            import cad_io
            try:
                with self.backend.certified_record(project.name) as record:
                    return cad_io.export_geometry(record, body.get("path"), body.get("format"), variant=body.get("variant", "neutral"), parts=body.get("parts"), overwrite=body.get("overwrite", False))
            except cad_io.CadIOError as error:
                raise WorkbenchError(error.status, error.code, str(error), getattr(error, "details", {})) from error
        fail(400, "UNKNOWN_ACTION", "Unknown workbench action")
