from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
BATCH_PATH = SCRIPTS / "batch_pipeline_v6.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
batch = load_module("batch_pipeline_v6_test", BATCH_PATH)
state = load_module("pipeline_state_batch_test", SCRIPTS / "pipeline_state.py")
audit = load_module("audit_result_batch_test", SCRIPTS / "audit_result.py")
sys.path.pop(0)


MANUAL_NOTES = {
    "skin_texture_natural": "Skin pores and fine texture remain visible across both cheeks at 100% view.",
    "face_neck_color_consistent": "Face and neck retain the same warm undertone without a visible color boundary.",
    "sclera_teeth_not_blue": "Visible eye whites remain neutral and no exposed teeth show blue color.",
    "lip_saturation_natural": "Lip color remains muted red with clean edges and no oversaturated pixels.",
    "background_lines_straight": "The vertical frame and horizontal background line remain straight in fit view.",
    "hair_edges_clean": "Hair edges retain individual strands without halos or painted smearing at 100% view.",
    "subject_background_brightness_cohesive": "Subject brightness remains consistent with the darker background lighting.",
    "zoom_and_thumbnail_coherent": "The 100% detail and 720 thumbnail preserve the same natural facial appearance.",
    "change_not_excessive_vs_original": "Original and candidate side-by-side views show only a small bounded tonal change.",
    "phone_viewing_comfortable": "The 1080 phone preview retains comfortable highlights and open shadow detail.",
}


def make_image(path: Path, color=(85, 120, 145)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (800, 1000), color)
    image.putpixel((400, 500), (190, 130, 110))
    image.save(path)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def item_by_relative(manifest: dict, relative: str) -> dict:
    return next(item for item in manifest["items"] if item["relative_path"] == relative)


def finish_run(run_dir: Path, *, changed: bool) -> None:
    run_state = read_json(run_dir / "run.json")
    original = run_dir / run_state["original"]["path"]
    for stage_id in state.STAGE_ORDER[:-1]:
        if changed and stage_id == "light_balance":
            candidate = run_dir.parent / f"{run_dir.name}-accepted.png"
            image = Image.open(original).convert("RGB")
            image.point(lambda value: min(255, value + 1)).save(candidate)
            state.record_stage(
                run_dir,
                stage_id,
                "accepted",
                candidate_path=candidate,
                notes="Image-specific visual review accepted this bounded candidate.",
            )
        else:
            state.record_stage(
                run_dir,
                stage_id,
                "no_change",
                notes="Image-specific inspection found no safe change was needed.",
            )
    current_state = read_json(run_dir / "run.json")
    current = run_dir / current_state["current_checkpoint"]["path"]
    proof_dir = run_dir / "proof-final"
    batch.render_proof.render_proofs(original, current, proof_dir)
    report = audit.audit_images(
        original,
        current,
        mode=read_json(run_dir / "plan.json")["mode"],
        manual_review={
            "checks": {
                check_id: {"status": "pass", "note": MANUAL_NOTES[check_id]}
                for check_id in audit.CHECK_IDS
            }
        },
        proof_manifest=proof_dir / "manifest.json",
    )
    if report["decision"] != "accept":
        raise AssertionError(f"batch fixture audit failed: {report['summary']}")
    audit_path = run_dir / "batch-review.json"
    write_json(audit_path, report)
    state.finalize(run_dir, audit_path)


