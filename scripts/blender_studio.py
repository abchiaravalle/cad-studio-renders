"""Blender side of cad-studio-renders.  Runs inside Blender:

    blender -b --factory-startup --python blender_studio.py -- job.json

job.json (written by studio.py) drives everything: which GLB, the per-part material table,
presets, shots, lighting, and which deliverables to produce.  One Blender session builds the
scene once and renders every requested output, so turntables and multi-shot runs reuse the
loaded geometry.

Modes:
  "render" -> stills (backdrop / transparent / clay / exploded / EXR passes), turntable frames,
              editable .blend
  "web"    -> web GLB (Draco) + USDZ for AR, materials matched to the renders
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time

import bmesh
import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

JOB = json.load(open(sys.argv[sys.argv.index("--") + 1]))
T0 = time.time()


def log(*a):
    print(f"[studio {time.time() - T0:6.1f}s]", *a, flush=True)


# ------------------------------------------------------------------ helpers
def srgb_hex_to_linear(h):
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c)


def set_input(node, names, value):
    for n in names if isinstance(names, (list, tuple)) else [names]:
        if n in node.inputs:
            node.inputs[n].default_value = value
            return True
    return False


def world_bounds(objs):
    lo = Vector((1e18,) * 3)
    hi = Vector((-1e18,) * 3)
    for o in objs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            lo = Vector(map(min, lo, w))
            hi = Vector(map(max, hi, w))
    return lo, hi


def corners(objs):
    pts = []
    for o in objs:
        mw = o.matrix_world
        pts.extend(mw @ Vector(c) for c in o.bound_box)
    return pts


# ------------------------------------------------------------------ scene setup
bpy.ops.wm.read_factory_settings(use_empty=True)
scn = bpy.context.scene
scn.unit_settings.system = "METRIC"

bpy.ops.import_scene.gltf(filepath=JOB["glb"], merge_vertices=False)
meshes = [o for o in scn.objects if o.type == "MESH"]
log("imported", len(meshes), "mesh objects")


def inst_of(o):
    for key in ("instance",):
        if key in o.keys():
            return str(o[key])
    p = o.parent
    while p is not None:
        if "instance" in p.keys():
            return str(p["instance"])
        p = p.parent
    return re.sub(r"#\d+$", "", o.name)


# bake world transforms (CAD mm -> m, up-axis) into the meshes
info = {o.name: inst_of(o) for o in meshes}
for o in meshes:
    mw = o.matrix_world.copy()
    o.parent = None
    o.matrix_world = mw
for o in list(scn.objects):
    if o.type == "EMPTY":
        bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
try:
    bpy.ops.object.make_single_user(object=True, obdata=True)
except RuntimeError:
    pass
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

# hidden parts
decisions = {d["name"]: d for d in JOB.get("materials", [])}
for o in list(meshes):
    d = decisions.get(info[o.name])
    if d and d.get("hide"):
        meshes.remove(o)
        bpy.data.objects.remove(o, do_unlink=True)

# ground the model: centred on XY, resting on Z = 0
lo, hi = world_bounds(meshes)
shift = Vector((-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z))
for o in meshes:
    o.location += shift
lo, hi = world_bounds(meshes)
SIZE = hi - lo
DIAG = SIZE.length
R = DIAG / 2
CTR = (lo + hi) / 2
log(f"model size {SIZE.x * 1000:.0f} x {SIZE.y * 1000:.0f} x {SIZE.z * 1000:.0f} mm, radius {R:.3f} m")

# hierarchy: Product (turntable pivot) > part empties (explode) > meshes
col_model = bpy.data.collections.new("Product")
scn.collection.children.link(col_model)
product = bpy.data.objects.new("Product", None)
product.empty_display_type = "ARROWS"
product.empty_display_size = R * 0.3
col_model.objects.link(product)
part_empties = {}
by_inst = {}
for o in meshes:
    by_inst.setdefault(info[o.name], []).append(o)
for inst, objs in by_inst.items():
    plo, phi = world_bounds(objs)
    e = bpy.data.objects.new(inst[:60], None)
    e.empty_display_size = max((phi - plo).length * 0.1, 0.001)
    e.location = (plo + phi) / 2
    col_model.objects.link(e)
    e.parent = product
    e["instance"] = inst
    part_empties[inst] = e
bpy.context.view_layer.update()  # parents need real world matrices before children attach
for inst, objs in by_inst.items():
    e = part_empties[inst]
    inv = e.matrix_world.inverted()
    for o in objs:
        for c in o.users_collection:
            c.objects.unlink(o)
        col_model.objects.link(o)
        o.parent = e
        o.matrix_parent_inverse = inv  # keep the part exactly where it was
        o["instance"] = inst
bpy.context.view_layer.update()
_chk_lo, _chk_hi = world_bounds(meshes)
assert (_chk_hi - _chk_lo - SIZE).length < 1e-6 * max(DIAG, 1), "part grouping moved geometry"

# ------------------------------------------------------------------ materials
PRESETS = JOB["presets"]["materials"]


def build_material(name, spec, color=None, params=None, web=False):
    spec = {**spec, **(params or {})}
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    N, L = nt.nodes, nt.links
    b = N.get("Principled BSDF")
    base = srgb_hex_to_linear(color) if color else tuple(spec.get("base", (0.5, 0.5, 0.5)))
    set_input(b, "Base Color", (*base, 1))
    set_input(b, "Metallic", spec.get("metallic", 0.0))
    set_input(b, "Roughness", spec.get("roughness", 0.5))
    set_input(b, ["Specular IOR Level", "Specular"], spec.get("spec", 0.5))
    if not web:
        set_input(b, ["Anisotropic"], spec.get("anisotropic", 0.0))
    if spec.get("coat"):
        set_input(b, ["Coat Weight", "Clearcoat"], spec["coat"])
        set_input(b, ["Coat Roughness", "Clearcoat Roughness"], 0.08)
    if spec.get("transmission"):
        set_input(b, ["Transmission Weight", "Transmission"], spec["transmission"])
        set_input(b, "IOR", spec.get("ior", 1.45))
    m.diffuse_color = (*base, 1)  # viewport colour
    m.metallic = spec.get("metallic", 0.0)
    m.roughness = spec.get("roughness", 0.5)
    if web:
        return m  # glTF/USD can only carry the BSDF constants
    tc = N.new("ShaderNodeTexCoord")
    normal_out = None
    if spec.get("bevel"):
        bv = N.new("ShaderNodeBevel")
        bv.samples = 6
        bv.inputs["Radius"].default_value = spec["bevel"] * JOB.get("bevel_scale", 1.0)
        normal_out = bv.outputs["Normal"]
    if spec.get("rough_var"):
        nz = N.new("ShaderNodeTexNoise")
        nz.inputs["Scale"].default_value = spec.get("var_scale", 40)
        nz.inputs["Detail"].default_value = 6
        L.new(tc.outputs["Object"], nz.inputs["Vector"])
        mr = N.new("ShaderNodeMapRange")
        r0 = spec.get("roughness", 0.5)
        mr.inputs["To Min"].default_value = max(0.0, r0 - spec["rough_var"])
        mr.inputs["To Max"].default_value = min(1.0, r0 + spec["rough_var"])
        L.new(nz.outputs["Fac"], mr.inputs["Value"])
        L.new(mr.outputs["Result"], b.inputs["Roughness"])
    if spec.get("wood"):
        wv = N.new("ShaderNodeTexWave")
        wv.wave_type = "RINGS"
        wv.inputs["Scale"].default_value = 3.0
        wv.inputs["Distortion"].default_value = 6.0
        wv.inputs["Detail"].default_value = 4
        L.new(tc.outputs["Object"], wv.inputs["Vector"])
        mix = N.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.inputs[6].default_value = (*base, 1)
        mix.inputs[7].default_value = (base[0] * 0.55, base[1] * 0.5, base[2] * 0.45, 1)
        L.new(wv.outputs["Fac"], mix.inputs[0])
        L.new(mix.outputs[2], b.inputs["Base Color"])
    if spec.get("bump") or spec.get("brush"):
        bp = N.new("ShaderNodeBump")
        bp.inputs["Distance"].default_value = 0.00005
        nz2 = N.new("ShaderNodeTexNoise")
        nz2.inputs["Detail"].default_value = 3
        if spec.get("bump"):
            nz2.inputs["Scale"].default_value = spec["bump"][0]
            L.new(tc.outputs["Object"], nz2.inputs["Vector"])
            bp.inputs["Strength"].default_value = spec["bump"][1]
        else:
            mp = N.new("ShaderNodeMapping")
            mp.inputs["Scale"].default_value = spec["brush"][0]
            L.new(tc.outputs["Object"], mp.inputs["Vector"])
            nz2.inputs["Scale"].default_value = 1.0
            L.new(mp.outputs["Vector"], nz2.inputs["Vector"])
            bp.inputs["Strength"].default_value = spec["brush"][1]
        L.new(nz2.outputs["Fac"], bp.inputs["Height"])
        if normal_out is not None:
            L.new(normal_out, bp.inputs["Normal"])
        normal_out = bp.outputs["Normal"]
    if normal_out is not None:
        L.new(normal_out, b.inputs["Normal"])
        if spec.get("coat") and "Coat Normal" in b.inputs:
            L.new(normal_out, b.inputs["Coat Normal"])
    return m


WEB = JOB["mode"] == "web"
mat_cache = {}
assigned = 0
multi = {k for k, v in by_inst.items() if len(v) > 1}
for o in meshes:
    key = f'{o["instance"]}#{int(o["solid"])}' if ("solid" in o.keys() and o["instance"] in multi) else None
    d = (decisions.get(key) if key else None) or decisions.get(o["instance"]) or \
        {"preset": "cad_color", "color": None, "params": {}}
    preset = d.get("preset") or "cad_color"
    spec = PRESETS.get(preset) or PRESETS["cad_color"]
    color = d.get("color") if (d.get("color") and (spec.get("tint") or d.get("force_color"))) else d.get("color")
    key = (preset, color, json.dumps(d.get("params") or {}, sort_keys=True))
    if key not in mat_cache:
        label = spec.get("label", preset) + (f" {color}" if color else "")
        mat_cache[key] = build_material(label, spec, color, d.get("params"), web=WEB)
    o.data.materials.clear()
    o.data.materials.append(mat_cache[key])
    for poly in o.data.polygons:
        poly.use_smooth = True
    assigned += 1
log("materials:", len(mat_cache), "unique across", assigned, "objects")

# ------------------------------------------------------------------ web export (separate mode)
if WEB:
    out = JOB["web"]
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=out["glb"], export_format="GLB", export_apply=True,
                              export_draco_mesh_compression_enable=out.get("draco", True),
                              export_draco_mesh_compression_level=7,
                              export_draco_position_quantization=16,
                              export_draco_normal_quantization=12,
                              export_extras=False, export_yup=True)
    log("web glb", os.path.getsize(out["glb"]))
    if out.get("usdz"):
        try:
            bpy.ops.wm.usd_export(filepath=out["usdz"], export_materials=True,
                                  generate_preview_surface=True, export_animation=False,
                                  export_cameras=False, export_lights=False, triangulate_meshes=True,
                                  convert_world_material=False)
            log("usdz", os.path.getsize(out["usdz"]))
        except Exception as e:  # keep the GLB even if USD export is unavailable
            log("usdz export failed:", e)
    sys.exit(0)

# ------------------------------------------------------------------ backdrop sweep
def make_sweep():
    bm = bmesh.new()
    W, front, rad, H, back = 26 * R, 12 * R, 3.0 * R, 13 * R, 2.6 * R
    prof = [(-front + i * (front + back) / 8, 0.0) for i in range(9)]
    prof += [(back + rad * math.sin((math.pi / 2) * i / 20), rad - rad * math.cos((math.pi / 2) * i / 20))
             for i in range(1, 21)]
    prof.append((back + rad, H))
    rows = [[bm.verts.new((x, y, z)) for (y, z) in prof] for x in (-W / 2, W / 2)]
    for i in range(len(prof) - 1):
        bm.faces.new((rows[0][i], rows[1][i], rows[1][i + 1], rows[0][i + 1]))
    me = bpy.data.meshes.new("Backdrop")
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    ob = bpy.data.objects.new("Backdrop", me)
    col = bpy.data.collections.new("Studio")
    scn.collection.children.link(col)
    col.objects.link(ob)
    mat = bpy.data.materials.new("Backdrop paper")
    mat.use_nodes = True
    pb = mat.node_tree.nodes["Principled BSDF"]
    set_input(pb, "Roughness", 0.85)
    set_input(pb, ["Specular IOR Level", "Specular"], 0.2)
    me.materials.append(mat)
    return ob, mat, col


sweep, sweep_mat, col_studio = make_sweep()


def set_backdrop(value):
    if isinstance(value, str):
        rgb = srgb_hex_to_linear(value)
    else:
        rgb = (value, value, value)
    set_input(sweep_mat.node_tree.nodes["Principled BSDF"], "Base Color", (*rgb, 1))


# ------------------------------------------------------------------ world + lights
world = bpy.data.worlds.new("Studio HDRI")
scn.world = world
world.use_nodes = True
wn, wl = world.node_tree.nodes, world.node_tree.links
env = wn.new("ShaderNodeTexEnvironment")
env.image = bpy.data.images.load(JOB["hdri"])
env_map = wn.new("ShaderNodeMapping")
env_tc = wn.new("ShaderNodeTexCoord")
wl.new(env_tc.outputs["Generated"], env_map.inputs["Vector"])
wl.new(env_map.outputs["Vector"], env.inputs["Vector"])
wl.new(env.outputs["Color"], wn["Background"].inputs["Color"])

LIGHT = JOB["presets"]["lighting"][JOB["lighting"]]
light_objs = []
for spec in LIGHT["lights"]:
    ld = bpy.data.lights.new(spec["name"], "AREA")
    ld.shape = "RECTANGLE"
    ld.size, ld.size_y = spec["size"][0] * R, spec["size"][1] * R
    ld.energy = spec["power"] * R * R * JOB.get("light_scale", 1.0)
    ld.color = spec.get("color", (1, 1, 1))
    ld.spread = math.radians(110)
    lo_ = bpy.data.objects.new(spec["name"], ld)
    col_studio.objects.link(lo_)
    lo_.visible_camera = False
    light_objs.append((lo_, spec))


def place_rig(cam_az, aim):
    """Lights, HDRI and backdrop follow the camera so every shot is lit the same way."""
    for lo_, spec in light_objs:
        a = cam_az + math.radians(spec["az"])
        e = math.radians(spec["el"])
        lo_.location = aim + Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e))) * spec["dist"] * R
        lo_.rotation_euler = (aim - lo_.location).to_track_quat("-Z", "Y").to_euler()
    env_map.inputs["Rotation"].default_value[2] = cam_az + math.radians(LIGHT.get("env_rot", 200) + JOB.get("hdri_rotation", 0))
    sweep.rotation_euler = (0, 0, cam_az)
    sweep.location = (0, 0, 0)


wn["Background"].inputs["Strength"].default_value = LIGHT.get("env_strength", 0.1) * JOB.get("env_scale", 1.0)
set_backdrop(JOB.get("backdrop") if JOB.get("backdrop") is not None else LIGHT.get("backdrop", 0.42))

# ------------------------------------------------------------------ render settings
scn.render.engine = "CYCLES"
cy = scn.cycles
device = "CPU"
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dt in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = dt
        except TypeError:
            continue
        prefs.get_devices()
        devs = [d for d in prefs.devices if d.type == dt]
        if devs:
            for d in prefs.devices:
                d.use = d.type == dt
            device = dt
            break
except Exception as e:
    log("GPU setup failed, using CPU:", e)
cy.device = "GPU" if device != "CPU" else "CPU"
log("render device:", device)
cy.use_denoising = True
cy.adaptive_threshold = JOB.get("noise_threshold", 0.01)
cy.max_bounces = 16
cy.glossy_bounces = 10
cy.diffuse_bounces = 6
cy.transmission_bounces = 12
cy.caustics_reflective = False
cy.caustics_refractive = False
cy.blur_glossy = 0.5
scn.render.use_persistent_data = True
vs = scn.view_settings
try:
    vs.view_transform = "AgX"
    vs.look = LIGHT.get("look", "AgX - Medium High Contrast")
except TypeError:
    vs.view_transform = "Filmic"
vs.exposure = LIGHT.get("exposure", 0.0) + JOB.get("exposure", 0.0)
vl = scn.view_layers[0]
for _attr in dir(scn.render):  # no dates, host names or file paths baked into image metadata
    if _attr.startswith("use_stamp") and _attr not in ("use_stamp",):
        try:
            setattr(scn.render, _attr, False)
        except (AttributeError, TypeError):
            pass
scn.render.use_stamp = False

# ------------------------------------------------------------------ cameras
SHOTS = JOB["presets"]["shots"]
cameras = {}


def aspect_dims(aspect, long_edge):
    a, b = (float(x) for x in aspect.split(":"))
    if a >= b:
        return long_edge, int(round(long_edge * b / a / 2) * 2)
    return int(round(long_edge * a / b / 2) * 2), long_edge


def fit_camera(name, spec, targets, rx, ry, pts=None):
    cam = cameras.get(name)
    if cam is None:
        cd = bpy.data.cameras.new(f"Cam {name}")
        cam = bpy.data.objects.new(f"Cam {name}", cd)
        col_studio.objects.link(cam)
        cameras[name] = cam
    cd = cam.data
    scn.render.resolution_x, scn.render.resolution_y = rx, ry
    scn.camera = cam
    az, el = math.radians(spec["az"]), math.radians(spec["el"])
    d = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
    tlo, thi = world_bounds(targets)
    tsize = (thi - tlo).length
    aim = (tlo + thi) / 2
    pts = pts or corners(targets)
    margin = spec.get("margin", 0.1)
    cd.clip_start = max(tsize * 0.002, 0.0005)
    cd.clip_end = R * 200
    cd.shift_x = cd.shift_y = 0
    if spec.get("ortho"):
        cd.type = "ORTHO"
        cam.location = aim + d * R * 10
        cam.rotation_euler = (aim - cam.location).to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()
        rot = cam.matrix_world.to_3x3()
        right, up = rot @ Vector((1, 0, 0)), rot @ Vector((0, 1, 0))
        xs = [(p - aim).dot(right) for p in pts]
        ys = [(p - aim).dot(up) for p in pts]
        cam.location += right * (max(xs) + min(xs)) / 2 + up * (max(ys) + min(ys)) / 2
        sx = (max(xs) - min(xs)) / (1 - 2 * margin)
        sy = (max(ys) - min(ys)) / (1 - 2 * margin)
        cd.ortho_scale = max(sx, sy * rx / ry)
        cd.dof.use_dof = False
        return cam, spec["az"]
    cd.type = "PERSP"
    cd.lens = spec.get("lens", 85)
    cd.sensor_width = 36
    cd.sensor_fit = "AUTO"
    dist = tsize * 3
    for _ in range(10):
        cam.location = aim + d * dist
        cam.rotation_euler = (aim - cam.location).to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()
        pp = [world_to_camera_view(scn, cam, p) for p in pts]
        if min(p.z for p in pp) <= 0:
            dist *= 1.5
            continue
        xs, ys = [p.x for p in pp], [p.y for p in pp]
        span = max((max(xs) - min(xs)) / (1 - 2 * margin), (max(ys) - min(ys)) / (1 - 2 * margin))
        dist *= span ** 0.85 if abs(span - 1) > 0.002 else 1
    pp = [world_to_camera_view(scn, cam, p) for p in pts]
    xs, ys = [p.x for p in pp], [p.y for p in pp]
    asp = rx / ry
    # centre with lens shift: keeps verticals vertical, like a shift lens on a product set
    cd.shift_x = ((max(xs) + min(xs)) / 2 - 0.5) * (1 if asp >= 1 else asp)
    cd.shift_y = ((max(ys) + min(ys)) / 2 - 0.5) * (1 / asp if asp >= 1 else 1)
    fstop = spec.get("fstop", 0) * JOB.get("dof_scale", 1.0)
    if fstop > 0:
        cd.dof.use_dof = True
        cd.dof.aperture_fstop = fstop / max(tsize, 1e-3)  # same look whatever the product size
        focus = aim.copy()
        focus -= d * (tsize * 0.15)  # focus a little in front of centre (nearest features)
        cd.dof.focus_distance = (focus - cam.location).length
    else:
        cd.dof.use_dof = False
    return cam, spec["az"]


# ------------------------------------------------------------------ render helpers
OUT = JOB["out_dir"]
os.makedirs(OUT, exist_ok=True)
outputs = []


def configure_passes(on):
    vl.use_pass_z = on
    vl.use_pass_normal = on
    vl.use_pass_mist = on
    vl.use_pass_ambient_occlusion = on
    vl.use_pass_cryptomatte_object = on
    vl.use_pass_cryptomatte_material = on
    vl.use_pass_diffuse_color = on
    vl.cycles.use_pass_shadow_catcher = on and scn.render.film_transparent
    if on:
        scn.world.mist_settings.start = 0
        scn.world.mist_settings.depth = R * 20


def save(path, fmt="PNG", depth="16", mode="RGBA"):
    im = scn.render.image_settings
    if "media_type" in im.bl_rna.properties:  # Blender 5+: multilayer EXR lives behind media_type
        im.media_type = "MULTI_LAYER_IMAGE" if fmt == "OPEN_EXR_MULTILAYER" else "IMAGE"
    im.file_format = fmt
    if fmt == "PNG":
        im.color_depth = depth
        im.color_mode = mode
        im.compression = 15
    elif fmt == "OPEN_EXR_MULTILAYER":
        im.color_depth = "16"
        im.exr_codec = "DWAA"
        im.color_mode = "RGBA"
    elif fmt == "JPEG":
        im.color_mode = "RGB"
        im.quality = 92
    bpy.data.images["Render Result"].save_render(path, scene=scn)
    outputs.append(os.path.relpath(path, OUT))
    log("wrote", os.path.relpath(path, OUT))


def back_lights_skip_floor(on):
    """Lights behind the product (rims) throw long streaks across the floor. In transparent
    cut-outs those streaks read as smudges, so there they light the product only."""
    for lo_, spec in light_objs:
        if abs(spec.get("az", 0)) >= 110 and hasattr(lo_, "light_linking"):
            lo_.light_linking.receiver_collection = col_model if on else None


def render_still(name, transparent, samples, passes=False, clay=False):
    scn.render.film_transparent = transparent
    back_lights_skip_floor(transparent)
    sweep.is_shadow_catcher = transparent and JOB.get("shadow", "soft") != "none"
    sweep.visible_camera = not transparent or sweep.is_shadow_catcher
    if transparent and JOB.get("shadow", "soft") == "none":
        sweep.visible_shadow = True
    cy.samples = samples
    vl.material_override = clay_mat if clay else None
    configure_passes(passes)
    bpy.ops.render.render(write_still=False)
    save(os.path.join(OUT, name + ".png"), "PNG", "16", "RGBA" if transparent else "RGB")
    if passes:
        save(os.path.join(OUT, name + "_passes.exr"), "OPEN_EXR_MULTILAYER")
    vl.material_override = None
    back_lights_skip_floor(False)


clay_mat = bpy.data.materials.new("Clay")
clay_mat.use_nodes = True
cb = clay_mat.node_tree.nodes["Principled BSDF"]
set_input(cb, "Base Color", (0.72, 0.72, 0.72, 1))
set_input(cb, "Roughness", 0.7)


def explode(factor):
    """Push parts away from the assembly centre. Per-part offsets from the material table win."""
    saved = {k: e.location.copy() for k, e in part_empties.items()}
    for inst, e in part_empties.items():
        d = decisions.get(inst) or {}
        if d.get("offset_mm"):
            e.location += Vector(d["offset_mm"]) / 1000
            continue
        v = e.location - Vector((CTR.x, CTR.y, CTR.z))
        v.z *= 1.4
        e.location += v * factor
    bpy.context.view_layer.update()
    lo2, _ = world_bounds(meshes)
    product.location.z = -lo2.z
    bpy.context.view_layer.update()
    return saved


def unexplode(saved):
    for k, loc in saved.items():
        part_empties[k].location = loc
    product.location = (0, 0, 0)
    bpy.context.view_layer.update()


# ------------------------------------------------------------------ run the job
long_edge = JOB.get("size", 2400)
samples = JOB.get("samples", 384)
want = JOB.get("outputs", {})
for shot in JOB["shots"]:
    spec = dict(SHOTS[shot])
    targets = meshes
    if spec.get("target") or (shot == "detail" and JOB.get("detail_target")):
        pat = re.compile(JOB.get("detail_target") or spec["target"], re.I)
        targets = [o for o in meshes if pat.search(o["instance"])] or meshes
    rx, ry = aspect_dims(JOB.get("aspect") or spec.get("aspect", "4:3"), long_edge)
    cam, az = fit_camera(shot, spec, targets, rx, ry)
    tlo, thi = world_bounds(targets)
    place_rig(math.radians(az), (tlo + thi) / 2)
    if want.get("backdrop", True):
        render_still(f"{shot}", False, samples, passes=want.get("passes", False))
    if want.get("transparent", True):
        render_still(f"{shot}_transparent", True, samples, passes=want.get("passes", False))
    if want.get("clay") and shot == JOB["shots"][0]:
        render_still(f"{shot}_clay", False, max(64, samples // 3), clay=True)

if want.get("exploded"):
    saved = explode(float(want["exploded"]))
    shot = JOB["shots"][0]
    spec = SHOTS[shot]
    rx, ry = aspect_dims(JOB.get("aspect") or spec.get("aspect", "4:3"), long_edge)
    cam, az = fit_camera(shot + "_exploded", spec, meshes, rx, ry)
    place_rig(math.radians(az), CTR)
    render_still(f"{shot}_exploded", False, samples)
    if want.get("transparent", True):
        render_still(f"{shot}_exploded_transparent", True, samples)
    unexplode(saved)

tt = want.get("turntable")
if tt:
    frames = int(tt.get("frames", 90))
    spec = dict(SHOTS[tt.get("shot", "three_quarter")])
    spec["fstop"] = 0  # sharp throughout
    rx, ry = aspect_dims(tt.get("aspect", "1:1"), int(tt.get("size", 1080)))
    # frame the bounding CYLINDER so no rotation angle crops the product
    rad = math.hypot(SIZE.x, SIZE.y) / 2
    cyl = [Vector((CTR.x + rad * math.cos(a), CTR.y + rad * math.sin(a), z))
           for a in [i * math.pi / 12 for i in range(24)] for z in (lo.z, hi.z)]
    cam, az = fit_camera("turntable", spec, meshes, rx, ry, pts=cyl)
    place_rig(math.radians(az), CTR)
    transparent = bool(tt.get("transparent", False))
    scn.render.film_transparent = transparent
    back_lights_skip_floor(transparent)
    sweep.is_shadow_catcher = transparent
    sweep.visible_camera = True
    cy.samples = int(tt.get("samples", 96))
    configure_passes(False)
    fdir = os.path.join(OUT, "turntable_frames")
    os.makedirs(fdir, exist_ok=True)
    for i in range(frames):
        product.rotation_euler = (0, 0, 2 * math.pi * i / frames * (1 if tt.get("clockwise", True) else -1))
        bpy.context.view_layer.update()
        bpy.ops.render.render(write_still=False)
        im = scn.render.image_settings
        if "media_type" in im.bl_rna.properties:
            im.media_type = "IMAGE"
        im.file_format = "PNG"
        im.color_depth = "8"
        im.color_mode = "RGBA" if transparent else "RGB"
        bpy.data.images["Render Result"].save_render(os.path.join(fdir, f"frame_{i:04d}.png"), scene=scn)
        if i % 10 == 0:
            log(f"turntable frame {i + 1}/{frames}")
    product.rotation_euler = (0, 0, 0)
    outputs.append("turntable_frames/")

# ------------------------------------------------------------------ editable .blend
if JOB.get("save_blend", True):
    first = JOB["shots"][0]
    scn.camera = cameras.get(first) or scn.camera
    spec = SHOTS[first]
    scn.render.resolution_x, scn.render.resolution_y = aspect_dims(JOB.get("aspect") or spec.get("aspect", "4:3"), long_edge)
    place_rig(math.radians(spec["az"]), CTR)
    scn.render.film_transparent = False
    sweep.is_shadow_catcher = False
    sweep.visible_camera = True
    cy.samples = samples
    configure_passes(False)
    if "media_type" in scn.render.image_settings.bl_rna.properties:
        scn.render.image_settings.media_type = "IMAGE"
    scn.render.image_settings.file_format = "PNG"
    scn.render.image_settings.color_depth = "16"
    scn.render.filepath = "//renders/"
    # timeline markers bound to cameras: scrub the timeline to switch shots in Blender
    for i, (nm, cam) in enumerate(cameras.items()):
        mk = scn.timeline_markers.new(nm, frame=i + 1)
        mk.camera = cam
    scn.frame_start, scn.frame_end = 1, max(1, len(cameras))
    try:
        bpy.ops.file.pack_all()
    except Exception as e:
        log("pack_all:", e)
    # packed data keeps its original absolute path as a label; scrub it so the .blend carries no
    # user names or machine paths (the pixels stay packed inside the file)
    for img in bpy.data.images:
        if img.packed_file is not None:
            neutral = "//" + os.path.basename(img.filepath_raw or img.name)
            img.filepath = img.filepath_raw = neutral
            for pf in getattr(img, "packed_files", []):
                try:
                    pf.filepath = neutral
                except (AttributeError, TypeError):
                    pass
    p = os.path.join(OUT, JOB.get("blend_name", "scene") + ".blend")
    # write only the scene's data-blocks: save_as_mainfile also stores UI state (recent folders),
    # which can contain the user's home path
    bpy.data.libraries.write(p, {scn}, path_remap="NONE", fake_user=False, compress=True)
    outputs.append(os.path.basename(p))
    log("saved", p)

json.dump({"outputs": outputs, "device": device, "seconds": round(time.time() - T0, 1),
           "model_size_mm": [round(v * 1000, 1) for v in SIZE]},
          open(os.path.join(OUT, "_render_result.json"), "w"), indent=1)
log("done")
