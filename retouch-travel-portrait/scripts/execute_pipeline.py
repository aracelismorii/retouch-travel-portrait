#!/usr/bin/env python3
"""Execute one deterministic or inspection-only stage of a V6 run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import check_geometry
import pipeline_state
import retouch_image


DETERMINISTIC_STAGES = {
    "photo_correction",
    "light_balance",
    "color_balance",
    "skin_cleanup",
}
INSPECTION_ONLY_STAGES = {"facial_features", "hair_clothing", "background"}
DELTA_LIMITS = {
    "light_balance": 24.0,
    "color_balance": 18.0,
    "skin_cleanup": 8.0,
}


class PipelineExecutionError(ValueError):
    """Raised when a V6 stage cannot be executed safely."""


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PipelineExecutionError(f"expected a JSON object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_plan(plan: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in plan.get("stages", []):
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            return stage
    raise PipelineExecutionError(f"stage is missing from plan: {stage_id}")


def execute_stage(
    run_dir: Path,
    stage_id: str,
    *,
    confirm_inspection: bool = False,
    review_decision: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state_path = run_dir / "run.json"
    plan_path = run_dir / "plan.json"
    state = _read_json(state_path)
    plan = _read_json(plan_path)
    if state.get("schema_version") != 6 or plan.get("schema_version") != 6:
        raise PipelineExecutionError("execute_pipeline.py requires a V6 run")
    if state.get("current_stage") != stage_id:
        raise PipelineExecutionError(
            f"expected stage {state.get('current_stage')}, got {stage_id}"
        )
    stage = _stage_plan(plan, stage_id)
    current = run_dir / str(state["current_checkpoint"]["path"])

    if stage_id in INSPECTION_ONLY_STAGES:
        if review_decision is not None:
            raise PipelineExecutionError(
                "inspection-only stages use --confirm-inspection, not --review-decision"
            )
        if not confirm_inspection:
            raise PipelineExecutionError(
                f"{stage_id} requires visual inspection and --confirm-inspection"
            )
        reason = notes or (
            "Visual inspection found no safe localized change that justified a "
            "semantic edit; the accepted checkpoint remains unchanged."
        )
        return pipeline_state.record_stage(
            run_dir,
            stage_id,
            "no_change",
            input_path=current,
            notes=reason,
        )

    if stage_id not in DETERMINISTIC_STAGES:
        raise PipelineExecutionError(f"unsupported executable stage: {stage_id}")
    if stage.get("action") == "no_change":
        if review_decision is not None:
            raise PipelineExecutionError(
                "planned no-change stages use --confirm-inspection, not --review-decision"
            )
        if not confirm_inspection:
            raise PipelineExecutionError(
                f"{stage_id} is a planned no-change and requires --confirm-inspection"
            )
        return pipeline_state.record_stage(
            run_dir,
            stage_id,
            "no_change",
            input_path=current,
            notes=notes or str(stage.get("no_change_reason", "No safe change needed.")),
        )

    if confirm_inspection:
        raise PipelineExecutionError(
            "applied stages require two steps: first generate the candidate, then "
            "use --review-decision accept or reject after viewing it"
        )

    if stage_id == "photo_correction" and any(
        float(value) > 0 for value in stage.get("parameters", {}).values()
    ):
        raise PipelineExecutionError(
            "automatic geometric photo correction is intentionally unavailable; "
            "inspect and record a geometry-safe external candidate or set this stage to zero"
        )

    candidates = run_dir / "candidates"
    candidates.mkdir(exist_ok=True, mode=0o700)
    os.chmod(candidates, 0o700)
    output = candidates / f"stage-{stage['index']:02d}-{stage_id}.png"
    review_path = candidates / f"stage-{stage['index']:02d}-{stage_id}.review.json"

    if review_decision is not None:
        if review_decision not in {"accept", "reject"}:
            raise PipelineExecutionError("review decision must be accept or reject")
        if not notes.strip():
            raise PipelineExecutionError("visual review requires image-specific --notes")
        if not output.is_file() or not review_path.is_file():
            raise PipelineExecutionError(
                "no pending candidate exists; run the stage once before reviewing it"
            )
        review = _read_json(review_path)
        if (
            review.get("stage") != stage_id
            or review.get("status") != "awaiting_visual_review"
            or review.get("input_sha256") != _sha256(current)
            or review.get("candidate_sha256") != _sha256(output)
        ):
            raise PipelineExecutionError(
                "pending candidate provenance changed; do not accept or chain it"
            )
        if review_decision == "accept":
            result = pipeline_state.record_stage(
                run_dir,
                stage_id,
                "accepted",
                input_path=current,
                candidate_path=output,
                notes=notes,
            )
            review["status"] = "accepted_after_visual_review"
            review["review_notes"] = notes
            _write_json(review_path, review)
            return {
                **result,
                "candidate": str(output),
                "review_manifest": str(review_path),
                "geometry": review["geometry"],
                "metrics": review["metrics"],
            }

        pipeline_state.record_stage(
            run_dir,
            stage_id,
            "rejected",
            input_path=current,
            candidate_path=output,
            notes=notes,
        )
        rollback = pipeline_state.record_stage(
            run_dir,
            stage_id,
            "no_change",
            input_path=current,
            notes="Visual review rejected the candidate; retained the prior checkpoint. "
            + notes,
        )
        review["status"] = "rejected_and_rolled_back"
        review["review_notes"] = notes
        _write_json(review_path, review)
        return {
            **rollback,
            "decision": "rejected_and_rolled_back",
            "candidate": str(output),
            "review_manifest": str(review_path),
            "geometry": review["geometry"],
            "metrics": review["metrics"],
        }

    if output.exists():
        raise PipelineExecutionError(
            f"pending candidate exists; review it instead of overwriting: {output}"
        )
    metrics = retouch_image.apply_v6_stage(
        current,
        output,
        stage_id,
        dict(stage.get("parameters", {})),
        mode=str(plan.get("mode", "natural")),
    )
    geometry = check_geometry.compare_geometry(
        run_dir / str(state["original"]["path"]), output
    )
    mean_delta = float(metrics["delta"]["mean_absolute_channel_delta"])
    maximum = DELTA_LIMITS.get(stage_id)
    review = {
        "schema_version": 6,
        "stage": stage_id,
        "status": "awaiting_visual_review",
        "input": str(current),
        "input_sha256": _sha256(current),
        "candidate": str(output),
        "candidate_sha256": _sha256(output),
        "geometry": geometry,
        "metrics": metrics,
    }
    _write_json(review_path, review)
    if geometry["decision"] != "accept" or (
        maximum is not None and mean_delta > maximum
    ):
        reason = (
            "Deterministic candidate failed exact geometry or bounded pixel-delta gate."
        )
        pipeline_state.record_stage(
            run_dir,
            stage_id,
            "rejected",
            input_path=current,
            candidate_path=output,
            notes=reason,
        )
        rollback = pipeline_state.record_stage(
            run_dir,
            stage_id,
            "no_change",
            input_path=current,
            notes="Mechanical gate rejected the candidate; retained the prior checkpoint.",
        )
        review["status"] = "mechanically_rejected_and_rolled_back"
        review["review_notes"] = reason
        _write_json(review_path, review)
        return {
            **rollback,
            "decision": "mechanically_rejected_and_rolled_back",
            "stage": stage_id,
            "candidate": str(output),
            "review_manifest": str(review_path),
            "geometry": geometry,
            "metrics": metrics,
            "reason": reason,
        }

    if float(metrics["delta"]["changed_pixel_fraction"]) == 0.0:
        decision = "no_change"
        result = pipeline_state.record_stage(
            run_dir,
            stage_id,
            decision,
            input_path=current,
            notes=notes or str(metrics["details"].get("reason", "No pixel change needed.")),
        )
        review["status"] = "no_pixel_change"
        review["review_notes"] = str(
            metrics["details"].get("reason", "No pixel change needed.")
        )
        _write_json(review_path, review)
    else:
        return {
            "status": "awaiting_visual_review",
            "stage": stage_id,
            "decision": None,
            "next_stage": stage_id,
            "current_checkpoint": str(current),
            "candidate": str(output),
            "review_manifest": str(review_path),
            "geometry": geometry,
            "metrics": metrics,
            "instructions": (
                "Inspect the candidate against the immutable original, then rerun "
                "with --review-decision accept or reject and image-specific --notes."
            ),
        }
    return {
        **result,
        "candidate": str(output),
        "review_manifest": str(review_path),
        "geometry": geometry,
        "metrics": metrics,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute one ordered V6 portrait-retouch stage."
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--stage",
        choices=sorted(DETERMINISTIC_STAGES | INSPECTION_ONLY_STAGES),
        required=True,
    )
    parser.add_argument("--confirm-inspection", action="store_true")
    parser.add_argument("--review-decision", choices=["accept", "reject"])
    parser.add_argument("--notes", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute_stage(
            args.run_dir,
            args.stage,
            confirm_inspection=args.confirm_inspection,
            review_decision=args.review_decision,
            notes=args.notes,
        )
    except (
        OSError,
        json.JSONDecodeError,
        PipelineExecutionError,
        pipeline_state.PipelineStateError,
        retouch_image.RetouchError,
        check_geometry.GeometryError,
    ) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
