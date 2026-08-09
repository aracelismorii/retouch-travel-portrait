from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load_module("audit_result", SCRIPTS / "audit_result.py")
proof = load_module("render_proof", SCRIPTS / "render_proof.py")


def make_fixture(path: Path, *, overexposed: bool = False) -> None:
    width, height = 800, 1200
    if overexposed:
        Image.new("RGB", (width, height), (255, 255, 255)).save(path)
        return
    image = Image.new("RGB", (width, height), (42, 68, 91))
    draw = ImageDraw.Draw(image)
    draw.rectangle((72, 90, 728, 1100), outline=(122, 148, 164), width=5)
    draw.ellipse((238, 120, 562, 510), fill=(198, 145, 122))
    draw.rectangle((190, 520, 610, 1199), fill=(63, 51, 76))
    for y in range(180, 470, 24):
        color = 184 + ((y // 24) % 2) * 12
        draw.line((300, y, 500, y), fill=(color, 126, 106), width=2)
    draw.ellipse((315, 270, 345, 292), fill=(35, 30, 28))
    draw.ellipse((455, 270, 485, 292), fill=(35, 30, 28))
    draw.arc((345, 335, 455, 420), 15, 165, fill=(130, 54, 55), width=6)
    image.save(path)


MANUAL_NOTES = {
    "skin_texture_natural": "Skin pores and fine texture remain visible across both cheeks at 100% view.",
    "face_neck_color_consistent": "Face and neck retain the same warm undertone without a visible color boundary.",
    "sclera_teeth_not_blue": "Both visible eye whites remain neutral and no teeth are exposed in this portrait.",
    "lip_saturation_natural": "Lip color remains muted red with clean edges and no oversaturated pixels.",
    "background_lines_straight": "The vertical wall frame and horizontal background line remain straight.",
    "hair_edges_clean": "Hair edges retain individual flyaway strands without halos or painted smearing.",
    "subject_background_brightness_cohesive": "Subject brightness remains consistent with the darker background lighting.",
    "zoom_and_thumbnail_coherent": "The 100% detail and 720 thumbnail both preserve the same natural facial appearance.",
    "change_not_excessive_vs_original": "Original and candidate side-by-side views show only a small tonal change in the heatmap.",
    "phone_viewing_comfortable": "The 1080 phone preview retains comfortable highlights and open shadow detail.",
}


def complete_manual(status: str = "pass") -> dict[str, dict[str, str]]:
    return {
        check_id: {"status": status, "note": MANUAL_NOTES[check_id]}
        for check_id in audit.CHECK_IDS
    }


def render_manifest(root: Path, original: Path, candidate: Path) -> Path:
    output = root / "proof"
    proof.render_proofs(original, candidate, output)
    manifest = output / proof.MANIFEST_NAME
    if not manifest.is_file():
        raise AssertionError("render_proofs did not persist its manifest")
    return manifest


class AuditV6Tests(unittest.TestCase):
    def test_manual_review_template_is_complete_but_cannot_auto_accept(self) -> None:
        template_path = SKILL_ROOT / "references" / "manual-review-template.json"
        template = audit.load_manual_review(str(template_path))
        self.assertEqual(tuple(template["checks"]), audit.CHECK_IDS)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            with self.assertRaises(audit.AuditConfigError):
                audit.audit_images(original, candidate, manual_review=template)

    def test_output_always_has_the_fixed_ten_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            result = audit.audit_images(original, candidate)
            self.assertEqual(tuple(result["checks"]), audit.CHECK_IDS)
            self.assertEqual(len(result["checks"]), 10)
            self.assertTrue(
                all(
                    item["status"] in audit.VALID_STATUSES
                    for item in result["checks"].values()
                )
            )

    def test_missing_manual_review_can_never_accept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            result = audit.audit_images(original, candidate)
            self.assertEqual(result["decision"], "visual_review_required")
            self.assertFalse(result["summary"]["manual_review_complete"])
            self.assertFalse(result["summary"]["may_auto_accept"])
            self.assertEqual(
                result["checks"]["skin_texture_natural"]["status"],
                "not_evaluable",
            )

    def test_any_manual_failure_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            manual = complete_manual()
            manual["hair_edges_clean"] = {
                "status": "fail",
                "note": "Hair edges show a bright halo and smeared flyaway strands above the left shoulder.",
            }
            manifest = render_manifest(root, original, candidate)
            result = audit.audit_images(
                original,
                candidate,
                manual_review=manual,
                proof_manifest=manifest,
            )
            self.assertEqual(result["decision"], "reject")
            self.assertEqual(result["checks"]["hair_edges_clean"]["status"], "reject")
            self.assertEqual(
                result["checks"]["hair_edges_clean"]["manual_status"],
                "fail",
            )

    def test_complete_manual_pass_and_current_proof_accept_safe_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            manifest = render_manifest(root, original, candidate)
            result = audit.audit_images(
                original,
                candidate,
                manual_review={"checks": complete_manual()},
                proof_manifest=manifest,
            )
            self.assertEqual(result["decision"], "accept")
            self.assertTrue(result["summary"]["manual_review_complete"])
            self.assertTrue(result["summary"]["proof_verified"])
            self.assertEqual(result["proof"]["status"], "verified")
            self.assertEqual(
                result["proof"]["inputs"]["candidate_sha256"],
                result["inputs"]["candidate"]["sha256"],
            )
            self.assertTrue(all(item["status"] == "pass" for item in result["checks"].values()))

    def test_complete_manual_pass_without_proof_cannot_accept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            result = audit.audit_images(
                original,
                candidate,
                manual_review={"checks": complete_manual()},
            )
            self.assertEqual(result["decision"], "visual_review_required")
            self.assertFalse(result["summary"]["proof_verified"])
            self.assertEqual(result["proof"]["status"], "missing")

    def test_plain_string_manual_status_and_missing_note_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            manifest = render_manifest(root, original, candidate)
            plain: dict[str, object] = complete_manual()
            plain["skin_texture_natural"] = "pass"
            with self.assertRaisesRegex(audit.AuditConfigError, "object with status"):
                audit.audit_images(
                    original,
                    candidate,
                    manual_review=plain,
                    proof_manifest=manifest,
                )

            missing: dict[str, object] = complete_manual()
            missing["skin_texture_natural"] = {"status": "pass"}
            with self.assertRaisesRegex(audit.AuditConfigError, "include a text note"):
                audit.audit_images(
                    original,
                    candidate,
                    manual_review=missing,
                    proof_manifest=manifest,
                )

    def test_placeholder_and_generic_manual_notes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            manifest = render_manifest(root, original, candidate)
            for note in (
                "Describe the 100% texture evidence.",
                "Skin check looks good and passes this test.",
            ):
                manual: dict[str, object] = complete_manual()
                manual["skin_texture_natural"] = {"status": "pass", "note": note}
                with self.subTest(note=note), self.assertRaises(audit.AuditConfigError):
                    audit.audit_images(
                        original,
                        candidate,
                        manual_review=manual,
                        proof_manifest=manifest,
                    )

    def test_not_applicable_manual_status_requires_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            manual: dict[str, object] = complete_manual()
            manual["sclera_teeth_not_blue"] = {
                "status": "not_applicable",
                "note": "",
            }
            with self.assertRaises(audit.AuditConfigError):
                audit.audit_images(original, candidate, manual_review=manual)
            manual["sclera_teeth_not_blue"] = {
                "status": "not_applicable",
                "note": "No teeth are visible and both eyes are fully closed in this portrait.",
            }
            manifest = render_manifest(root, original, candidate)
            result = audit.audit_images(
                original,
                candidate,
                manual_review=manual,
                proof_manifest=manifest,
            )
            self.assertEqual(result["decision"], "accept")
            self.assertEqual(
                result["checks"]["sclera_teeth_not_blue"]["manual_status"],
                "not_applicable",
            )

    def test_extreme_overexposure_is_a_hard_reject(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate, overexposed=True)
            manifest = render_manifest(root, original, candidate)
            result = audit.audit_images(
                original,
                candidate,
                manual_review=complete_manual(),
                proof_manifest=manifest,
            )
            self.assertEqual(result["decision"], "reject")
            self.assertEqual(result["summary"]["automated_precheck"], "reject")
            self.assertIn(
                result["checks"]["phone_viewing_comfortable"]["automatic_status"],
                {"reject"},
            )

    def test_render_proof_creates_all_five_review_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            output = root / "proof"
            make_fixture(original)
            make_fixture(candidate)
            array = np.asarray(Image.open(candidate).convert("RGB"), dtype=np.uint8).copy()
            array[..., 0] = np.clip(array[..., 0].astype(np.int16) + 4, 0, 255)
            Image.fromarray(array).save(candidate)
            manifest = proof.render_proofs(original, candidate, output)
            manifest_path = output / proof.MANIFEST_NAME
            self.assertTrue(manifest_path.is_file())
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted, manifest)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["generator"], proof.PROOF_GENERATOR)
            self.assertEqual(len(manifest["original_sha256"]), 64)
            self.assertEqual(len(manifest["candidate_sha256"]), 64)
            self.assertEqual(set(manifest["files"]), set(proof.OUTPUT_NAMES))
            self.assertEqual(set(manifest["file_sha256"]), set(proof.OUTPUT_NAMES))
            self.assertEqual(
                [point["role"] for point in manifest["detail_focus_points_normalized"]],
                ["skin_density_primary", "upper_center_fallback"],
            )
            self.assertTrue(
                {"100_percent", "fit_view", "thumbnail", "phone_normal_brightness"}
                <= set(manifest["views"])
            )
            for name in proof.OUTPUT_NAMES.values():
                path = output / name
                self.assertTrue(path.is_file(), name)
                with Image.open(path) as image:
                    image.verify()
            with Image.open(output / "thumbnail-720.png") as thumbnail:
                self.assertEqual(thumbnail.size, (720, 720))
            with Image.open(output / "mobile-preview-1080.png") as mobile:
                self.assertEqual(mobile.size, (1080, 1920))
            with Image.open(output / "detail-100-percent.png") as detail:
                self.assertGreater(detail.height, detail.width)

    def test_stale_and_tampered_proof_cannot_accept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            manifest = render_manifest(root, original, candidate)
            pixels = np.asarray(Image.open(candidate).convert("RGB"), dtype=np.uint8).copy()
            pixels[0, 0, 0] = (int(pixels[0, 0, 0]) + 1) % 256
            Image.fromarray(pixels).save(candidate)
            with self.assertRaisesRegex(audit.AuditConfigError, "stale for the candidate"):
                audit.audit_images(
                    original,
                    candidate,
                    manual_review=complete_manual(),
                    proof_manifest=manifest,
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            make_fixture(original)
            make_fixture(candidate)
            manifest = render_manifest(root, original, candidate)
            thumbnail = manifest.parent / proof.OUTPUT_NAMES["thumbnail_720"]
            thumbnail.write_bytes(thumbnail.read_bytes() + b"tampered")
            with self.assertRaisesRegex(audit.AuditConfigError, "modified after rendering"):
                audit.audit_images(
                    original,
                    candidate,
                    manual_review=complete_manual(),
                    proof_manifest=manifest,
                )

    def test_cli_requires_proof_manifest_and_accepts_verified_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            review_path = root / "manual.json"
            audit_path = root / "audit.json"
            make_fixture(original)
            make_fixture(candidate)
            review_path.write_text(
                json.dumps({"checks": complete_manual()}), encoding="utf-8"
            )
            manifest = render_manifest(root, original, candidate)
            with self.assertRaises(SystemExit):
                audit.build_parser().parse_args(
                    [str(original), str(candidate), "--output", str(audit_path)]
                )
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = audit.main(
                    [
                        str(original),
                        str(candidate),
                        "--manual-review",
                        str(review_path),
                        "--proof-manifest",
                        str(manifest),
                        "--output",
                        str(audit_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(audit_path.read_text(encoding="utf-8"))["decision"],
                "accept",
            )
            self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)

    def test_cli_visual_review_required_has_distinct_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            candidate = root / "candidate.png"
            audit_path = root / "pending-audit.json"
            make_fixture(original)
            make_fixture(candidate)
            manifest = render_manifest(root, original, candidate)
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = audit.main(
                    [
                        str(original),
                        str(candidate),
                        "--proof-manifest",
                        str(manifest),
                        "--output",
                        str(audit_path),
                    ]
                )
            self.assertEqual(exit_code, 3)
            self.assertEqual(
                json.loads(audit_path.read_text(encoding="utf-8"))["decision"],
                "visual_review_required",
            )


if __name__ == "__main__":
    unittest.main()
