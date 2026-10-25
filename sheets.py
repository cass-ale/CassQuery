"""CassQuery drawing export (SPEC.md §13): printable sheets of a payload's
state, generated worker-side from the OCP BREP shapes at payload build time.

Two sheets per payload (assembled: front/top/right/isometric; exploded:
isometric), SVG primary + PDF from the same SVG via PyMuPDF. Large
assemblies (see LARGE_ASSEMBLY_ISO_S) drop top+right from the assembled
sheet per SPEC §13's 2026-10-25 re-decision. Every part on
a sheet comes from the payload inventory; the parts list is generated from
the same list, and generation asserts the two agree before writing files.

Geometry is exact BREP edges through OCP hidden-line removal — never the
payload's tessellated triangles. Sheets are keyed by payload hash and are
build-time artifacts: this module runs on the worker's sheet thread, never
in the rebuild/announce path.
"""
import datetime
import os
import time
from xml.sax.saxutils import escape

import numpy as np

PAGE_W, PAGE_H = 420.0, 297.0  # A3 landscape, units are mm
ROLES = ("sharp", "outline", "smooth")

# Large-assembly view reduction (SPEC §13; Cassian re-decision 2026-10-25,
# after kill trigger 1 tripped on time). render_state times the isometric
# pass; over this threshold the assembled sheet keeps front+isometric and
# omits top+right. Calibration (this VM, isometric project_view):
# TESTPLATE 0.03 s (19 BREP faces), BARD 0.13 s (314 faces), IFRIT 87.8 s
# (4,659 faces) — and IFRIT's assembly-only ortho passes measured top
# 542.1 s / right 109.5 s, the cost this rule avoids. 15 s is spec §4's
# own expense line (>15 s/view put sheets on the build-time branch); the
# measured fleet sits ~100x under and ~6x over it.
# shortcut: a project within timing noise of the line could land on
# either side across hosts; pin the decision per payload hash if a
# project ever measures near 15 s.
LARGE_ASSEMBLY_ISO_S = 15.0

# view name -> (direction toward viewer, screen-right direction), CAD axes
VIEWS = {
    "isometric": ((0.5774, -0.5774, 0.5774), (0.7071, 0.7071, 0.0)),
    "front": ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),
    "top": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
}


def _dir(v):
    from OCP.gp import gp_Dir
    return gp_Dir(*v)


def _hlr(shapes, view):
    """One HLR pass over TopoDS shapes. Returns {role: [polyline, ...]},
    each polyline a list of (x, y) in view-plane mm (origin = world origin,
    scale 1:1 — the sheet transform handles placement)."""
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.gp import gp_Ax2, gp_Pnt
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    zdir, xdir = VIEWS[view] if isinstance(view, str) else view
    algo = HLRBRep_Algo()
    for s in shapes:
        algo.Add(s)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), _dir(zdir), _dir(xdir))))
    algo.Update()
    algo.Hide()
    hs = HLRBRep_HLRToShape(algo)
    out = {}
    for role, comp in (("sharp", hs.VCompound()), ("outline", hs.OutLineVCompound()),
                       ("smooth", hs.Rg1LineVCompound())):
        out[role] = _polylines(comp)
    extra = _polylines(hs.RgNLineVCompound())
    if extra:
        out["smooth"] = out["smooth"] + extra
    return out


def _polylines(compound):
    if compound is None:
        return []
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    lines = []
    ex = TopExp_Explorer(compound, TopAbs_EDGE)
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())  # explorer yields TopoDS_Shape;
        ex.Next()                            # the adaptor wants the Edge cast
        try:
            ad = BRepAdaptor_Curve(edge)
            dq = GCPnts_QuasiUniformDeflection(ad, 0.02)
            if dq.IsDone() and dq.NbPoints() >= 2:
                pts = []
                for i in range(1, dq.NbPoints() + 1):
                    p = ad.Value(dq.Parameter(i))
                    pts.append((p.X(), p.Y()))
                lines.append(pts)
                continue
        except Exception:
            pass
        # degenerate edge (a point or an un-sampled curve): keep endpoints
        try:
            ad = BRepAdaptor_Curve(edge)
            a, b = ad.FirstParameter(), ad.LastParameter()
            pa, pb = ad.Value(a), ad.Value(b)
            lines.append([(pa.X(), pa.Y()), (pb.X(), pb.Y())])
        except Exception:
            continue
    return lines


def _length(lines):
    total = 0.0
    for pts in lines:
        arr = np.asarray(pts)
        if len(arr) > 1:
            total += float(np.linalg.norm(np.diff(arr, axis=0), axis=1).sum())
    return total


