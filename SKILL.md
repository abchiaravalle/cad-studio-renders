---
name: cad-studio-renders
description: "Use when turning a CAD file (STEP/IGES) into product shots, transparent PNGs, or a 3D web embed."
license: MIT
compatibility: "Needs Blender 4.2+ and uv on PATH; ffmpeg optional (turntable videos). macOS, Linux, Windows."
metadata:
  version: "1.0.0"
  tags: "cad, step, blender, product-rendering, 3d, model-viewer, ar, packshot"
---

# CAD studio renders

Turn a CAD assembly into the things a product page needs: photoreal studio shots, transparent
cut-outs, a turntable video, and a self-hosted 3D viewer with phone AR. A whole run is one command,
and everything runs locally. The GPU does the rendering; no AI image generation is involved, so the
pixels are the real geometry.

```
python3 scripts/studio.py model.step --name "Product Name"
```

## When to use

- "Render this STEP file", "make product shots / packshots from CAD", "photoreal render of this part"
- "Put a 3D model of this on our website", "make it rotatable", "AR view of the product"
- "Transparent PNG of the product", "cut-out for the catalogue", "turntable video"
- A 3D artist wants an editable Blender scene to finish by hand

Not this skill: modelling a part from scratch (use a CAD tool), image generation from a text prompt,
or rendering a scene that is not a product (interiors, characters).

## Requirements (check first)

```
blender --version       # 4.2 or newer. If missing: https://www.blender.org/download/
uv --version            # reads the CAD file. If missing: https://docs.astral.sh/uv/
ffmpeg -version         # only needed for --turntable videos
```

If Blender is installed but not on PATH, set `BLENDER=/path/to/blender`. A GPU (Apple Silicon,
NVIDIA, AMD) makes renders 5-20x faster; CPU works, just slower. First run downloads a CAD
library (~60 MB) and a studio HDRI (24 MB, CC0) into `~/.cache/cad-studio-renders/`.

## Workflow

1. **Map materials before rendering anything.** CAD files rarely carry real finishes.
   ```
   python3 scripts/studio.py model.step --map-only
   ```
   Read the table. Every part shows its preset and the reason. Lines ending `(review me)` are
   guesses. Fix wrong ones with a rules file (next section) and re-run `--map-only` until it's right.
2. **Preview** (about 10 s): `--preview` renders one 1000 px draft. Look at it before spending
   minutes on finals. Check: right way up, parts assembled (nothing floating), framing, materials.
3. **Final**: drop `--preview`. Defaults give a 2400 px hero shot on a backdrop, the same shot as a
   transparent PNG, an editable `.blend`, and the web embed package.
4. **Check the output** at full size (zoom into the busiest region) before handing it over.

## Fixing materials: the rules file

```json
{"rules": [
  {"match": "frame|base",    "preset": "powder_coat",  "color": "#2b2f33"},
  {"match": "^Cover",        "preset": "plastic_textured"},
  {"match": "motor",         "preset": "motor_housing"},
  {"match": "logo plate",    "preset": "anodised_color", "color": "#c8102e"},
  {"match": "spindle",       "preset": "steel_polished", "params": {"roughness": 0.05}},
  {"match": "safety guard",  "hide": true},
  {"match": "gantry",        "offset_mm": [0, 0, 80]}
]}
```

`match` is a case-insensitive regex tested against the part name, instance name and assembly path.
First matching rule wins. `params` overrides any preset value (roughness, metallic, coat,
anisotropic, bevel, bump...). `offset_mm` sets that part's position in the exploded view. Presets
live in `assets/presets/materials.json`; add new ones there.

Without rules, the automatic mapper uses, in order: part-name keywords (fasteners, bearings, linear
rails, ball screws, motors, rubber, glass...), then the CAD colour (saturated colours become paint,
parts sharing a colour share a finish), then size.

## Options that matter

| Flag | What it does |
|---|---|
| `--shots hero,front,side,top,iso,back,square,hero_low,detail` | Which camera angles (`assets/presets/shots.json`) |
| `--lighting studio_soft / high_key / dramatic / hdri_only` | Lighting rig (`assets/presets/lighting.json`) |
| `--backdrop 0.42` or `--backdrop "#1d1f22"` | Backdrop grey or colour |
| `--size 4000 --aspect 16:9` | Output size and shape |
| `--samples 768` | More samples = cleaner glossy detail, longer render |
| `--exposure 0.3`, `--light-scale 1.2`, `--env-scale 0.8` | Brightness |
| `--hdri photo_studio_01` or `--hdri my.exr`, `--hdri-rotation 40` | Reflection environment |
| `--dof 0` / `--dof 2` | Background blur off / less blur |
| `--bevel-scale 0` | Turn off edge rounding (rounding is what makes CAD look real) |
| `--detail-target "spindle|chuck"` with `--shots detail` | Close-up framed on matching parts |
| `--no-transparent`, `--no-backdrop`, `--no-embed`, `--no-blend` | Skip deliverables |
| `--clay` | Grey clay pass (lighting/shape check, design reviews) |
| `--passes` | Multilayer EXR: depth, normals, AO, mist, cryptomatte (object + material IDs) |
| `--exploded 0.4` | Exploded view (parts pushed from centre; `offset_mm` rules override) |
| `--turntable [--turntable-transparent]` | 360° MP4 + WebM (alpha optional) + PNG frames |
| `--up y` | Force the up axis if auto-detection guesses wrong |

