from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
COMPILER_PATH = SCRIPTS / "compile_workflow.py"
PREPARE_PATH = SCRIPTS / "prepare_run.py"
EXECUTE_PATH = SCRIPTS / "execute_run.py"
RETOUCH_PATH = SCRIPTS / "retouch_image.py"
GEOMETRY_PATH = SCRIPTS / "check_geometry.py"
BATCH_PATH = SCRIPTS / "batch_retouch.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
workflow = load_module("compile_workflow", COMPILER_PATH)
geometry = load_module("check_geometry", GEOMETRY_PATH)
retouch = load_module("retouch_image", RETOUCH_PATH)
prepare = load_module("prepare_run", PREPARE_PATH)
execute = load_module("execute_run", EXECUTE_PATH)
batch = load_module("batch_retouch", BATCH_PATH)
sys.path.pop(0)


def make_portrait(path: Path, width: int, height: int, alpha: bool = False) -> None:
    mode = "RGBA" if alpha else "RGB"
    background = (34, 64, 82, 173) if alpha else (34, 64, 82)
    image = Image.new(mode, (width, height), background)
    draw = ImageDraw.Draw(image)
    face_box = (
        int(width * 0.31),
        int(height * 0.12),
        int(width * 0.69),
        int(height * 0.50),
    )
    skin = (190, 135, 112, 173) if alpha else (190, 135, 112)
    shirt = (54, 45, 68, 173) if alpha else (54, 45, 68)
    draw.ellipse(face_box, fill=skin)
    draw.rectangle(
        (int(width * 0.25), int(height * 0.52), int(width * 0.75), height - 1),
        fill=shirt,
    )
    draw.ellipse(
        (
            int(width * 0.42),
            int(height * 0.27),
            int(width * 0.46),
            int(height * 0.31),
        ),
        fill=(40, 30, 25, 173) if alpha else (40, 30, 25),
    )
    draw.ellipse(
        (
            int(width * 0.54),
            int(height * 0.27),
            int(width * 0.58),
            int(height * 0.31),
        ),
        fill=(40, 30, 25, 173) if alpha else (40, 30, 25),
    )
    draw.ellipse(
        (
            int(width * 0.49),
            int(height * 0.36),
            int(width * 0.52),
            int(height * 0.39),
        ),
        fill=(214, 91, 82, 173) if alpha else (214, 91, 82),
    )
    image.save(path)


class WorkflowCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = workflow.load_schema()

    def test_default_plan_is_deterministic_and_has_no_model_calls(self) -> None:
        plan = workflow.compile_plan(self.schema)
        self.assertEqual(plan["schema_version"], 3)
        self.assertEqual(plan["preset"], "natural-v3")
        self.assertEqual(plan["engine"]["name"], "deterministic-raster-v3")
        self.assertEqual(plan["planned_model_calls"], 0)
        self.assertEqual([stage["id"] for stage in plan["stages"]], ["lighting", "skin"])
        self.assertTrue(all("prompt" not in stage for stage in plan["stages"]))

    def test_zero_parameters_return_original(self) -> None:
        zeroes = {name: 0 for name in self.schema["parameters"]}
        plan = workflow.compile_plan(self.schema, zeroes)
        self.assertEqual(plan["stages"], [])
        self.assertIn("unchanged", plan["zero_edit_behavior"])

    def test_only_named_adjustment(self) -> None:
        plan = workflow.compile_plan(
            self.schema, {"skin_smoothing": 0.3}, use_defaults=False
        )
        self.assertEqual([stage["id"] for stage in plan["stages"]], ["skin"])
        self.assertEqual(plan["parameters"]["skin_smoothing"], 0.3)

    def test_under_eye_is_explicitly_deferred(self) -> None:
        with self.assertRaisesRegex(workflow.ConfigError, "not supported"):
            workflow.compile_plan(self.schema, {"under_eye_reduction": 0.1})

    def test_unknown_and_out_of_range_parameters_are_rejected(self) -> None:
        with self.assertRaisesRegex(workflow.ConfigError, "unknown parameters"):
            workflow.compile_plan(self.schema, {"make_eyes_larger": 0.2})
        with self.assertRaisesRegex(workflow.ConfigError, "between 0 and 0.5"):
            workflow.compile_plan(self.schema, {"skin_smoothing": 0.9})

    def test_config_and_cli_override_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(
                json.dumps(
                    {"preset": "natural-v3", "beauty": {"skin_smoothing": 0.1}}
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(COMPILER_PATH),
                    "--config",
                    str(config),
                    "--set",
                    "skin_smoothing=0.3",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(json.loads(completed.stdout)["parameters"]["skin_smoothing"], 0.3)


class DeterministicImageTests(unittest.TestCase):
    @staticmethod
    def _parameter_safety_portrait() -> np.ndarray:
        image = Image.new("RGB", (240, 180), (40, 75, 95))
        draw = ImageDraw.Draw(image)
        draw.ellipse((72, 18, 168, 142), fill=(196, 140, 112))
        draw.arc((72, 18, 168, 142), 185, 355, fill=(45, 29, 24), width=14)
        draw.ellipse((98, 59, 108, 69), fill=(35, 25, 22))
        draw.ellipse((132, 59, 142, 69), fill=(35, 25, 22))
        draw.arc((104, 79, 138, 108), 20, 160, fill=(125, 48, 43), width=3)
        for box, color in [
            ((89, 85, 101, 97), (224, 128, 108)),
            ((139, 86, 151, 98), (172, 139, 120)),
            ((112, 35, 126, 47), (211, 157, 119)),
            ((93, 102, 99, 108), (236, 82, 78)),
            ((145, 72, 151, 78), (238, 79, 75)),
        ]:
            draw.ellipse(box, fill=color)
        return np.asarray(image)

    @staticmethod
    def _mean_absolute_delta(before: np.ndarray, after: np.ndarray) -> float:
        return float(np.abs(after.astype(np.int16) - before.astype(np.int16)).mean())

    def test_skin_parameter_maxima_remain_monotonic_past_old_saturation(self) -> None:
        portrait = self._parameter_safety_portrait()
        blemish_old_limit, _ = retouch.apply_skin(portrait, 0.0, 0.40, 0.0)
        blemish_schema_max, _ = retouch.apply_skin(portrait, 0.0, 0.50, 0.0)
        tone_old_limit, _ = retouch.apply_skin(portrait, 0.0, 0.0, 0.40)
        tone_schema_max, _ = retouch.apply_skin(portrait, 0.0, 0.0, 0.45)

        self.assertGreater(
            self._mean_absolute_delta(portrait, blemish_schema_max),
            self._mean_absolute_delta(portrait, blemish_old_limit),
        )
        self.assertGreater(
            self._mean_absolute_delta(portrait, tone_schema_max),
            self._mean_absolute_delta(portrait, tone_old_limit),
        )

    def test_v6_light_balance_protects_near_white_highlights(self) -> None:
        pixels = np.full((120, 100, 3), 78, dtype=np.uint8)
        pixels[:18, :, :] = 249
        result, details = retouch.apply_light_balance(pixels, 0.25)
        before_luma = np.mean(pixels.astype(np.float32) / 255.0, axis=2)
        after_luma = np.mean(result.astype(np.float32) / 255.0, axis=2)
        before_white = float(np.mean(before_luma >= 0.98))
        after_white = float(np.mean(after_luma >= 0.98))
        self.assertEqual(
            details["operation"], "highlight_protected_whole_image_light_balance"
        )
        self.assertGreater(float(result[30:, :, :].mean()), float(pixels[30:, :, :].mean()))
        self.assertLessEqual(after_white - before_white, 0.02)

    def test_lighting_and_skin_preserve_exact_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.jpg"
            lighting = root / "lighting.png"
            skin = root / "skin.png"
            make_portrait(source, 137, 211)
            retouch.apply_stage(
                source, lighting, "lighting", {"lighting_improvement": 0.25}
            )
            retouch.apply_stage(
                lighting,
                skin,
                "skin",
                {
                    "skin_smoothing": 0.2,
                    "blemish_reduction": 0.2,
                    "skin_tone_evenness": 0.15,
                },
            )
            self.assertEqual(geometry.read_dimensions(source), (137, 211))
            self.assertEqual(geometry.read_dimensions(lighting), (137, 211))
            self.assertEqual(geometry.read_dimensions(skin), (137, 211))

    def test_phone_jpeg_exif_orientation_is_normalized_non_destructively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "phone-portrait.jpg"
            output = root / "lighting.png"
            image = Image.new("RGB", (1200, 800), (70, 92, 110))
            exif = image.getexif()
            exif[274] = 6
            image.save(source, exif=exif)
            source_hash = source.read_bytes()

            result = retouch.apply_v6_stage(
                source,
                output,
                "light_balance",
                {"lighting_improvement": 0.25},
            )

            self.assertEqual(source.read_bytes(), source_hash)
            self.assertEqual(geometry.read_dimensions(source), (800, 1200))
            self.assertEqual(geometry.read_dimensions(output), (800, 1200))
            self.assertEqual(result["source_dimensions"], {"width": 800, "height": 1200})
            self.assertEqual(geometry.compare_geometry(source, output)["decision"], "accept")

    def test_v6_output_is_private_and_embeds_srgb_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "profiled.png"
            output = root / "balanced.png"
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            Image.new("RGB", (800, 1200), (80, 110, 135)).save(
                source, icc_profile=profile.tobytes()
            )

            retouch.apply_v6_stage(
                source,
                output,
                "color_balance",
                {"white_balance_correction": 0.18, "style_strength": 0.0},
                mode="natural",
            )

            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with Image.open(output) as result:
                embedded = ImageCms.ImageCmsProfile(
                    io.BytesIO(result.info["icc_profile"])
                )
                self.assertIn("sRGB", ImageCms.getProfileDescription(embedded))

    def test_skin_stage_leaves_non_skin_background_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            output = root / "skin.png"
            make_portrait(source, 96, 128)
            retouch.apply_stage(
                source,
                output,
                "skin",
                {
                    "skin_smoothing": 0.4,
                    "blemish_reduction": 0.3,
                    "skin_tone_evenness": 0.3,
                },
            )
            before = np.asarray(Image.open(source).convert("RGB"))
            after = np.asarray(Image.open(output).convert("RGB"))
            self.assertTrue(np.array_equal(before[0:20, 0:20], after[0:20, 0:20]))
            self.assertTrue(np.any(before != after))

    def test_alpha_channel_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            output = root / "lighting.png"
            make_portrait(source, 71, 109, alpha=True)
            retouch.apply_stage(
                source, output, "lighting", {"lighting_improvement": 0.25}
            )
            before_alpha = np.asarray(Image.open(source).getchannel("A"))
            after_alpha = np.asarray(Image.open(output).getchannel("A"))
            self.assertTrue(np.array_equal(before_alpha, after_alpha))

    def test_skin_stage_skips_an_unreliably_broad_mask(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "all-skin-tone.png"
            output = root / "skin.png"
            Image.new("RGB", (80, 60), (190, 135, 112)).save(source)
            result = retouch.apply_stage(
                source,
                output,
                "skin",
                {
                    "skin_smoothing": 0.4,
                    "blemish_reduction": 0.3,
                    "skin_tone_evenness": 0.3,
                },
            )
            self.assertEqual(result["details"]["operation"], "skin_stage_skipped")
            self.assertEqual(source.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            before = np.asarray(Image.open(source).convert("RGB"))
            after = np.asarray(Image.open(output).convert("RGB"))
            self.assertTrue(np.array_equal(before, after))

    def test_skin_stage_rescues_a_detailed_portrait_on_warm_background(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "warm-portrait.png"
            output = root / "skin.png"
            image = Image.new("RGB", (240, 160), (190, 135, 112))
            draw = ImageDraw.Draw(image)
            draw.ellipse((82, 20, 158, 112), fill=(202, 145, 118))
            draw.arc((82, 20, 158, 112), 185, 355, fill=(45, 29, 24), width=12)
            draw.ellipse((103, 57, 111, 65), fill=(35, 25, 22))
            draw.ellipse((129, 57, 137, 65), fill=(35, 25, 22))
            draw.arc((108, 67, 134, 91), 20, 160, fill=(125, 48, 43), width=3)
            image.save(source)
            result = retouch.apply_stage(
                source,
                output,
                "skin",
                {
                    "skin_smoothing": 0.4,
                    "blemish_reduction": 0.3,
                    "skin_tone_evenness": 0.3,
                },
            )
            self.assertEqual(result["details"]["operation"], "masked_skin_retouch")
            self.assertEqual(
                result["details"]["mask_strategy"], "adaptive_spatial_rescue"
            )
            before = np.asarray(Image.open(source).convert("RGB"))
            after = np.asarray(Image.open(output).convert("RGB"))
            self.assertTrue(np.any(before != after))


class RunIntegrationTests(unittest.TestCase):
    def test_prepare_and_execute_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.jpg"
            make_portrait(source, 123, 187)
            prepared = prepare.prepare_run(source, root / "run")
            result = execute.execute_run(Path(prepared["run_dir"]))
            manifest = json.loads(
                (Path(prepared["run_dir"]) / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["accepted_stages"], ["lighting", "skin"])
            self.assertEqual(result["planned_model_calls"], 0)
            self.assertEqual(result["geometry_pass_rate"], 1.0)
            self.assertEqual(geometry.read_dimensions(Path(result["final"])), (123, 187))
            self.assertEqual(manifest["status"], "complete")

    def test_five_aspect_ratios_pass_geometry_and_quality(self) -> None:
        dimensions = [(73, 91), (120, 80), (80, 120), (64, 64), (57, 133)]
        passed = 0
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (width, height) in enumerate(dimensions):
                source = root / f"portrait-{index}.png"
                make_portrait(source, width, height)
                prepared = prepare.prepare_run(source, root / f"run-{index}")
                result = execute.execute_run(Path(prepared["run_dir"]))
                if (
                    result["status"] == "complete"
                    and result["geometry_pass_rate"] == 1.0
                    and geometry.read_dimensions(Path(result["final"])) == (width, height)
                ):
                    passed += 1
        self.assertGreaterEqual(passed / len(dimensions), 0.8)

    def test_zero_edit_run_returns_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            make_portrait(source, 63, 95)
            prepared = prepare.prepare_run(
                source,
                root / "run",
                overrides={name: 0 for name in workflow.load_schema()["parameters"]},
            )
            result = execute.execute_run(Path(prepared["run_dir"]))
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["accepted_stages"], [])
            self.assertEqual(Path(result["final"]).read_bytes(), source.read_bytes())


class BatchRunTests(unittest.TestCase):
    def test_batch_isolates_bad_files_and_reports_the_target_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_portrait(input_dir / "good-a.jpg", 96, 144)
            make_portrait(input_dir / "good-b.png", 120, 80)
            (input_dir / "broken.jpg").write_bytes(b"not a jpeg")
            result = batch.run_batch(
                input_dir,
                root / "batch-output",
                target_rate=0.9,
            )
            self.assertEqual(result["counts"]["total"], 3)
            self.assertEqual(result["counts"]["success"], 2)
            self.assertEqual(result["counts"]["failed"], 1)
            self.assertAlmostEqual(result["rates"]["full_success"], 2 / 3, places=5)
            self.assertFalse(result["rates"]["target_met"])
            self.assertEqual(
                len(list((root / "batch-output" / "finals").iterdir())), 2
            )
            self.assertTrue((root / "batch-output" / "summary.json").is_file())


class SkillContractTests(unittest.TestCase):
    def test_required_resources_and_v6_contract(self) -> None:
        required = [
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "agents" / "openai.yaml",
            SKILL_ROOT / "references" / "parameters.json",
            SKILL_ROOT / "references" / "model-parameters.json",
            SKILL_ROOT / "references" / "model-prompts.md",
            SKILL_ROOT / "references" / "quality-gates.md",
            COMPILER_PATH,
            PREPARE_PATH,
            EXECUTE_PATH,
            RETOUCH_PATH,
            GEOMETRY_PATH,
            BATCH_PATH,
            SCRIPTS / "compile_model_prompt.py",
            SCRIPTS / "check_model_candidate.py",
            SCRIPTS / "compile_pipeline.py",
            SCRIPTS / "pipeline_state.py",
            SCRIPTS / "execute_pipeline.py",
            SCRIPTS / "audit_result.py",
            SCRIPTS / "render_proof.py",
            SKILL_ROOT / "references" / "pipeline-v6.json",
            SKILL_ROOT / "references" / "stage-contracts.md",
            SKILL_ROOT / "references" / "audit-gates.json",
            SKILL_ROOT / "references" / "manual-review-template.json",
            SKILL_ROOT / "requirements.txt",
        ]
        self.assertTrue(all(path.is_file() for path in required))

    def test_v6_pipeline_and_modes_are_documented_in_skill_and_metadata(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        for mode in ["natural", "fresh", "warm-film"]:
            self.assertIn(mode, skill_text)
        self.assertIn("V6", metadata)
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("TODO", skill)
        self.assertIn("V6", skill)
        self.assertIn("V3", skill)
        for stage in [
            "photo_correction",
            "light_balance",
            "color_balance",
            "skin_cleanup",
            "facial_features",
            "hair_clothing",
            "background",
            "final_review",
        ]:
            self.assertIn(stage, skill)
        self.assertIn("Mandatory ten-item self-audit", skill)
        self.assertIn("Generate exactly one", skill)
        self.assertIn("never resize", skill.lower())


if __name__ == "__main__":
    unittest.main()
