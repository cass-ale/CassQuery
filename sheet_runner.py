#!/usr/bin/env python3
"""Drawing-sheet job runner (SPEC.md §13): executed by the worker as a
subprocess, so HLR's long GIL-holding OCP calls can never starve the
worker's HTTP, websocket, or build threads. Rebuilds the project's
neutral shapes from its entry point (the gate0 path), generates both
sheets + PDFs via sheets.generate, and writes the sheets index JSON.

Usage: sheet_runner.py PROJECT_ROOT NAME TIER HASH OUTDIR
Prints one line per build note; the worker relays them into the project
history. Exit 0 on success; the traceback is the report on failure.
"""
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets  # noqa: E402


def main():
    root, name, tier, h, outdir = sys.argv[1:6]
    man = json.load(open(os.path.join(root, "viewer.json")))
    entry_file, _, fn = man["entry"].partition(":")
    spec = importlib.util.spec_from_file_location("sheet_entry", os.path.join(root, entry_file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = getattr(mod, fn or "build")()
    scale = float(man.get("unit_scale", 1.0))
    if scale != 1.0:
        import worker  # only for _scale_shape; the runner never serves
        parts_src = {n: worker._scale_shape(s, scale) for n, s in result["parts"].items()}
    else:
        parts_src = result["parts"]
    mats = man.get("materials", {})
    parts, inv = [], []
    for n in result["names"]:
        shp = parts_src[n]
        mat = mats.get(n, {})
        parts.append({"name": n, "shape": shp,
                      "explode": man.get("explode", {}).get(n, [0, 0, 0]),
                      "material": mat.get("material", ""), "finish": mat.get("finish", ""),
                      "solids": len(shp.Solids()),
                      "center": [shp.Center().x, shp.Center().y, shp.Center().z]})
        inv.append({"name": n, "material": mat.get("material", ""),
                    "finish": mat.get("finish", ""), "solids": len(shp.Solids())})
    idx = sheets.generate(name, tier, h, parts, inv, outdir)
    tmp = os.path.join(outdir, f".{name}-{tier}-{h}-sheets.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(idx, fh)
    os.replace(tmp, os.path.join(outdir, f"{name}-{tier}-{h}-sheets.json"))
    for note in idx["notes"]:
        print(f"NOTE {note}", flush=True)
    print(f"DONE {idx['seconds']}", flush=True)


if __name__ == "__main__":
    main()
