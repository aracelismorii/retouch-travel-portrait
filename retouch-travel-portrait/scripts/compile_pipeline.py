#!/usr/bin/env python3
"""Compile the low-freedom V6 eight-stage portrait-retouch pipeline."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = SKILL_ROOT / "references"
SCHEMA_PATH = REFERENCES / "pipeline-v6.json"
MODE_STAGE_IDS = frozenset({"light_balance", "color_balance"})
SEMANTIC_STAGE_IDS = frozenset({"facial_features", "hair_clothing", "background"})
EXPECTED_STAGE_COUNT = 8

STAGE_CHANGE_CONTRACTS: dict[str, dict[str, list[str]]] = {
    "photo_correction": {
        "allowed_changes": [
            "inspect EXIF orientation, horizon, lens distortion, perspective, crop, and canvas",
            "record a geometry-safe no-change; V6 MVP does not execute geometric correction",
        ],
        "forbidden_changes": [
            "bend straight lines",
            "invent edges",
            "crop unexpectedly",
            "move or reshape the subject",
            "apply local liquify or elastic geometry",
        ],
    },
    "light_balance": {
        "allowed_changes": [
            "balance whole-image exposure, highlights, shadows, and subject-background luminance"
        ],
        "forbidden_changes": [
            "detach the subject from the background",
            "change lighting direction or scene depth",
            "clip highlights or block shadows",
            "change skin undertone",
        ],
    },
    "color_balance": {
        "allowed_changes": [
            "correct global white balance",
            "apply only the selected bounded color recipe",
        ],
        "forbidden_changes": [
            "change skin undertone",
            "turn neutral whites, sclera, or teeth blue",
            "oversaturate lips",
            "aggressively recolor the scene",
        ],
    },
    "skin_cleanup": {
        "allowed_changes": [
            "reduce temporary blemishes, redness, and patchy tone",
            "apply texture-preserving skin smoothing",
        ],
        "forbidden_changes": [
            "erase pores or fine lines",
            "erase moles, freckles, scars, or permanent marks",
            "change complexion or natural skin highlights",
            "change facial geometry",
        ],
    },
    "facial_features": {
        "allowed_changes": [
            "slightly soften under-eye shadow",
            "add restrained non-geometric feature definition",
        ],
        "forbidden_changes": [
            "change eye, nose, lip, jaw, or tooth geometry",
            "change expression, makeup, apparent age, eye color, tooth color, or lip color",
        ],
    },
    "hair_clothing": {
        "allowed_changes": [
            "tidy isolated temporary flyaways",
            "remove small lint and temporary clothing distractions",
        ],
        "forbidden_changes": [
            "change hairline, hairstyle, hair volume, or hair texture",
            "smear or paint over hair edges",
            "change body outline, garment design, pattern, logo, or fabric texture",
        ],
    },
    "background": {
        "allowed_changes": [
            "reduce minor background distractions",
            "maintain subtle subject-background visual balance",
        ],
        "forbidden_changes": [
            "replace the background",
            "invent, remove, or reshape background objects",
            "warp straight lines or perspective",
            "change depth of field",
        ],
    },
    "final_review": {
        "allowed_changes": [
            "record review evidence and an accept-or-reject decision without editing pixels"
        ],
        "forbidden_changes": [
            "perform any image edit",
            "omit a required audit check or required viewing scale",
            "accept tool completion as quality evidence",
        ],
    },
}


class PipelineConfigError(ValueError):
    """Raised when the V6 schemas or a requested override are invalid."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PipelineConfigError(f"{label} must contain a JSON object")
    return data


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Load the V6 pipeline schema."""

    return _read_json(path, "pipeline schema")


def load_linked_schemas(
    pipeline: dict[str, Any], reference_dir: Path = REFERENCES
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the parameter and audit schemas named by the pipeline schema."""

    references = pipeline.get("references")
    if not isinstance(references, dict):
        raise PipelineConfigError("pipeline references must be an object")
    parameter_name = references.get("parameters")
    audit_name = references.get("audit_gates")
    if not isinstance(parameter_name, str) or not parameter_name:
        raise PipelineConfigError("pipeline references.parameters must name a file")
    if not isinstance(audit_name, str) or not audit_name:
        raise PipelineConfigError("pipeline references.audit_gates must name a file")
    return (
        _read_json(reference_dir / parameter_name, "parameter schema"),
        _read_json(reference_dir / audit_name, "audit schema"),
    )


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PipelineConfigError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise PipelineConfigError(f"{label} must be finite")
    return number


