#!/usr/bin/env python3
"""Execute and validate a prepared V3 portrait-retouch run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import check_geometry
import retouch_image


class ExecuteError(ValueError):
    """Raised when a prepared run is invalid or cannot finish safely."""


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ExecuteError(f"expected a JSON object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _quality_decision(
    stage_id: str,
    result: dict[str, Any],
    limits: dict[str, Any],
) -> tuple[bool, str]:
    delta = result["delta"]
    mean_delta = float(delta["mean_absolute_channel_delta"])
    changed_fraction = float(delta["changed_pixel_fraction"])
    if stage_id == "lighting":
        maximum = float(limits["lighting_mean_absolute_channel_delta_max"])
        if mean_delta > maximum:
            return False, f"lighting mean channel delta {mean_delta:g} exceeds {maximum:g}"
    elif stage_id == "skin":
        maximum = float(limits["skin_mean_absolute_channel_delta_max"])
        fraction_max = float(limits["skin_changed_pixel_fraction_max"])
        if mean_delta > maximum:
            return False, f"skin mean channel delta {mean_delta:g} exceeds {maximum:g}"
        if changed_fraction > fraction_max:
            return False, (
                f"skin changed-pixel fraction {changed_fraction:g} exceeds "
                f"{fraction_max:g}"
            )
    return True, "bounded deterministic edit"


def execute_run(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest_path = run_dir / "run.json"
    plan_path = run_dir / "plan.json"
    if not run_dir.is_dir() or not manifest_path.is_file() or not plan_path.is_file():
        raise ExecuteError("run directory must contain plan.json and run.json")

    manifest = _read_json(manifest_path)
    plan = _read_json(plan_path)
    if plan.get("schema_version") != 3 or plan.get("preset") != "natural-v3":
        raise ExecuteError("execute_run.py requires a natural-v3 plan")
    if manifest.get("status") not in {"ready", "no-edit-needed"}:
        raise ExecuteError("run has already been executed or is not ready")

    original = run_dir / str(manifest["original_copy"])
    if not original.is_file():
        raise ExecuteError("immutable original is missing")

    stages = plan.get("stages", [])
    if not stages:
        manifest["status"] = "complete"
        manifest["events"].append(
            {"type": "completed", "message": "All parameters were zero; original returned."}
        )
        _write_json(manifest_path, manifest)
        return {
            "status": "complete",
            "final": str(original),
            "accepted_stages": [],
            "geometry_pass_rate": 1.0,
        }

    current = original
    accepted: list[str] = []
    geometry_passes = 0
    for stage in stages:
        stage_id = str(stage["id"])
        candidate = run_dir / str(stage["suggested_output_name"])
        result = retouch_image.apply_stage(
            current,
            candidate,
            stage_id,
            {name: float(value) for name, value in stage["parameters"].items()},
        )
        geometry = check_geometry.compare_geometry(original, candidate)
        if geometry["decision"] == "accept":
            geometry_passes += 1
        quality_ok, quality_reason = _quality_decision(
            stage_id, result, plan["quality_limits"]
        )
        if geometry["decision"] != "accept" or not quality_ok:
            manifest["status"] = "rolled_back"
            manifest["current_accepted_image"] = current.name
            manifest["accepted_stages"] = accepted
            manifest["events"].append(
                {
                    "type": "stage_rejected",
                    "stage": stage_id,
                    "candidate": candidate.name,
                    "geometry": geometry,
                    "quality_reason": quality_reason,
                }
            )
            _write_json(manifest_path, manifest)
            return {
                "status": "rolled_back",
                "final": str(current),
                "rejected_candidate": str(candidate),
                "accepted_stages": accepted,
                "geometry_pass_rate": geometry_passes / len(stages),
            }

        accepted.append(stage_id)
        current = candidate
        manifest["current_accepted_image"] = candidate.name
        manifest["accepted_stages"] = accepted.copy()
        manifest["events"].append(
            {
                "type": "stage_accepted",
                "stage": stage_id,
                "candidate": candidate.name,
                "geometry": geometry,
                "quality": {"decision": "accept", "reason": quality_reason},
                "metrics": result,
            }
        )
        _write_json(manifest_path, manifest)

    manifest["status"] = "complete"
    manifest["events"].append(
        {"type": "completed", "message": "All deterministic stages passed."}
    )
    _write_json(manifest_path, manifest)
    return {
        "status": "complete",
        "final": str(current),
        "accepted_stages": accepted,
        "geometry_pass_rate": geometry_passes / len(stages),
        "planned_model_calls": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute a prepared natural-v3 portrait-retouch run."
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute_run(args.run_dir)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result["status"] == "complete" else 2
    except (
        OSError,
        json.JSONDecodeError,
        ExecuteError,
        retouch_image.RetouchError,
        check_geometry.GeometryError,
    ) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
