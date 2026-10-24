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
import asyncio, base64, hashlib, importlib, importlib.util, json, os, sys, threading, time, traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


def _preflight():
    # Fail before serving if this interpreter cannot build: a missing
    # websockets used to kill the announce thread silently, and a missing
    # cadquery only surfaced as an opaque per-build error.
    missing = []
    for name in ("numpy", "cadquery", "OCP", "websockets"):
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


_preflight()
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.join(HERE, "viewer")
PAYLOAD_DIR = os.path.join(HERE, "payloads")
SHOT_DIR = os.path.join(HERE, "evidence", "shots")
os.makedirs(PAYLOAD_DIR, exist_ok=True)
os.makedirs(SHOT_DIR, exist_ok=True)

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


def _weld(verts, tris, tol=1e-4):
    if len(verts) == 0:
        return verts, tris
    _, idx, inv = np.unique(np.round(verts / tol).astype(np.int64), axis=0,
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


def _closure_check(verts, tris, eps, brep_area=None, brep_closed=False):
    """Watertight verdict for one solid's mesh. Returns (ok, method, detail).

    'exact': every edge of the welded mesh is shared by exactly two
    triangles (native CadQuery solids meet this directly).
    'brep-closed+area': for patchwork STEP solids (thousands of faces that
    sample shared edges independently, so edge pairing measures
    tessellation bookkeeping, not closure) closure is proven by the source
    solid being a valid closed shell, the mesh reproducing the BREP surface
    area within 1% (no dropped or doubled face), and every boundary edge
    lying within eps of another mesh vertex (no true hole). A dropped face
    or a real opening fails this on area and gap distance alike."""
    wv, wt = _weld(verts, tris)
    uniq, counts = _edge_counts(wt)
    nbad = int((counts != 2).sum())
    if nbad == 0:
        return True, "exact", {}
    if not eps or not brep_closed or not brep_area:
        return False, "exact", {"boundary_edges": nbad}
    area_ratio = _mesh_area(wv, wt) / brep_area
    if not (0.99 <= area_ratio <= 1.01):
        return False, "brep-closed+area", {"boundary_edges": nbad,
                                           "area_ratio": round(area_ratio, 4)}
    if nbad > 0.02 * len(uniq):
        return False, "brep-closed+area", {"boundary_edges": nbad,
                                           "area_ratio": round(area_ratio, 4),
                                           "note": "boundary fraction > 2%"}
    # every boundary edge must lie within eps of another mesh edge
    # (segment-to-segment): the two sides of a tessellation seam sample the
    # shared curve independently, so coverage is polyline-to-polyline
    from collections import defaultdict

    def seg_seg(p1, q1, p2, q2):
        d1, d2, r = q1 - p1, q2 - p2, p1 - p2
        a, e = float(d1 @ d1), float(d2 @ d2)
        if a <= 1e-30 and e <= 1e-30:
            return float(np.linalg.norm(r))
        f, c = float(d2 @ r), float(d1 @ r)
        if a <= 1e-30:
            s, t = 0.0, min(1.0, max(0.0, f / e))
        elif e <= 1e-30:
            t, s = 0.0, min(1.0, max(0.0, -c / a))
        else:
            b = float(d1 @ d2)
            den = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / den)) if den > 1e-30 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t, s = 1.0, min(1.0, max(0.0, (b - c) / a))
        return float(np.linalg.norm((p1 + d1 * s) - (p2 + d2 * t)))

    sample = uniq[: min(len(uniq), 500)]
    med = float(np.median(np.linalg.norm(wv[sample[:, 1]] - wv[sample[:, 0]], axis=1)))
    cell = max(eps, med)
    vgrid = defaultdict(list)
    for i, p in enumerate(wv):
        vgrid[tuple(np.floor(p / cell).astype(int))].append(i)
    vedges = defaultdict(list)
    for ei, (a2, b2) in enumerate(uniq):
        vedges[a2].append(ei)
        vedges[b2].append(ei)
    bad_edges = uniq[counts != 2]
    max_gap = 0.0
    for u, v in bad_edges:
        lo = np.minimum(wv[u], wv[v]) - eps
        hi = np.maximum(wv[u], wv[v]) + eps
        cand_rows = set()
        for cx in range(int(np.floor(lo[0] / cell)), int(np.floor(hi[0] / cell)) + 1):
            for cy in range(int(np.floor(lo[1] / cell)), int(np.floor(hi[1] / cell)) + 1):
                for cz in range(int(np.floor(lo[2] / cell)), int(np.floor(hi[2] / cell)) + 1):
                    for j in vgrid.get((cx, cy, cz), ()):
                        cand_rows.update(vedges[j])
        best = 1e18
        for ei in cand_rows:
            a2, b2 = uniq[ei]
            if (a2, b2) == (u, v):
                continue
            best = min(best, seg_seg(wv[u], wv[v], wv[a2], wv[b2]))
            if best <= 1e-9:
                break
        max_gap = max(max_gap, best)
        if best > eps:
            return False, "brep-closed+area", {"boundary_edges": nbad,
                                               "area_ratio": round(area_ratio, 4),
                                               "max_gap_mm": round(best, 3)}
    return True, "brep-closed+area", {"boundary_edges": nbad,
                                      "area_ratio": round(area_ratio, 4),
                                      "max_gap_mm": round(max_gap, 3)}