def project_view(parts, view, exploded=False):
    """HLR for one view of a configuration. parts: [{name, shape(cq),
    explode}]. Returns (attributed, solo): attributed maps part name ->
    {role: [polylines]} from the whole-assembly pass (visibility is the
    assembly's truth); solo maps part name -> total visible line length of
    the part drawn alone (the occlusion proof's denominator)."""
    def placed(p):
        if exploded and any(p["explode"]):
            return _translated(p["shape"], p["explode"])
        return p["shape"]

    asm = _hlr([placed(p).wrapped for p in parts], view)
    solo, solo_len = {}, {}
    for p in parts:
        one = _hlr([placed(p).wrapped], view)
        solo[p["name"]] = one
        solo_len[p["name"]] = sum(_length(one[r]) for r in ROLES)
    # attribute each assembly polyline to the part whose solo point cloud
    # its midpoint sits nearest (assembly edges lie on part surfaces).
    # One concatenated cloud + blocked distance pass: per-part loops over
    # IFRIT's ~16k lines x ~200k points were minutes of overhead.
    names = [p["name"] for p in parts]
    clouds = []
    for i, name in enumerate(names):
        roles = solo[name]
        pts = [pt for r in ROLES for line in roles[r] for pt in line]
        if pts:
            arr = np.asarray(pts)
            clouds.append(np.column_stack([arr, np.full(len(arr), i)]))
    cloud = np.vstack(clouds) if clouds else np.zeros((0, 3))
    attributed = {n: {r: [] for r in ROLES} for n in names}
    for role in ROLES:
        lines = asm[role]
        if not lines or not len(cloud):
            continue
        mids = np.array([line[len(line) // 2] for line in lines])
        owner = np.zeros(len(lines), dtype=int)
        for s in range(0, len(lines), 32):  # 32 x cloud-points x 8B stays ~50 MB
            chunk = mids[s:s + 32]
            d = ((chunk[:, None, :] - cloud[None, :, :2]) ** 2).sum(axis=2)
            owner[s:s + len(chunk)] = cloud[d.argmin(axis=1), 2].astype(int)
        for line, oi in zip(lines, owner):
            attributed[names[oi]][role].append(line)
    return attributed, solo_len


def _translated(shape, vec):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec
    import cadquery as cq
    tr = gp_Trsf()
    tr.SetTranslation(gp_Vec(*[float(v) for v in vec]))
    return cq.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped, tr, True).Shape())


def view_axes(view):
    """(xdir, ydir) unit vectors of the view plane, for projecting 3D
    centres to sheet-plane coordinates."""
    zdir, xdir = VIEWS[view] if isinstance(view, str) else view
    z = np.asarray(zdir, float)
    x = np.asarray(xdir, float)
    return x, np.cross(z, x)


# ---------------- SVG sheet ----------------

# Presentation is inline attributes, never a <style> block: the verified
# PDF route (MuPDF) does not apply class CSS, and a sheet must render
# identically in every consumer (Phase Gate 0 finding, 2026-10-25).
_ROLE_ATTRS = {
    "sharp": 'stroke="#191919" stroke-width="0.34" fill="none" stroke-linecap="round"',
    "outline": 'stroke="#191919" stroke-width="0.55" fill="none" stroke-linecap="round"',
    "smooth": 'stroke="#6f6f6f" stroke-width="0.17" fill="none" stroke-linecap="round"',
    "leader": 'stroke="#555555" stroke-width="0.18" fill="none"',
}
_TEXT_ATTRS = {
    "label": 'font-size="4.1" fill="#191919"',
    "viewname": 'font-size="3.4" letter-spacing="0.4" fill="#555555"',
    "plist": 'font-size="4.0" fill="#191919"',
    "ptitle": 'font-size="3.4" letter-spacing="0.4" fill="#555555"',
    "furniture": 'font-size="3.9" fill="#333333"',
    "stamp": 'font-size="11" letter-spacing="3.3" fill="#8c3a30" font-weight="600"',
}
_FONT = 'font-family="sans-serif"'


def _text(cls, x, y, content, anchor=None):
    a = f' text-anchor="{anchor}"' if anchor else ""
    return (f'<text class="{cls}" {_FONT} {_TEXT_ATTRS[cls]} x="{x:.1f}" '
            f'y="{y:.1f}"{a}>{escape(content)}</text>')


def _path(pts, tf):
    d = []
    for i, (x, y) in enumerate(pts):
        sx, sy = tf(x, y)
        d.append(("M" if i == 0 else "L") + f"{sx:.2f} {sy:.2f}")
    return "".join(d)


class _View:
    """Places one view's polylines into a sheet cell."""

    def __init__(self, attributed, cell, label_parts=None):
        self.cell = cell
        xs = [p[0] for roles in attributed.values() for r in ROLES
              for line in roles[r] for p in line]
        ys = [p[1] for roles in attributed.values() for r in ROLES
              for line in roles[r] for p in line]
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 1, 1)
        self.label_parts = label_parts or []
        band = 31.0 if self.label_parts else 0.0
        cx, cy, cw, ch = cell
        pad = 5.0
        bw = max(self.bbox[2] - self.bbox[0], 1e-6)
        bh = max(self.bbox[3] - self.bbox[1], 1e-6)
        self.scale = min((cw - 2 * band - pad) / bw, (ch - 2 * pad) / bh)
        self.ox = cx + band + (cw - 2 * band - bw * self.scale) / 2
        self.oy = cy + (ch - bh * self.scale) / 2

    def tf(self, x, y):
        return (self.ox + (x - self.bbox[0]) * self.scale,
                self.oy + (self.bbox[3] - y) * self.scale)

    def svg(self, attributed, view_name):
        cx, cy, cw, ch = self.cell
        out = [_text("viewname", cx + 1, cy + 4.4, view_name.upper())]
        for name, roles in attributed.items():
            body = "".join(f'<path class="{r}" {_ROLE_ATTRS[r]} d="{_path(line, self.tf)}"/>'
                           for r in ROLES for line in roles[r])
            if body:
                out.append(f'<g id="part-{escape(name)}" data-part="{escape(name)}">{body}</g>')
        if self.label_parts:
            out.append(self._labels())
        return "\n".join(out)

    def _labels(self):
        cx, cy, cw, ch = self.cell
        # Slot 0 is the TOP of the column; view-plane y grows upward (the
        # sheet transform flips it), so sort descending — ascending put
        # IFRIT's topmost part (Crown) in the bottom slot with a
        # full-height leader (caught on the first IFRIT sheet, 2026-10-25).
        items = sorted(self.label_parts, key=lambda it: -it[1][1])
        out = []
        n = len(items)
        for i, (name, (px, py)) in enumerate(items):
            slot_y = cy + ch * (i + 0.5) / max(n, 1)
            sx, sy = self.tf(px, py)
            if px < (self.bbox[0] + self.bbox[2]) / 2:
                tx, anchor, lx = cx + 29.5, "end", cx + 31.0
            else:
                tx, anchor, lx = cx + cw - 29.5, "start", cx + cw - 31.0
            out.append(f'<path class="leader" {_ROLE_ATTRS["leader"]} '
                       f'd="M{lx:.2f} {slot_y:.2f}L{sx:.2f} {sy:.2f}"/>')
            out.append(_text("label", tx, slot_y + 1.4, name, anchor))
        return "\n".join(out)


