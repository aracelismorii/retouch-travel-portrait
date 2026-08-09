from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
COMPILER_PATH = SKILL_ROOT / "scripts" / "compile_pipeline.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compiler = load_module("compile_pipeline_v6", COMPILER_PATH)

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
EXPECTED_AUDIT_CHECKS = [
    "skin_texture_natural",
    "face_neck_color_consistent",
    "sclera_teeth_not_blue",
    "lip_saturation_natural",
    "background_lines_straight",
    "hair_edges_clean",
    "subject_background_brightness_cohesive",
    "zoom_and_thumbnail_coherent",
    "change_not_excessive_vs_original",
    "phone_viewing_comfortable",
]


class PipelineV6CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = compiler.load_schema()
        cls.parameters, cls.audit = compiler.load_linked_schemas(cls.pipeline)

    def compile(self, mode: str = "natural", overrides=None):
        return compiler.compile_pipeline(
            self.pipeline,
            self.parameters,
            self.audit,
            mode=mode,
            overrides=overrides,
        )

    def test_emits_exact_eight_stage_contract_in_order(self) -> None:
        plan = self.compile()
        self.assertEqual(plan["schema_version"], 6)
        self.assertEqual(plan["stage_order"], EXPECTED_ORDER)
        self.assertEqual([stage["id"] for stage in plan["stages"]], EXPECTED_ORDER)
        self.assertEqual([stage["index"] for stage in plan["stages"]], list(range(1, 9)))
        required_fields = {
            "action",
            "parameters",
            "allowed_changes",
            "forbidden_changes",
            "executor",
            "checkpoint",
        }
        for stage in plan["stages"]:
            self.assertTrue(required_fields.issubset(stage))
            self.assertTrue(stage["allowed_changes"])
            self.assertTrue(stage["forbidden_changes"])
            self.assertTrue(stage["executor"])
            self.assertEqual(stage["checkpoint"]["compare_to"], "immutable_original")

    def test_stage_schema_reordering_is_rejected(self) -> None:
        bad = copy.deepcopy(self.pipeline)
        bad["stage_order"][0], bad["stage_order"][1] = (
            bad["stage_order"][1],
            bad["stage_order"][0],
        )
        with self.assertRaisesRegex(
            compiler.PipelineConfigError, "follow stage_order exactly"
        ):
            compiler.compile_pipeline(bad, self.parameters, self.audit)

    def test_modes_change_only_light_and_color_balance(self) -> None:
        plans = {
            mode: self.compile(mode)
            for mode in ("natural", "fresh", "warm-film")
        }
        by_mode = {
            mode: {stage["id"]: stage for stage in plan["stages"]}
            for mode, plan in plans.items()
        }
        invariant_ids = set(EXPECTED_ORDER) - {"light_balance", "color_balance"}
        for stage_id in invariant_ids:
            self.assertEqual(
                by_mode["natural"][stage_id], by_mode["fresh"][stage_id]
            )
            self.assertEqual(
                by_mode["natural"][stage_id], by_mode["warm-film"][stage_id]
            )
        self.assertNotEqual(
            by_mode["natural"]["light_balance"],
            by_mode["fresh"]["light_balance"],
        )
        self.assertNotEqual(
            by_mode["natural"]["color_balance"],
            by_mode["warm-film"]["color_balance"],
        )

    def test_mode_recipe_outside_two_mode_stages_is_rejected(self) -> None:
        bad_parameters = copy.deepcopy(self.parameters)
        bad_parameters["modes"]["natural"]["stage_recipes"]["skin_cleanup"] = {
            "skin_smoothing": 0.2
        }
        with self.assertRaisesRegex(
            compiler.PipelineConfigError,
            "only light_balance and color_balance",
        ):
            compiler.compile_pipeline(
                self.pipeline, bad_parameters, self.audit, mode="natural"
            )

    def test_unknown_mode_stage_parameter_and_wrong_stage_are_rejected(self) -> None:
        with self.assertRaisesRegex(compiler.PipelineConfigError, "unknown mode"):
            self.compile("cinematic")
        with self.assertRaisesRegex(compiler.PipelineConfigError, "unknown stage"):
            self.compile(overrides={"not_a_stage.skin_smoothing": 0.1})
        with self.assertRaisesRegex(compiler.PipelineConfigError, "unknown parameter"):
            self.compile(overrides={"skin_cleanup.face_shape": 0.1})
        with self.assertRaisesRegex(compiler.PipelineConfigError, "belongs to"):
            self.compile(overrides={"background.skin_smoothing": 0.1})

    def test_out_of_range_and_nonfinite_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(compiler.PipelineConfigError, "between 0 and 0.5"):
            self.compile(overrides={"skin_cleanup.skin_smoothing": 0.8})
        with self.assertRaisesRegex(compiler.PipelineConfigError, "finite"):
            self.compile(overrides={"skin_cleanup.skin_smoothing": float("nan")})

    def test_photo_correction_is_inspection_only_and_fixed_at_zero(self) -> None:
        plan = self.compile()
        photo = plan["stages"][0]
        definition = self.parameters["parameters"]["photo_correction"]
        self.assertEqual(
            (definition["minimum"], definition["default"], definition["maximum"]),
            (0.0, 0.0, 0.0),
        )
        self.assertEqual(photo["parameters"], {"photo_correction": 0.0})
        self.assertEqual(photo["action"], "no_change")
        self.assertEqual(photo["parameter_scope"], "inspection_only_v6_mvp")
        self.assertFalse(photo["parameters_active_in_default_path"])
        with self.assertRaisesRegex(
            compiler.PipelineConfigError, "between 0 and 0"
        ):
            self.compile(overrides={"photo_correction.photo_correction": 0.01})

    def test_cli_assignment_parser_is_strict(self) -> None:
        self.assertEqual(
            compiler.parse_assignments(
                ["skin_cleanup.skin_smoothing=0.2", "background.background_balance=0"]
            ),
            {
                "skin_cleanup.skin_smoothing": 0.2,
                "background.background_balance": 0.0,
            },
        )
        with self.assertRaisesRegex(compiler.PipelineConfigError, "STAGE.PARAM"):
            compiler.parse_assignments(["skin_smoothing=0.2"])
        with self.assertRaisesRegex(compiler.PipelineConfigError, "duplicate"):
            compiler.parse_assignments(
                [
                    "skin_cleanup.skin_smoothing=0.2",
                    "skin_cleanup.skin_smoothing=0.3",
                ]
            )

    def test_all_zero_still_emits_every_stage_as_no_change_or_inspect(self) -> None:
        overrides = {
            f"{definition['stage']}.{name}": 0.0
            for name, definition in self.parameters["parameters"].items()
        }
        plan = self.compile(overrides=overrides)
        self.assertEqual([stage["id"] for stage in plan["stages"]], EXPECTED_ORDER)
        self.assertEqual(len(plan["stages"]), 8)
        self.assertTrue(
            all(
                stage["action"] == "no_change"
                for stage in plan["stages"]
                if stage["id"] != "final_review"
            )
        )
        for stage in plan["stages"][:-1]:
            self.assertTrue(stage["no_change_reason_required"])
            self.assertTrue(stage["no_change_reason"])
        final = plan["stages"][-1]
        self.assertEqual(final["action"], "inspect")
        self.assertFalse(final["edits_allowed"])

    def test_model_policy_is_closed_to_retry_and_chaining(self) -> None:
        policy = self.compile()["model_policy"]
        self.assertEqual(policy["default_model_calls"], 0)
        self.assertEqual(policy["maximum_model_calls"], 1)
        self.assertEqual(policy["maximum_successful_candidates"], 1)
        self.assertFalse(policy["model_chaining_allowed"])
        self.assertFalse(policy["model_retry_allowed"])
        self.assertEqual(policy["model_candidate_source"], "immutable_original")

    def test_checkpoint_policy_requires_visual_commit_or_rollback(self) -> None:
        policy = self.compile()["checkpoint_policy"]
        self.assertTrue(policy["immutable_original_required"])
        self.assertTrue(policy["stage_order_is_strict"])
        self.assertTrue(policy["safe_noop_requires_reason"])
        self.assertTrue(policy["deterministic_candidate_requires_visual_acceptance"])
        self.assertTrue(policy["visual_reject_retains_previous_checkpoint"])

    def test_final_review_contains_all_ten_required_checks(self) -> None:
        plan = self.compile()
        self.assertEqual(plan["required_audit_checks"], EXPECTED_AUDIT_CHECKS)
        self.assertEqual(len(plan["required_audit_checks"]), 10)
        final = plan["stages"][-1]
        self.assertEqual(final["id"], "final_review")
        self.assertEqual(final["parameters"], {})
        self.assertEqual(final["action"], "inspect")
        self.assertFalse(final["edits_allowed"])

    def test_semantic_parameters_are_explicitly_model_only(self) -> None:
        plan = self.compile()
        stages = {stage["id"]: stage for stage in plan["stages"]}
        for stage_id in ("facial_features", "hair_clothing", "background"):
            stage = stages[stage_id]
            self.assertEqual(stage["parameter_scope"], "optional_model_candidate_only")
            self.assertFalse(stage["parameters_active_in_default_path"])
            self.assertEqual(
                stage["default_path_action"], "inspect_and_record_safe_no_change"
            )
            self.assertTrue(all(value == 0 for value in stage["parameters"].values()))
            self.assertEqual(stage["explicit_nonzero_parameters"], {})
            self.assertEqual(
                set(stage["preserve_or_inspect_parameters"]),
                set(stage["parameters"]),
            )
        for stage_id in ("light_balance", "color_balance", "skin_cleanup"):
            self.assertEqual(stages[stage_id]["parameter_scope"], "deterministic_stage")
            self.assertTrue(stages[stage_id]["parameters_active_in_default_path"])

    def test_semantic_schema_defaults_cannot_silently_enable_model_edits(self) -> None:
        for default_location in ("shared", "definition"):
            bad = copy.deepcopy(self.parameters)
            if default_location == "shared":
                bad["shared_parameter_defaults"]["under_eye_reduction"] = 0.1
            else:
                bad["parameters"]["under_eye_reduction"]["default"] = 0.1
            with self.subTest(default_location=default_location):
                with self.assertRaisesRegex(
                    compiler.PipelineConfigError,
                    "semantic parameter under_eye_reduction must default to zero",
                ):
                    compiler.compile_pipeline(self.pipeline, bad, self.audit)

    def test_cli_lists_modes_and_writes_compiled_output(self) -> None:
        listed = subprocess.run(
            [sys.executable, str(COMPILER_PATH), "--list-modes"],
            check=True,
            capture_output=True,
            text=True,
        )
        mode_payload = json.loads(listed.stdout)
        self.assertEqual(
            [item["name"] for item in mode_payload["modes"]],
            ["natural", "fresh", "warm-film"],
        )
        self.assertEqual(
            set(mode_payload["mode_affected_stages"]), MODE_STAGE_IDS
        )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "plan.json"
            subprocess.run(
                [
                    sys.executable,
                    str(COMPILER_PATH),
                    "--mode",
                    "fresh",
                    "--set",
                    "skin_cleanup.skin_smoothing=0.2",
                    "--output",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "fresh")
        self.assertEqual(len(payload["stages"]), 8)
        skin = next(stage for stage in payload["stages"] if stage["id"] == "skin_cleanup")
        self.assertEqual(skin["parameters"]["skin_smoothing"], 0.2)


MODE_STAGE_IDS = {"light_balance", "color_balance"}


if __name__ == "__main__":
    unittest.main()
