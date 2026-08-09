from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
compiler = load_module("compile_pipeline_state_test", SCRIPTS / "compile_pipeline.py")
state = load_module("pipeline_state_test", SCRIPTS / "pipeline_state.py")
executor = load_module("execute_pipeline_test", SCRIPTS / "execute_pipeline.py")
audit = load_module("audit_result_state_test", SCRIPTS / "audit_result.py")
proof = load_module("render_proof_state_test", SCRIPTS / "render_proof.py")
model_prompt = load_module(
    "compile_model_prompt_state_test", SCRIPTS / "compile_model_prompt.py"
)
sys.path.pop(0)


MANUAL_NOTES = {
    "skin_texture_natural": "Skin pores and fine texture remain visible across both cheeks at 100% view.",
    "face_neck_color_consistent": "Face and neck retain the same warm undertone without a visible color boundary.",
    "sclera_teeth_not_blue": "Both visible eye whites remain neutral and no teeth are exposed in this portrait.",
    "lip_saturation_natural": "Lip color remains muted red with clean edges and no oversaturated pixels.",
    "background_lines_straight": "The vertical frame and horizontal background line remain straight in fit view.",
    "hair_edges_clean": "Hair edges retain individual strands without halos or painted smearing at 100% view.",
    "subject_background_brightness_cohesive": "Subject brightness remains consistent with the darker background lighting.",
    "zoom_and_thumbnail_coherent": "The 100% detail and 720 thumbnail preserve the same natural facial appearance.",
    "change_not_excessive_vs_original": "Original and candidate side-by-side views show only a small bounded tonal change.",
    "phone_viewing_comfortable": "The 1080 phone preview retains comfortable highlights and open shadow detail.",
}


def complete_manual() -> dict[str, dict[str, str]]:
    return {
        check_id: {"status": "pass", "note": MANUAL_NOTES[check_id]}
        for check_id in audit.CHECK_IDS
    }


def make_accepted_audit(run: Path, candidate: Path, name: str = "review") -> Path:
    run_state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    original = run / run_state["original"]["path"]
    proof_dir = run / f"proof-{name}"
    proof.render_proofs(original, candidate, proof_dir)
    report = audit.audit_images(
        original,
        candidate,
        mode=run_state["mode"],
        manual_review={"checks": complete_manual()},
        proof_manifest=proof_dir / "manifest.json",
    )
    if report["decision"] != "accept":
        raise AssertionError("fixture audit was not accepted")
    audit_path = run / f"{name}-audit.json"
    audit_path.write_text(json.dumps(report), encoding="utf-8")
    return audit_path


def make_model_request(root: Path, mode: str = "natural") -> tuple[Path, Path]:
    request = model_prompt.compile_request(mode=mode, enable_model=True)
    request_path = root / "model-request.json"
    prompt_path = root / "model-prompt.txt"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    prompt_path.write_text(request["prompt"] + "\n", encoding="utf-8")
    return request_path, prompt_path


def make_portrait(path: Path) -> None:
    image = Image.new("RGB", (800, 1200), (42, 66, 82))
    draw = ImageDraw.Draw(image)
    draw.rectangle((65, 80, 735, 1110), outline=(150, 165, 174), width=5)
    draw.ellipse((230, 110, 570, 520), fill=(190, 136, 112))
    draw.rectangle((180, 520, 620, 1199), fill=(53, 44, 68))
    for y in range(180, 470, 22):
        draw.line((300, y, 500, y), fill=(183, 126, 105), width=2)
    draw.ellipse((310, 270, 345, 296), fill=(35, 28, 25))
    draw.ellipse((455, 270, 490, 296), fill=(35, 28, 25))
    draw.arc((345, 350, 455, 430), 15, 165, fill=(126, 54, 55), width=6)
    image.save(path)


def compile_plan(path: Path) -> dict:
    pipeline = compiler.load_schema()
    parameters, gates = compiler.load_linked_schemas(pipeline)
    plan = compiler.compile_pipeline(pipeline, parameters, gates)
    path.write_text(json.dumps(plan), encoding="utf-8")
    return plan


