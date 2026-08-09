from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
prompt = load_module("compile_model_prompt", SCRIPTS / "compile_model_prompt.py")
candidate = load_module("check_model_candidate", SCRIPTS / "check_model_candidate.py")
sys.path.pop(0)


EXPECTED_ORDER = [
    "photo_correction",
    "light_balance",
    "color_balance",
    "skin_cleanup",
    "facial_features",
    "hair_clothing",
    "background",
    "final_review",
]


class ModelPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = prompt.load_schema()

    def test_default_path_uses_no_model_and_keeps_full_pipeline(self) -> None:
        request = prompt.compile_request(self.schema)
        self.assertEqual(request["schema_version"], 6)
        self.assertEqual(request["preset"], "natural-v6")
        self.assertEqual(request["mode"], "natural")
        self.assertEqual(request["planned_model_calls"], 0)
        self.assertEqual(request["stage_order"], EXPECTED_ORDER)
        self.assertIn("No image-model call is enabled", request["prompt"])

    def test_explicit_model_candidate_is_single_ordered_identity_locked_call(self) -> None:
        request = prompt.compile_request(self.schema, enable_model=True)
        self.assertEqual(request["planned_model_calls"], 1)
        self.assertEqual(request["maximum_total_model_calls"], 1)
        self.assertFalse(request["model_retry_allowed"])
        self.assertFalse(request["model_chaining_allowed"])
        self.assertEqual(request["model_candidate_source"], "immutable_original")
        text = request["prompt"]
        positions = [text.index(f"{index}. {stage}") for index, stage in enumerate(EXPECTED_ORDER[:-1], 1)]
        self.assertEqual(positions, sorted(positions))
        for required in [
            "complete professional travel-portrait retouch",
            "exact identity",
            "face and neck color coherent",
            "sclera or teeth blue/cyan",
            "oversaturate lips",
            "hair-edge smearing",
            "bent background lines",
            "There is no model retry",
            "Semantic modification: none explicitly requested.",
            "Preserve/inspect only: under_eye_reduction, feature_definition",
            "Preserve/inspect only: hair_cleanup, clothing_cleanup",
            "Preserve/inspect only: background_balance, background_cleanup",
        ]:
            self.assertIn(required, text)

    def test_only_explicit_nonzero_semantic_control_receives_edit_permission(self) -> None:
        request = prompt.compile_request(
            self.schema,
            {"facial_features.under_eye_reduction": 0.2},
            enable_model=True,
        )
        text = request["prompt"]
        facial = text.split("5. facial_features", 1)[1].split(
            "6. hair_clothing", 1
        )[0]
        self.assertIn(
            "Semantic modification explicitly requested: under_eye_reduction=0.2.",
            facial,
        )
        self.assertIn(
            "Preserve/inspect without modification: feature_definition.", facial
        )
        self.assertNotIn("feature_definition=", facial)
        self.assertIn(
            "Allowed targets for the requested controls: under_eye_reduction:",
            facial,
        )
        self.assertNotIn("add restrained non-geometric feature definition", facial)

        hair = text.split("6. hair_clothing", 1)[1].split("7. background", 1)[0]
        background = text.split("7. background", 1)[1].split(
            "8. final_review", 1
        )[0]
        for untouched in (hair, background):
            self.assertIn("Semantic modification: none explicitly requested.", untouched)
            self.assertIn("make no pixel changes in this stage", untouched)

    def test_modes_only_change_light_and_color_stages(self) -> None:
        requests = {
            mode: prompt.compile_request(self.schema, mode=mode)
            for mode in ("natural", "fresh", "warm-film")
        }
        by_mode = {
            mode: {stage["id"]: stage for stage in request["stages"]}
            for mode, request in requests.items()
        }
        for stage_id in set(EXPECTED_ORDER) - {"light_balance", "color_balance"}:
            self.assertEqual(by_mode["natural"][stage_id], by_mode["fresh"][stage_id])
            self.assertEqual(by_mode["natural"][stage_id], by_mode["warm-film"][stage_id])
        self.assertNotEqual(
            by_mode["natural"]["light_balance"], by_mode["fresh"]["light_balance"]
        )
        self.assertNotEqual(
            by_mode["natural"]["color_balance"], by_mode["warm-film"]["color_balance"]
        )

    def test_stage_qualified_override_wins(self) -> None:
        request = prompt.compile_request(
            self.schema,
            {"skin_cleanup.skin_smoothing": 0.2},
            mode="fresh",
        )
        skin = next(stage for stage in request["stages"] if stage["id"] == "skin_cleanup")
        self.assertEqual(skin["parameters"]["skin_smoothing"], 0.2)
        light = next(stage for stage in request["stages"] if stage["id"] == "light_balance")
        self.assertEqual(light["parameters"]["lighting_improvement"], 0.32)

    def test_mode_listing_and_unknown_mode(self) -> None:
        listing = prompt.list_modes(self.schema)
        self.assertEqual(listing["default_mode"], "natural")
        self.assertEqual(
            [item["name"] for item in listing["modes"]],
            ["natural", "fresh", "warm-film"],
        )
        self.assertEqual(
            listing["mode_affected_stages"], ["light_balance", "color_balance"]
        )
        with self.assertRaises(prompt.PromptConfigError):
            prompt.compile_request(self.schema, mode="glamour")

    def test_all_zero_disables_even_explicit_model_call(self) -> None:
        overrides = {
            f"{definition['stage']}.{name}": 0.0
            for name, definition in self.schema["parameters"].items()
        }
        request = prompt.compile_request(
            self.schema, overrides=overrides, enable_model=True
        )
        self.assertEqual(request["planned_model_calls"], 0)
        self.assertIn("No image-model call is enabled", request["prompt"])

    def test_unknown_out_of_range_and_unqualified_controls_are_rejected(self) -> None:
        with self.assertRaises(prompt.PromptConfigError):
            prompt.compile_request(self.schema, {"skin_cleanup.face_slimming": 0.2})
        with self.assertRaises(prompt.PromptConfigError):
            prompt.compile_request(self.schema, {"facial_features.under_eye_reduction": 0.4})
        with self.assertRaises(prompt.PromptConfigError):
            prompt.compile_request(self.schema, {"skin_smoothing": 0.2})

    def test_retry_flag_no_longer_exists(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "compile_model_prompt.py"), "--retry-stronger"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unrecognized arguments", completed.stderr)


class ModelCandidateTests(unittest.TestCase):
    def test_matching_ratio_requires_visual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            model = root / "model.png"
            Image.new("RGB", (1200, 1800)).save(original)
            Image.new("RGB", (1024, 1536)).save(model)
            result = candidate.compare_model_candidate(original, model)
            self.assertTrue(result["automated_gates_passed"])
            self.assertEqual(result["decision"], "visual_review_required")

    def test_stretched_candidate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            model = root / "model.png"
            Image.new("RGB", (1200, 1800)).save(original)
            Image.new("RGB", (1024, 1400)).save(model)
            result = candidate.compare_model_candidate(original, model)
            self.assertFalse(result["automated_gates_passed"])
            self.assertEqual(result["decision"], "reject")


if __name__ == "__main__":
    unittest.main()
