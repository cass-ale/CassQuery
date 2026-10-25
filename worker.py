#!/usr/bin/env python3
"""CassQuery worker (spec v0.3 §5): warm CadQuery build server.

One process: HTTP (viewer + payloads + agent protocol), a localhost
websocket that announces new payloads by {name, hash}, a file watcher,
and a single build thread. Preview builds are coarse and ungated;
certified builds run the §6 gates and are the only payloads carrying
step states. Failed builds announce nothing; the last certified model
stays live. No authentication in v1 (loopback only; deferred to v2).

Run:  python worker.py [project_dir ...] [--port N]
      (or CASSQUERY_PROJECTS=/path/projA:/path/projB python worker.py;
      with neither, the bundled testpart demo is served)
"""
import asyncio, base64, copy, hashlib, importlib, importlib.machinery, importlib.util, json, math, os, re, stat, sys, tempfile, threading, time, traceback, uuid
from contextlib import contextmanager
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, unquote, urlsplit

import workbench

try:
    import numpy as np
except ImportError:
    np = None  # HTTP/manifest tests need no geometry runtime; main fails preflight.


def _preflight():
    # Fail before serving if this interpreter cannot build: a missing
    # websockets used to kill the announce thread silently, and a missing
    # cadquery only surfaced as an opaque per-build error.
    missing = []
    for name in ("numpy", "cadquery", "OCP", "websockets.asyncio.server"):
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    if missing:
        print(f"CassQuery worker cannot start: {sys.executable} is missing: "
              f"{', '.join(missing)}.\n"
              "Install CadQuery 2.5+ (and websockets) into a virtualenv and run "
              "this worker with that interpreter; the Electron shell picks its "
              "interpreter from CASSQUERY_PYTHON.", file=sys.stderr)
        raise SystemExit(1)


def _data_dir():
    if os.environ.get("CASSQUERY_DATA_DIR"):
        return os.path.abspath(os.path.expanduser(os.environ["CASSQUERY_DATA_DIR"]))
    if not getattr(sys, "frozen", False):
        return HERE
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/CassQuery")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CassQuery")
    return os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "CassQuery")


HERE = os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))))
VIEWER_DIR = os.path.join(HERE, "viewer")
DATA_DIR = _data_dir()
PAYLOAD_DIR = os.path.join(DATA_DIR, "payloads")
SHOT_DIR = os.path.join(DATA_DIR, "evidence", "shots")


