#!/usr/bin/env python3
"""CassQuery test project — 3 primitive parts, mm."""
import cadquery as cq


def build():
    base = (
        cq.Workplane("XY")
        .box(40, 30, 6, centered=(True, True, False))
        .faces(">Z").workplane()
        .pushPoints([(15, 10), (15, -10), (-15, 10), (-15, -10)])
        .hole(5)
        .val()
    )
    post = cq.Workplane("XY").workplane(offset=6).circle(4).extrude(22).val()
    cap = cq.Workplane("XY").workplane(offset=28).rect(16, 16).extrude(4).val()
    parts = {"Base": base, "Post": post, "Cap": cap}
    for s in parts.values():
        assert s.isValid()
    names = ["Base", "Post", "Cap"]
    return {"names": names, "parts": parts,
            "groups": {"ALL": names}, "axes": {}}
