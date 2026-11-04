# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["cadquery-ocp-novtk>=7.8,<8", "numpy>=1.24"]
# ///
"""Convert a CAD assembly (STEP / IGES / BREP) into a glTF binary (.glb).

Keeps the assembly structure so parts can be told apart downstream:

    root  (scale 0.001: CAD millimetres -> glTF metres)
      <part instance node>    name = part name, extras = {part, instance, path, cad_color}
        <solid mesh node>     name = "<part>#<n>" when a part has several solids

Tessellation tolerance is relative to the model size, so a 30 mm bracket and a
3 m machine get the same visual smoothness for the same preset.

Usage:
    uv run --script cad_to_glb.py model.step out.glb --quality render
    uv run --script cad_to_glb.py model.step out.glb --linear 0.05 --angular 0.2
    uv run --script cad_to_glb.py model.step out.glb --parts-json parts.json
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Bnd import Bnd_Box
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

# linear deflection as a fraction of the bounding-box diagonal, angular in radians
# CAD programs disagree on "up". These default to Y-up; nearly everything else is Z-up.
Y_UP_SYSTEMS = ("solidworks", "swstep", "inventor", "autodesk inventor", "pro/engineer", "creo", "spaceclaim",
                "solid edge", "fusion")


def detect_up(path: Path) -> tuple[str, str]:
    """Guess the source up-axis from the STEP header (which program wrote the file)."""
    if path.suffix.lower() not in (".step", ".stp"):
        return "z", "not a STEP file, assuming Z-up"
    head = path.open("rb").read(4096).decode("latin-1", "ignore").lower()
    m = head.find("file_name")
    hdr = head[m:m + 900] if m >= 0 else head
    for s in Y_UP_SYSTEMS:
        if s in hdr:
            return "y", f"written by {s.title()} (Y-up by default)"
    return "z", "no Y-up CAD program named in the header, assuming Z-up"


def to_gltf_axes(V, up):
    """Rotate CAD coordinates so the CAD up-axis becomes glTF +Y."""
    x, y, z = V[:, 0].copy(), V[:, 1].copy(), V[:, 2].copy()
    if up == "y":
        return V
    if up == "-y":
        return np.stack([x, -y, -z], 1)
    if up == "z":
        return np.stack([x, z, -y], 1)
    if up == "-z":
        return np.stack([x, -z, y], 1)
    if up == "x":
        return np.stack([-y, x, z], 1)
    if up == "-x":
        return np.stack([y, -x, z], 1)
    raise ValueError(up)


QUALITY = {
    "draft":  (6e-4, 0.6),
    "web":    (2.5e-4, 0.5),
    "render": (5e-5, 0.2),
    "hero":   (2e-5, 0.1),
}


def read_doc(path: Path):
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    ext = path.suffix.lower()
    if ext in (".step", ".stp"):
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        r = STEPCAFControl_Reader()
    elif ext in (".iges", ".igs"):
        from OCP.IGESCAFControl import IGESCAFControl_Reader
        r = IGESCAFControl_Reader()
    elif ext == ".brep":
        return None
    else:
        sys.exit(f"unsupported CAD format: {ext}")
    r.SetNameMode(True)
    r.SetColorMode(True)
    if r.ReadFile(str(path)) != IFSelect_RetDone:
        sys.exit(f"could not read {path}")
    r.Transfer(doc)
    return doc


def label_name(lbl) -> str:
    a = TDataStd_Name()
    if lbl.FindAttribute(TDataStd_Name.GetID_s(), a):
        return a.Get().ToExtString().strip()
    return ""


def label_color(color_tool, lbl, shape):
    c = Quantity_Color()
    for ct in (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen):
        found = False
        for target in (lbl, shape):
            try:
                if color_tool.GetColor_s(target, ct, c) if hasattr(color_tool, "GetColor_s") else color_tool.GetColor(target, ct, c):
                    found = True
                    break
            except TypeError:
                continue
        if found:
            r, g, b = c.Values(Quantity_TOC_sRGB)
            return "#%02x%02x%02x" % tuple(int(round(max(0, min(1, v)) * 255)) for v in (r, g, b))
    return None


def collect_parts(doc):
    """Walk the XCAF tree -> list of (instance_name, part_name, path, located_shape, color)."""
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    out = []

    def walk(lbl, loc, path, inherited_color, inst_name):
        if st.IsReference_s(lbl):
            ref = TDF_Label()
            st.GetReferredShape_s(lbl, ref)
            col = label_color(ct, lbl, st.GetShape_s(lbl)) or inherited_color
            walk(ref, loc.Multiplied(st.GetLocation_s(lbl)), path, col, label_name(lbl))
            return
        name = label_name(lbl) or "part"
        shp = st.GetShape_s(lbl)
        col = label_color(ct, lbl, shp) or inherited_color
        if st.IsAssembly_s(lbl):
            comps = TDF_LabelSequence()
            st.GetComponents_s(lbl, comps)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), loc, path + [name], col, None)
            return
        out.append({"part": name, "instance": inst_name or name, "path": path + [name],
                    "shape": shp.Located(loc.Multiplied(shp.Location())), "color": col})

    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), TopLoc_Location(), [], None, None)
    return out


def bbox_diag(shapes) -> float:
    box = Bnd_Box()
    for s in shapes:
        BRepBndLib.Add_s(s, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


def mesh_solid(sol):
    V, N, F = [], [], []
    fe = TopExp_Explorer(sol, TopAbs_FACE)
    while fe.More():
        face = TopoDS.Face_s(fe.Current())
        L = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, L)
        if tri is not None:
            trsf = L.Transformation()
            rev = face.Orientation() == TopAbs_REVERSED
            base = len(V)
            has_n = tri.HasNormals()
            for k in range(1, tri.NbNodes() + 1):
                p = tri.Node(k).Transformed(trsf)
                V.append((p.X(), p.Y(), p.Z()))
                if has_n:
                    d = tri.Normal(k).Transformed(trsf)
                    n = (d.X(), d.Y(), d.Z())
                    N.append((-n[0], -n[1], -n[2]) if rev else n)
                else:
                    N.append((0.0, 0.0, 0.0))
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                if rev:
                    b, c = c, b
                F.append((base + a - 1, base + b - 1, base + c - 1))
        fe.Next()
    if not F:
        return None
    V = np.asarray(V, np.float32)
    N = np.asarray(N, np.float32)
    F = np.asarray(F, np.uint32)
    bad = np.linalg.norm(N, axis=1) < 1e-6
    if bad.any():  # fill missing normals from face normals
        fn = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
        acc = np.zeros_like(V)
        for j in range(3):
            np.add.at(acc, F[:, j], fn)
        N[bad] = acc[bad]
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
    return V, N, F


def srgb_to_linear(hexcol):
    h = hexcol.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]


def write_glb(parts_meshes, out: Path):
    bin_ = bytearray()
    views, accessors, meshes, nodes, materials, mat_index = [], [], [], [], [], {}

    def view(arr, target):
        while len(bin_) % 4:
            bin_.extend(b"\0")
        views.append({"buffer": 0, "byteOffset": len(bin_), "byteLength": arr.nbytes, "target": target})
        bin_.extend(arr.tobytes())
        return len(views) - 1

    def material(color):
        key = color or "_default"
        if key not in mat_index:
            base = srgb_to_linear(color) if color else [0.6, 0.6, 0.62]
            materials.append({"name": f"cad {color}" if color else "cad default",
                              "pbrMetallicRoughness": {"baseColorFactor": base + [1.0],
                                                       "metallicFactor": 0.0, "roughnessFactor": 0.5}})
            mat_index[key] = len(materials) - 1
        return mat_index[key]

    part_nodes = []
    for p in parts_meshes:
        child_ids = []
        for si, (V, N, F) in enumerate(p["solids"]):
            va = view(V, 34962)
            accessors.append({"bufferView": va, "componentType": 5126, "count": len(V), "type": "VEC3",
                              "min": V.min(0).tolist(), "max": V.max(0).tolist()})
            pos_acc = len(accessors) - 1
            na = view(N, 34962)
            accessors.append({"bufferView": na, "componentType": 5126, "count": len(N), "type": "VEC3"})
            nrm_acc = len(accessors) - 1
            fa = view(F.reshape(-1), 34963)
            accessors.append({"bufferView": fa, "componentType": 5125, "count": int(F.size), "type": "SCALAR"})
            idx_acc = len(accessors) - 1
            nm = p["node_name"] if len(p["solids"]) == 1 else f'{p["node_name"]}#{si}'
            meshes.append({"name": nm, "primitives": [{"attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
                                                       "indices": idx_acc,
                                                       "material": material(p["color"])}]})
            nodes.append({"name": nm, "mesh": len(meshes) - 1,
                          "extras": {"part": p["part"], "instance": p["node_name"], "solid": si}})
            child_ids.append(len(nodes) - 1)
        nodes.append({"name": p["node_name"], "children": child_ids,
                      "extras": {"part": p["part"], "instance": p["node_name"],
                                 "path": " / ".join(p["path"]), "cad_color": p["color"] or ""}})
        part_nodes.append(len(nodes) - 1)
    nodes.append({"name": "model", "children": part_nodes, "scale": [0.001, 0.001, 0.001]})
    gltf = {"asset": {"version": "2.0", "generator": "cad-studio-renders cad_to_glb"},
            "scene": 0, "scenes": [{"nodes": [len(nodes) - 1]}], "nodes": nodes, "meshes": meshes,
            "materials": materials, "accessors": accessors, "bufferViews": views,
            "buffers": [{"byteLength": len(bin_)}]}
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    bin_.extend(b"\0" * (-len(bin_) % 4))
    with open(out, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 28 + len(js) + len(bin_)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(bin_), 0x004E4942) + bytes(bin_))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--quality", choices=QUALITY, default="render")
    ap.add_argument("--linear", type=float, help="absolute linear deflection in mm (overrides --quality)")
    ap.add_argument("--angular", type=float, help="angular deflection in radians (overrides --quality)")
    ap.add_argument("--parts-json", type=Path, help="also write a part list (names, sizes, triangle counts)")
    ap.add_argument("--up", default="auto", choices=["auto", "z", "y", "x", "-z", "-y", "-x"],
                    help="which CAD axis points up (default: guess from the program that wrote the file)")
    a = ap.parse_args()

    if a.src.suffix.lower() == ".brep":
        from OCP.BRep import BRep_Builder
        from OCP.BRepTools import BRepTools
        from OCP.TopoDS import TopoDS_Shape
        shp = TopoDS_Shape()
        BRepTools.Read_s(shp, str(a.src), BRep_Builder())
        parts = [{"part": a.src.stem, "instance": a.src.stem, "path": [a.src.stem], "shape": shp, "color": None}]
    else:
        parts = collect_parts(read_doc(a.src))
    if not parts:
        sys.exit("no geometry found")

    up, why = detect_up(a.src) if a.up == "auto" else (a.up, "set by --up")
    print(f"up axis: {up.upper()}  ({why})")
    diag = bbox_diag([p["shape"] for p in parts])
    lin_rel, ang = QUALITY[a.quality]
    lin = a.linear if a.linear else max(diag * lin_rel, 1e-4)
    ang = a.angular if a.angular else ang
    print(f"parts: {len(parts)}  size (diagonal): {diag:.1f} mm  linear tol: {lin:.4f} mm  angular: {ang}")

    seen = {}
    meshed, report, total = [], [], 0
    for p in parts:
        BRepMesh_IncrementalMesh(p["shape"], lin, False, ang, True)
        exp = TopExp_Explorer(p["shape"], TopAbs_SOLID)
        sols = []
        while exp.More():
            sols.append(exp.Current())
            exp.Next()
        meshes = [m for m in (mesh_solid(s) for s in (sols or [p["shape"]])) if m is not None]
        if not meshes:
            continue
        base = p["part"]
        seen[base] = seen.get(base, 0) + 1
        p["node_name"] = base if seen[base] == 1 else f"{base} ({seen[base]})"
        p["solids"] = [(to_gltf_axes(V, up).astype(np.float32), to_gltf_axes(N, up).astype(np.float32), F)
                       for V, N, F in meshes]
        tris = sum(len(m[2]) for m in meshes)
        total += tris
        allv = np.concatenate([m[0] for m in meshes])
        report.append({"name": p["node_name"], "part": base, "path": " / ".join(p["path"]),
                       "solids": len(meshes), "triangles": tris, "cad_color": p["color"],
                       "size_mm": [round(float(v), 2) for v in (allv.max(0) - allv.min(0))],
                       "center_mm": [round(float(v), 2) for v in (allv.max(0) + allv.min(0)) / 2]})
        meshed.append(p)

    for p in meshed:  # second pass so "(1)" is only added when a part name repeats
        if seen[p["part"]] > 1 and p["node_name"] == p["part"]:
            p["node_name"] = f'{p["part"]} (1)'
    for r, p in zip(report, meshed):
        r["name"] = p["node_name"]

    a.out.parent.mkdir(parents=True, exist_ok=True)
    write_glb(meshed, a.out)
    print(f"wrote {a.out}  ({a.out.stat().st_size / 1e6:.1f} MB, {total:,} triangles, {len(meshed)} parts)")
    if a.parts_json:
        a.parts_json.write_text(json.dumps({"source": a.src.name, "diagonal_mm": round(diag, 2), "up": up,
                                            "triangles": total, "parts": report}, indent=1))


if __name__ == "__main__":
    main()