def execute_and_accept(run: Path, stage_id: str) -> dict:
    pending = executor.execute_stage(run, stage_id)
    if pending.get("status") != "awaiting_visual_review":
        raise AssertionError(f"{stage_id} did not create a pending candidate")
    return executor.execute_stage(
        run,
        stage_id,
        review_decision="accept",
        notes=f"Visual comparison accepted the bounded {stage_id} candidate.",
    )


class PipelineStateV6Tests(unittest.TestCase):
    def test_cannot_skip_a_required_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            state.init_run(source, plan_path, root / "run")
            with self.assertRaisesRegex(state.PipelineStateError, "expected stage"):
                state.record_stage(
                    root / "run",
                    "light_balance",
                    "no_change",
                    notes="Attempted skip",
                )

    def test_run_plan_is_immutable_after_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            copied_plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
            copied_plan["mode"] = "fresh"
            (run / "plan.json").write_text(json.dumps(copied_plan), encoding="utf-8")
            with self.assertRaisesRegex(state.PipelineStateError, "plan hash changed"):
                state.record_stage(
                    run,
                    "photo_correction",
                    "no_change",
                    notes="The edited plan must be rejected.",
                )

    def test_semantic_stage_requires_explicit_visual_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            executor.execute_stage(run, "photo_correction", confirm_inspection=True)
            execute_and_accept(run, "light_balance")
            execute_and_accept(run, "color_balance")
            execute_and_accept(run, "skin_cleanup")
            with self.assertRaisesRegex(
                executor.PipelineExecutionError, "requires visual inspection"
            ):
                executor.execute_stage(run, "facial_features")

    def test_complete_ordered_run_requires_ten_item_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            plan = compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)

            executor.execute_stage(run, "photo_correction", confirm_inspection=True)
            execute_and_accept(run, "light_balance")
            execute_and_accept(run, "color_balance")
            execute_and_accept(run, "skin_cleanup")
            for stage_id in ("facial_features", "hair_clothing", "background"):
                executor.execute_stage(
                    run,
                    stage_id,
                    confirm_inspection=True,
                    notes="Visual inspection found no safe localized correction needed.",
                )

            run_state = json.loads((run / "run.json").read_text(encoding="utf-8"))
            current = run / run_state["current_checkpoint"]["path"]
            original = run / run_state["original"]["path"]
            proof_dir = run / "proof-final"
            proof.render_proofs(original, current, proof_dir)
            with self.assertRaisesRegex(state.PipelineStateError, "audit check"):
                incomplete = audit.audit_images(
                    original,
                    current,
                    proof_manifest=proof_dir / "manifest.json",
                )
                incomplete_path = root / "audit-incomplete.json"
                incomplete_path.write_text(json.dumps(incomplete), encoding="utf-8")
                state.finalize(run, incomplete_path)

            complete = audit.audit_images(
                original,
                current,
                manual_review={"checks": complete_manual()},
                proof_manifest=proof_dir / "manifest.json",
            )
            self.assertEqual(complete["decision"], "accept")
            wrong_origin = json.loads(json.dumps(complete))
            wrong_origin["inputs"]["original"]["sha256"] = "0" * 64
            wrong_origin_path = root / "audit-wrong-origin.json"
            wrong_origin_path.write_text(json.dumps(wrong_origin), encoding="utf-8")
            with self.assertRaisesRegex(state.PipelineStateError, "fresh verification"):
                state.finalize(run, wrong_origin_path)
            # The documented CLI writes audit.json directly into the run folder.
            audit_path = run / "audit.json"
            audit_path.write_text(json.dumps(complete), encoding="utf-8")
            result = state.finalize(run, audit_path)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(Path(result["final"]).is_file())
            self.assertEqual(state.validate_run(run)["status"], "complete")
            final_state = json.loads((run / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(final_state["status"], "complete")
            self.assertEqual(
                [final_state["stages"][stage_id]["status"] for stage_id in plan["stage_order"]],
                [
                    "no_change",
                    "accepted",
                    "accepted",
                    "accepted",
                    "no_change",
                    "no_change",
                    "no_change",
                    "accepted",
                ],
            )
            comparison = proof_dir / "comparison.png"
            comparison.write_bytes(comparison.read_bytes() + b"post-finalize-tamper")
            with self.assertRaisesRegex(state.PipelineStateError, "revalidation"):
                state.validate_run(run)

    def test_applied_stage_cannot_advance_before_visual_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            executor.execute_stage(run, "photo_correction", confirm_inspection=True)

            pending = executor.execute_stage(run, "light_balance")
            self.assertEqual(pending["status"], "awaiting_visual_review")
            with self.assertRaisesRegex(
                executor.PipelineExecutionError, "pending candidate exists"
            ):
                executor.execute_stage(run, "light_balance")
            run_state = json.loads((run / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run_state["current_stage"], "light_balance")
            self.assertEqual(
                run_state["current_checkpoint"]["sha256"],
                run_state["original"]["sha256"],
            )
            with self.assertRaisesRegex(executor.PipelineExecutionError, "--notes"):
                executor.execute_stage(
                    run, "light_balance", review_decision="accept"
                )
            accepted = executor.execute_stage(
                run,
                "light_balance",
                review_decision="accept",
                notes="Compared highlights, shadows, and subject/background cohesion.",
            )
            self.assertEqual(accepted["next_stage"], "color_balance")

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_candidate_review_artifacts_are_private_even_with_open_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            executor.execute_stage(run, "photo_correction", confirm_inspection=True)

            candidates = run / "candidates"
            candidates.mkdir(mode=0o755)
            os.chmod(candidates, 0o755)
            previous_umask = os.umask(0o000)
            try:
                pending = executor.execute_stage(run, "light_balance")
            finally:
                os.umask(previous_umask)

            review_path = Path(pending["review_manifest"])
            candidate_path = Path(pending["candidate"])
            self.assertEqual(candidates.stat().st_mode & 0o777, 0o700)
            self.assertEqual(review_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(candidate_path.stat().st_mode & 0o777, 0o600)

            os.chmod(review_path, 0o644)
            executor.execute_stage(
                run,
                "light_balance",
                review_decision="accept",
                notes="Reviewed private candidate and accepted its bounded light change.",
            )
            self.assertEqual(review_path.stat().st_mode & 0o777, 0o600)

    def test_pending_candidate_hash_change_blocks_visual_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            executor.execute_stage(run, "photo_correction", confirm_inspection=True)
            pending = executor.execute_stage(run, "light_balance")
            candidate_path = Path(pending["candidate"])
            pixels = Image.open(candidate_path).convert("RGB")
            pixels.putpixel((0, 0), (255, 0, 255))
            pixels.save(candidate_path)
            with self.assertRaisesRegex(
                executor.PipelineExecutionError, "provenance changed"
            ):
                executor.execute_stage(
                    run,
                    "light_balance",
                    review_decision="accept",
                    notes="Attempted to accept a modified pending candidate.",
                )

    def test_visual_reject_rolls_back_and_records_safe_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            executor.execute_stage(run, "photo_correction", confirm_inspection=True)
            executor.execute_stage(run, "light_balance")
            rejected = executor.execute_stage(
                run,
                "light_balance",
                review_decision="reject",
                notes="Window highlights looked harsher than the immutable original.",
            )
            self.assertEqual(rejected["decision"], "rejected_and_rolled_back")
            self.assertEqual(rejected["next_stage"], "color_balance")
            run_state = json.loads((run / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run_state["stages"]["light_balance"]["status"], "no_change")
            self.assertEqual(len(run_state["stages"]["light_balance"]["attempts"]), 2)
            self.assertEqual(
                run_state["current_checkpoint"]["sha256"],
                run_state["original"]["sha256"],
            )

    def test_tampered_stage_completion_without_attempts_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            document = json.loads((run / "run.json").read_text(encoding="utf-8"))
            for stage_id in state.STAGE_ORDER[:-1]:
                document["stages"][stage_id]["status"] = "no_change"
                document["stages"][stage_id]["accepted_checkpoint"] = document["original"]
            document["current_stage"] = "final_review"
            (run / "run.json").write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(state.PipelineStateError, "final attempt"):
                state.validate_run(run)

    def test_noncanonical_plan_with_fake_audit_keys_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            plan = compile_plan(plan_path)
            plan["required_audit_checks"] = [f"fake-{index}" for index in range(10)]
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(state.PipelineStateError, "fixed ordered"):
                state.init_run(source, plan_path, root / "run")

    def test_same_run_concurrent_mutation_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            with state._run_lock(run):
                with self.assertRaisesRegex(state.PipelineStateError, "another process"):
                    state.record_stage(
                        run,
                        "photo_correction",
                        "no_change",
                        notes="The concurrent operation must not alter this run.",
                    )

    def test_tampered_proof_file_blocks_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            for stage_id in state.STAGE_ORDER[:-1]:
                state.record_stage(
                    run,
                    stage_id,
                    "no_change",
                    notes=f"Image-specific {stage_id} inspection found no safe change was needed.",
                )
            run_state = json.loads((run / "run.json").read_text(encoding="utf-8"))
            current = run / run_state["current_checkpoint"]["path"]
            audit_path = make_accepted_audit(run, current, "tamper-proof")
            proof_file = run / "proof-tamper-proof" / "comparison.png"
            proof_file.write_bytes(proof_file.read_bytes() + b"tamper")
            with self.assertRaisesRegex(state.PipelineStateError, "proof revalidation"):
                state.finalize(run, audit_path)

    def test_isolated_model_candidate_requires_reserved_one_call_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            candidate = root / "candidate.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            Image.open(source).save(candidate)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            request_path, prompt_path = make_model_request(root)
            reserved = state.reserve_model_attempt(run, request_path, prompt_path)
            self.assertEqual(reserved["model_calls"], 1)
            completed_candidate = state.complete_model_attempt(run, candidate)
            copied_candidate = Path(completed_candidate["candidate"])
            audit_path = make_accepted_audit(run, copied_candidate, "model")
            result = state.record_model_candidate(
                run,
                audit_path,
                notes="One isolated candidate reviewed across all seven logical stages.",
            )
            self.assertEqual(result["next_stage"], "final_review")
            run_state = json.loads((run / "run.json").read_text(encoding="utf-8"))
            self.assertFalse(run_state["model"]["chained"])
            self.assertFalse(run_state["model"]["retried"])
            self.assertEqual(run_state["model"]["calls_recorded"], 1)
            with self.assertRaisesRegex(state.PipelineStateError, "before any stage attempt"):
                state.reserve_model_attempt(run, request_path, prompt_path)
            completed = state.finalize(run, audit_path)
            self.assertEqual(completed["status"], "complete")

    def test_failed_model_attempt_consumes_call_and_allows_deterministic_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            request_path, prompt_path = make_model_request(root)
            state.reserve_model_attempt(run, request_path, prompt_path)
            failed = state.fail_model_attempt(
                run, "The external image service timed out before returning a candidate."
            )
            self.assertEqual(failed["model_calls"], 1)
            with self.assertRaisesRegex(state.PipelineStateError, "already consumed"):
                state.reserve_model_attempt(run, request_path, prompt_path)
            fallback = state.record_stage(
                run,
                "photo_correction",
                "no_change",
                notes="Geometry and orientation inspection found no correction was needed.",
            )
            self.assertEqual(fallback["next_stage"], "light_balance")

    def test_rejected_model_attempt_consumes_call_and_request_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "portrait.png"
            candidate = root / "candidate.png"
            plan_path = root / "plan.json"
            make_portrait(source)
            Image.open(source).save(candidate)
            compile_plan(plan_path)
            run = root / "run"
            state.init_run(source, plan_path, run)
            request_path, prompt_path = make_model_request(root)
            state.reserve_model_attempt(run, request_path, prompt_path)
            state.complete_model_attempt(run, candidate)
            rejected = state.reject_model_attempt(
                run,
                "Hair edges showed a visible halo at 100% view, so the candidate was rejected.",
            )
            self.assertFalse(rejected["retry_allowed"])
            with self.assertRaisesRegex(state.PipelineStateError, "already consumed"):
                state.reserve_model_attempt(run, request_path, prompt_path)

            copied_request = run / "model" / "request.json"
            document = json.loads(copied_request.read_text(encoding="utf-8"))
            document["mode"] = "fresh"
            copied_request.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(state.PipelineStateError, "model request changed"):
                state.validate_run(run)


if __name__ == "__main__":
    unittest.main()