def _stage_order(pipeline: dict[str, Any]) -> list[str]:
    order = pipeline.get("stage_order")
    if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
        raise PipelineConfigError("stage_order must be a list of stage ids")
    if len(order) != EXPECTED_STAGE_COUNT:
        raise PipelineConfigError(
            f"V6 requires exactly {EXPECTED_STAGE_COUNT} stages; got {len(order)}"
        )
    if len(set(order)) != len(order):
        raise PipelineConfigError("stage_order contains duplicate stage ids")
    return list(order)


def _stage_definitions(
    pipeline: dict[str, Any], order: list[str]
) -> dict[str, dict[str, Any]]:
    raw = pipeline.get("stages")
    if not isinstance(raw, list) or len(raw) != EXPECTED_STAGE_COUNT:
        raise PipelineConfigError("stages must contain exactly eight stage objects")
    result: dict[str, dict[str, Any]] = {}
    listed_order: list[str] = []
    for expected_index, stage in enumerate(raw, start=1):
        if not isinstance(stage, dict):
            raise PipelineConfigError("every stage must be an object")
        stage_id = stage.get("id")
        if not isinstance(stage_id, str) or not stage_id:
            raise PipelineConfigError("every stage must have a non-empty id")
        if stage_id in result:
            raise PipelineConfigError(f"duplicate stage definition: {stage_id}")
        if stage.get("index") != expected_index:
            raise PipelineConfigError(
                f"stage {stage_id} index must be {expected_index}"
            )
        listed_order.append(stage_id)
        result[stage_id] = stage
    if listed_order != order:
        raise PipelineConfigError("stage definitions must follow stage_order exactly")
    return result


def _parameter_definitions(
    parameters: dict[str, Any], order: list[str]
) -> dict[str, dict[str, Any]]:
    raw = parameters.get("parameters")
    if not isinstance(raw, dict):
        raise PipelineConfigError("parameters.parameters must be an object")
    result: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not isinstance(definition, dict):
            raise PipelineConfigError("parameter definitions must be objects")
        stage_id = definition.get("stage")
        if stage_id not in order:
            raise PipelineConfigError(f"parameter {name} names unknown stage {stage_id!r}")
        for required in ("default", "minimum", "maximum"):
            if required not in definition:
                raise PipelineConfigError(f"parameter {name} is missing {required}")
        minimum = _finite_number(definition["minimum"], f"{name}.minimum")
        maximum = _finite_number(definition["maximum"], f"{name}.maximum")
        default = _finite_number(definition["default"], f"{name}.default")
        if minimum > maximum:
            raise PipelineConfigError(f"{name}.minimum must not exceed maximum")
        if not minimum <= default <= maximum:
            raise PipelineConfigError(
                f"{name}.default must be between {minimum:g} and {maximum:g}"
            )
        result[name] = definition
    return result


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineConfigError(f"{label} must be an object")
    return value


def _validate_value(name: str, value: Any, definition: dict[str, Any]) -> float:
    number = _finite_number(value, name)
    minimum = float(definition["minimum"])
    maximum = float(definition["maximum"])
    if not minimum <= number <= maximum:
        raise PipelineConfigError(
            f"{name} must be between {minimum:g} and {maximum:g}; got {number:g}"
        )
    return round(number, 6)


