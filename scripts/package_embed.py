"""Build the self-hosted 3D embed package.

Output (everything is relative, zero external requests at runtime):

    embed/
      index.html          demo page: copy-paste snippets + the viewer + a download list
      embed.html          bare full-window viewer, for <iframe> embedding
      snippet.html        the exact markup to paste into any page
      assets/
        <slug>.glb        Draco-compressed web model (materials matched to the renders)
        <slug>.usdz       iOS Quick Look AR (Safari: tap the AR button)
        <slug>-poster.webp / .jpg   placeholder shown before the model loads
        environment.hdr   studio lighting for reflections
      vendor/
        model-viewer.min.js     Google <model-viewer> (Apache-2.0)
        draco/                  Draco decoder (Apache-2.0)
        LICENSES.md
      stills/             the rendered PNGs, for the download list
      README.md           how to host it (any static host, WordPress, Shopify, Webflow, Squarespace)
"""
from __future__ import annotations

import html
import io
import json
import shutil
import tarfile
import urllib.request
from pathlib import Path

MODEL_VIEWER = "4.4.0"
THREE = "0.187.0"  # only the Draco decoder is taken from three's tarball
NPM = "https://registry.npmjs.org/{pkg}/-/{name}-{ver}.tgz"
CACHE = Path.home() / ".cache" / "cad-studio-renders" / "vendor"


def _tgz(pkg, name, ver):
    dest = CACHE / f"{name}-{ver}.tgz"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = NPM.format(pkg=pkg, name=name, ver=ver)
        print(f"  downloading {url}")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cad-studio-renders"}),
                                    timeout=180) as r:
            dest.write_bytes(r.read())
    return tarfile.open(dest, "r:gz")


def vendor_into(vdir: Path):
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "draco").mkdir(exist_ok=True)
    with _tgz("@google/model-viewer", "model-viewer", MODEL_VIEWER) as t:
        (vdir / "model-viewer.min.js").write_bytes(t.extractfile("package/dist/model-viewer.min.js").read())
        mv_license = t.extractfile("package/LICENSE").read().decode()
    with _tgz("three", "three", THREE) as t:
        for f in ("draco_decoder.js", "draco_wasm_wrapper.js", "draco_decoder.wasm"):
            (vdir / "draco" / f).write_bytes(t.extractfile(f"package/examples/jsm/libs/draco/gltf/{f}").read())
    (vdir / "LICENSES.md").write_text(
        f"# Third-party code in this folder\n\n"
        f"- `model-viewer.min.js`: Google model-viewer {MODEL_VIEWER}, Apache License 2.0, "
        f"https://github.com/google/model-viewer\n"
        f"- `draco/`: Draco 3D geometry decoder, Apache License 2.0, https://github.com/google/draco "
        f"(files taken from three.js {THREE}, MIT)\n\n"
        f"## model-viewer LICENSE\n\n```\n{mv_license}\n```\n")


def poster(png: Path, out_webp: Path, out_jpg: Path, width=1600, bg="#e9eaec"):
    """Make a light poster image.  Uses Blender-free tooling: sips (macOS) or ffmpeg, else copies PNG."""
    import shutil as sh
    import subprocess
    ff = sh.which("ffmpeg")
    if ff:
        # flatten transparent renders onto the viewer background so the poster has no black box
        flat = (f"color=c=0x{bg.lstrip('#')}:s=16x16:d=1[bg];[bg][0:v]scale2ref[bg2][fg];"
                f"[bg2][fg]overlay=format=auto:shortest=1,scale={width}:-2,format=yuv420p")
        enc = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libwebp" in enc:
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(png), "-filter_complex", flat,
                            "-frames:v", "1", "-c:v", "libwebp", "-quality", "82", str(out_webp)], check=False)
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(png), "-filter_complex", flat,
                        "-frames:v", "1", "-q:v", "3", str(out_jpg)], check=False)
    if not out_jpg.exists():
        shutil.copy(png, out_jpg.with_suffix(".png"))
        return None, out_jpg.with_suffix(".png").name
    return (out_webp.name if out_webp.exists() else None), out_jpg.name


TEMPLATE_VIEWER = """<model-viewer
  src="{base}assets/{slug}.glb"
  ios-src="{base}assets/{slug}.usdz"
{poster_attr}  environment-image="{base}assets/environment.hdr"
  alt="{alt}"
  camera-controls touch-action="pan-y"
  auto-rotate auto-rotate-delay="2500" rotation-per-second="18deg" interaction-prompt="auto"
  camera-orbit="32deg 76deg auto" min-camera-orbit="auto 5deg auto" max-camera-orbit="auto 92deg auto"
  shadow-intensity="1" shadow-softness="0.9" exposure="1" tone-mapping="agx"
  ar ar-modes="webxr scene-viewer quick-look"
  loading="lazy" reveal="auto"
  style="display:block;width:100%;height:auto;aspect-ratio:4/3;background:{bg};--poster-color:{bg};">
</model-viewer>"""


