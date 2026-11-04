"""QA an embed package: loads, renders the model, no console errors, zero cross-origin requests,
desktop + phone screenshots.

    python3 qa_embed.py <embed_dir> <out_prefix>
"""
import http.server
import socketserver
import sys
import threading
import time
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

root = Path(sys.argv[1]).resolve()
prefix = sys.argv[2]
handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
handler.log_message = lambda *a, **k: None
http.server.SimpleHTTPRequestHandler.extensions_map.update({".glb": "model/gltf-binary", ".wasm": "application/wasm",
                                                           ".usdz": "model/vnd.usdz+zip"})
srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}/"

ok = True
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--enable-gpu", "--ignore-gpu-blocklist"])
    for page_name in ("index.html", "embed.html"):
        for label, vp, mob in (("desk", {"width": 1440, "height": 900}, False), ("m430", {"width": 430, "height": 932}, True)):
            ctx = b.new_context(viewport=vp, device_scale_factor=2 if mob else 1, is_mobile=mob, has_touch=mob)
            pg = ctx.new_page()
            errs, foreign, reqs = [], [], []
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)))

            def on_req(r):
                reqs.append(r.url)
                host = urlparse(r.url).hostname
                if r.url.startswith(("data:", "blob:")):
                    return
                if host not in ("127.0.0.1", "localhost"):
                    foreign.append(r.url)
            pg.on("request", on_req)
            t0 = time.time()
            pg.goto(base + page_name, wait_until="domcontentloaded")
            try:
                pg.wait_for_function("(() => { const m = document.querySelector('model-viewer'); return m && m.loaded; })()",
                                     timeout=60000)
                loaded = True
            except Exception:
                loaded = False
            secs = time.time() - t0
            pg.evaluate("document.querySelector('model-viewer') && document.querySelector('model-viewer').scrollIntoView()")
            time.sleep(2.5)
            shot = f"{prefix}_{page_name.split('.')[0]}_{label}.png"
            pg.screenshot(path=shot)
            info = pg.evaluate("""(() => { const m = document.querySelector('model-viewer');
                return m ? {w: m.clientWidth, h: m.clientHeight, ar: m.canActivateAR, src: m.src.split('/').pop()} : null; })()""")
            glb = [u for u in reqs if u.endswith(".glb")]
            draco = [u for u in reqs if "draco" in u]
            print(f"{page_name:10s} {label}: loaded={loaded} {secs:.1f}s viewer={info} glb={len(glb)} draco_files={len(draco)} "
                  f"foreign={len(foreign)} console_errors={len(errs)}")
            if foreign:
                print("   foreign:", foreign[:5])
            if errs:
                print("   errors:", errs[:5])
            visible = bool(info) and info["w"] > 200 and info["h"] > 150
            if not visible:
                print("   viewer is not visibly sized:", info)
            ok = ok and loaded and visible and not foreign and not errs
            ctx.close()
    b.close()
srv.shutdown()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
