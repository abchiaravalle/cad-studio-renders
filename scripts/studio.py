#!/usr/bin/env python3
"""cad-studio-renders: CAD file in -> studio product shots, transparent cut-outs and a
self-hostable 3D embed out.

    python3 studio.py model.step                       # hero + transparent + embed package
    python3 studio.py model.step --shots hero,front,iso --lighting high_key
    python3 studio.py model.step --preview             # fast low-res check of framing/materials
    python3 studio.py model.step --map-only            # just print the part -> material table
    python3 studio.py model.step --turntable --exploded 0.35 --passes --clay
    python3 studio.py model.step --rules my_rules.json --backdrop "#1d1f22"

Needs: Blender 4.2+ (on PATH, or BLENDER=/path/to/blender), uv (for the CAD reader), ffmpeg
(only for turntable videos).  Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRESETS = ROOT / "assets" / "presets"
CACHE = Path(os.environ.get("CAD_STUDIO_CACHE", Path.home() / ".cache" / "cad-studio-renders"))

# CC0 studio HDRIs from Poly Haven, fetched on first use
HDRIS = {
    "studio_small_09": "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/{res}/studio_small_09_{res}.hdr",
    "studio_small_08": "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/{res}/studio_small_08_{res}.hdr",
    "photo_studio_01": "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/{res}/photo_studio_01_{res}.hdr",
    "brown_photostudio_02": "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/{res}/brown_photostudio_02_{res}.hdr",
}


def die(msg):
    sys.exit(f"error: {msg}")


def find_blender():
    cands = [os.environ.get("BLENDER"), shutil.which("blender"),
             "/Applications/Blender.app/Contents/MacOS/Blender",
             r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"]
    for c in cands:
        if c and Path(c).exists():
            return c
    die("Blender not found. Install Blender 4.2+ and put it on PATH, or set BLENDER=/path/to/blender")


def fetch(url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print(f"downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "cad-studio-renders"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.rename(dest)
    return dest


def hdri_path(name_or_file, res):
    p = Path(name_or_file)
    if p.suffix.lower() in (".hdr", ".exr") and p.exists():
        return p.resolve()
    if name_or_file not in HDRIS:
        die(f"unknown HDRI '{name_or_file}'. Use one of {list(HDRIS)} or a path to an .hdr/.exr file")
    return fetch(HDRIS[name_or_file].format(res=res), CACHE / "hdri" / f"{name_or_file}_{res}.hdr")


def run(cmd, log_path=None, quiet=False):
    t = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in p.stdout:
        lines.append(line)
        if not quiet and (line.startswith("[studio") or line.startswith("wrote") or line.startswith("parts:")
                          or line.startswith("up axis") or line.startswith("warning")
                          or "Error" in line or "Traceback" in line):
            print("  " + line.rstrip())
    p.wait()
    if log_path:
        Path(log_path).write_text("".join(lines))
    if p.returncode != 0:
        tail = "".join(lines[-25:])
        die(f"command failed ({p.returncode}): {' '.join(map(str, cmd[:3]))} ...\n{tail}")
    return time.time() - t


def cad_to_glb(src: Path, out: Path, quality, parts_json: Path, logf):
    if src.suffix.lower() in (".glb", ".gltf"):
        shutil.copy(src, out)
        return None
    uv = shutil.which("uv")
    if not uv:
        die("uv is needed to read CAD files (https://docs.astral.sh/uv/). Or pass a .glb instead.")
    cmd = [uv, "run", "--script", str(HERE / "cad_to_glb.py"), str(src), str(out), "--quality", quality,
           "--parts-json", str(parts_json)]
    run(cmd, logf)


def parts_from_glb(glb: Path):
    """Read part names straight from a GLB (when the input was already a mesh)."""
    import struct
    b = glb.read_bytes()
    ln = struct.unpack("<I", b[12:16])[0]
    g = json.loads(b[20:20 + ln])
    parts = []
    for n in g.get("nodes", []):
        if "mesh" in n or not n.get("children"):
            continue
        parts.append({"name": n.get("name", "part"), "part": (n.get("extras") or {}).get("part", n.get("name")),
                      "path": n.get("name"), "cad_color": (n.get("extras") or {}).get("cad_color"),
                      "size_mm": None})
    return {"parts": parts}


def main():
    shots_all = json.loads((PRESETS / "shots.json").read_text())
    lights_all = json.loads((PRESETS / "lighting.json").read_text())
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", type=Path, help="STEP / STP / IGES / BREP / GLB")
    ap.add_argument("-o", "--out", type=Path, help="output folder (default: <model>_studio/)")
    ap.add_argument("--name", help="product name used in the embed package and file names")
    g = ap.add_argument_group("shots")
    g.add_argument("--shots", default="hero", help=f"comma list from: {', '.join(k for k in shots_all if not k.startswith('_'))}")
    g.add_argument("--size", type=int, default=2400, help="long edge in px (default 2400)")
    g.add_argument("--aspect", help="override aspect, e.g. 16:9, 1:1, 4:5")
    g.add_argument("--samples", type=int, default=256, help="max samples per pixel (default 256; 512+ for hero finals)")
    g.add_argument("--preview", action="store_true", help="fast draft: 1000 px, 48 samples, coarse mesh, no extras")
    g.add_argument("--detail-target", help="regex of part names to frame for the 'detail' shot")
    g = ap.add_argument_group("look")
    g.add_argument("--lighting", default="studio_soft", choices=[k for k in lights_all if not k.startswith("_")])
    g.add_argument("--hdri", default="studio_small_09", help=f"{list(HDRIS)} or a path to your own .hdr/.exr")
    g.add_argument("--hdri-res", default="4k", choices=["1k", "2k", "4k"])
    g.add_argument("--hdri-rotation", type=float, default=0.0, help="extra HDRI rotation in degrees")
    g.add_argument("--backdrop", help="backdrop grey (0-1) or hex colour, e.g. 0.42 or '#1d1f22'")
    g.add_argument("--exposure", type=float, default=0.0, help="stops, + brighter")
    g.add_argument("--light-scale", type=float, default=1.0, help="multiply all area-light power")
    g.add_argument("--env-scale", type=float, default=1.0, help="multiply HDRI strength")
    g.add_argument("--dof", type=float, default=1.0, help="depth-of-field scale: 0 = off, 2 = twice the f-stop (less blur)")
    g.add_argument("--bevel-scale", type=float, default=1.0, help="edge rounding scale (0 = razor CAD edges)")
    g.add_argument("--shadow", choices=["soft", "none"], default="soft", help="contact shadow in transparent PNGs")
    g = ap.add_argument_group("materials")
    g.add_argument("--rules", type=Path, help="JSON rules: [{match, preset, color, params, hide, offset_mm}]")
    g.add_argument("--map-only", action="store_true", help="print the part -> material table and stop")
    g = ap.add_argument_group("deliverables")
    g.add_argument("--no-backdrop", action="store_true", help="skip the on-backdrop studio shots")
    g.add_argument("--no-transparent", action="store_true", help="skip the transparent cut-outs")
    g.add_argument("--clay", action="store_true", help="also render a grey clay pass of the first shot")
    g.add_argument("--passes", action="store_true", help="also write multilayer EXR (depth, normal, AO, mist, cryptomatte)")
    g.add_argument("--exploded", type=float, default=0.0, help="also render an exploded view, e.g. 0.35")
    g.add_argument("--turntable", action="store_true", help="also render a 360 turntable (MP4 + WebM + frames)")
    g.add_argument("--turntable-frames", type=int, default=96)
    g.add_argument("--turntable-transparent", action="store_true", help="WebM with alpha + PNG frames")
    g.add_argument("--no-embed", action="store_true", help="skip the self-hosted 3D web package")
    g.add_argument("--no-blend", action="store_true", help="skip the editable .blend")
    g.add_argument("--quality", default=None, choices=["draft", "web", "render", "hero"], help="tessellation for renders")
    a = ap.parse_args()

    src = a.model.resolve()
    if not src.exists():
        die(f"{src} not found")
    name = a.name or src.stem.replace("_", " ").replace("-", " ").strip().title()
    slug = "".join(c if c.isalnum() else "-" for c in (a.name or src.stem).lower()).strip("-") or "model"
    out = (a.out or src.parent / f"{src.stem}_studio").resolve()
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    blender = None if a.map_only else find_blender()
    shots = [s.strip() for s in a.shots.split(",") if s.strip()]
    bad = [s for s in shots if s not in shots_all]
    if bad:
        die(f"unknown shot(s) {bad}. Choose from {[k for k in shots_all if not k.startswith('_')]}")

    if a.preview:
        a.size, a.samples = min(a.size, 1000), 48
        a.quality = a.quality or "web"
    quality = a.quality or "render"
    t0 = time.time()

    # 1. CAD -> GLB (render resolution) + part list
    print(f"1/4  reading {src.name}")
    glb = work / f"{slug}_{quality}.glb"
    parts_json = work / "parts.json"
    if not glb.exists() or glb.stat().st_mtime < src.stat().st_mtime:
        cad_to_glb(src, glb, quality, parts_json, work / "cad_to_glb.log")
    parts = json.loads(parts_json.read_text()) if parts_json.exists() else parts_from_glb(glb)

    # 2. materials
    sys.path.insert(0, str(HERE))
    import automap
    mats = json.loads((PRESETS / "materials.json").read_text())
    decisions = automap.resolve(parts["parts"], automap.load_rules(a.rules), mats)
    table = out / "materials_map.json"
    table.write_text(json.dumps([{k: v for k, v in d.items() if v not in (None, {}, False)} for d in decisions], indent=1))
    review = sum(1 for d in decisions if "review me" in (d.get("reason") or ""))
    print(f"2/4  materials: {len(decisions)} parts mapped, {review} on a guess (see materials_map.json)")
    if a.map_only:
        for d in decisions:
            print(f'     {d["name"][:46]:46s} {str(d.get("preset")):20s} {d["reason"]}')
        return

    hdri = hdri_path(a.hdri, a.hdri_res)
    job = {
        "mode": "render", "glb": str(glb), "out_dir": str(out), "hdri": str(hdri),
        "presets": {"materials": mats, "lighting": lights_all, "shots": shots_all},
        "materials": decisions, "lighting": a.lighting, "shots": shots, "size": a.size, "aspect": a.aspect,
        "samples": a.samples, "exposure": a.exposure, "light_scale": a.light_scale, "env_scale": a.env_scale,
        "hdri_rotation": a.hdri_rotation, "dof_scale": a.dof, "bevel_scale": a.bevel_scale, "shadow": a.shadow,
        "detail_target": a.detail_target, "blend_name": f"{slug}_scene", "save_blend": not (a.no_blend or a.preview),
        "backdrop": (a.backdrop if (a.backdrop and a.backdrop.startswith("#")) else float(a.backdrop)) if a.backdrop else None,
        "outputs": {"backdrop": not a.no_backdrop, "transparent": not a.no_transparent and not a.preview,
                    "clay": a.clay, "passes": a.passes, "exploded": a.exploded,
                    "turntable": ({"frames": a.turntable_frames, "transparent": a.turntable_transparent,
                                   "size": 1080, "samples": 48} if a.turntable else None)},
    }
    jf = work / "job_render.json"
    jf.write_text(json.dumps(job))
    print(f"3/4  rendering {', '.join(shots)} with Blender ({a.size}px, {a.samples} samples)")
    run([blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(HERE / "blender_studio.py"), "--", str(jf)],
        work / "blender_render.log")
    res = json.loads((out / "_render_result.json").read_text())

    if a.turntable:
        encode_turntable(out, slug, a.turntable_transparent)

    # 4. web package
    if not a.no_embed and not a.preview:
        print("4/4  building the self-hosted 3D embed package")
        web_glb = work / f"{slug}_web.glb"
        web_parts = work / "parts_web.json"
        cad_to_glb(src, web_glb, "web", web_parts, work / "cad_to_glb_web.log")
        pkg = out / "embed"
        (pkg / "assets").mkdir(parents=True, exist_ok=True)
        wjob = {**job, "mode": "web", "glb": str(web_glb),
                "web": {"glb": str(pkg / "assets" / f"{slug}.glb"), "usdz": str(pkg / "assets" / f"{slug}.usdz"),
                        "draco": True}}
        wjf = work / "job_web.json"
        wjf.write_text(json.dumps(wjob))
        run([blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(HERE / "blender_studio.py"), "--", str(wjf)],
            work / "blender_web.log")
        import package_embed
        poster = next((out / f"{s}.png" for s in shots if (out / f"{s}.png").exists()), None) or \
            next((out / f"{s}_transparent.png" for s in shots if (out / f"{s}_transparent.png").exists()), None)
        package_embed.build(pkg, slug=slug, name=name, poster_png=poster, hdri=hdri_path(a.hdri, "1k"),
                            stills=[out / f for f in res["outputs"] if f.endswith(".png")],
                            turntable=(out / f"{slug}_turntable.mp4") if a.turntable else None)
    else:
        print("4/4  embed package skipped")

    if not os.environ.get("CAD_STUDIO_KEEP_METADATA"):
        for f in out.rglob("*.png"):
            if "_work" not in f.parts:
                strip_png_metadata(f)
    write_manifest(out, src, name, job, res, time.time() - t0)
    if not os.environ.get("CAD_STUDIO_KEEP_WORK"):
        for f in work.glob("*.glb"):
            f.unlink()
    print(f"\ndone in {time.time() - t0:.0f}s -> {out}")
    for f in sorted(out.rglob("*")):
        if f.is_file() and "_work" not in f.parts and "turntable_frames" not in f.parts:
            print(f"   {f.relative_to(out)}  ({f.stat().st_size / 1e6:.1f} MB)")


KEEP_PNG_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"pHYs", b"sBIT"}


def strip_png_metadata(path: Path):
    """Drop text chunks (render settings, part-name manifests) from a delivered PNG."""
    b = path.read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        return
    out, i = bytearray(b[:8]), 8
    while i + 8 <= len(b):
        n = int.from_bytes(b[i:i + 4], "big")
        t = b[i + 4:i + 8]
        if t in KEEP_PNG_CHUNKS:
            out += b[i:i + 12 + n]
        i += 12 + n
        if t == b"IEND":
            break
    path.write_bytes(bytes(out))


def encode_turntable(out: Path, slug: str, transparent: bool):
    ff = shutil.which("ffmpeg")
    frames = out / "turntable_frames" / "frame_%04d.png"
    if not ff:
        print("  ffmpeg not found: turntable left as PNG frames in turntable_frames/")
        return
    mp4 = out / f"{slug}_turntable.mp4"
    run([ff, "-y", "-loglevel", "error", "-framerate", "30", "-i", str(frames), "-vf", "format=yuv420p",
         "-c:v", "libx264", "-crf", "18", "-preset", "slow", "-movflags", "+faststart", str(mp4)], quiet=True)
    webm = out / f"{slug}_turntable.webm"
    vf = ["-pix_fmt", "yuva420p"] if transparent else ["-pix_fmt", "yuv420p"]
    run([ff, "-y", "-loglevel", "error", "-framerate", "30", "-i", str(frames), *vf, "-c:v", "libvpx-vp9",
         "-b:v", "0", "-crf", "30", "-row-mt", "1", str(webm)], quiet=True)
    print(f"  turntable: {mp4.name}, {webm.name}{' (with alpha)' if transparent else ''}")


def write_manifest(out: Path, src: Path, name: str, job: dict, res: dict, secs: float):
    files = []
    for f in sorted(out.rglob("*")):
        if f.is_file() and "_work" not in f.parts:
            h = hashlib.sha256(f.read_bytes()).hexdigest()[:16] if f.stat().st_size < 200e6 else ""
            files.append({"file": str(f.relative_to(out)), "bytes": f.stat().st_size, "sha256_16": h})
    manifest = {
        "product": name, "source_file": src.name, "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(secs, 1), "render_device": res.get("device"), "model_size_mm": res.get("model_size_mm"),
        "settings": {k: job[k] for k in ("lighting", "shots", "size", "aspect", "samples", "exposure",
                                         "light_scale", "env_scale", "hdri_rotation", "dof_scale", "bevel_scale",
                                         "shadow", "backdrop")},
        "hdri": Path(job["hdri"]).name, "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (out / "_render_result.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