def _parts_list(inventory, x, y, w):
    out = [_text("ptitle", x, y + 3.4, "PARTS")]
    yy = y + 10.0
    for inv in inventory:
        mat = f'{inv["material"]} {inv["finish"]}'.strip() or "—"
        out.append(_text("plist", x, yy, inv["name"]))
        out.append(_text("plist", x + w, yy, f"{mat} · {inv['solids']}", "end"))
        yy += 6.0
    return "\n".join(out)


def sheet_svg(project, state, tier, hash_, inventory, views_svg, notes=()):
    """views_svg: pre-rendered view groups. inventory: [{name, material,
    finish, solids}]. Returns the full SVG text."""
    date = datetime.date.today().isoformat()
    title = f"{project} · {state} · {tier}"
    meta = (f"project={project}; state={state}; tier={tier}; payload={hash_}; "
            f"built={datetime.datetime.now().isoformat(timespec='seconds')}")
    # Stamp sits bottom-right on the furniture baseline: top-right is the
    # parts list's corner, and the first IFRIT preview sheet's stamp
    # obscured a parts-list row (caught by reading the render, 2026-10-25).
    stamp = _text("stamp", 412, 294.2, "PREVIEW", "end") if tier == "preview" else ""
    furn = (f"{project} · {state} · {tier} · {hash_[:8]} · {date} · NTS")
    body = [f"<title>{escape(title)}</title>",
            f"<metadata>{escape(meta)}</metadata>", stamp]
    body.extend(views_svg)
    body.append(_text("furniture", 8, 294.2, furn))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" '
            f'viewBox="0 0 {PAGE_W:.0f} {PAGE_H:.0f}">\n' + "\n".join(body) + "\n</svg>\n")