## What you get

```
<model>_studio/
  hero.png                    16-bit, on the studio backdrop
  hero_transparent.png        16-bit RGBA, soft contact shadow kept (--shadow none to drop it)
  hero_clay.png, hero_exploded.png, hero_passes.exr       (when asked)
  <slug>_turntable.mp4 / .webm, turntable_frames/          (when asked)
  <slug>_scene.blend          lights, cameras, materials, backdrop, textures packed
  materials_map.json          every part -> preset + reason (edit-and-rerun via --rules)
  manifest.json               settings, timings, file list with sizes and hashes
  embed/                      self-hosted 3D viewer package (see below)
```

**The `.blend` is built for finishing by hand.** Collections: `Product` (one empty per part, so
parts can be moved or animated individually) and `Studio` (backdrop, lights, cameras). Every
camera is bound to a timeline marker, so scrubbing the timeline switches shots. Materials are
plain Principled BSDF node trees. Edge rounding is a Bevel shader node, so geometry stays exact.

## The embed package

`embed/` is fully static and makes zero external requests. It vendors Google `<model-viewer>`
(Apache-2.0) and the Draco decoder. Upload the folder anywhere and paste `snippet.html`, or iframe
`embed.html`. It includes:

- `assets/<slug>.glb`: Draco compressed, with materials matched to the renders
- `assets/<slug>.usdz`: iPhone/iPad AR via Quick Look
- a poster image
- the studio HDRI for reflections
- `index.html`: a demo page with copy buttons and render downloads

Hosting notes for WordPress, Shopify, Webflow and others are in `embed/README.md`.

## Pitfalls (each one bit during development)

- **Model on its side or upside down.** SolidWorks, Inventor, Creo and Fusion default to Y-up;
  most others are Z-up. The converter reads the STEP header to guess, and prints the guess
  (`up axis: Y (written by Solidworks)`). If it's wrong, pass `--up z` (or `y`, `-y`, `x`...).
- **Parts drifting apart / "accidentally exploded".** The cause was assembly transforms applied
  twice. The scene builder now checks that grouping parts doesn't move geometry and aborts if it
  would. If you edit the scene code, keep that check.
- **Flat, fake-looking metal.** Usually lighting, not materials: no environment means nothing for
  metal to reflect. Keep an HDRI on even with area lights (`--env-scale`, not 0).
- **Washed-out first render.** Light power is scaled to the model's size; if you add custom lights
  in `lighting.json`, power is per (model radius in metres)². Start low and use `--preview`.
- **Wrong-looking material guess.** Cryptic part names (`P-0042`, `ASSY_7_REV2`, transliterated
  non-English names) defeat keyword matching and fall back to colour/size. That is what
  `(review me)` flags. Write rules for those parts.
- **OCP 8 broke the reader** (`TDF_LabelSequence` moved). The CAD reader pins
  `cadquery-ocp-novtk<8`. Don't unpin without re-testing.
- **Web model looks different from the render.** glTF can't carry procedural bump, bevel or
  roughness noise, so the web model gets the base values only. That's expected; the poster image
  is the real render.
- **Huge assemblies** (>5M triangles): use `--quality web` for previews; final renders still
  work but need RAM (around 1 GB per 2M triangles in Blender).

## Verification before handing over

1. Open the hero PNG at 100 % and inspect the densest region: no black faces, no faceting on
   curves, no fireflies (white specks).
2. Open the transparent PNG over a dark and a light background: clean edges, shadow only under
   the product.
3. Serve `embed/` locally (`python3 -m http.server -d embed 8000`), open it, and confirm the model
   loads, rotates and has no console errors. Check it on a phone if AR matters.
4. If `materials_map.json` still has `review me` lines, say so in the hand-off.

## Files

- `scripts/studio.py`: entry point (CLI above)
- `scripts/cad_to_glb.py`: STEP/IGES/BREP to glTF with assembly names and CAD colours (uv script)
- `scripts/automap.py`: part to material decisions (pure Python, runnable alone)
- `scripts/blender_studio.py`: scene, materials, lights, cameras, renders, .blend, web export
- `scripts/package_embed.py`: the self-hosted viewer package
- `assets/presets/*.json`: materials, lighting rigs, camera shots
- `examples/make_demo_model.py`: builds the demo bench vise (original geometry, CC0)