def redact_sensitive(value):
    token = os.environ.get("CASSQUERY_WORKBENCH_TOKEN", "")
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, dict):
        return {redact_sensitive(key): redact_sensitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


def _atomic_write(path, data, before_replace=None):
    # Unique sibling temporary files avoid collisions between simultaneous writes.
    fd, tmp = tempfile.mkstemp(prefix=".cq-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if before_replace is not None:
            before_replace()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

# finish table: (material, finish) -> renderer PBR (pilot values for 316L)
FINISH = {
    ("316L", "brushed"):  {"color": "#96999e", "roughness": 0.38, "metalness": 1.0},
    ("316L", "polished"): {"color": "#a8a9ad", "roughness": 0.16, "metalness": 1.0},
    ("Gr2 Ti", "brushed"):  {"color": "#8a857e", "roughness": 0.42, "metalness": 1.0},
    ("Gr2 Ti", "polished"): {"color": "#99948c", "roughness": 0.20, "metalness": 1.0},
    ("440C", "polished"): {"color": "#b2b3b5", "roughness": 0.12, "metalness": 1.0},
    ("440C", "brushed"):  {"color": "#a3a4a6", "roughness": 0.30, "metalness": 1.0},
}
FINISH_DEFAULT = {"color": "#999ba0", "roughness": 0.35, "metalness": 1.0}

COARSE = (0.1, 0.2)    # (linear mm, angular rad) — preview
FINE = (0.01, 0.05)    # certified
TRI_BUDGET = 300_000
BYTE_BUDGET = 5_000_000


# ---------- tessellation (OCP, world-space, per solid) ----------
def _tessellate_solid(shape, tol, ang):
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    BRepTools.Clean_s(shape.wrapped)
    BRepMesh_IncrementalMesh(shape.wrapped, tol, False, ang, True)
    verts, tris, missing = [], [], 0
    ex = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            missing += 1
        else:
            trsf = loc.Transformation()
            base = len(verts)
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                verts.append((p.X(), p.Y(), p.Z()))
            rev = face.Orientation() == TopAbs_REVERSED
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                tris.append((base + a - 1, base + c - 1, base + b - 1) if rev
                            else (base + a - 1, base + b - 1, base + c - 1))
        ex.Next()
    return (np.asarray(verts, float).reshape(-1, 3),
            np.asarray(tris, np.int64).reshape(-1, 3), missing)


def _validate_mesh(verts, tris):
    if (verts.ndim != 2 or verts.shape[1] != 3 or tris.ndim != 2 or tris.shape[1] != 3 or
            not len(verts) or not len(tris) or not np.isfinite(verts).all() or
            not np.issubdtype(tris.dtype, np.integer) or tris.min() < 0 or tris.max() >= len(verts) or
            np.any(tris[:, 0] == tris[:, 1]) or np.any(tris[:, 1] == tris[:, 2]) or
            np.any(tris[:, 2] == tris[:, 0])):
        raise ValueError("empty, non-finite or invalid triangle mesh")


def _weld(verts, tris, tol=1e-4):
    if len(verts) == 0:
        return verts, tris
    grid = np.round(verts / tol)
    if not np.isfinite(grid).all() or (np.abs(grid) >= np.iinfo(np.int64).max).any():
        raise ValueError("mesh coordinates exceed welding range")
    _, idx, inv = np.unique(grid.astype(np.int64), axis=0,
                            return_index=True, return_inverse=True)
    return verts[idx], inv[tris]


def _edge_counts(tris):
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    return uniq, counts


def _watertight(tris):
    if len(tris) == 0:
        return False
    _, counts = _edge_counts(tris)
    return bool((counts == 2).all())


def _mesh_area(verts, tris):
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    return float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())


def _capsule_intervals(p, q, a, b, eps):
    """Parameter intervals of p+t(q-p) within eps of segment ab (no sampling)."""
    dot = lambda x, y: sum(u * v for u, v in zip(x, y))
    d = tuple(v - u for u, v in zip(p, q))
    c = tuple(v - u for u, v in zip(a, b))
    r = tuple(v - u for u, v in zip(a, p))

    def interval(x, v):
        aa, bb, cc = dot(v, v), dot(x, v), dot(x, x) - eps * eps
        if aa <= 1e-30:
            return (0.0, 1.0) if cc <= 0 else None
        disc = bb * bb - aa * cc
        if disc < 0:
            return None
        radius = math.sqrt(disc)
        return ((-bb - radius) / aa, (-bb + radius) / aa)

    intervals = [interval(r, d), interval(tuple(v - u for u, v in zip(b, p)), d)]
    length2 = dot(c, c)
    if length2 > 1e-30:
        u0, u1 = dot(r, c) / length2, dot(d, c) / length2
        x = tuple(v - u0 * w for v, w in zip(r, c))
        v = tuple(v - u1 * w for v, w in zip(d, c))
        inside = interval(x, v)
        if abs(u1) <= 1e-30:
            projection = (0.0, 1.0) if 0 <= u0 <= 1 else None
        else:
            projection = tuple(sorted((-u0 / u1, (1 - u0) / u1)))
        if inside and projection:
            intervals.append((max(inside[0], projection[0]), min(inside[1], projection[1])))
    return [(max(0.0, lo), min(1.0, hi)) for item in intervals if item
            for lo, hi in [item] if max(0.0, lo) <= min(1.0, hi)]


def _edge_covered(p, q, segments, eps):
    intervals = sorted(i for a, b in segments for i in _capsule_intervals(p, q, a, b, eps))
    end = 0.0
    for lo, hi in intervals:
        if lo > end + 1e-12:
            return False
        end = max(end, hi)
        if end >= 1.0:
            return True
    return False


def _closure_check(verts, tris, eps, brep_area=None, brep_closed=False):
    """Exact edge pairing, or closed BREP + 1% area + full seam coverage."""
    if not len(verts) or not len(tris) or not np.isfinite(verts).all():
        return False, "exact", {"note": "empty or non-finite mesh"}
    wv, wt = _weld(verts, tris)
    uniq, counts = _edge_counts(wt)
    nbad = int((counts != 2).sum())
    if (counts > 2).any():
        return False, "exact", {"note": "non-manifold edges", "boundary_edges": nbad}
    if nbad == 0:
        directed = np.concatenate([wt[:, [0, 1]], wt[:, [1, 2]], wt[:, [2, 0]]])
        _, inverse = np.unique(np.sort(directed, axis=1), axis=0, return_inverse=True)
        winding = np.bincount(inverse, weights=np.where(directed[:, 0] < directed[:, 1], 1, -1))
        if np.any(winding != 0):
            return False, "exact", {"note": "inconsistent triangle winding"}
        return True, "exact", {}
    if not eps or not brep_closed or not brep_area:
        return False, "exact", {"boundary_edges": nbad}
    area_ratio = _mesh_area(wv, wt) / brep_area
    if not (0.99 <= area_ratio <= 1.01):
        return False, "brep-closed+area", {"boundary_edges": nbad, "area_ratio": round(area_ratio, 4)}
    if nbad > 0.02 * len(uniq):
        return False, "brep-closed+area", {"boundary_edges": nbad, "area_ratio": round(area_ratio, 4),
                                           "note": "boundary fraction > 2%"}
    # Minimum segment distance was zero for adjacent edges of any hole. Prove
    # coverage of the ENTIRE edge by other boundary-edge epsilon capsules.
    from collections import defaultdict
    sample = uniq[:min(len(uniq), 500)]
    med = float(np.median(np.linalg.norm(wv[sample[:, 1]] - wv[sample[:, 0]], axis=1)))
    cell = max(eps, med)
    bad_edges = uniq[counts != 2]
    grid = defaultdict(set)

    def cells(lo, hi):
        low, high = np.floor(lo / cell).astype(int), np.floor(hi / cell).astype(int)
        for x in range(low[0], high[0] + 1):
            for y in range(low[1], high[1] + 1):
                for z in range(low[2], high[2] + 1):
                    yield x, y, z

    for i, (a, b) in enumerate(bad_edges):
        for key in cells(np.minimum(wv[a], wv[b]), np.maximum(wv[a], wv[b])):
            grid[key].add(i)
    for i, (u, v) in enumerate(bad_edges):
        candidates = set()
        for key in cells(np.minimum(wv[u], wv[v]) - eps, np.maximum(wv[u], wv[v]) + eps):
            candidates.update(grid.get(key, ()))
        segments = [(wv[a], wv[b]) for j in candidates if j != i for a, b in [bad_edges[j]]]
        if not _edge_covered(wv[u], wv[v], segments, eps):
            return False, "brep-closed+area", {"boundary_edges": nbad, "area_ratio": round(area_ratio, 4),
                                               "note": "uncovered boundary edge"}
    return True, "brep-closed+area", {"boundary_edges": nbad, "area_ratio": round(area_ratio, 4),
                                      "max_gap_mm": eps}


def _mesh_volume(verts, tris):
    # Translation removes cancellation on geometry far from the world origin.
    shifted = verts - verts.mean(axis=0) if len(verts) else verts
    a, b, c = shifted[tris[:, 0]], shifted[tris[:, 1]], shifted[tris[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _b64(arr):
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode()


def _pack_solid(wv, wt):
    """Positions quantized to uint16 per solid (offset + scale): step size is
    extent/65535 (~0.0004 mm on a 28 mm part), far below tessellation error;
    gates always run on the unquantized arrays. (uint16, not int16: the full
    0..65535 range is used — int16 silently wrapped the upper half of every
    axis and folded the geometry; caught by the IFRIT step screenshots,
    2026-10-24, and now covered by the payload-roundtrip gate check.)"""
    if len(wv) == 0:
        return {"positions": "", "indices": "", "itype": "u16", "qoffset": [0, 0, 0], "qscale": 1.0}
    _validate_mesh(wv, wt)
    lo = wv.min(axis=0)
    ext = float((wv.max(axis=0) - lo).max()) or 1.0
    scale = ext / 65535.0
    q = np.round((wv - lo) / scale).astype("<u2")
    u16 = len(wv) <= 65535
    return {"positions": _b64(q),
            "indices": _b64(wt.astype("<u2" if u16 else "<u4")),
            "itype": "u16" if u16 else "u32",
            "qoffset": [float(x) for x in lo], "qscale": scale}


def _scale_shape(shape, k):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf
    import cadquery as cq
    tr = gp_Trsf()
    tr.SetScale(gp_Pnt(0, 0, 0), k)
    # Uniform scale is a gp_Trsf, not a general transform: GTransform may
    # invalidate analytic faces (e.g. the demo's cylindrical holes).
    return cq.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped, tr, True).Shape())


def _brep_bounds(shape):
    # CadQuery's default BoundingBox may use attached triangulation and inherit
    # its deflection padding. Nominal truth/inspector bounds must use the BREP.
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    import cadquery as cq
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
    return cq.BoundBox(box)


def _workbench_shapes(parts, transforms):
    unknown = set(transforms) - set(parts)
    if unknown:
        raise ValueError(f"workbench transforms identify unknown parts: {', '.join(sorted(unknown))}")
    result = {}
    for name, shape in parts.items():
        values = transforms.get(name, {})
        scale = values.get("scale", 1)
        if scale != 1:
            shape = _scale_shape(shape, scale)
        for axis, angle in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), values.get("rotation", [0, 0, 0])):
            if angle:
                shape = shape.rotate((0, 0, 0), axis, angle)
        translation = values.get("translation", [0, 0, 0])
        if any(translation):
            shape = shape.translate(tuple(translation))
        result[name] = shape
    return result


# ---------- projects ----------
def _component(value, label):
    # Components become filenames and URL segments, never paths or query strings.
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 100 or
            value in (".", "..") or value[-1] in " ." or
            any(ord(c) < 32 or c in '/\\\\:?#%<>|\"' for c in value)):
        raise ValueError(f"invalid {label}: expected a safe filename component")
    return value


def _inside(root, path):
    try:
        return os.path.commonpath((os.path.realpath(root), os.path.realpath(path))) == os.path.realpath(root)
    except ValueError:
        return False


def _input_inside(root, path):
    # Snapshot keys retain lexical ownership after deletion or symlink retargeting.
    # This is only change routing; source/HTTP access still uses realpath _inside.
    try:
        return os.path.commonpath((os.path.abspath(root), os.path.abspath(path))) == os.path.abspath(root)
    except ValueError:
        return False


def _source_path(root, filename):
    if not isinstance(filename, str) or not filename.endswith(".py"):
        raise ValueError("entry/poses must name a Python source file")
    path = os.path.realpath(os.path.join(root, filename))
    if not _inside(root, path):
        raise ValueError("entry/poses must stay inside the project directory")
    if not os.path.isfile(path):
        raise ValueError(f"entry/poses source does not exist: {filename}")
    return path


def _finite_number(value):
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def _number(value, label, positive=False):
    if not _finite_number(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"invalid {label}")
    return value


def _vector(value, label):
    if (not isinstance(value, (list, tuple)) or len(value) != 3 or
            any(not _finite_number(x) for x in value)):
        raise ValueError(f"invalid {label}: expected three finite numbers")