def _validate_mode_recipes(
    pipeline: dict[str, Any],
    parameters: dict[str, Any],
    definitions: dict[str, dict[str, Any]],
) -> None:
    modes = parameters.get("modes")
    if not isinstance(modes, dict) or not modes:
        raise PipelineConfigError("parameters.modes must be a non-empty object")
    policy = _require_object(pipeline.get("mode_policy"), "mode_policy")
    available = policy.get("available_modes")
    if not isinstance(available, list) or available != list(modes):
        raise PipelineConfigError(
            "mode_policy.available_modes must exactly match parameter modes"
        )
    if policy.get("default_mode") != parameters.get("default_mode"):
        raise PipelineConfigError("pipeline and parameter default modes must match")
    if policy.get("mode_affected_stages") != list(MODE_STAGE_IDS):
        if set(policy.get("mode_affected_stages", [])) != MODE_STAGE_IDS:
            raise PipelineConfigError(
                "mode_affected_stages must contain only light_balance and color_balance"
            )

    for mode_name, mode in modes.items():
        mode = _require_object(mode, f"mode {mode_name}")
        recipes = _require_object(mode.get("stage_recipes"), f"mode {mode_name}.stage_recipes")
        if set(recipes) != MODE_STAGE_IDS:
            raise PipelineConfigError(
                f"mode {mode_name} recipes must contain only light_balance and color_balance"
            )
        for stage_id, recipe_value in recipes.items():
            recipe = _require_object(recipe_value, f"mode {mode_name}.{stage_id}")
            for parameter_name, value in recipe.items():
                if parameter_name == "instruction":
                    if not isinstance(value, str) or not value:
                        raise PipelineConfigError(
                            f"mode {mode_name}.{stage_id}.instruction must be text"
                        )
                    continue
                if parameter_name not in definitions:
                    raise PipelineConfigError(
                        f"mode {mode_name} has unknown parameter {parameter_name}"
                    )
                definition = definitions[parameter_name]
                if definition["stage"] != stage_id:
                    raise PipelineConfigError(
                        f"mode {mode_name} cannot assign {stage_id}.{parameter_name}"
                    )
                _validate_value(parameter_name, value, definition)