def build(pkg: Path, slug: str, name: str, poster_png: Path | None, hdri: Path, stills: list[Path],
          turntable: Path | None = None, bg="#e9eaec"):
    pkg.mkdir(parents=True, exist_ok=True)
    assets = pkg / "assets"
    assets.mkdir(exist_ok=True)
    vendor_into(pkg / "vendor")
    shutil.copy(hdri, assets / "environment.hdr")
    poster_name = None
    if poster_png and poster_png.exists():
        webp, poster_name = poster(poster_png, assets / f"{slug}-poster.webp", assets / f"{slug}-poster.jpg", bg=bg)
    sdir = pkg / "stills"
    sdir.mkdir(exist_ok=True)
    import subprocess
    ff = shutil.which("ffmpeg")
    for s in stills:
        if not s.exists():
            continue
        # web-weight copies: full-res 16-bit originals stay in the main output folder
        if ff and "transparent" in s.stem:
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(s), "-vf", "scale='min(2000,iw)':-2",
                            "-pix_fmt", "rgba", str(sdir / f"{s.stem}.png")], check=False)
        elif ff:
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(s), "-vf", "scale='min(2000,iw)':-2",
                            "-q:v", "2", str(sdir / f"{s.stem}.jpg")], check=False)
        else:
            shutil.copy(s, sdir / s.name)
    if turntable and turntable.exists():
        shutil.copy(turntable, sdir / turntable.name)

    alt = html.escape(f"3D model of {name}")
    viewer_rel = TEMPLATE_VIEWER.format(base="", slug=slug, alt=alt, bg=bg,
                                        poster_attr=f'  poster="assets/{poster_name}"\n' if poster_name else "")
    viewer_abs = TEMPLATE_VIEWER.format(base="https://YOUR-SITE.com/3d/" + slug + "/", slug=slug, alt=alt, bg=bg,
                                        poster_attr=(f'  poster="https://YOUR-SITE.com/3d/{slug}/assets/{poster_name}"\n'
                                                     if poster_name else ""))
    script_tag = '<script type="module" src="{base}vendor/model-viewer.min.js"></script>'
    draco = ('<script>self.ModelViewerElement = self.ModelViewerElement || {{}};\n'
             '  self.ModelViewerElement.dracoDecoderLocation = "{base}vendor/draco/";</script>')
    head_rel = draco.format(base="") + "\n" + script_tag.format(base="")
    head_abs = (draco.format(base="https://YOUR-SITE.com/3d/" + slug + "/") + "\n"
                + script_tag.format(base="https://YOUR-SITE.com/3d/" + slug + "/"))

    (pkg / "snippet.html").write_text(
        f"<!-- {name}: 3D viewer. Upload this whole folder to https://YOUR-SITE.com/3d/{slug}/ then paste: -->\n"
        f"{head_abs}\n{viewer_abs}\n")

    (pkg / "embed.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>{html.escape(name)}: 3D</title>
<style>html,body{{margin:0;height:100%;background:{bg}}}model-viewer{{width:100%!important;height:100%!important;aspect-ratio:auto!important}}</style>
{head_rel}
</head><body>
{viewer_rel}
</body></html>
""")

    files = sorted(p for p in sdir.iterdir() if p.is_file())
    dl = "\n".join(f'      <li><a href="stills/{html.escape(f.name)}" download>{html.escape(f.name)}</a>'
                   f' <span>{f.stat().st_size / 1e6:.1f} MB</span></li>' for f in files)

    def tile(f):
        label = f.stem.replace("_", " ").replace("-", " ")
        src = f"stills/{html.escape(f.name)}"
        if f.suffix == ".mp4":
            media = f'<video src="{src}" autoplay muted loop playsinline preload="metadata"></video>'
        else:
            media = f'<img src="{src}" alt="{html.escape(label)}" loading="lazy">'
        cls = "tile alpha" if "transparent" in f.stem else "tile"
        return f'      <figure class="{cls}"><a href="{src}">{media}</a><figcaption>{html.escape(label)}</figcaption></figure>'
    gallery = "\n".join(tile(f) for f in files if f.suffix in (".jpg", ".png", ".webp", ".mp4"))
    (pkg / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="data:,">
<title>{html.escape(name)}: 3D viewer and renders</title>
<style>
  :root{{--ink:#17191c;--mute:#666b72;--line:#dcdee1}}
  *{{box-sizing:border-box}}
  body{{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color:var(--ink);background:#fff}}
  main{{max-width:1100px;margin:0 auto;padding:40px 24px 80px}}
  h1{{font-size:clamp(28px,4vw,44px);margin:0 0 6px;letter-spacing:-.01em}}
  h2{{font-size:20px;margin:48px 0 10px}}
  p{{color:var(--mute);margin:0 0 16px}}
  pre{{background:#f4f5f6;border:1px solid var(--line);padding:16px;overflow:auto;font-size:13px;line-height:1.45}}
  ul{{padding:0;list-style:none}} li{{padding:8px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}}
  li span{{color:var(--mute)}} a{{color:var(--ink)}}
  .viewer{{border:1px solid var(--line)}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin:12px 0 8px}}
  .tile{{margin:0;border:1px solid var(--line);background:#f4f5f6}}
  .tile img,.tile video{{display:block;width:100%;aspect-ratio:4/3;object-fit:contain}}
  .tile.alpha{{background:#fff conic-gradient(#e8e9eb 25%,#fff 0 50%,#e8e9eb 0 75%,#fff 0) 0 0/20px 20px}}
  .tile figcaption{{font-size:13px;color:var(--mute);padding:8px 10px;background:#fff;border-top:1px solid var(--line);text-transform:capitalize}}
  button.copy{{float:right;font:inherit;font-size:13px;padding:4px 10px;border:1px solid var(--line);background:#fff;cursor:pointer}}
</style>
{head_rel}
</head><body><main>
  <h1>{html.escape(name)}</h1>
  <p>Drag to rotate, scroll or pinch to zoom. On a phone, tap the AR button to place it in your room.</p>
  <div class="viewer">
{viewer_rel}
  </div>

  <h2>Put it on your website</h2>
  <p>1. Upload this whole folder to your site, for example to <code>/3d/{slug}/</code>.
     2. Paste this where the viewer should appear. Change <code>YOUR-SITE.com</code> to your domain.</p>
  <button class="copy" data-for="snip">Copy</button>
  <pre id="snip">{html.escape(head_abs + chr(10) + viewer_abs)}</pre>
  <p>Or embed the viewer page in a frame:</p>
  <button class="copy" data-for="ifr">Copy</button>
  <pre id="ifr">{html.escape(f'<iframe src="https://YOUR-SITE.com/3d/{slug}/embed.html" title="{name} 3D" style="width:100%;aspect-ratio:4/3;border:0" loading="lazy" allow="xr-spatial-tracking; fullscreen"></iframe>')}</pre>

  <h2>Renders</h2>
  <p>Click any image for the full-size file. Transparent cut-outs are shown on a checkerboard.</p>
  <div class="grid">
{gallery}
  </div>
  <h2>Downloads</h2>
  <ul>
{dl}
  </ul>
</main>
<script>
document.querySelectorAll('button.copy').forEach(b=>b.onclick=()=>{{
  navigator.clipboard.writeText(document.getElementById(b.dataset.for).textContent);
  b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1500);}});
</script>
</body></html>
""")

    (pkg / "README.md").write_text(f"""# {name}: self-hosted 3D viewer

Everything here is static and self-contained. No CDN, no tracking, no external requests.

| File | What it is |
|---|---|
| `index.html` | Demo page with the viewer, copy-paste snippets and render downloads |
| `embed.html` | Bare full-window viewer, for an `<iframe>` |
| `snippet.html` | The two tags to paste into any page |
| `assets/{slug}.glb` | Web model (Draco compressed) |
| `assets/{slug}.usdz` | iPhone / iPad AR (Quick Look) |
| `assets/environment.hdr` | Studio lighting for reflections (CC0, Poly Haven) |
| `vendor/` | model-viewer + Draco decoder, see `vendor/LICENSES.md` |
| `stills/` | Rendered images |

## Hosting

Upload the folder as-is to any static host (your web server, Cloudflare Pages, Netlify, S3, GitHub Pages).
Then paste `snippet.html` where you want the viewer, with `YOUR-SITE.com/3d/{slug}/` pointing at the folder.

- **WordPress:** upload the folder by SFTP into `wp-content/uploads/3d/{slug}/`, then add a Custom HTML block with the snippet.
- **Shopify:** upload `{slug}.glb` as a 3D model on the product (Shopify hosts it natively), or put the folder on any static host and use the snippet in a Custom Liquid section.
- **Webflow / Squarespace / Wix:** these can't host the folder. Host it on any static host and use the `embed.html` iframe snippet.

Your server should send `.glb` as `model/gltf-binary`, `.usdz` as `model/vnd.usdz+zip` and `.wasm` as `application/wasm`. Most hosts already do.

## Tuning

All viewer options are attributes on `<model-viewer>`: `camera-orbit`, `exposure`, `shadow-intensity`, `auto-rotate`, `environment-image`. Reference: https://modelviewer.dev/docs/
""")
    total = sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file())
    print(f"  embed package: {pkg} ({total / 1e6:.1f} MB)")
    return pkg