def _read_manifest(root, expected_name=None):
    with open(os.path.join(root, "viewer.json"), encoding="utf-8") as fh:
        m = json.load(fh)
    if not isinstance(m, dict):
        raise ValueError("viewer.json must be an object")
    json.dumps(m, allow_nan=False)  # Unknown metadata must also be valid JSON on the wire.
    for key in ("name", "entry"):
        if key not in m:
            raise ValueError(f"viewer.json is missing required key '{key}'")
    name = _component(m["name"], "project name")
    if expected_name is not None and name != expected_name:
        raise ValueError("renaming a running project requires a worker restart")
    if not isinstance(m["entry"], str):
        raise ValueError("entry must be a file:function string")
    entry, _, fn = m["entry"].partition(":")
    _source_path(root, entry)
    if fn and not fn.isidentifier():
        raise ValueError("entry function must be a Python identifier")
    if m.get("poses"):
        _source_path(root, m["poses"])
    for key in ("live", "on_demand"):
        if key in m and not isinstance(m[key], bool):
            raise ValueError(f"{key} must be boolean")
    _number(m.get("unit_scale", 1.0), "unit_scale", positive=True)
    _number(m.get("weld_epsilon", 0.0), "weld_epsilon")
    for key in ("materials", "explode", "tessellation"):
        if not isinstance(m.get(key, {}), dict):
            raise ValueError(f"{key} must be an object")
    for values in m.get("materials", {}).values():
        if not isinstance(values, dict):
            raise ValueError("each material must be an object")
        if any(k in values and not isinstance(values[k], str) for k in ("material", "finish", "xray")):
            raise ValueError("material/finish/xray must be strings")
    for vec in m.get("explode", {}).values():
        _vector(vec, "explode vector")
    for values in m.get("tessellation", {}).values():
        if not isinstance(values, (list, tuple)) or len(values) != 2:
            raise ValueError("tessellation settings must be [linear, angular]")
        for x in values:
            _number(x, "tessellation tolerance", positive=True)
    for key in ("steps", "chips"):
        if key in m and not isinstance(m[key], list):
            raise ValueError(f"{key} must be an array")
    for step in m.get("steps", []):
        if not isinstance(step, dict):
            raise ValueError("each step must be an object")
        if "camera" in step:
            camera = step["camera"]
            if not isinstance(camera, dict):
                raise ValueError("step camera must be an object")
            for key in ("position", "target"):
                _vector(camera.get(key), "step camera " + key)
    workbench.validate_transforms(m)
    return m


class StaleBuild(Exception):
    pass


