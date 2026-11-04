"""Pick a material preset for every part, and say why.

Order of precedence (first hit wins):
  1. user rules      -- a JSON file of {"match": <regex>, "preset": ..., "color": ..., "params": {...}}
  2. part-name keywords (fasteners, bearings, rails, motors, rubber, glass, ...)
  3. CAD colour      -- saturated colours become paint; parts sharing a colour share a finish
  4. size/brightness fallbacks

Pure Python (no Blender), so it can be run and unit-tested on its own:

    python3 automap.py parts.json [--rules my_rules.json] [--out resolved.json]
"""
from __future__ import annotations

import colorsys
import json
import re
import sys
from pathlib import Path

# (regex on lower-case part name, preset, reason).  Order matters: specific before generic.
KEYWORDS = [
    (r"\b(end ?mill|cutter|drill bit|router bit|insert|endmill|tool ?bit)\b", "carbide", "cutting tool"),
    (r"\b(o-?ring|gasket|seal|grommet|tyre|tire|rubber|foot|feet|bumper|grip|damper)\b", "rubber", "rubber part"),
    (r"\b(glass|lens|window|acrylic|lexan|polycarbonate|plexi|perspex|clear)\b", "glass_clear", "transparent part"),
    (r"\b(bearing|bushing ball|6\d{3}(?:-?2?rs|zz)?|608|ball ?race)\b", "steel_polished", "bearing"),
    (r"\b(hg[rhw]\d+\w*|mg[nw]\d+\w*|lm ?guide|linear ?(rail|guide|way|block|carriage)|guide ?rail|rail|"
     r"carriage block|slide block)\b", "steel_ground", "linear rail / block"),
    (r"\b(ball ?screw|lead ?screw|sfu\d*|bnk\d*|threaded rod|acme)\b", "steel_polished", "lead / ball screw"),
    (r"\b(coupling|coupler|jaw coupling)\b", "aluminium_machined", "shaft coupling"),
    (r"\b(spider)\b", "plastic_color", "coupling spider"),
    (r"\b(shaft|axle|rod|pin|dowel|spindle shaft|arbor)\b", "steel_ground", "shaft / pin"),
    (r"\b(spring)\b", "steel_satin", "spring"),
    (r"\b(motor|stepper|servo|nema ?\d*|gearmotor|actuator)\b", "motor_housing", "motor"),
    (r"\b(screw|bolt|nut|washer|shcs|bhcs|fhcs|set ?screw|stud|rivet|m\d{1,2}(x\d+)?)\b", "_fastener", "fastener"),
    (r"\b(brass|bronze)\b", "brass", "brass / bronze"),
    (r"\b(copper|coil|winding)\b", "copper", "copper"),
    (r"\b(wood|timber|oak|walnut|maple|ply(wood)?)\b", "wood", "wood"),
    (r"\b(pcb|circuit board)\b", "plastic_color", "circuit board"),
    (r"\b(knob|button|cap|bezel|cover|enclosure|housing|case|shell|handle grip)\b", "_plastic", "plastic-type part"),
]
STRUCTURAL = re.compile(r"\b(base|frame|body|chassis|bed|stand|casting|column|gantry|bridge|housing|"
                        r"carriage|saddle|table|jaw|plate|bracket|mount|block|flange|adapter|support|"
                        r"beam|arm|panel|extrusion|profile)\b")
CASTING = re.compile(r"\b(base|body|frame|casting|column|stand|bed|jaw|saddle|bridge|gantry)\b")


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def colour_class(hexcol):
    """-> (kind, rgb) where kind in none|saturated|dark|mid|light."""
    if not hexcol:
        return "none", None
    rgb = hex_to_rgb(hexcol)
    h, l, s = colorsys.rgb_to_hls(*rgb)
    chroma = max(rgb) - min(rgb)
    if chroma > 0.12 and l > 0.06:
        return "saturated", rgb
    if l < 0.18:
        return "dark", rgb
    if l < 0.55:
        return "mid", rgb
    return "light", rgb


def load_rules(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text())
    rules = data.get("rules", data) if isinstance(data, dict) else data
    out = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or "match" not in r:
            continue
        out.append({**r, "_re": re.compile(r["match"], re.I), "_i": i + 1})
    return out


