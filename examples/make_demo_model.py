# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["build123d>=0.9"]
# ///
"""Build the demo model: a small machinist's bench vise, exported as a STEP assembly
with named parts and CAD colours, the way a real CAD export arrives.

    uv run --script examples/make_demo_model.py examples/demo-vise.step

Original geometry made for this repo (no third-party CAD). Units: millimetres, Z up,
long axis along X, handle on the +X end.
"""
import sys

from build123d import Axis, Box, Color, Compound, Cylinder, Location, Sphere, chamfer, export_step, fillet

out = sys.argv[1] if len(sys.argv) > 1 else "demo-vise.step"
SCREW_Z = 50.0


def at(shape, x=0, y=0, z=0):
    return shape.moved(Location((x, y, z)))


def box(x0, x1, y0, y1, z0, z1):
    return at(Box(x1 - x0, y1 - y0, z1 - z0), (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)


def rod_x(r, x0, x1, y=0, z=SCREW_Z):
    return at(Cylinder(r, x1 - x0, rotation=(0, 90, 0)), (x0 + x1) / 2, y, z)


def rod_y(r, y0, y1, x=0, z=SCREW_Z):
    return at(Cylinder(r, y1 - y0, rotation=(90, 0, 0)), x, (y0 + y1) / 2, z)


def soft(shape, r, axis=Axis.Z):
    try:
        return fillet(shape.edges().filter_by(axis), r)
    except Exception:
        return shape


def part(shape, name, rgb):
    shape.label = name
    shape.color = Color(*rgb)
    return shape


PAINT = (0.10, 0.22, 0.36)     # blue machine paint
STEEL = (0.62, 0.63, 0.65)
BRIGHT = (0.80, 0.80, 0.82)
BLACK = (0.05, 0.05, 0.05)

# ---- body: base + guide rail + fixed jaw (one casting)
base = soft(box(-85, 85, -45, 45, 0, 16), 6)
rail = box(-75, 75, -17, 17, 16, 38)
fixed = soft(box(-80, -50, -45, 45, 16, 70), 3, Axis.Y)
body = base + rail + fixed
for x in (-70, 70):
    for y in (-34, 34):
        body -= at(Cylinder(4.5, 40), x, y, 8)
body = part(body, "Vise body", PAINT)

# ---- moving jaw with the nut housing behind it
mjaw = soft(box(0, 34, -45, 45, 38, 70), 3, Axis.Y) + soft(box(34, 72, -22, 22, 38, 62), 2, Axis.X)
mjaw -= rod_x(6.4, -1, 80)
mjaw = part(mjaw, "Moving jaw", PAINT)

# ---- hardened jaw plates with two counterbored screw holes each
def jaw_plate(x0, x1):
    p = box(x0, x1, -44, 44, 46, 68)
    for y in (-28, 28):
        p -= rod_x(3.3, x0 - 1, x1 + 1, y=y, z=57)
    return part(p, "Jaw plate", STEEL)


plate_fixed = jaw_plate(-50, -44)
plate_moving = jaw_plate(-6, 0)

# ---- socket head screws (heads proud of the plate faces)
screws = []
for hx0, hx1 in ((-44, -40), (-10, -6)):
    for y in (-28, 28):
        s = rod_x(4.3, hx0, hx1, y=y, z=57)
        face = hx1 if hx0 == -44 else hx0  # the exposed face, toward the clamping gap
        s -= rod_x(2.0, face - 2.5, face + 2.5, y=y, z=57)  # hex socket (round, for simplicity)
        screws.append(part(s, "Socket head screw M6", BLACK))

# ---- lead screw with thread grooves
screw = rod_x(6, 10, 122)
for i in range(26):
    x = 40 + i * 3.0
    screw -= rod_x(6.6, x, x + 1.2) - rod_x(5.0, x, x + 1.2)
screw = part(screw, "Lead screw", STEEL)

# ---- handle: hub, bar across Y, two knobs
hub = chamfer(rod_x(13, 118, 140).edges(), 1.5) - rod_y(4.3, -20, 20, x=129)
hub = part(hub, "Handle hub", STEEL)
bar = part(rod_y(4, -62, 62, x=129), "Handle bar", BRIGHT)
knobs = [part(at(Sphere(8.5), 129, y, SCREW_Z), "Handle knob", BLACK) for y in (-66, 66)]

# ---- rubber feet under the base corners
feet = [part(at(Cylinder(7, 4), x, y, -2), "Rubber foot", (0.03, 0.03, 0.03))
        for x in (-72, 72) for y in (-36, 36)]

asm = Compound(label="Bench vise",
               children=[body, mjaw, plate_fixed, plate_moving, *screws, screw, hub, bar, *knobs, *feet])
export_step(asm, out)
print("wrote", out)