class Project:
    def __init__(self, root):
        self.root = os.path.realpath(os.path.expanduser(root))
        self.manifest = _read_manifest(self.root)
        self.manifest_error = None
        self.name = self.manifest["name"]
        self.gen = 0
        self.state = "idle"
        self.preview_dirty = False
        self.certified_due = False
        self.lock = threading.RLock()
        self.hashes = {}
        self.gates = None
        self.truth = None
        self.certified = None  # Only a passed certified publication updates this BREP record.
        self.workbench_conflict = workbench.load_conflict(self.root)
        if self.workbench_conflict:
            self.state = "conflict"
        self.history = []
        self.last_change = 0.0
        self._mod_counter = 0

    def log(self, event, detail=""):
        with self.lock:
            self.history.append({"t": time.time(), "event": redact_sensitive(event), "detail": redact_sensitive(detail)})
            self.history = self.history[-20:]

    def load_module(self, filename):
        self._mod_counter += 1
        path = _source_path(self.root, filename)
        spec = importlib.util.spec_from_file_location(
            f"cq_{self.name}_{self._mod_counter}", path)
        mod = importlib.util.module_from_spec(spec)
        # Read source, not timestamp-based pyc: rapid same-sized saves must build anew.
        with open(path, "rb") as fh:
            code = compile(fh.read(), path, "exec")
        sys.modules[spec.name] = mod  # dataclasses and other decorators require this.
        try:
            exec(code, mod.__dict__)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        return mod

    # ----- prepare one full build at one tier; publication is in run_build -----
    def build(self, tier):
        if tier not in ("preview", "certified"):
            raise ValueError("unknown build tier")
        with self.lock:
            try:
                latest = _read_manifest(self.root, expected_name=self.name)
            except Exception as e:
                self.manifest_error = f"viewer.json: {type(e).__name__}: {e}"
                raise ValueError(self.manifest_error) from e
            if latest != self.manifest:
                self.manifest = latest
                self.gen += 1
                broadcast({"type": "manifest", "project": self.name})
            self.manifest_error = None
            gen, manifest = self.gen, copy.deepcopy(self.manifest)
        sources = snapshot([self.root])
        t0 = time.time()
        entry_file, _, fn_name = manifest["entry"].partition(":")
        entry = self.load_module(entry_file)
        result = getattr(entry, fn_name or "build")()
        scale = float(manifest.get("unit_scale", 1.0))
        if not isinstance(result, dict) or not isinstance(result.get("parts"), dict):
            raise ValueError("build() must return {names, parts}")
        names = result.get("names")
        if (not isinstance(names, (list, tuple)) or not names or
                any(not isinstance(n, str) or not n for n in names) or
                len(set(names)) != len(names) or set(names) != set(result["parts"])):
            raise ValueError("build names must uniquely identify every returned part")

        # Source BREP is the nominal-mm gate reference. unit_scale changes the
        # mesh, not this truth; a deliberate scale mismatch must fail closed.
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps

        def _solid_facts(s):
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(s.wrapped, props)
            volume, area = s.Volume(), props.Mass()
            if not s.isValid() or not math.isfinite(volume) or volume <= 0 or not math.isfinite(area) or area <= 0:
                raise ValueError("source must contain valid positive-volume solids")
            return {"area": area, "closed": True}

        transforms = manifest.get("workbench", {}).get("transforms", {})
        neutral_parts = _workbench_shapes(result["parts"], transforms)
        truth = {}
        for n in names:
            shp = neutral_parts[n]
            solids = shp.Solids()
            if not solids or not shp.isValid():
                raise ValueError(f"part {n} has no valid solids")
            bb = _brep_bounds(shp)
            facts = [_solid_facts(s) for s in solids]
            solid_bounds = [_brep_bounds(s) for s in solids]
            truth[n] = {"volume": shp.Volume(), "solids": len(solids),
                        "center": [shp.Center().x, shp.Center().y, shp.Center().z],
                        "bbox_min_dim_per_solid": [min(b.xlen, b.ylen, b.zlen) for b in solid_bounds],
                        "solid_areas": [f["area"] for f in facts],
                        "solid_closed": [f["closed"] for f in facts],
                        "bbox": [bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax]}
        # variants: neutral + poses
        variants_src = {"neutral": neutral_parts}
        if manifest.get("poses"):
            poses = self.load_module(manifest["poses"])
            if not isinstance(poses.POSES, dict):
                raise ValueError("POSES must be an object of named callables")
            for pname, fn in poses.POSES.items():
                _component(pname, "pose name")
                if pname == "neutral":
                    raise ValueError("poses cannot replace neutral")
                vparts = fn(result)
                if not isinstance(vparts, dict) or set(vparts) != set(names):
                    raise ValueError(f"pose {pname} must return all named parts")
                variants_src[pname] = _workbench_shapes(vparts, transforms)

        tess = manifest.get("tessellation", {})
        tol, ang = tess.get("coarse" if tier == "preview" else "fine",
                            COARSE if tier == "preview" else FINE)
        weld_eps = float(manifest.get("weld_epsilon", 0.0))
        closure_methods = []
        variants_out, gate_checks, diag = {}, [], []
        shape_variants, variant_truth = {}, {}
        variant_tris = {}
        truth_extent = None
        for vi, (vname, vparts) in enumerate(variants_src.items()):
            parts_out = []
            shape_variants[vname], variant_truth[vname] = {}, {}
            mesh_extent = [1e18, 1e18, 1e18, -1e18, -1e18, -1e18]
            for n in names:
                shp = vparts[n]
                if scale != 1.0:
                    shp = _scale_shape(shp, scale)
                shape_variants[vname][n] = shp
                mat = manifest.get("materials", {}).get(n, {})
                fin = FINISH.get((mat.get("material"), mat.get("finish")), FINISH_DEFAULT)
                solids_out, part_tris = [], 0
                part_mesh_vol = 0.0
                part_area = 0.0
                solids = shp.Solids()
                if not solids or not shp.isValid():
                    raise ValueError(f"pose {vname}/{n} has no valid solids")
                for si, solid in enumerate(solids):
                    # relative deflection cap: a solid is never tessellated
                    # coarser than 5% of its smallest dimension (small parts
                    # keep fine absolute detail under coarse project settings)
                    sb = _brep_bounds(solid)
                    stol = min(tol, 0.05 * min(sb.xlen, sb.ylen, sb.zlen))
                    # small chunky solids (beads) are all curvature: their
                    # volume error is set by the angular tolerance alone
                    # (sphere deficit ~ 3a^2/8), so they get a finer angle
                    sang = ang * 0.35 if max(sb.xlen, sb.ylen, sb.zlen) <= 4.0 else ang
                    verts, tris, missing = _tessellate_solid(solid, stol, sang)
                    _validate_mesh(verts, tris)
                    wv, wt = _weld(verts, tris)
                    _validate_mesh(wv, wt)
                    part_tris += len(wt)
                    vol = abs(_mesh_volume(wv, wt))
                    part_mesh_vol += vol
                    if tier == "certified":
                        if missing:
                            diag.append(f"{vname}/{n}/solid{si}: {missing} face(s) without triangulation")
                        facts = _solid_facts(solid)
                        part_area += facts["area"]
                        ok, method, det = _closure_check(
                            verts, tris, weld_eps,
                            brep_area=facts["area"], brep_closed=facts["closed"])
                        closure_methods.append(method if ok else f"FAILED {det}")
                        if not ok:
                            diag.append(f"{vname}/{n}/solid{si}: mesh not watertight ({method}, {det})")
                        if vname == "neutral":
                            bd = truth[n]["bbox_min_dim_per_solid"][si]
                            md = min(wv.max(axis=0) - wv.min(axis=0)) if len(wv) else 0.0
                            if abs(md - bd) > 0.02:
                                diag.append(f"neutral/{n}/solid{si}: thin extent mesh {md:.3f} vs BREP {bd:.3f} mm")
                    packed = _pack_solid(wv, wt)
                    if tier == "certified" and len(wv):
                        # payload roundtrip: the packed bytes must decode
                        # back to the gated mesh (catches encoding bugs the
                        # geometry gates cannot see — they run pre-pack)
                        q = np.frombuffer(base64.b64decode(packed["positions"], validate=True),
                                          dtype="<u2").astype(np.float64)
                        dec = (q.reshape(-1, 3) * packed["qscale"] + np.array(packed["qoffset"])).astype(np.float32).astype(np.float64)
                        err = float(np.abs(dec - wv).max()) if len(dec) == len(wv) else 1e9
                        decoded_indices = np.frombuffer(base64.b64decode(packed["indices"], validate=True),
                                                        dtype="<u2" if packed["itype"] == "u16" else "<u4")
                        if (not math.isfinite(err) or err > 2 * packed["qscale"] or
                                not np.array_equal(decoded_indices, wt.ravel())):
                            diag.append(f"{vname}/{n}/solid{si}: payload roundtrip error {err:.4f} mm or index mismatch")
                    solids_out.append(packed)
                    if len(wv):
                        lo, hi = wv.min(axis=0), wv.max(axis=0)
                        for k in range(3):
                            mesh_extent[k] = min(mesh_extent[k], lo[k])
                            mesh_extent[3 + k] = max(mesh_extent[3 + k], hi[k])
                if tier == "certified":
                    bb = _brep_bounds(shp)
                    center = shp.Center()
                    variant_truth[vname][n] = {"volume": shp.Volume(), "area": part_area, "solids": len(solids),
                                              "center": [center.x, center.y, center.z],
                                              "bbox": [bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax],
                                              "dimensions": [bb.xlen, bb.ylen, bb.zlen], "units": "mm"}
                    ref = truth[n]["volume"]
                    dv = abs(part_mesh_vol - ref) / ref * 100 if ref else 0.0
                    if dv > 1.0:
                        diag.append(f"{vname}/{n}: mesh volume off BREP by {dv:.2f}%")
                variant_tris[vname] = variant_tris.get(vname, 0) + part_tris
                parts_out.append({
                    "name": n, "finish": fin, "xray": mat.get("xray", "opaque"),
                    "explode": manifest.get("explode", {}).get(n, [0, 0, 0]),
                    "center": [shp.Center().x, shp.Center().y, shp.Center().z], "solids": solids_out})
            ext = [mesh_extent[3 + k] - mesh_extent[k] for k in range(3)]
            if vname == "neutral":
                tt = [truth[n]["bbox"] for n in names]
                truth_extent = [max(b[3 + k] for b in tt) - min(b[k] for b in tt)
                                for k in range(3)]
                if tier == "certified":
                    for k in range(3):
                        dv = abs(ext[k] - truth_extent[k]) / truth_extent[k] * 100
                        if dv > 1.0:
                            diag.append(f"units: axis {k} mesh extent {ext[k]:.2f} vs BREP {truth_extent[k]:.2f} mm ({dv:.1f}% off)")
            c = [(mesh_extent[k] + mesh_extent[3 + k]) / 2 for k in range(3)]
            variants_out[vname] = {
                "bounds": {"min": mesh_extent[:3], "max": mesh_extent[3:],
                           "center": c, "radius": float(np.linalg.norm(ext) / 2)},
                "parts": parts_out}

        # Per-variant payload files (the pilot's one-file-per-state shape):
        # budgets apply per variant file. The index payload carries variant
        # hashes + gates; its hash is over the variant geometry hashes, so an
        # unchanged model keeps its hash across rebuilds.
        variant_files = {}
        for vname, vdata in variants_out.items():
            vraw = json.dumps(vdata, separators=(",", ":"), allow_nan=False).encode()
            vh = hashlib.sha256(vraw).hexdigest()[:16]
            variant_files[vname] = {"hash": vh, "raw": vraw,
                                    "url": "/payload/" + quote(f"{self.name}-{tier}-{vname}-{vh}.json"),
                                    "tris": variant_tris.get(vname, 0),
                                    "bytes": len(vraw)}
        h = hashlib.sha256("".join(variant_files[v]["hash"] for v in sorted(variant_files)).encode()).hexdigest()[:16]
        worst_tris = max(v["tris"] for v in variant_files.values())
        worst_bytes = max(v["bytes"] for v in variant_files.values())

        gates = None
        if tier == "certified":
            checks = [
                {"id": "units", "status": "fail" if any(d.startswith("units:") for d in diag) else "pass"},
                {"id": "watertight", "status": "fail" if any("watertight" in d for d in diag) else "pass"},
                {"id": "volume", "status": "fail" if any("mesh volume" in d for d in diag) else "pass"},
                {"id": "thin-extent", "status": "fail" if any("thin extent" in d for d in diag) else "pass"},
                {"id": "triangulation", "status": "fail" if any("triangulation" in d for d in diag) else "pass"},
                {"id": "payload-roundtrip", "status": "fail" if any("roundtrip" in d for d in diag) else "pass"},
                {"id": "budget-tris", "status": "pass" if worst_tris <= TRI_BUDGET else "fail",
                 "value": worst_tris, "budget": TRI_BUDGET, "scope": "per variant"},
                {"id": "budget-bytes", "status": "pass" if worst_bytes <= BYTE_BUDGET else "fail",
                 "value": worst_bytes, "budget": BYTE_BUDGET, "scope": "per variant"}]
            gates = {"pass": all(c["status"] == "pass" for c in checks),
                     "checks": checks, "diagnostics": diag,
                     "closure_methods": sorted(set(closure_methods)),
                     "weld_epsilon": weld_eps}

        failed = tier == "certified" and gates is not None and not gates["pass"]
        index = {"project": self.name, "tier": tier, "built_at": time.time(),
                 "gen": gen, "hash": h, "gates": gates,
                 "variants": {v: {k: f[k] for k in ("hash", "url", "tris", "bytes")}
                              for v, f in variant_files.items()}}

        return h, {"seconds": round(time.time() - t0, 2), "tris": worst_tris,
                   "bytes": worst_bytes, "gates": gates, "index": index,
                   "variant_files": variant_files, "truth": truth, "sources": sources, "manifest": manifest,
                   "shape_variants": shape_variants, "variant_truth": variant_truth}

    def publish(self, tier, summary):
        # Caller holds lock through publication, state update and announcement.
        index = summary["index"]
        def current():
            return (self.gen == index["gen"] and not self.manifest_error and
                    _read_manifest(self.root, expected_name=self.name) == summary["manifest"] and
                    snapshot([self.root]) == summary["sources"])
        if not current():
            raise StaleBuild("source changed while building")
        failed = tier == "certified" and not summary["gates"]["pass"]
        if not failed:
            for f in summary["variant_files"].values():
                fname = unquote(f["url"].rsplit("/", 1)[-1])
                _atomic_write(os.path.join(PAYLOAD_DIR, fname), f["raw"])
        if not current():
            raise StaleBuild("source changed while writing payloads")
        fname = f"{self.name}-certified-failed.json" if failed else f"{self.name}-{tier}.json"
        def check_commit():
            if not current():
                raise StaleBuild("source changed before index commit")
        _atomic_write(os.path.join(PAYLOAD_DIR, fname),
                      json.dumps(index, separators=(",", ":"), allow_nan=False).encode(),
                      before_replace=check_commit)
        if not failed:
            self.truth = summary["truth"]
            if tier == "certified" and "shape_variants" in summary:
                source_fingerprint = hashlib.sha256(json.dumps(
                    {"manifest": summary["manifest"], "sources": summary["sources"]},
                    sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                self.certified = {"project": self.name, "root": self.root, "hash": index["hash"],
                                  "built_at": index["built_at"], "gen": index["gen"], "units": "mm",
                                  "mesh_unit_scale": float(summary["manifest"].get("unit_scale", 1)),
                                  "manifest": copy.deepcopy(summary["manifest"]), "sources": summary["sources"].copy(),
                                  "source_fingerprint": source_fingerprint, "truth": copy.deepcopy(summary["truth"]),
                                  "variants": {v: parts.copy() for v, parts in summary["shape_variants"].items()},
                                  "variant_truth": copy.deepcopy(summary["variant_truth"]),
                                  "gates": copy.deepcopy(summary["gates"]),
                                  "source_files": [{"path": os.path.relpath(path, self.root).replace(os.sep, "/"),
                                                    "bytes": facts[2], "sha256": facts[-1]}
                                                   for path, facts in sorted(summary["sources"].items())]}

    def status(self):
        with self.lock:
            return redact_sensitive({"name": self.name, "root": self.root, "state": self.state,
                                     "live": self.manifest.get("live", False),
                                     "on_demand": self.manifest.get("on_demand", False),
                                     "hashes": self.hashes.copy(), "gates_pass": (self.gates or {}).get("pass"),
                                     "history": copy.deepcopy(self.history[-8:])})


PROJECTS = {}
PROJECTS_LOCK = threading.RLock()
GEOMETRY_LOCK = threading.RLock()  # OCP units/settings and all project geometry share one lane.
LOAD_ERRORS = []


def project_values():
    with PROJECTS_LOCK:
        return tuple(PROJECTS.values())


def get_project(name):
    with PROJECTS_LOCK:
        return PROJECTS.get(name)


def check_project_name(name, root):
    with PROJECTS_LOCK:
        for project in PROJECTS.values():
            if project.name.casefold() == name.casefold() and project.root != os.path.realpath(root):
                raise workbench.WorkbenchError(409, "PROJECT_NAME_CONFLICT", "A different registered project has this name")


def register_project(root):
    project = Project(root)
    with PROJECTS_LOCK:
        for existing in PROJECTS.values():
            if existing.root == project.root:
                if existing.name != project.name:
                    raise workbench.WorkbenchError(409, "PROJECT_RENAMED", "Renaming a running project requires restart")
                return existing
        check_project_name(project.name, project.root)
        PROJECTS[project.name] = project
    with project.lock:
        project.preview_dirty = bool(project.manifest.get("live")) and not project.workbench_conflict
        project.certified_due = not bool(project.workbench_conflict)
    broadcast({"type": "projects", "projects": [p.status() for p in project_values()]})
    return project


def resource_read_only(path):
    resolved = os.path.realpath(path)
    roots = [os.path.join(HERE, "testpart")]
    if getattr(sys, "frozen", False):
        roots.append(HERE)
    for base in (HERE, os.path.abspath(sys.executable)):
        parent = base
        while parent != os.path.dirname(parent):
            if parent.lower().endswith(".app"):
                roots.append(parent)
                break
            parent = os.path.dirname(parent)
    return any(_inside(root, resolved) for root in roots)


@contextmanager
def geometry_operation(timeout=30):
    if not GEOMETRY_LOCK.acquire(timeout=timeout):
        raise workbench.WorkbenchError(409, "GEOMETRY_BUSY", "Another geometry operation is still running")
    try:
        yield
    finally:
        GEOMETRY_LOCK.release()


def certificate_current(project):
    with project.lock:
        record = project.certified
        if (record is None or project.workbench_conflict or project.state != "certified" or project.preview_dirty or project.certified_due
                or project.manifest_error or project.gen != record["gen"]):
            return False
        try:
            return (_read_manifest(project.root, expected_name=project.name) == record["manifest"]
                    and snapshot([project.root]) == record["sources"])
        except (OSError, ValueError, StaleBuild):
            return False


@contextmanager
def certified_record(name):
    project = get_project(name)
    if project is None:
        raise workbench.WorkbenchError(404, "PROJECT_NOT_FOUND", "Project is not registered")
    with geometry_operation():
        with project.lock:
            if not certificate_current(project):
                raise workbench.WorkbenchError(409, "STALE_CERTIFICATE", "Export requires current certified source; rebuild first")
            captured = project.certified
            record = {**captured, "manifest": copy.deepcopy(captured["manifest"]), "truth": copy.deepcopy(captured["truth"]),
                      "sources": captured["sources"].copy(), "gates": copy.deepcopy(captured["gates"]),
                      "variants": {variant: parts.copy() for variant, parts in captured["variants"].items()}}
        def assert_current():
            with project.lock:
                if project.certified is not captured or not certificate_current(project):
                    raise workbench.WorkbenchError(409, "STALE_CERTIFICATE", "Source changed during export; rebuild first")
        record["assert_current"] = assert_current
        yield record


def changed_project(project, path, auto_build=None, force=False):
    with project.lock:
        if auto_build is not None:
            project.workbench_auto_build = auto_build
            if not auto_build:
                project.preview_dirty = project.certified_due = False
    _apply_changes([path])
    with project.lock:
        if force or getattr(project, "workbench_auto_build", True):
            if project.manifest.get("live"):
                project.preview_dirty = True
            else:
                project.certified_due = True


WS_CLIENTS = set()
WS_LOOP = None
SHOTS_PENDING = {}
SHOTS_LOCK = threading.Lock()


@contextmanager
def _project_imports(root):
    class SourceLoader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            return compile(self.get_data(self.path), self.path, "exec")

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py") and _inside(root, spec.origin):
                spec.loader = SourceLoader(fullname, spec.origin)
                return spec
            return None

    def purge():
        for name, mod in list(sys.modules.items()):
            path = getattr(mod, "__file__", None)
            if isinstance(path, str) and _inside(root, path):
                sys.modules.pop(name, None)

    finder = Finder()
    purge()
    importlib.invalidate_caches()
    sys.path.insert(0, root)
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.path.remove(root)
        purge()


def broadcast(msg):
    # Only the event-loop thread touches clients or sends; futures observe errors.
    loop = WS_LOOP
    if loop is None or loop.is_closed() or not loop.is_running():
        return
    raw = json.dumps(redact_sensitive(msg), allow_nan=False)
    async def send_all():
        for ws in list(WS_CLIENTS):
            try:
                await asyncio.wait_for(ws.send(raw), timeout=5)
            except Exception:
                WS_CLIENTS.discard(ws)
    future = asyncio.run_coroutine_threadsafe(send_all(), loop)
    def done(f):
        if not f.cancelled() and f.exception() is not None:
            print(f"CassQuery websocket send failed: {f.exception()}", file=sys.stderr)
    future.add_done_callback(done)


def set_state(p, state, detail=""):
    state, detail = redact_sensitive(state), redact_sensitive(detail)
    with p.lock:
        p.state = state
        p.log(state, detail)
        broadcast({"type": "status", "project": p.name, "state": state, "detail": detail,
                   "hashes": p.hashes.copy(), "gates_pass": (p.gates or {}).get("pass")})


def run_build(p, tier):
    with p.lock:
        if p.workbench_conflict:
            set_state(p, "conflict", "Resolve the retained source conflict before building")
            return False
    set_state(p, "building-" + tier)
    with GEOMETRY_LOCK:
        with p.lock:
            if p.workbench_conflict:
                set_state(p, "conflict", "Resolve the retained source conflict before building")
                return False
        return _run_build(p, tier)


def _run_build(p, tier):
    try:
        with _project_imports(p.root):
            h, summary = p.build(tier)
        with p.lock:
            p.publish(tier, summary)
            if tier == "certified":
                p.gates = summary["gates"]
                if not p.gates["pass"]:
                    p.hashes["certified_failed"] = h
                    set_state(p, "gate-failed", "; ".join(p.gates["diagnostics"][:4]))
                    broadcast({"type": "gate_failed", "project": p.name, "hash": h,
                               "gates": p.gates,
                               "url": "/payload/" + quote(f"{p.name}-certified-failed.json")})
                    return False
            p.hashes[tier] = h
            set_state(p, tier, f"{summary['seconds']}s, {summary['tris']} tris")
            broadcast({"type": "announce", "project": p.name, "tier": tier, "hash": h,
                       "url": "/payload/" + quote(f"{p.name}-{tier}.json"),
                       "gates_pass": (summary["gates"] or {}).get("pass")})
            return True
    except StaleBuild as e:
        with p.lock:
            if p.workbench_conflict:
                p.preview_dirty = p.certified_due = False
                set_state(p, "conflict", "Resolve the retained source conflict before building")
            else:
                if getattr(p, "workbench_auto_build", True):
                    if p.manifest.get("live"):
                        p.preview_dirty = True
                    else:
                        p.certified_due = True
                set_state(p, "stale", str(e))
        return False
    except BaseException:
        # Project entry code may raise SystemExit/KeyboardInterrupt; neither may
        # silently terminate the sole build thread. Ctrl-C in main still exits.
        tb = redact_sensitive(traceback.format_exc())
        last = tb.strip().splitlines()[-1] if tb.strip() else "build failed"
        own = [f for f in traceback.extract_tb(sys.exc_info()[2]) if _inside(p.root, f.filename)]
        detail = (f"{os.path.basename(own[-1].filename)}:{own[-1].lineno}: {last}"
                  if own else last)
        set_state(p, "error", detail)
        p.log("build-failed", tb[-1500:])
        broadcast({"type": "build_failed", "project": p.name, "tier": tier,
                   "error": last, "detail": detail})
        return False


def _build_pending(p):
    worked = False
    with p.lock:
        if p.workbench_conflict:
            p.preview_dirty = p.certified_due = False
            return False
        preview, gen = p.preview_dirty, p.gen
        p.preview_dirty = False
    if preview:
        ok = run_build(p, "preview")
        with p.lock:
            if ok and p.manifest.get("live") and p.gen == gen:
                p.certified_due = True
        worked = True
    with p.lock:
        # Do not certify while a newer preview is pending.
        certified = p.certified_due and not p.preview_dirty
        if certified:
            p.certified_due = False
    if certified:
        run_build(p, "certified")
        worked = True
    return worked


def build_thread():
    while True:
        worked = False
        for p in project_values():
            worked = _build_pending(p) or worked
        if not worked:
            time.sleep(0.3)


def snapshot(roots):
    """Fingerprint regular project inputs, including CAD/data files, by content.

    Dependency/cache trees, directory symlinks and worker-owned payload/shot
    outputs aren't inputs. Build-generated files must live outside the input
    tree. Trusted project code may read external files, but this filesystem
    boundary doesn't watch those files and isn't a sandbox.
    """
    snap = {}
    outputs = (os.path.realpath(PAYLOAD_DIR), os.path.realpath(SHOT_DIR))
    def unreadable(error):
        raise error
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=unreadable):
            if any(_inside(output, dirpath) for output in outputs):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in
                           (".git", ".venv", "venv", "node_modules", "__pycache__")]
            for f in filenames:
                if f.startswith(".cq-workbench-"):
                    continue  # Owned sibling save staging is not a project input.
                fp = os.path.join(dirpath, f)
                if any(_inside(output, fp) for output in outputs):
                    continue  # An in-tree alias of a worker output is still output.
                if not _inside(root, fp):
                    continue  # Linked external dependencies aren't project-tree inputs.
                try:
                    if not stat.S_ISREG(os.stat(fp).st_mode):
                        continue  # Never read devices, sockets or FIFOs as source.
                    with open(fp, "rb") as fh:
                        before = os.fstat(fh.fileno())
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            digest.update(chunk)
                        after = os.fstat(fh.fileno())
                    metadata = lambda st: (st.st_mtime_ns, st.st_ctime_ns, st.st_size)
                    if metadata(before) != metadata(after):
                        raise StaleBuild("project input changed while snapshotting")
                    snap[fp] = (*metadata(after), digest.hexdigest())
                except FileNotFoundError:
                    continue  # An atomic save may remove a file mid-snapshot.
    return snap