def validate_schemas(
    pipeline: dict[str, Any],
    parameters: dict[str, Any],
    audit: dict[str, Any],
) -> None:
    """Validate cross-file V6 invariants before compiling a request."""

    if pipeline.get("schema_version") != 6 or parameters.get("schema_version") != 6:
        raise PipelineConfigError("pipeline and parameter schemas must be V6")
    order = _stage_order(pipeline)
    if set(order) != set(STAGE_CHANGE_CONTRACTS):
        raise PipelineConfigError(
            "stage change contracts must exactly cover the configured eight stages"
        )
    stages = _stage_definitions(pipeline, order)
    if parameters.get("stage_order") != order:
        raise PipelineConfigError("parameter stage_order must match pipeline stage_order")
    definitions = _parameter_definitions(parameters, order)
    _validate_mode_recipes(pipeline, parameters, definitions)
    shared_defaults = _require_object(
        parameters.get("shared_parameter_defaults"), "shared_parameter_defaults"
    )
    photo_definition = definitions.get("photo_correction", {})
    if any(
        float(photo_definition.get(field, float("nan"))) != 0.0
        for field in ("minimum", "default", "maximum")
    ) or float(shared_defaults.get("photo_correction", float("nan"))) != 0.0:
        raise PipelineConfigError(
            "photo_correction must be fixed at zero for inspection-only V6 MVP execution"
        )
    semantic_policy = _require_object(
        parameters.get("semantic_parameter_policy"), "semantic_parameter_policy"
    )
    if semantic_policy.get("stages") != [
        "facial_features",
        "hair_clothing",
        "background",
    ]:
        raise PipelineConfigError(
            "semantic_parameter_policy.stages must name the three semantic stages"
        )
    if semantic_policy.get("active_only_when_optional_model_is_enabled") is not True:
        raise PipelineConfigError(
            "semantic parameters must be active only for the optional model"
        )
    if semantic_policy.get("default_zero_model_action") != "inspect_and_record_safe_no_change":
        raise PipelineConfigError(
            "semantic default action must be inspect_and_record_safe_no_change"
        )
    semantic_parameters = {
        name
        for name, definition in definitions.items()
        if definition["stage"] in SEMANTIC_STAGE_IDS
    }
    for name in semantic_parameters:
        if float(definitions[name]["default"]) != 0.0 or float(
            shared_defaults.get(name, float("nan"))
        ) != 0.0:
            raise PipelineConfigError(
                f"semantic parameter {name} must default to zero in both schema defaults"
            )
        if not isinstance(definitions[name].get("target"), str) or not definitions[
            name
        ]["target"]:
            raise PipelineConfigError(
                f"semantic parameter {name} must define a non-empty target"
            )

    for stage_id in order:
        stage = stages[stage_id]
        for field in ("scope", "purpose"):
            if not isinstance(stage.get(field), str) or not stage[field]:
                raise PipelineConfigError(f"stage {stage_id}.{field} must be text")
        preserve = stage.get("must_preserve")
        if not isinstance(preserve, list) or not all(isinstance(item, str) for item in preserve):
            raise PipelineConfigError(f"stage {stage_id}.must_preserve must be text items")
        if stage.get("mode_sensitive") is not (stage_id in MODE_STAGE_IDS):
            raise PipelineConfigError(
                f"stage {stage_id}.mode_sensitive violates the two-stage mode boundary"
            )
    if stages["final_review"].get("edits_allowed") is not False:
        raise PipelineConfigError("final_review must forbid edits")

    execution = _require_object(pipeline.get("execution_policy"), "execution_policy")
    required_model_policy = {
        "immutable_original_required": True,
        "stage_order_is_strict": True,
        "deterministic_candidate_requires_visual_acceptance": True,
        "visual_reject_retains_previous_checkpoint": True,
        "default_model_calls": 0,
        "maximum_model_calls": 1,
        "maximum_successful_candidates": 1,
        "model_chaining_allowed": False,
        "model_retry_allowed": False,
        "model_candidate_source": "immutable_original",
        "accept_tool_completion_as_quality_evidence": False,
    }
    for key, expected in required_model_policy.items():
        if execution.get(key) != expected:
            raise PipelineConfigError(f"execution_policy.{key} must be {expected!r}")

    if audit.get("schema_version") != 6:
        raise PipelineConfigError("audit schema must be V6")
    required_checks = audit.get("required_keys")
    gates = audit.get("gates")
    if not isinstance(required_checks, list) or len(required_checks) != 10:
        raise PipelineConfigError("audit.required_keys must contain exactly ten checks")
    if len(set(required_checks)) != len(required_checks):
        raise PipelineConfigError("audit.required_keys contains duplicates")
    if not isinstance(gates, dict) or set(gates) != set(required_checks):
        raise PipelineConfigError("audit gates must exactly match required_keys")


def parse_assignments(items: list[str]) -> dict[str, float]:
    """Parse repeated STAGE.PARAM=VALUE overrides."""

    result: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            raise PipelineConfigError(
                f"override must use STAGE.PARAM=VALUE: {item!r}"
            )
        target, raw = item.split("=", 1)
        target = target.strip()
        parts = target.split(".")
        if len(parts) != 2 or not all(parts):
            raise PipelineConfigError(
                f"override must use STAGE.PARAM=VALUE: {item!r}"
            )
        try:
            value = float(raw)
        except ValueError as exc:
            raise PipelineConfigError(f"{target} must be numeric, got {raw!r}") from exc
        if not math.isfinite(value):
            raise PipelineConfigError(f"{target} must be finite")
        if target in result:
            raise PipelineConfigError(f"duplicate override: {target}")
        result[target] = value
    return result


def _normalize_overrides(
    overrides: dict[str, Any],
    order: list[str],
    definitions: dict[str, dict[str, Any]],
) -> dict[str, float]:
    result: dict[str, float] = {}
    for target, value in overrides.items():
        parts = target.split(".")
        if len(parts) != 2 or not all(parts):
            raise PipelineConfigError(
                f"override must use STAGE.PARAM names; got {target!r}"
            )
        stage_id, parameter_name = parts
        if stage_id not in order:
            raise PipelineConfigError(f"unknown stage in override: {stage_id}")
        if parameter_name not in definitions:
            raise PipelineConfigError(f"unknown parameter in override: {parameter_name}")
        definition = definitions[parameter_name]
        if definition["stage"] != stage_id:
            raise PipelineConfigError(
                f"parameter {parameter_name} belongs to {definition['stage']}, not {stage_id}"
            )
        result[target] = _validate_value(target, value, definition)
    return result