class BatchPipelineV6Tests(unittest.TestCase):
    def test_recursive_discovery_is_relative_path_sorted_and_excludes_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = inputs / "generated-queue"
            make_image(inputs / "B.JPG")
            make_image(inputs / "a" / "x.png")
            make_image(inputs / "z.webp")
            make_image(output / "must-not-be-discovered.png")
            (inputs / "ignore.gif").write_bytes(b"GIF89a")

            discovered = batch.discover_images(inputs, exclude=[output])
            self.assertEqual(
                [path.relative_to(inputs.resolve()).as_posix() for path in discovered],
                ["a/x.png", "B.JPG", "z.webp"],
            )

    def test_stable_run_ids_are_readable_deterministic_and_collision_resistant(self) -> None:
        first = batch.stable_run_id("same/a b.jpg")
        second = batch.stable_run_id("same/a-b.jpg")
        third = batch.stable_run_id("same/a b.png")
        self.assertEqual(first, batch.stable_run_id(Path("same/a b.jpg")))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertTrue(first.startswith("same-a-b--"))
        self.assertEqual(len(first.rsplit("--", 1)[1]), 32)
        self.assertNotEqual(
            batch.stable_run_id("照片.jpg"), batch.stable_run_id("相片.jpg")
        )

    def test_nested_output_is_not_rediscovered_on_idempotent_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs = Path(temporary) / "input"
            output = inputs / "queue-output"
            make_image(inputs / "portrait.png")
            prepared = batch.prepare_queue(inputs, output)
            self.assertEqual(prepared["counts"]["total"], 1)
            self.assertEqual(prepared["items"][0]["relative_path"], "portrait.png")

            resumed = batch.resume_queue(inputs, output)
            self.assertEqual(resumed["counts"]["total"], 1)
            self.assertEqual(resumed["items"][0]["relative_path"], "portrait.png")

    def test_prepare_builds_independent_v6_runs_and_isolates_bad_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            make_image(inputs / "a.png", (80, 100, 120))
            make_image(inputs / "nested" / "b.jpg", (95, 115, 135))
            bad = inputs / "nested" / "broken.webp"
            bad.write_bytes(b"not a webp")

            manifest = batch.prepare_queue(
                inputs,
                output,
                mode="fresh",
                overrides={"skin_cleanup.skin_smoothing": 0.2},
            )

            self.assertEqual(
                [item["relative_path"] for item in manifest["items"]],
                ["a.png", "nested/b.jpg", "nested/broken.webp"],
            )
            self.assertEqual(
                manifest["counts"],
                {
                    "total": 3,
                    "complete": 0,
                    "safe_but_subtle": 0,
                    "partial_v3_fallback": 0,
                    "failed": 1,
                    "awaiting_review": 0,
                    "prepared": 2,
                },
            )
            self.assertFalse(manifest["policy"]["auto_accept_visual_gates"])
            self.assertEqual(manifest["policy"]["model_calls_by_queue_runner"], 0)
            self.assertFalse(manifest["policy"]["reuse_review_decisions"])
            plan = read_json(output / batch.PLAN_NAME)
            self.assertEqual(plan["schema_version"], 6)
            self.assertEqual(plan["mode"], "fresh")
            skin = next(stage for stage in plan["stages"] if stage["id"] == "skin_cleanup")
            self.assertEqual(skin["parameters"]["skin_smoothing"], 0.2)

            good_items = [
                item for item in manifest["items"] if item["classification"] == "prepared"
            ]
            self.assertEqual(len({item["id"] for item in good_items}), 2)
            original_paths = []
            for item in good_items:
                run_dir = Path(item["run_dir"])
                run_state = read_json(run_dir / "run.json")
                self.assertEqual(run_state["schema_version"], 6)
                self.assertEqual(run_state["current_stage"], "photo_correction")
                self.assertEqual(run_state["source_sha256"], item["source_sha256"])
                self.assertEqual(
                    run_state["source_sha256"], file_hash(Path(item["source_path"]))
                )
                original_paths.append(run_dir / run_state["original"]["path"])
            self.assertNotEqual(original_paths[0], original_paths[1])
            failed = item_by_relative(manifest, "nested/broken.webp")
            self.assertIn("image", failed["error"].lower())
            self.assertFalse(Path(failed["run_dir"]).exists())
            self.assertFalse(any(output.rglob("*.tmp")))
            self.assertFalse(any(path.name.startswith(".") for path in (output / "runs").iterdir()))

    def test_preflight_failure_survives_status_and_resume_without_generic_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            disguised = inputs / "disguised.jpg"
            disguised.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (800, 1000), (85, 120, 145)).save(
                disguised, format="PNG"
            )

            prepared = batch.prepare_queue(inputs, output)
            prepared_item = prepared["items"][0]
            original_error = prepared_item["error"]
            self.assertEqual(prepared_item["classification"], "failed")
            self.assertIn("format_extension_mismatch", original_error)
            self.assertFalse(Path(prepared_item["run_dir"]).exists())

            refreshed = batch.refresh_status(inputs, output)
            refreshed_item = refreshed["items"][0]
            self.assertEqual(refreshed_item["classification"], "failed")
            self.assertEqual(refreshed_item["error"], original_error)
            self.assertNotIn("run has not been prepared", refreshed_item["error"])
            self.assertEqual(
                refreshed_item["source_sha256"], prepared_item["source_sha256"]
            )
            self.assertEqual(
                refreshed_item["observed_source_sha256"],
                prepared_item["observed_source_sha256"],
            )

            resumed = batch.resume_queue(inputs, output)
            resumed_item = resumed["items"][0]
            self.assertEqual(resumed_item["classification"], "failed")
            self.assertEqual(resumed_item["error"], original_error)
            self.assertNotIn("run has not been prepared", resumed_item["error"])
            self.assertFalse(Path(resumed_item["run_dir"]).exists())

            second_refresh = batch.refresh_status(inputs, output)
            self.assertEqual(second_refresh["items"][0]["error"], original_error)

            make_image(disguised, (100, 130, 160))
            corrected_status = batch.refresh_status(inputs, output)
            corrected_item = corrected_status["items"][0]
            self.assertEqual(corrected_item["classification"], "failed")
            self.assertIn("run has not been prepared", corrected_item["error"])
            self.assertNotIn("format_extension_mismatch", corrected_item["error"])

            corrected_resume = batch.resume_queue(inputs, output)
            corrected_item = corrected_resume["items"][0]
            self.assertEqual(corrected_item["classification"], "prepared")
            self.assertNotIn("error", corrected_item)
            self.assertTrue(Path(corrected_item["run_dir"]).is_dir())

    def test_prepare_and_resume_are_idempotent_and_only_add_missing_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            make_image(inputs / "one.png")
            first = batch.prepare_queue(inputs, output, mode="natural")
            first_item = first["items"][0]
            first_run = Path(first_item["run_dir"])
            state_before = (first_run / "run.json").read_bytes()
            original_before = file_hash(
                first_run / read_json(first_run / "run.json")["original"]["path"]
            )

            second = batch.prepare_queue(inputs, output, mode="natural")
            self.assertEqual(second["items"][0]["id"], first_item["id"])
            self.assertEqual(second["items"][0]["prepared_at"], first_item["prepared_at"])
            self.assertEqual((first_run / "run.json").read_bytes(), state_before)
            self.assertEqual(
                file_hash(first_run / read_json(first_run / "run.json")["original"]["path"]),
                original_before,
            )
            self.assertEqual(len(list((output / "runs").iterdir())), 1)

            make_image(inputs / "two.png", (120, 90, 80))
            resumed = batch.resume_queue(inputs, output)
            self.assertEqual(resumed["counts"]["prepared"], 2)
            self.assertEqual(len(list((output / "runs").iterdir())), 2)
            self.assertEqual((first_run / "run.json").read_bytes(), state_before)

            with self.assertRaisesRegex(batch.BatchPipelineError, "differ"):
                batch.resume_queue(inputs, output, mode="fresh")

    def test_status_and_resume_never_advance_or_reuse_visual_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            make_image(inputs / "needs-review.png", (30, 40, 50))
            make_image(inputs / "untouched.png", (70, 90, 110))
            prepared = batch.prepare_queue(inputs, output)
            review_item = item_by_relative(prepared, "needs-review.png")
            review_run = Path(review_item["run_dir"])
            state.record_stage(
                review_run,
                "photo_correction",
                "no_change",
                notes="This portrait's geometry was inspected independently.",
            )
            before = {
                item["relative_path"]: (Path(item["run_dir"]) / "run.json").read_bytes()
                for item in prepared["items"]
            }

            refreshed = batch.refresh_status(inputs, output)
            self.assertEqual(refreshed["counts"]["awaiting_review"], 1)
            self.assertEqual(refreshed["counts"]["prepared"], 1)
            self.assertEqual(
                item_by_relative(refreshed, "needs-review.png")["current_stage"],
                "light_balance",
            )
            for item in refreshed["items"]:
                self.assertEqual(
                    (Path(item["run_dir"]) / "run.json").read_bytes(),
                    before[item["relative_path"]],
                )

            resumed = batch.resume_queue(inputs, output)
            self.assertEqual(resumed["counts"], refreshed["counts"])
            resumed_review = item_by_relative(resumed, "needs-review.png")
            self.assertEqual(resumed_review["resume_action"], "candidate_generated")
            self.assertTrue(Path(resumed_review["resume_result"]["candidate"]).is_file())
            for item in resumed["items"]:
                self.assertEqual(
                    (Path(item["run_dir"]) / "run.json").read_bytes(),
                    before[item["relative_path"]],
                )

    def test_resume_generates_final_review_proofs_without_finalizing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            make_image(inputs / "portrait.png", (55, 75, 95))
            manifest = batch.prepare_queue(inputs, output)
            run_dir = Path(manifest["items"][0]["run_dir"])
            for stage_id in state.STAGE_ORDER[:-1]:
                state.record_stage(
                    run_dir,
                    stage_id,
                    "no_change",
                    notes="This portrait was independently inspected at this stage.",
                )
            before = (run_dir / "run.json").read_bytes()

            resumed = batch.resume_queue(inputs, output)
            item = resumed["items"][0]
            self.assertEqual(item["resume_action"], "proof_generated")
            self.assertEqual(item["classification"], "awaiting_review")
            proof_dir = Path(item["proof_dir"])
            proof_manifest = read_json(proof_dir / "manifest.json")
            self.assertEqual(len(proof_manifest["files"]), 5)
            self.assertTrue(all(Path(path).is_file() for path in proof_manifest["files"].values()))
            self.assertEqual((run_dir / "run.json").read_bytes(), before)
            self.assertEqual(read_json(run_dir / "run.json")["status"], "in_progress")

            second = batch.resume_queue(inputs, output)
            self.assertEqual(second["items"][0]["resume_action"], "proof_already_current")
            self.assertEqual((run_dir / "run.json").read_bytes(), before)

            comparison = proof_dir / "comparison.png"
            comparison.write_bytes(comparison.read_bytes() + b"tampered")
            third = batch.resume_queue(inputs, output)
            self.assertEqual(third["items"][0]["classification"], "failed")
            self.assertIn("incomplete", third["items"][0]["error"])

    def test_status_detects_source_mutation_instead_of_reusing_review_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            source = inputs / "portrait.png"
            make_image(source)
            prepared = batch.prepare_queue(inputs, output)
            initial_hash = prepared["items"][0]["source_sha256"]
            make_image(source, (200, 40, 60))

            refreshed = batch.refresh_status(inputs, output)
            item = refreshed["items"][0]
            self.assertEqual(item["classification"], "failed")
            self.assertEqual(item["source_sha256"], initial_hash)
            self.assertNotEqual(item["observed_source_sha256"], initial_hash)
            self.assertIn("source content changed", item["error"])

    def test_manifest_reports_all_six_required_classifications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            names = [
                "01-complete.png",
                "02-subtle.png",
                "03-partial.png",
                "04-failed.png",
                "05-awaiting.png",
                "06-prepared.png",
            ]
            for index, name in enumerate(names):
                make_image(inputs / name, (70 + index * 8, 100, 125))
            manifest = batch.prepare_queue(inputs, output)

            complete_run = Path(item_by_relative(manifest, names[0])["run_dir"])
            subtle_run = Path(item_by_relative(manifest, names[1])["run_dir"])
            finish_run(complete_run, changed=True)
            finish_run(subtle_run, changed=False)

            partial_run = Path(item_by_relative(manifest, names[2])["run_dir"])
            partial_state = read_json(partial_run / "run.json")
            partial_state["delivery_classification"] = "partial_v3_fallback"
            write_json(partial_run / "run.json", partial_state)

            failed_run = Path(item_by_relative(manifest, names[3])["run_dir"])
            failed_state = read_json(failed_run / "run.json")
            failed_state["status"] = "failed"
            write_json(failed_run / "run.json", failed_state)

            awaiting_run = Path(item_by_relative(manifest, names[4])["run_dir"])
            state.record_stage(
                awaiting_run,
                "photo_correction",
                "no_change",
                notes="Independent visual inspection completed for this image.",
            )

            refreshed = batch.refresh_status(inputs, output)
            self.assertEqual(
                refreshed["counts"],
                {
                    "total": 6,
                    "complete": 1,
                    "safe_but_subtle": 1,
                    "partial_v3_fallback": 1,
                    "failed": 1,
                    "awaiting_review": 1,
                    "prepared": 1,
                },
            )
            classifications = {
                item["relative_path"]: item["classification"]
                for item in refreshed["items"]
            }
            self.assertEqual(classifications[names[0]], "complete")
            self.assertEqual(classifications[names[1]], "safe_but_subtle")
            self.assertEqual(classifications[names[2]], "partial_v3_fallback")
            self.assertEqual(classifications[names[3]], "failed")
            self.assertEqual(classifications[names[4]], "awaiting_review")
            self.assertEqual(classifications[names[5]], "prepared")

    def test_status_does_not_prepare_new_sources_but_resume_does(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            make_image(inputs / "one.png")
            batch.prepare_queue(inputs, output)
            make_image(inputs / "two.png")

            status = batch.refresh_status(inputs, output)
            second = item_by_relative(status, "two.png")
            self.assertEqual(second["classification"], "failed")
            self.assertIn("not been prepared", second["error"])
            self.assertFalse(Path(second["run_dir"]).exists())

            resumed = batch.resume_queue(inputs, output)
            second = item_by_relative(resumed, "two.png")
            self.assertEqual(second["classification"], "prepared")
            self.assertTrue(Path(second["run_dir"]).is_dir())

    def test_cli_supports_prepare_resume_and_status_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            output = root / "queue"
            make_image(inputs / "one.png")
            make_image(inputs / "first" / "portrait.jpg", (70, 90, 110))
            make_image(inputs / "second" / "portrait.jpg", (90, 110, 130))
            broken = inputs / "broken.webp"
            broken.write_bytes(b"malformed webp")
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(BATCH_PATH),
                    "prepare",
                    str(inputs),
                    str(output),
                    "--mode",
                    "warm-film",
                    "--set",
                    "skin_cleanup.skin_smoothing=0.18",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(prepared.stdout)
            self.assertEqual(payload["mode"], "warm-film")
            self.assertEqual(payload["counts"]["total"], 4)
            self.assertEqual(payload["counts"]["prepared"], 3)
            self.assertEqual(payload["counts"]["failed"], 1)
            duplicate_stems = [
                item for item in payload["items"] if item["relative_path"].endswith("portrait.jpg")
            ]
            self.assertEqual(len({item["id"] for item in duplicate_stems}), 2)
            self.assertIn(
                "image",
                item_by_relative(payload, "broken.webp")["error"].lower(),
            )

            refreshed = subprocess.run(
                [
                    sys.executable,
                    str(BATCH_PATH),
                    "refresh-status",
                    str(inputs),
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(refreshed.stdout)["phase"], "status")

            make_image(inputs / "two.png")
            resumed = subprocess.run(
                [
                    sys.executable,
                    str(BATCH_PATH),
                    "resume",
                    str(inputs),
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            resumed_payload = json.loads(resumed.stdout)
            self.assertEqual(resumed_payload["counts"]["prepared"], 4)
            self.assertEqual(resumed_payload["counts"]["failed"], 1)

            bad = subprocess.run(
                [
                    sys.executable,
                    str(BATCH_PATH),
                    "resume",
                    str(inputs),
                    str(output),
                    "--set",
                    "skin_cleanup.skin_smoothing=9",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(bad.returncode, 2)
            self.assertIn("error:", bad.stderr)


if __name__ == "__main__":
    unittest.main()