def _apply_changes(changed):
    for p in project_values():
        mine = [fp for fp in changed if _input_inside(p.root, fp)]
        if not mine:
            continue
        with p.lock:
            # A polling watcher may recognize an edit only after an explicit
            # rebuild already certified those exact inputs. Do not invalidate
            # that current certificate for the watcher's obsolete baseline.
            if certificate_current(p):
                continue
            p.gen += 1
            p.last_change = time.time()
            if p.workbench_conflict:
                p.preview_dirty = p.certified_due = False
                set_state(p, "conflict", "Resolve the retained source conflict before building")
                continue
            if os.path.join(p.root, "viewer.json") in mine:
                try:
                    p.manifest = _read_manifest(p.root, expected_name=p.name)
                    p.manifest_error = None
                    broadcast({"type": "manifest", "project": p.name})
                except Exception as e:
                    p.manifest_error = f"viewer.json: {type(e).__name__}: {e}"
                    p.preview_dirty = p.certified_due = False
                    set_state(p, "error", p.manifest_error)
                    broadcast({"type": "build_failed", "project": p.name,
                               "tier": "certified", "error": p.manifest_error,
                               "detail": p.manifest_error})
                    continue
            if p.manifest.get("live") and not p.manifest_error and getattr(p, "workbench_auto_build", True):
                p.preview_dirty = True
            set_state(p, "stale", f"changed: {os.path.basename(mine[0])}")