def _mesh_volume(verts, tris):
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
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
        return {"positions": "", "indices": "", "qoffset": [0, 0, 0], "qscale": 1.0}
    lo = wv.min(axis=0)
    ext = float((wv.max(axis=0) - lo).max()) or 1.0
    scale = ext / 65535.0
    q = np.round((wv - lo) / scale).astype(np.uint16)
    u16 = len(wv) <= 65535
    return {"positions": _b64(q),
            "indices": _b64(wt.astype(np.uint16 if u16 else np.uint32)),
            "itype": "u16" if u16 else "u32",
            "qoffset": [float(x) for x in lo], "qscale": scale}


def _scale_shape(shape, k):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
    from OCP.gp import gp_GTrsf, gp_Pnt, gp_Trsf
    import cadquery as cq
    tr = gp_Trsf()
    tr.SetScale(gp_Pnt(0, 0, 0), k)
    return cq.Shape.cast(BRepBuilderAPI_GTransform(shape.wrapped, gp_GTrsf(tr), True).Shape())


# ---------- projects ----------
class Project:
    def __init__(self, root):
        self.root = root
        self.manifest = json.load(open(os.path.join(root, "viewer.json")))
        for key in ("name", "entry"):
            if key not in self.manifest:
                raise ValueError(f"viewer.json is missing required key '{key}'")
        self.name = self.manifest["name"]
        self.gen = 0
        self.state = "idle"
        self.preview_dirty = False
        self.certified_due = False
        self.lock = threading.Lock()
        self.hashes = {}
        self.gates = None
        self.truth = None
        self.history = []
        self.last_change = 0.0
        self._mod_counter = 0

    def log(self, event, detail=""):
        self.history.append({"t": time.time(), "event": event, "detail": detail})
        self.history = self.history[-20:]

    def load_module(self, filename):
        self._mod_counter += 1
        path = os.path.join(self.root, filename)
        spec = importlib.util.spec_from_file_location(
            f"cq_{self.name}_{self._mod_counter}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    # ----- one full build at one tier; returns (payload_hash, summary) -----
    def build(self, tier):
        gen = self.gen
        t0 = time.time()
        entry_file, _, fn_name = self.manifest["entry"].partition(":")
        entry = self.load_module(entry_file)
        result = getattr(entry, fn_name or "build")()
        scale = float(self.manifest.get("unit_scale", 1.0))
        names = result["names"]

        # BREP truth from the UNSCALED source shapes
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import TopAbs_SHELL
        from OCP.TopExp import TopExp_Explorer

        def _solid_facts(s):
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(s.wrapped, props)
            # a solid with inner (void) shells is still closed; validity +
            # positive volume is the BREP-side closure fact — the mesh-side
            # proof is the area ratio + boundary gap test in _closure_check
            return {"area": props.Mass(),
                    "closed": bool(s.isValid() and s.Volume() > 0)}

        truth = {}
        for n in names:
            shp = result["parts"][n]
            solids = shp.Solids()
            bb = shp.BoundingBox()
            facts = [_solid_facts(s) for s in solids]
            truth[n] = {"volume": shp.Volume(), "solids": len(solids),
                        "center": [shp.Center().x, shp.Center().y, shp.Center().z],
                        "bbox_min_dim_per_solid": [
                            min(s.BoundingBox().xlen, s.BoundingBox().ylen,
                                s.BoundingBox().zlen) for s in solids],
                        "solid_areas": [f["area"] for f in facts],
                        "solid_closed": [f["closed"] for f in facts],
                        "bbox": [bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax]}
        self.truth = truth

        # variants: neutral + poses
        variants_src = {"neutral": result["parts"]}
        if self.manifest.get("poses"):
            poses = self.load_module(self.manifest["poses"])
            for pname, fn in poses.POSES.items():
                variants_src[pname] = fn(result)

        tess = self.manifest.get("tessellation", {})
        tol, ang = tess.get("coarse" if tier == "preview" else "fine",
                            COARSE if tier == "preview" else FINE)
        weld_eps = float(self.manifest.get("weld_epsilon", 0.0))
        closure_methods = []
        variants_out, gate_checks, diag = {}, [], []
        variant_tris = {}
        truth_extent = None
        for vi, (vname, vparts) in enumerate(variants_src.items()):
            parts_out = []
            mesh_extent = [1e18, 1e18, 1e18, -1e18, -1e18, -1e18]
            for n in names:
                shp = vparts[n]
                if scale != 1.0:
                    shp = _scale_shape(shp, scale)
                mat = self.manifest.get("materials", {}).get(n, {})
                fin = FINISH.get((mat.get("material"), mat.get("finish")), FINISH_DEFAULT)
                solids_out, part_tris = [], 0
                part_mesh_vol = 0.0
                for si, solid in enumerate(shp.Solids()):
                    # relative deflection cap: a solid is never tessellated
                    # coarser than 5% of its smallest dimension (small parts
                    # keep fine absolute detail under coarse project settings)
                    sb = solid.BoundingBox()
                    stol = min(tol, 0.05 * min(sb.xlen, sb.ylen, sb.zlen))
                    # small chunky solids (beads) are all curvature: their
                    # volume error is set by the angular tolerance alone
                    # (sphere deficit ~ 3a^2/8), so they get a finer angle
                    sang = ang * 0.35 if max(sb.xlen, sb.ylen, sb.zlen) <= 4.0 else ang
                    verts, tris, missing = _tessellate_solid(solid, stol, sang)
                    wv, wt = _weld(verts, tris)
                    part_tris += len(wt)
                    vol = abs(_mesh_volume(wv, wt))
                    part_mesh_vol += vol
                    if tier == "certified":
                        if missing:
                            diag.append(f"{vname}/{n}/solid{si}: {missing} face(s) without triangulation")
                        ok, method, det = _closure_check(
                            verts, tris, weld_eps,
                            brep_area=truth[n]["solid_areas"][si],
                            brep_closed=truth[n]["solid_closed"][si])
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
                        q = np.frombuffer(base64.b64decode(packed["positions"]),
                                          dtype=np.uint16).astype(np.float64)
                        dec = q.reshape(-1, 3) * packed["qscale"] + np.array(packed["qoffset"])
                        err = float(np.abs(dec - wv).max()) if len(dec) == len(wv) else 1e9
                        if err > 2 * packed["qscale"]:
                            diag.append(f"{vname}/{n}/solid{si}: payload roundtrip error {err:.4f} mm")
                    solids_out.append(packed)
                    if len(wv):
                        lo, hi = wv.min(axis=0), wv.max(axis=0)
                        for k in range(3):
                            mesh_extent[k] = min(mesh_extent[k], lo[k])
                            mesh_extent[3 + k] = max(mesh_extent[3 + k], hi[k])
                if tier == "certified":
                    ref = truth[n]["volume"]
                    dv = abs(part_mesh_vol - ref) / ref * 100 if ref else 0.0
                    if dv > 1.0:
                        diag.append(f"{vname}/{n}: mesh volume off BREP by {dv:.2f}%")
                variant_tris[vname] = variant_tris.get(vname, 0) + part_tris
                parts_out.append({
                    "name": n, "finish": fin, "xray": mat.get("xray", "opaque"),
                    "explode": self.manifest.get("explode", {}).get(n, [0, 0, 0]),
                    "center": truth[n]["center"], "solids": solids_out})
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
            vraw = json.dumps(vdata, separators=(",", ":")).encode()
            vh = hashlib.sha256(vraw).hexdigest()[:16]
            variant_files[vname] = {"hash": vh, "raw": vraw,
                                    "url": f"/payload/{self.name}-{tier}-{vname}.json",
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

        def _write(fname, data):
            tmp = os.path.join(PAYLOAD_DIR, f".{fname}.tmp")
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, os.path.join(PAYLOAD_DIR, fname))

        if not failed:
            for vname, f in variant_files.items():
                _write(f"{self.name}-{tier}-{vname}.json", f["raw"])
        _write(f"{self.name}-certified-failed.json" if failed else f"{self.name}-{tier}.json",
               json.dumps(index, separators=(",", ":")).encode())
        return h, {"seconds": round(time.time() - t0, 2), "tris": worst_tris,
                   "bytes": worst_bytes, "gates": gates}

    def status(self):
        return {"name": self.name, "root": self.root, "state": self.state,
                "live": self.manifest.get("live", False),
                "on_demand": self.manifest.get("on_demand", False),
                "hashes": self.hashes, "gates_pass": (self.gates or {}).get("pass"),
                "history": self.history[-8:]}


PROJECTS = {}
LOAD_ERRORS = []
WS_CLIENTS = set()
WS_LOOP = None
SHOTS_PENDING = {}


def broadcast(msg):
    if WS_LOOP is None:
        return
    dead = []
    for ws in list(WS_CLIENTS):
        try:
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps(msg)), WS_LOOP)
        except Exception:
            dead.append(ws)
    for ws in dead:
        WS_CLIENTS.discard(ws)