def resolve(parts, rules=None, presets=None):
    """parts: list of dicts with name, part, path, cad_color, size_mm.  Returns list of decisions."""
    rules = rules or []
    decisions = []
    # colour groups: parts that share a saturated CAD colour get one finish, decided by the biggest
    groups = {}
    for p in parts:
        kind, _ = colour_class(p.get("cad_color"))
        if kind == "saturated":
            vol = 1.0
            for v in p.get("size_mm") or [1, 1, 1]:
                vol *= max(v, 0.1)
            g = groups.setdefault(p["cad_color"], {"vol": 0, "casting": False})
            g["vol"] = max(g["vol"], vol)
            if CASTING.search(_clean(p.get("part", ""))) and vol >= g["vol"]:
                g["casting"] = True
    for p in parts:
        d = _decide(p, rules, groups, presets)
        decisions.append(d)
        # multi-body parts: a rule can target one body as "<part>#<n>" (n from 0)
        if rules and (p.get("solids") or 1) > 1:
            for i in range(p["solids"]):
                sd = _decide({**p, "name": f'{p["name"]}#{i}', "path": f'{p.get("path", "")}#{i}'},
                             rules, groups, presets)
                if (sd.get("preset"), sd.get("color"), sd.get("hide")) != (d.get("preset"), d.get("color"), d.get("hide")):
                    decisions.append({**sd, "solid_of": p["name"]})
    return decisions


def _clean(name):
    n = name.lower()
    n = re.sub(r"[_\-./]+", " ", n)
    n = re.sub(r"\(\d+\)|#\d+", " ", n)
    return n


def _decide(p, rules, groups, presets):
    names = [p.get("name", ""), p.get("path", "")] if "#" in (p.get("name") or "") else \
        [p.get("name", ""), p.get("part", ""), p.get("path", "")]
    for r in rules:
        if any(r["_re"].search(n or "") for n in names):
            if r.get("hide"):
                return {**_base(p), "preset": None, "hide": True, "reason": f'rule {r["_i"]}: hidden'}
            return {**_base(p), "preset": r.get("preset", "cad_color"), "color": r.get("color"),
                    "params": r.get("params", {}), "offset_mm": r.get("offset_mm"),
                    "reason": f'rule {r["_i"]} ({r["match"]})'}
    name = _clean(p.get("part", "") + " " + p.get("name", ""))
    kind, rgb = colour_class(p.get("cad_color"))
    for pat, preset, why in KEYWORDS:
        if re.search(pat, name):
            if preset == "_fastener":
                preset = "stainless_brushed" if kind in ("light", "mid") else "black_oxide"
            elif preset == "_plastic":
                # a big structural part named "...housing" / "...body" is a casting or sheet metal,
                # not a moulded plastic cover: let the colour rules below decide
                if STRUCTURAL.search(name) and max(p.get("size_mm") or [0]) >= 150:
                    break
                if kind == "saturated":
                    return {**_base(p), "preset": "plastic_color", "color": p["cad_color"],
                            "reason": f"keyword: {why}, coloured in CAD"}
                preset = "plastic_textured" if kind in ("dark", "none") else "plastic_color"
            col = p.get("cad_color") if (presets or {}).get(preset, {}).get("tint") else None
            return {**_base(p), "preset": preset, "color": col, "reason": f"keyword: {why}"}
    big = max(p.get("size_mm") or [0])
    structural = bool(STRUCTURAL.search(name))
    if kind == "saturated":
        g = groups.get(p["cad_color"], {})
        preset = "cast_paint" if g.get("casting") else "powder_coat"
        return {**_base(p), "preset": preset, "color": p["cad_color"],
                "reason": f'CAD colour {p["cad_color"]} -> painted ({"casting" if g.get("casting") else "powder coat"})'}
    if kind == "dark":
        preset = "anodised_black" if structural else "plastic_black"
        return {**_base(p), "preset": preset, "reason": f"dark CAD colour, {'structural' if structural else 'small'} part"}
    if kind == "light":
        return {**_base(p), "preset": "aluminium_machined", "reason": "light neutral CAD colour"}
    if kind == "mid":
        preset = "aluminium_bead" if structural else "steel_satin"
        return {**_base(p), "preset": preset, "reason": "mid-grey CAD colour"}
    if structural:
        return {**_base(p), "preset": "aluminium_machined", "reason": "no CAD colour, structural name"}
    return {**_base(p), "preset": "steel_satin" if big < 60 else "aluminium_machined",
            "reason": "no CAD colour, no keyword: size fallback (review me)"}


def _base(p):
    return {"name": p.get("name"), "part": p.get("part"), "cad_color": p.get("cad_color"), "color": None,
            "params": {}, "offset_mm": None, "hide": False}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("parts_json")
    ap.add_argument("--rules")
    ap.add_argument("--presets", default=str(Path(__file__).resolve().parent.parent / "assets" / "presets" / "materials.json"))
    ap.add_argument("--out")
    a = ap.parse_args()
    parts = json.loads(Path(a.parts_json).read_text())["parts"]
    presets = json.loads(Path(a.presets).read_text())
    dec = resolve(parts, load_rules(a.rules), presets)
    for d in dec:
        print(f'{d["name"][:44]:44s} {str(d["preset"]):20s} {d["reason"]}')
    unknown = [d for d in dec if d["preset"] and d["preset"] not in presets]
    if unknown:
        sys.exit(f"unknown presets: {sorted({d['preset'] for d in unknown})}")
    if a.out:
        Path(a.out).write_text(json.dumps(dec, indent=1))


if __name__ == "__main__":
    main()