def watcher(roots, prev=None):
    if prev is None:
        prev = snapshot(roots)
    while True:
        time.sleep(0.5)
        try:
            dynamic_roots = list(dict.fromkeys([*roots, *(p.root for p in project_values())]))
            cur = snapshot(dynamic_roots)
            changed = [fp for fp in set(cur) | set(prev) if cur.get(fp) != prev.get(fp)]
            _apply_changes(changed)
            prev = cur
        except (OSError, StaleBuild) as e:
            # An unreadable/changing input must not silently kill the watch thread.
            print(f"CassQuery watcher: {e}", file=sys.stderr)
            for p in project_values():
                set_state(p, "error", f"watcher: {e}")


# ---------- websocket ----------
async def ws_handler(ws):
    WS_CLIENTS.add(ws)
    try:
        await ws.send(json.dumps({"type": "hello",
                                  "projects": [p.status() for p in project_values()]}))
        async for _ in ws:
            pass
    finally:
        WS_CLIENTS.discard(ws)


def _header_values(headers, name):
    try:
        return headers.get_all(name) or []
    except KeyError:
        return []


def _boundary_error(headers, port, origin_port):
    hosts = _header_values(headers, "Host")
    origins = _header_values(headers, "Origin")
    if len(hosts) != 1 or len(origins) > 1:
        return "one loopback Host and at most one Origin are required"
    try:
        host = urlsplit("//" + hosts[0])
        if (host.hostname not in ("127.0.0.1", "localhost") or host.port != port or
                host.username is not None or host.password is not None or
                host.path or host.query or host.fragment):
            return "Host must name this loopback listener"
        if origins:
            origin = urlsplit(origins[0])
            if (origin.scheme != "http" or origin.hostname not in ("127.0.0.1", "localhost") or
                    origin.port != origin_port or origin.username is not None or
                    origin.password is not None or origin.path or origin.query or origin.fragment):
                return "Origin must name this worker's HTTP viewer"
    except (ValueError, TypeError):
        return "invalid Host or Origin"
    return None