def set_state(p, state, detail=""):
    p.state = state
    p.log(state, detail)
    broadcast({"type": "status", "project": p.name, "state": state, "detail": detail,
               "hashes": p.hashes, "gates_pass": (p.gates or {}).get("pass")})


def run_build(p, tier):
    set_state(p, "building-" + tier)
    try:
        h, summary = p.build(tier)
    except Exception:
        tb = traceback.format_exc()
        last = tb.strip().splitlines()[-1] if tb.strip() else "build failed"
        # Point at the deepest frame inside the project's own files: that is
        # where the user's code failed, not the worker's tessellation stack.
        own = [f for f in traceback.extract_tb(sys.exc_info()[2])
               if os.path.abspath(f.filename).startswith(os.path.abspath(p.root) + os.sep)]
        detail = (f"{os.path.basename(own[-1].filename)}:{own[-1].lineno}: {last}"
                  if own else last)
        set_state(p, "error", detail)
        p.log("build-failed", tb[-1500:])
        broadcast({"type": "build_failed", "project": p.name, "tier": tier,
                   "error": last, "detail": detail})
        return False
    if tier == "certified":
        p.gates = summary["gates"]
        if not summary["gates"]["pass"]:
            p.hashes["certified_failed"] = h
            set_state(p, "gate-failed", "; ".join(summary["gates"]["diagnostics"][:4]))
            broadcast({"type": "gate_failed", "project": p.name, "hash": h,
                       "gates": summary["gates"],
                       "url": f"/payload/{p.name}-certified-failed.json"})
            return False
    p.hashes[tier] = h
    set_state(p, tier, f"{summary['seconds']}s, {summary['tris']} tris")
    broadcast({"type": "announce", "project": p.name, "tier": tier, "hash": h,
               "url": f"/payload/{p.name}-{tier}.json",
               "gates_pass": (summary["gates"] or {}).get("pass")})
    return True


