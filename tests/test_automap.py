"""Tests for the material mapper (pure Python, no Blender needed).

    python3 -m unittest discover -s tests
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import automap  # noqa: E402

PRESETS = json.loads((ROOT / "assets" / "presets" / "materials.json").read_text())


def part(name, color=None, size=(50, 50, 50), solids=1):
    return {"name": name, "part": name, "path": name, "cad_color": color, "size_mm": list(size), "solids": solids}


class AutomapTests(unittest.TestCase):
    def decide(self, parts, rules=None):
        rules_l = []
        if rules:
            tmp = ROOT / "tests" / "_rules.json"
            tmp.write_text(json.dumps({"rules": rules}))
            rules_l = automap.load_rules(tmp)
            tmp.unlink()
        return automap.resolve(parts, rules_l, PRESETS)

    def test_every_decision_uses_a_real_preset(self):
        names = ["M6 socket head screw", "6201-2RS bearing", "HGR15 rail", "Frame", "Cover", "O-ring",
                 "Acrylic window", "NEMA 17 stepper", "Brass bushing", "P-0042", "Lead screw", "Spider"]
        for d in self.decide([part(n) for n in names]):
            self.assertIn(d["preset"], PRESETS, d)

    def test_keywords(self):
        got = {d["name"]: d["preset"] for d in self.decide([
            part("M6x20 socket head screw", "#202020"), part("6201-2RS bearing"), part("MGN12 rail"),
            part("NEMA 23 stepper motor"), part("Rubber foot"), part("Lead screw")])}
        self.assertEqual(got["M6x20 socket head screw"], "black_oxide")
        self.assertEqual(got["6201-2RS bearing"], "steel_polished")
        self.assertEqual(got["MGN12 rail"], "steel_ground")
        self.assertEqual(got["NEMA 23 stepper motor"], "motor_housing")
        self.assertEqual(got["Rubber foot"], "rubber")
        self.assertEqual(got["Lead screw"], "steel_polished")

    def test_saturated_cad_colour_becomes_paint_and_keeps_colour(self):
        d = self.decide([part("Housing body", "#c8102e", (300, 200, 150))])[0]
        self.assertIn(d["preset"], ("cast_paint", "powder_coat"))
        self.assertEqual(d["color"], "#c8102e")

    def test_unknown_part_is_flagged_for_review(self):
        d = self.decide([part("P-0042", None, (12, 8, 4))])[0]
        self.assertIn("review me", d["reason"])

    def test_rules_win_over_keywords_and_can_hide(self):
        ds = self.decide([part("M6 screw"), part("Safety guard")],
                         [{"match": "screw", "preset": "brass"}, {"match": "guard", "hide": True}])
        self.assertEqual(ds[0]["preset"], "brass")
        self.assertTrue(ds[1]["hide"])

    def test_rule_can_target_one_body_of_a_multibody_part(self):
        ds = self.decide([part("Coupling", solids=3)],
                         [{"match": "^Coupling#1$", "preset": "plastic_color", "color": "#ff0000"},
                          {"match": "^Coupling", "preset": "aluminium_machined"}])
        by = {d["name"]: d for d in ds}
        self.assertEqual(by["Coupling"]["preset"], "aluminium_machined")
        self.assertEqual(by["Coupling#1"]["preset"], "plastic_color")
        self.assertNotIn("Coupling#0", by)  # same as the part, so no separate entry


class PresetFileTests(unittest.TestCase):
    def test_lighting_and_shots_are_well_formed(self):
        light = json.loads((ROOT / "assets" / "presets" / "lighting.json").read_text())
        shots = json.loads((ROOT / "assets" / "presets" / "shots.json").read_text())
        for k, v in light.items():
            if k.startswith("_"):
                continue
            for L in v["lights"]:
                self.assertEqual(len(L["size"]), 2, (k, L))
                self.assertGreater(L["power"], 0)
        for k, v in shots.items():
            if k.startswith("_"):
                continue
            self.assertIn("az", v)
            self.assertIn("el", v)
            a, b = v["aspect"].split(":")
            self.assertGreater(int(a) * int(b), 0)


if __name__ == "__main__":
    unittest.main()