def ws_thread(sock, ready, errors, stop):
    global WS_LOOP
    async def main():
        from websockets.asyncio.server import serve
        async def check(connection, request):
            error = _boundary_error(request.headers, sock.getsockname()[1], sock.getsockname()[1] - 1000)
            return connection.respond(HTTPStatus.FORBIDDEN, error + "\n") if error else None
        async with serve(ws_handler, sock=sock, process_request=check):
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.1)
    loop = asyncio.new_event_loop()
    WS_LOOP = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except Exception as e:
        errors.append(e)
        print(f"CassQuery websocket server failed: {e}", file=sys.stderr)
    finally:
        ready.set()
        WS_LOOP = None
        sock.close()
        loop.close()


# ---------- HTTP: viewer, payloads, agent protocol ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *a):
        pass

    def _guard(self):
        error = _boundary_error(self.headers, self.server.server_port, self.server.server_port)
        if error:
            self.close_connection = True
            self._send(403, {"error": error})
            return False
        try:
            u = urlsplit(self.path)
            if u.scheme or u.netloc:
                raise ValueError("absolute request targets are not supported")
        except ValueError:
            self.close_connection = True
            self._send(400, {"error": "invalid request target"})
            return False
        return True

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(redact_sensitive(body), allow_nan=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []):
            raise ValueError("Transfer-Encoding is not supported")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise ValueError("one nonnegative Content-Length is required")
        n = int(lengths[0])
        if n > 16 * 1024 * 1024:
            raise OverflowError("request body exceeds 16 MiB")
        data = self.rfile.read(n)
        if len(data) != n:
            raise ValueError("incomplete request body")
        def bad_constant(value):
            raise ValueError(f"non-finite JSON number: {value}")
        body = json.loads(data or b"{}", parse_constant=bad_constant)
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def _project(self, name):
        if not isinstance(name, str):
            self._send(400, {"error": "project must be a string"})
            return None
        p = get_project(name)
        if not p:
            self._send(404, {"error": f"unknown project {name}"})
        return p

    def _file(self, root, rel, ctype):
        fp = os.path.realpath(os.path.join(root, rel))
        if not _inside(root, fp) or not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        try:
            with open(fp, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        return self._send(200, data, ctype)

    def do_GET(self):
        if not self._guard():
            return
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/projects":
            return self._send(200, [p.status() for p in project_values()])
        if u.path == "/api/status":
            name = (q.get("project") or [None])[0]
            if name:
                p = self._project(name)
                return p and self._send(200, p.status())
            # api_version marks this shape: v1 returned a bare list here;
            # the object (with load_errors) is version 2.
            return self._send(200, {"api_version": 2,
                                    "projects": [p.status() for p in project_values()],
                                    "load_errors": LOAD_ERRORS})
        if u.path == "/api/manifest":
            p = self._project((q.get("project") or [""])[0])
            if p:
                with p.lock:
                    manifest = copy.deepcopy(p.manifest)
                return self._send(200, manifest)
            return
        if u.path == "/api/gates":
            p = self._project((q.get("project") or [""])[0])
            if p:
                with p.lock:
                    gates = copy.deepcopy(p.gates)
                return self._send(200, gates or {"pass": None, "note": "no certified build yet"})
            return
        if u.path == "/api/parts":
            p = self._project((q.get("project") or [""])[0])
            if not p:
                return
            with p.lock:
                truth = copy.deepcopy(p.truth)
            if not truth:
                return self._send(200, {"note": "no build yet", "parts": []})
            return self._send(200, {"project": p.name, "parts": [
                {"name": n, **{k: t[k] for k in ("volume", "solids", "center", "bbox")}}
                for n, t in truth.items()]})
        path = unquote(u.path)
        if "\x00" in path:
            return self._send(404, {"error": "not found"})
        if path.startswith("/payload/"):
            rel = path[len("/payload/"):]
            if "/" in rel or "\\" in rel or not rel.endswith(".json"):
                return self._send(404, {"error": "no such payload"})
            return self._file(PAYLOAD_DIR, rel, "application/json")
        rel = path.lstrip("/") or "index.html"
        if "\\" in rel or "\x00" in rel:
            return self._send(404, {"error": "not found"})
        ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css",
                 "json": "application/json", "svg": "image/svg+xml"}.get(rel.rsplit(".", 1)[-1], "application/octet-stream")
        return self._file(VIEWER_DIR, rel, ctype)

    def do_POST(self):
        if not self._guard():
            return
        u = urlsplit(self.path)
        is_workbench = u.path == "/api/workbench"
        if is_workbench:
            try:
                workbench.authorize(self.headers)
            except workbench.WorkbenchError as error:
                self.close_connection = True
                return self._send(error.status, {"error": str(error), "code": error.code})
        try:
            body = self._body()
        except (ValueError, OverflowError, TimeoutError) as e:
            self.close_connection = True
            code = 413 if isinstance(e, OverflowError) else 408 if isinstance(e, TimeoutError) else 400
            response = {"error": str(e)}
            if is_workbench:
                response["code"] = "INVALID_REQUEST"
            return self._send(code, response)
        if is_workbench:
            try:
                result = workbench.Workbench(sys.modules[__name__]).dispatch(body)
            except workbench.WorkbenchError as error:
                return self._send(error.status, {"error": str(error), "code": error.code, **error.details})
            except (ValueError, TypeError, UnicodeError) as error:
                return self._send(400, {"error": str(error), "code": "INVALID_REQUEST"})
            except FileNotFoundError:
                return self._send(404, {"error": "Selected file or directory does not exist", "code": "FILE_NOT_FOUND"})
            except PermissionError:
                return self._send(403, {"error": "Selected path is read-only", "code": "READ_ONLY"})
            except ImportError:
                return self._send(501, {"error": "CAD I/O is unavailable in this worker", "code": "CAD_IO_UNAVAILABLE"})
            except OSError as error:
                return self._send(400, {"error": str(error), "code": "FILESYSTEM_ERROR"})
            return self._send(200, result)
        if u.path == "/api/rebuild":
            p = self._project(body.get("project", ""))
            if not p:
                return
            tier = body.get("tier", "certified")
            if tier not in ("preview", "certified", "both"):
                return self._send(400, {"error": "tier must be preview, certified or both"})
            with p.lock:
                if p.workbench_conflict:
                    return self._send(409, {"error": "Resolve the retained source conflict before rebuilding", "code": "UNRESOLVED_CONFLICT"})
                if tier in ("preview", "both"):
                    p.preview_dirty = True
                if tier in ("certified", "both"):
                    p.certified_due = True
            return self._send(202, {"accepted": True, "project": p.name, "tier": tier})
        if u.path == "/api/view":
            if body.get("project") is not None and not self._project(body["project"]):
                return
            try:
                for k in ("step", "variant"):
                    if k in body and not isinstance(body[k], str):
                        raise ValueError(f"{k} must be a string")
                if "explode" in body:
                    _number(body["explode"], "explode")
                if "xray" in body and not isinstance(body["xray"], bool):
                    raise ValueError("xray must be boolean")
                if "camera" in body:
                    if not isinstance(body["camera"], dict):
                        raise ValueError("camera must be an object")
                    for k in ("position", "target"):
                        if k in body["camera"]:
                            _vector(body["camera"][k], "camera " + k)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            cmd = {"type": "command", "project": body.get("project")}
            for k in ("step", "camera", "variant", "explode", "xray"):
                if k in body:
                    cmd[k] = body[k]
            broadcast(cmd)
            return self._send(202, {"accepted": True})
        if u.path == "/api/screenshot":
            p = self._project(body.get("project", ""))
            if not p:
                return
            rid = f"{p.name}-{uuid.uuid4().hex}"
            ev, holder = threading.Event(), {}
            with SHOTS_LOCK:
                if len(SHOTS_PENDING) >= 64:
                    return self._send(429, {"error": "too many pending screenshots"})
                SHOTS_PENDING[rid] = (ev, holder, p.name)
            try:
                broadcast({"type": "screenshot", "project": p.name, "id": rid})
                if ev.wait(timeout=25):
                    return self._send(200, holder.copy())
                return self._send(504, {"error": "no viewer answered (is the viewer open?)"})
            finally:
                with SHOTS_LOCK:
                    SHOTS_PENDING.pop(rid, None)
        if u.path == "/api/shot":
            rid, data = body.get("id"), body.get("data")
            if not isinstance(rid, str) or not isinstance(data, str) or not data.startswith("data:image/png;base64,"):
                return self._send(400, {"error": "unknown shot id or bad data"})
            try:
                png = base64.b64decode(data.split(",", 1)[1], validate=True)
                if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("not a PNG")
            except ValueError:
                return self._send(400, {"error": "invalid PNG data"})
            with SHOTS_LOCK:
                pending = SHOTS_PENDING.get(rid)
                if not pending or pending[0].is_set() or body.get("project") != pending[2]:
                    return self._send(400, {"error": "unknown shot id or project"})
                path = os.path.join(SHOT_DIR, f"{rid}.png")
                try:
                    _atomic_write(path, png)
                except OSError as e:
                    return self._send(500, {"error": f"could not save screenshot: {e}"})
                ev, holder, project = pending
                holder.update({"path": path, "project": project})
                ev.set()
            return self._send(200, {"saved": path})
        return self._send(404, {"error": "not found"})