def build_thread():
    while True:
        worked = False
        for p in PROJECTS.values():
            if p.preview_dirty:
                p.preview_dirty = False
                if run_build(p, "preview"):
                    gen_at_preview = p.gen
                    # certified settles after the preview if nothing changed since
                    if p.manifest.get("live") and p.gen == gen_at_preview:
                        p.certified_due = True
                worked = True
            if p.certified_due:
                p.certified_due = False
                run_build(p, "certified")
                worked = True
        if not worked:
            time.sleep(0.3)


def snapshot(roots):
    snap = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for f in filenames:
                if f.endswith(".py") or f == "viewer.json":
                    fp = os.path.join(dirpath, f)
                    try:
                        snap[fp] = os.stat(fp).st_mtime_ns
                    except OSError:
                        pass
    return snap


def watcher(roots):
    prev = snapshot(roots)
    while True:
        time.sleep(0.5)
        cur = snapshot(roots)
        changed = [fp for fp in set(cur) | set(prev) if cur.get(fp) != prev.get(fp)]
        if changed:
            for p in PROJECTS.values():
                mine = [fp for fp in changed if fp.startswith(p.root)]
                if mine:
                    p.gen += 1
                    p.last_change = time.time()
                    if any(os.path.basename(fp) == "viewer.json" for fp in mine):
                        try:
                            p.manifest = json.load(open(os.path.join(p.root, "viewer.json")))
                            broadcast({"type": "manifest", "project": p.name})
                        except Exception:
                            pass
                    if p.manifest.get("live"):
                        p.preview_dirty = True
                        set_state(p, "stale", f"changed: {os.path.basename(mine[0])}")
        prev = cur