def _defaults(parameters: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> dict[str, float]:
    shared = _require_object(
        parameters.get("shared_parameter_defaults"), "shared_parameter_defaults"
    )
    unknown = set(shared) - set(definitions)
    if unknown:
        raise PipelineConfigError(
            "unknown shared parameter defaults: " + ", ".join(sorted(unknown))
        )
    values = {
        name: _validate_value(name, definition["default"], definition)
        for name, definition in definitions.items()
    }
    for name, value in shared.items():
        values[name] = _validate_value(name, value, definitions[name])
    return values


def _model_policy(pipeline: dict[str, Any]) -> dict[str, Any]:
    execution = copy.deepcopy(pipeline["execution_policy"])
    return {
        key: execution[key]
        for key in (
            "default_model_calls",
            "maximum_model_calls",
            "maximum_successful_candidates",
            "model_chaining_allowed",
            "model_retry_allowed",
            "model_candidate_source",
            "accept_tool_completion_as_quality_evidence",
        )
    }


def _checkpoint_policy(pipeline: dict[str, Any]) -> dict[str, Any]:
    execution = pipeline["execution_policy"]
    return {
        key: execution[key]
        for key in (
            "immutable_original_required",
            "stage_order_is_strict",
            "safe_noop_requires_reason",
            "deterministic_candidate_requires_visual_acceptance",
            "visual_reject_retains_previous_checkpoint",
        )
    }


def _checkpoint(stage_id: str, index: int) -> dict[str, str]:
    return {
        "input": "immutable_original" if index == 1 else f"stage-{index - 1:02d}-accepted",
        "output": "final-reviewed" if stage_id == "final_review" else f"stage-{index:02d}-{stage_id}",
        "compare_to": "immutable_original",
    }


def compile_pipeline(
    pipeline: dict[str, Any],
    parameters: dict[str, Any],
    audit: dict[str, Any],
    mode: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile every V6 stage; no-op stages remain explicit checkpoints."""

    validate_schemas(pipeline, parameters, audit)
    order = _stage_order(pipeline)
    stages = _stage_definitions(pipeline, order)
    definitions = _parameter_definitions(parameters, order)
    modes = parameters["modes"]
    selected_mode = mode or parameters["default_mode"]
    if selected_mode not in modes:
        raise PipelineConfigError(
            f"unknown mode: {selected_mode!r}; choose one of: " + ", ".join(modes)
        )

    values = _defaults(parameters, definitions)
    instructions: dict[str, str] = {}
    for stage_id, recipe in modes[selected_mode]["stage_recipes"].items():
        for name, value in recipe.items():
            if name == "instruction":
                instructions[stage_id] = value
            else:
                values[name] = _validate_value(name, value, definitions[name])

    normalized_overrides = _normalize_overrides(
        overrides or {}, order, definitions
    )
    for target, value in normalized_overrides.items():
        _, name = target.split(".", 1)
        values[name] = value

    compiled_stages: list[dict[str, Any]] = []
    for stage_id in order:
        stage = stages[stage_id]
        stage_parameters = {
            name: values[name]
            for name, definition in definitions.items()
            if definition["stage"] == stage_id
        }
        if stage_id == "final_review":
            action = "inspect"
            executor = "scripts/audit_result.py"
        elif stage_id == "photo_correction":
            action = "no_change"
            executor = "agent visual review; scripts/execute_pipeline.py records a safe no-change"
        elif stage_id in SEMANTIC_STAGE_IDS:
            action = (
                "inspect_then_optional_model"
                if any(value != 0 for value in stage_parameters.values())
                else "no_change"
            )
            executor = "agent visual review; scripts/execute_pipeline.py records a safe no-change"
        else:
            action = "apply" if any(value != 0 for value in stage_parameters.values()) else "no_change"
            executor = "scripts/execute_pipeline.py"
        compiled = {
            "index": stage["index"],
            "id": stage_id,
            "action": action,
            "parameters": stage_parameters,
            "allowed_changes": copy.deepcopy(
                STAGE_CHANGE_CONTRACTS[stage_id]["allowed_changes"]
            ),
            "forbidden_changes": copy.deepcopy(
                STAGE_CHANGE_CONTRACTS[stage_id]["forbidden_changes"]
            ),
            "executor": executor,
            "checkpoint": _checkpoint(stage_id, stage["index"]),
        }
        if stage_id in SEMANTIC_STAGE_IDS:
            compiled["parameter_scope"] = "optional_model_candidate_only"
            compiled["parameters_active_in_default_path"] = False
            compiled["default_path_action"] = "inspect_and_record_safe_no_change"
            compiled["explicit_nonzero_parameters"] = {
                name: value
                for name, value in stage_parameters.items()
                if value > 0
            }
            compiled["explicit_nonzero_parameter_targets"] = {
                name: definitions[name]["target"]
                for name, value in stage_parameters.items()
                if value > 0
            }
            compiled["preserve_or_inspect_parameters"] = [
                name
                for name, value in stage_parameters.items()
                if value == 0
            ]
        elif stage_id == "photo_correction":
            compiled["parameter_scope"] = "inspection_only_v6_mvp"
            compiled["parameters_active_in_default_path"] = False
            compiled["default_path_action"] = "inspect_and_record_safe_no_change"
        elif stage_id == "final_review":
            compiled["parameter_scope"] = "not_applicable"
            compiled["parameters_active_in_default_path"] = False
        else:
            compiled["parameter_scope"] = "deterministic_stage"
            compiled["parameters_active_in_default_path"] = action == "apply"
        if action == "no_change":
            compiled["no_change_reason_required"] = True
            compiled["no_change_reason"] = (
                "Configured parameters are zero; inspect the stage scope and record why "
                "the accepted checkpoint remains unchanged."
            )
        if stage_id in instructions:
            compiled["mode_instruction"] = instructions[stage_id]
        if stage_id == "final_review":
            compiled["edits_allowed"] = False
        compiled_stages.append(compiled)

    if [stage["id"] for stage in compiled_stages] != order:
        raise AssertionError("compiler must emit all eight stages in exact order")
    return {
        "schema_version": 6,
        "pipeline_id": pipeline["pipeline_id"],
        "preset": parameters["modes"][selected_mode]["preset_name"],
        "mode": selected_mode,
        "stage_order": order,
        "stages": compiled_stages,
        "checkpoint_policy": _checkpoint_policy(pipeline),
        "model_policy": _model_policy(pipeline),
        "required_audit_checks": copy.deepcopy(audit["required_keys"]),
    }


def list_modes(
    pipeline: dict[str, Any], parameters: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    validate_schemas(pipeline, parameters, audit)
    return {
        "default_mode": parameters["default_mode"],
        "mode_affected_stages": list(pipeline["mode_policy"]["mode_affected_stages"]),
        "modes": [
            {
                "name": name,
                "preset": definition["preset_name"],
                "display_name": definition["display_name"],
                "description": definition["description"],
                "stage_recipes": copy.deepcopy(definition["stage_recipes"]),
            }
            for name, definition in parameters["modes"].items()
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile the V6 eight-stage professional portrait-retouch pipeline."
    )
    parser.add_argument("--mode", help="Select natural, fresh, or warm-film.")
    parser.add_argument(
        "--set",
        dest="assignments",
        action="append",
        default=[],
        metavar="STAGE.PARAM=VALUE",
        help="Override one bounded stage parameter. Repeat as needed.",
    )
    parser.add_argument("--list-modes", action="store_true")
    parser.add_argument("--output", type=Path, help="Write compiled JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        pipeline = load_schema()
        parameters, audit = load_linked_schemas(pipeline)
        result = (
            list_modes(pipeline, parameters, audit)
            if args.list_modes
            else compile_pipeline(
                pipeline,
                parameters,
                audit,
                mode=args.mode,
                overrides=parse_assignments(args.assignments),
            )
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        else:
            sys.stdout.write(payload)
        return 0
    except (PipelineConfigError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