def render_state(project, state, tier, hash_, parts, inventory):
    """Run HLR for one sheet state and return (svg_text, notes, seconds).
    parts carry cq shapes in assembled positions plus explode vectors."""
    t0 = time.time()
    exploded = state == "exploded"
    notes = []
    rendered = []
    if exploded:
        attributed, solo_len = project_view(parts, "isometric", exploded=True)
        asm_len = {n: sum(_length(attributed[n][r]) for r in ROLES) for n in attributed}
        for n, total in solo_len.items():
            if total > 0 and asm_len.get(n, 0.0) < 0.5 * total:
                notes.append(f"occlusion: {n} shows {asm_len.get(n, 0.0):.0f} of "
                             f"{total:.0f} mm solo linework in the exploded view (<50%)")
        centers = _centers(parts, "isometric", exploded=True)
        v = _View(attributed, (8, 8, 268, 281),
                  label_parts=[(n, centers[n]) for n in attributed])
        rendered.append(v.svg(attributed, "isometric"))
        rendered.append(_parts_list(inventory, 284, 8, 128))
    else:
        cells = {"isometric": (8, 8, 268, 180), "front": (8, 196, 132, 93),
                 "top": (144, 196, 132, 93), "right": (280, 196, 132, 93)}
        t_iso = time.time()
        iso_attr, _ = project_view(parts, "isometric")
        iso_s = time.time() - t_iso
        views = [("isometric", iso_attr)]
        if iso_s > LARGE_ASSEMBLY_ISO_S:
            notes.append(f"view set reduced (large assembly): isometric HLR "
                         f"took {iso_s:.0f}s (> {LARGE_ASSEMBLY_ISO_S:.0f}s "
                         f"threshold); top/right omitted — SPEC §13 "
                         f"re-decision 2026-10-25")
            cells["front"] = (8, 196, 404, 93)  # front spans the row alone
            views.append(("front", project_view(parts, "front")[0]))
        else:
            for view in ("front", "top", "right"):
                views.append((view, project_view(parts, view)[0]))
        for view, attributed in views:
            labels = None
            if view == "isometric":
                centers = _centers(parts, view)
                labels = [(n, centers[n]) for n in attributed]
            v = _View(attributed, cells[view], label_parts=labels)
            rendered.append(v.svg(attributed, view))
        rendered.append(_parts_list(inventory, 284, 8, 128))
    svg = sheet_svg(project, state, tier, hash_, inventory, rendered, notes)
    return svg, notes, round(time.time() - t0, 2)


def _centers(parts, view, exploded=False):
    x, y = view_axes(view)
    out = {}
    for p in parts:
        c = np.asarray(p["center"], float)
        if exploded:
            c = c + np.asarray(p["explode"], float)
        out[p["name"]] = (float(c @ x), float(c @ y))
    return out


def svg_to_pdf(svg_path, pdf_path, title):
    """The one verified SVG->PDF route (Phase Gate 0, 2026-10-25):
    PyMuPDF converts the SVG file itself — vector paths stay vector, text
    stays selectable; render-compared pixel-identical at 110 dpi."""
    import pymupdf
    doc = pymupdf.open(svg_path)
    data = doc.convert_to_pdf()
    out = pymupdf.open("pdf", data)
    out.set_metadata({"title": title, "producer": "CassQuery drawing export"})
    out.save(pdf_path)
    return len(data)


def generate(project, tier, hash_, parts, inventory, outdir):
    """Generate both sheets + PDFs for one payload. Returns an index dict
    (files, seconds, notes). Raises on the inventory assertion — a sheet
    whose drawing and parts list disagree is never written."""
    names = [inv["name"] for inv in inventory]
    assert sorted(names) == sorted(p["name"] for p in parts), \
        "sheet inventory does not match payload parts"
    os.makedirs(outdir, exist_ok=True)
    base = f"{project}-{tier}-{hash_}"
    files, notes, total = {}, [], 0.0
    for state in ("assembled", "exploded"):
        svg, n, secs = render_state(project, state, tier, hash_, parts, inventory)
        # the assertion, on the artifact itself: group names in the SVG
        # must equal the inventory (the int16 lesson — check the output,
        # not the inputs)
        import re
        groups = sorted(set(re.findall(r'data-part="([^"]+)"', svg)))
        assert groups == sorted(names), \
            f"{state} sheet groups {groups} != inventory {sorted(names)}"
        total += secs
        notes.extend(n)
        sp = os.path.join(outdir, f"{base}-{state}.svg")
        with open(sp, "w") as fh:
            fh.write(svg)
        pp = os.path.join(outdir, f"{base}-{state}.pdf")
        svg_to_pdf(sp, pp, f"{project} · {state} · {tier}")
        files[state] = {"svg": os.path.basename(sp), "pdf": os.path.basename(pp)}
    return {"project": project, "tier": tier, "hash": hash_, "files": files,
            "seconds": round(total, 2), "notes": notes}