# ---------- websocket ----------
async def ws_handler(ws):
    WS_CLIENTS.add(ws)
    try:
        await ws.send(json.dumps({"type": "hello",
                                  "projects": [p.status() for p in PROJECTS.values()]}))
        async for _ in ws:
            pass
    finally:
        WS_CLIENTS.discard(ws)


def ws_thread(port):
    global WS_LOOP
    async def main():
        from websockets.asyncio.server import serve
        async with serve(ws_handler, "127.0.0.1", port):
            await asyncio.Future()
    WS_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(WS_LOOP)
    WS_LOOP.run_until_complete(main())


# ---------- HTTP: viewer, payloads, agent protocol ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _project(self, name):
        p = PROJECTS.get(name)
        if not p:
            self._send(404, {"error": f"unknown project {name}"})
        return p

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/projects":
            return self._send(200, [p.status() for p in PROJECTS.values()])
        if u.path == "/api/status":
            name = (q.get("project") or [None])[0]
            if name:
                p = self._project(name)
                return p and self._send(200, p.status())
            # api_version marks this shape: v1 returned a bare list here;
            # the object (with load_errors) is version 2.
            return self._send(200, {"api_version": 2,
                                    "projects": [p.status() for p in PROJECTS.values()],
                                    "load_errors": LOAD_ERRORS})
        if u.path == "/api/manifest":
            p = self._project((q.get("project") or [""])[0])
            return p and self._send(200, p.manifest)
        if u.path == "/api/gates":
            p = self._project((q.get("project") or [""])[0])
            return p and self._send(200, p.gates or {"pass": None, "note": "no certified build yet"})
        if u.path == "/api/parts":
            p = self._project((q.get("project") or [""])[0])
            if not p:
                return
            if not p.truth:
                return self._send(200, {"note": "no build yet", "parts": []})
            return self._send(200, {"project": p.name, "parts": [
                {"name": n, **{k: t[k] for k in ("volume", "solids", "center", "bbox")}}
                for n, t in p.truth.items()]})
        if u.path.startswith("/payload/"):
            fp = os.path.join(PAYLOAD_DIR, os.path.basename(u.path))
            if os.path.exists(fp):
                return self._send(200, open(fp, "rb").read())
            return self._send(404, {"error": "no such payload"})
        # static viewer
        rel = u.path.lstrip("/") or "index.html"
        fp = os.path.join(VIEWER_DIR, rel)
        if os.path.isfile(fp):
            ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css",
                     "json": "application/json"}.get(rel.rsplit(".", 1)[-1], "application/octet-stream")
            return self._send(200, open(fp, "rb").read(), ctype)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/rebuild":
            p = self._project(body.get("project", ""))
            if not p:
                return
            tier = body.get("tier", "certified")
            if tier in ("preview", "both"):
                p.preview_dirty = True
            if tier in ("certified", "both"):
                p.certified_due = True
            return self._send(202, {"accepted": True, "project": p.name, "tier": tier})
        if u.path == "/api/view":
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
            rid = f"{p.name}-{int(time.time() * 1000)}"
            ev = threading.Event()
            SHOTS_PENDING[rid] = (ev, {})
            broadcast({"type": "screenshot", "project": p.name, "id": rid})
            if ev.wait(timeout=25):
                return self._send(200, SHOTS_PENDING[rid][1])
            SHOTS_PENDING.pop(rid, None)
            return self._send(504, {"error": "no viewer answered (is the viewer open?)"})
        if u.path == "/api/shot":
            rid = body.get("id", "")
            data = body.get("data", "")
            if rid in SHOTS_PENDING and data.startswith("data:image/png;base64,"):
                path = os.path.join(SHOT_DIR, f"{rid}.png")
                with open(path, "wb") as f:
                    f.write(base64.b64decode(data.split(",", 1)[1]))
                ev, holder = SHOTS_PENDING[rid]
                holder.update({"path": path, "project": body.get("project")})
                ev.set()
                return self._send(200, {"saved": path})
            return self._send(400, {"error": "unknown shot id or bad data"})
        return self._send(404, {"error": "not found"})