def _port(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        port = int(value)
        if str(port) != str(value) or not 1 <= port <= 64476:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("port must be an integer from 1 to 64476 (60 attempts + WS offset)")
    return port


def bind_servers(start):
    """Reserve real HTTP and WS listening sockets together; never probe-and-close."""
    import socket
    start = _port(start)
    last = None
    for port in range(start, start + 60):
        http = ws = None
        try:
            http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            ws = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ws.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ws.bind(("127.0.0.1", port + 1000))
            ws.listen()
            return http, ws
        except OSError as e:
            last = e
            if ws is not None:
                ws.close()
            if http is not None:
                http.server_close()
    raise RuntimeError(f"no free HTTP/websocket port pair: {last}")


def _split_projects(value):
    # os.pathsep is the platform separator (';' on Windows, ':' elsewhere);
    # commas keep working as a convenience separator.
    roots = []
    for chunk in value.replace(",", os.pathsep).split(os.pathsep):
        if chunk.strip():
            roots.append(os.path.expanduser(chunk.strip()))
    return roots


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="worker.py",
        description="CassQuery worker: warm CadQuery build server (loopback only).")
    ap.add_argument("projects", nargs="*", metavar="PROJECT_DIR",
                    help="project directories to serve (default: CASSQUERY_PROJECTS, "
                         "else the bundled testpart demo)")
    ap.add_argument("--port", type=_port, default=None,
                    help="HTTP starting port, 1..64476 (default: CASSQUERY_PORT or 3939; scans 60 HTTP/WS pairs)")
    ap.add_argument("--registry-file", default=os.path.expanduser("~/.cassquery.json"),
                    help="owned-worker readiness registry (written after both listeners bind)")
    args = ap.parse_args(argv)
    try:
        start_port = args.port if args.port is not None else _port(os.environ.get("CASSQUERY_PORT", "3939"))
    except ValueError as e:
        ap.error(str(e))
    _preflight()
    os.makedirs(PAYLOAD_DIR, exist_ok=True)
    os.makedirs(SHOT_DIR, exist_ok=True)

    roots = [os.path.expanduser(r) for r in args.projects] or \
        _split_projects(os.environ.get("CASSQUERY_PROJECTS", ""))
    defaulted = not roots
    if defaulted:
        roots = [os.path.join(HERE, "testpart")]
    loaded = []
    for r in roots:
        try:
            p = Project(r)
        except Exception as e:
            # One bad project must not take the whole worker down.
            reason = f"{type(e).__name__}: {e}"
            LOAD_ERRORS.append({"path": r, "error": reason})
            print(f"CassQuery: skipping project {r}: {reason}", flush=True)
            continue
        same_name = next((n for n in PROJECTS if n.casefold() == p.name.casefold()), None)
        if same_name is not None:
            # Case-only aliases collide on macOS/Windows payload files.
            reason = (f"duplicate project name '{p.name}': {r} skipped; "
                      f"already serving {PROJECTS[same_name].root}")
            LOAD_ERRORS.append({"path": r, "error": reason})
            print(f"CassQuery: skipping project {r}: {reason}", flush=True)
            continue
        if p.manifest.get("live"):
            p.preview_dirty = True
            p.certified_due = True
        PROJECTS[p.name] = p
        loaded.append(p.root)
    if defaulted:
        print("CassQuery: no projects configured — serving the bundled testpart "
              "demo. Pass project directories as arguments or set "
              "CASSQUERY_PROJECTS (os.pathsep-separated) to serve your own.",
              flush=True)
    http, ws_sock = bind_servers(start_port)
    port = http.server_port
    registry_path = os.path.abspath(os.path.expanduser(args.registry_file))
    ready, stop, errors = threading.Event(), threading.Event(), []
    ws = threading.Thread(target=ws_thread, args=(ws_sock, ready, errors, stop), daemon=True)
    registry_written = False
    try:
        ws.start()
        if not ready.wait(timeout=10) or errors:
            raise RuntimeError(f"websocket startup failed: {errors or 'timed out'}")
        initial_sources = snapshot(loaded)
        os.makedirs(os.path.dirname(registry_path), exist_ok=True)
        _atomic_write(registry_path, json.dumps({"port": port, "pid": os.getpid(), "started": time.time()}).encode())
        registry_written = True
        threading.Thread(target=build_thread, daemon=True).start()
        threading.Thread(target=watcher, args=(loaded, initial_sources), daemon=True).start()
        print(f"CassQuery worker on http://127.0.0.1:{port} (ws {port + 1000}), "
              f"projects: {', '.join(PROJECTS) or '(none)'}", flush=True)
        http.timeout = 0.5
        while not errors:
            http.handle_request()
        raise RuntimeError(f"websocket server stopped: {errors[0]}")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        ws.join(timeout=10)
        ws_sock.close()
        http.server_close()
        if registry_written:
            try:
                with open(registry_path, encoding="utf-8") as fh:
                    owned = json.load(fh).get("pid") == os.getpid()
                if owned:
                    os.unlink(registry_path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
