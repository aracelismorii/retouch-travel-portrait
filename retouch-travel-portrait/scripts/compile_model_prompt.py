#!/usr/bin/env python3
"""Compile one optional V6 full-retouch model candidate prompt.

The V6 default path uses zero image-model calls. This compiler is only for an
explicitly enabled, isolated candidate generated from the immutable original.
It never emits a retry or permits a model output to become another model input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import compile_pipeline


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = SKILL_ROOT / "references" / "model-parameters.json"
PIPELINE_PATH = SKILL_ROOT / "references" / "pipeline-v6.json"
AUDIT_PATH = SKILL_ROOT / "references" / "audit-gates.json"
SEMANTIC_STAGE_IDS = frozenset({"facial_features", "hair_clothing", "background"})


class PromptConfigError(ValueError):
    """Raised when an optional model request violates the V6 contract."""


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PromptConfigError("model parameter schema must be a JSON object")
    return data


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PromptConfigError(f"expected a JSON object: {path}")
    return data


def parse_assignments(items: list[str]) -> dict[str, float]:
    try:
        return compile_pipeline.parse_assignments(items)
    except compile_pipeline.PipelineConfigError as exc:
        raise PromptConfigError(str(exc)) from exc


def list_modes(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_mode": schema["default_mode"],
        "mode_affected_stages": schema["mode_policy"]["affected_stages"],
        "modes": [
            {
                "name": name,
                "preset": definition["preset_name"],
                "display_name": definition["display_name"],
                "description": definition["description"],
                "stage_recipes": definition["stage_recipes"],
            }
            for name, definition in schema["modes"].items()
        ],
    }


def _format_parameters(parameters: dict[str, Any]) -> str:
    if not parameters:
        return "none; inspect only and make no pixel change"
    return ", ".join(f"{name}={value:g}" for name, value in parameters.items())


def compile_prompt(plan: dict[str, Any]) -> str:
    stages = [stage for stage in plan["stages"] if stage["id"] != "final_review"]
    stage_lines: list[str] = []
    for stage in stages:
        stage_lines.append(f"{stage['index']}. {stage['id']}")
        if stage["id"] in SEMANTIC_STAGE_IDS:
            active = dict(stage.get("explicit_nonzero_parameters", {}))
            preserved = list(stage.get("preserve_or_inspect_parameters", []))
            if active:
                active_targets = dict(
                    stage.get("explicit_nonzero_parameter_targets", {})
                )
                stage_lines.append(
                    "   Semantic modification explicitly requested: "
                    + _format_parameters(active)
                    + "."
                )
                stage_lines.append(
                    "   Modification permission is limited to those explicitly requested "
                    "nonzero controls; the general scope description grants no additional edits."
                )
                if preserved:
                    stage_lines.append(
                        "   Preserve/inspect without modification: "
                        + ", ".join(preserved)
                        + "."
                    )
                stage_lines.append(
                    "   Allowed targets for the requested controls: "
                    + "; ".join(
                        f"{name}: {active_targets[name]}" for name in active
                    )
                    + "."
                )
            else:
                stage_lines.append("   Semantic modification: none explicitly requested.")
                stage_lines.append(
                    "   Preserve/inspect only: "
                    + ", ".join(preserved)
                    + "; make no pixel changes in this stage."
                )
                stage_lines.append(
                    "   Inspection references only (not edit permission): "
                    + "; ".join(stage["allowed_changes"])
                    + "."
                )
        elif stage.get("action") == "no_change":
            stage_lines.extend(
                [
                    f"   Parameters: {_format_parameters(stage['parameters'])}.",
                    "   Preserve/inspect only; make no pixel changes in this stage.",
                    "   Inspection references only (not edit permission): "
                    + "; ".join(stage["allowed_changes"])
                    + ".",
                ]
            )
        else:
            stage_lines.extend(
                [
                    f"   Parameters: {_format_parameters(stage['parameters'])}.",
                    "   Allowed: " + "; ".join(stage["allowed_changes"]) + ".",
                    "   If no correction is visibly needed, make no change in this stage.",
                ]
            )
        stage_lines.append(
            "   Forbidden: " + "; ".join(stage["forbidden_changes"]) + "."
        )
        if stage.get("mode_instruction"):
            stage_lines.append("   Mode direction: " + stage["mode_instruction"])
    stage_lines.extend(
        [
            "8. final_review",
            "   Parameters: none; make no pixel changes in this stage.",
            "   Allowed: return the single candidate for external ten-item review.",
            "   Forbidden: self-certify success, hide uncertainty, or perform another edit.",
        ]
    )

    return "\n".join(
        [
            "Use case: identity-preserve",
            "Asset type: complete professional travel-portrait retouch, not a beauty-filter pass",
            "Input images: Image 1 is the immutable original and the only model input and edit target.",
            f"Selected mode: {plan['mode']} ({plan['preset']}).",
            (
                "Work internally in the exact numbered order below. First inspect whole-photo "
                "geometry, then balance whole-image light, then color, and only afterward perform "
                "local skin, facial-feature, hair/clothing, and background work. Do not revisit "
                "or intensify an earlier stage after moving forward."
            ),
            *stage_lines,
            (
                "Composition and structure locks: preserve the exact identity, apparent age, "
                "facial and body geometry, expression, gaze, pose, hands and fingers, crop, "
                "aspect ratio, perspective, subject placement, clothing design, accessories, "
                "background objects, straight lines, depth of field, and lighting direction."
            ),
            (
                "Skin and color locks: preserve original complexion and undertone; keep pores, "
                "fine lines, moles, freckles, scars, and natural highlights; keep face and neck "
                "color coherent; do not make sclera or teeth blue/cyan; do not oversaturate lips."
            ),
            (
                "Naturalness locks: no face or body reshaping, eye enlargement, nose/lip/jaw "
                "changes, age change, whitening, makeup changes, waxy skin, hair-edge smearing, "
                "bent background lines, cutout-like subject relighting, background replacement, "
                "new or missing objects, text, logo, or watermark."
            ),
            (
                "Return exactly one photorealistic candidate. This candidate will be compared "
                "with the immutable original at 100%, fit view, thumbnail, and phone-normal-"
                "brightness view. There is no model retry and this output must never be used as "
                "input to another model edit."
            ),
        ]
    )


def compile_request(
    schema: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    mode: str | None = None,
    enable_model: bool = False,
) -> dict[str, Any]:
    parameters = schema or load_schema()
    pipeline = _load_json(PIPELINE_PATH)
    audit = _load_json(AUDIT_PATH)
    try:
        plan = compile_pipeline.compile_pipeline(
            pipeline,
            parameters,
            audit,
            mode=mode,
            overrides=overrides,
        )
    except compile_pipeline.PipelineConfigError as exc:
        raise PromptConfigError(str(exc)) from exc

    any_edit = any(
        any(float(value) > 0 for value in stage["parameters"].values())
        for stage in plan["stages"]
        if stage["id"] != "final_review"
    )
    planned_calls = 1 if enable_model and any_edit else 0
    prompt = (
        compile_prompt(plan)
        if planned_calls
        else (
            "No image-model call is enabled. Execute the ordered deterministic/inspection "
            "pipeline and return the immutable original for every unsafe or unsupported stage."
        )
    )
    return {
        "schema_version": 6,
        "preset": plan["preset"],
        "mode": plan["mode"],
        "model_enabled": bool(enable_model),
        "planned_model_calls": planned_calls,
        "maximum_total_model_calls": 1,
        "model_candidate_source": "immutable_original",
        "model_retry_allowed": False,
        "model_chaining_allowed": False,
        "stage_order": plan["stage_order"],
        "stages": plan["stages"],
        "required_audit_checks": plan["required_audit_checks"],
        "prompt": prompt,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile one explicitly enabled V6 full-retouch model candidate."
    )
    parser.add_argument(
        "--mode", choices=["natural", "fresh", "warm-film"], default=None
    )
    parser.add_argument("--enable-model", action="store_true")
    parser.add_argument("--list-modes", action="store_true")
    parser.add_argument(
        "--set", dest="assignments", action="append", default=[], metavar="STAGE.PARAM=VALUE"
    )
    parser.add_argument("--plan-output", type=Path)
    parser.add_argument("--prompt-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        schema = load_schema()
        if args.list_modes:
            sys.stdout.write(json.dumps(list_modes(schema), ensure_ascii=False, indent=2) + "\n")
            return 0
        request = compile_request(
            schema,
            overrides=parse_assignments(args.assignments),
            mode=args.mode,
            enable_model=args.enable_model,
        )
        payload = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
        if args.plan_output:
            args.plan_output.parent.mkdir(parents=True, exist_ok=True)
            args.plan_output.write_text(payload, encoding="utf-8")
        if args.prompt_output:
            args.prompt_output.parent.mkdir(parents=True, exist_ok=True)
            args.prompt_output.write_text(request["prompt"] + "\n", encoding="utf-8")
        sys.stdout.write(payload)
        return 0
    except (PromptConfigError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