def find_port(start):
    import socket
    for port in range(start, start + 60):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("no free port")


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
    ap.add_argument("--port", type=int, default=None,
                    help="HTTP port (default: CASSQUERY_PORT or 3939; scans upward if busy)")
    args = ap.parse_args(argv)

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
        if p.name in PROJECTS:
            # A second project with the same name must not silently
            # replace the first in PROJECTS.
            reason = (f"duplicate project name '{p.name}': {r} skipped; "
                      f"already serving {PROJECTS[p.name].root}")
            LOAD_ERRORS.append({"path": r, "error": reason})
            print(f"CassQuery: skipping project {r}: {reason}", flush=True)
            continue
        if p.manifest.get("live"):
            p.preview_dirty = True
            p.certified_due = True
        PROJECTS[p.name] = p
        loaded.append(r)
    if defaulted:
        print("CassQuery: no projects configured — serving the bundled testpart "
              "demo. Pass project directories as arguments or set "
              "CASSQUERY_PROJECTS (os.pathsep-separated) to serve your own.",
              flush=True)
    port = find_port(args.port or int(os.environ.get("CASSQUERY_PORT", "3939")))
    json.dump({"port": port, "pid": os.getpid(), "started": time.time()},
              open(os.path.expanduser("~/.cassquery.json"), "w"))
    threading.Thread(target=ws_thread, args=(port + 1000,), daemon=True).start()
    threading.Thread(target=build_thread, daemon=True).start()
    threading.Thread(target=watcher, args=(loaded,), daemon=True).start()
    print(f"CassQuery worker on http://127.0.0.1:{port} (ws {port + 1000}), "
          f"projects: {', '.join(PROJECTS) or '(none)'}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
