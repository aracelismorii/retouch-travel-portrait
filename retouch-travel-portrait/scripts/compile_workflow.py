#!/usr/bin/env python3
"""Compile validated V3 parameters into deterministic raster operations."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = SKILL_ROOT / "references" / "parameters.json"


class ConfigError(ValueError):
    """Raised when the requested workflow configuration is invalid."""


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _numeric(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{name} must be finite")
    return number


def parse_assignments(items: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"override must use name=value: {item!r}")
        name, raw = item.split("=", 1)
        name = name.strip()
        if not name:
            raise ConfigError(f"override has an empty name: {item!r}")
        try:
            result[name] = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be numeric, got {raw!r}") from exc
    return result


def load_config_overrides(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConfigError("config must contain a JSON object")
    unknown_top = set(data) - {"beauty", "preset"}
    if unknown_top:
        raise ConfigError(
            "unsupported top-level config keys: " + ", ".join(sorted(unknown_top))
        )
    if "preset" in data and data["preset"] != "natural-v3":
        raise ConfigError("V3 supports only the natural-v3 preset")
    beauty = data.get("beauty", {})
    if not isinstance(beauty, dict):
        raise ConfigError("config.beauty must be a JSON object")
    return beauty


def normalize_parameters(
    schema: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    use_defaults: bool = True,
) -> dict[str, float]:
    definitions = schema["parameters"]
    overrides = overrides or {}
    unknown = set(overrides) - set(definitions)
    if unknown:
        raise ConfigError("unknown parameters: " + ", ".join(sorted(unknown)))

    normalized: dict[str, float] = {}
    for name, definition in definitions.items():
        base_value = definition["default"] if use_defaults else 0.0
        value = _numeric(overrides.get(name, base_value), name)
        minimum = float(definition["minimum"])
        maximum = float(definition["maximum"])
        if not minimum <= value <= maximum:
            if maximum == 0:
                raise ConfigError(f"{name} is not supported in V3; use 0")
            raise ConfigError(
                f"{name} must be between {minimum:g} and {maximum:g}; got {value:g}"
            )
        normalized[name] = round(value, 4)
    return normalized


def compile_plan(
    schema: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    use_defaults: bool = True,
    only_stage: str | None = None,
) -> dict[str, Any]:
    parameters = normalize_parameters(schema, overrides, use_defaults=use_defaults)
    valid_stages = list(schema["workflow"]["stage_order"])
    if only_stage is not None and only_stage not in valid_stages:
        raise ConfigError("only_stage must be one of: " + ", ".join(valid_stages))

    stages: list[dict[str, Any]] = []
    for stage in valid_stages:
        if only_stage is not None and stage != only_stage:
            continue
        stage_parameters = {
            name: value
            for name, value in parameters.items()
            if schema["parameters"][name]["stage"] == stage
        }
        if not any(value > 0 for value in stage_parameters.values()):
            continue
        operations = [
            schema["parameters"][name]["operation"]
            for name, value in stage_parameters.items()
            if value > 0
        ]
        stages.append(
            {
                "id": stage,
                "executor": "scripts/retouch_image.py",
                "parameters": stage_parameters,
                "operations": operations,
                "suggested_output_name": f"stage-{len(stages) + 1:02d}-{stage}.png",
            }
        )

    return {
        "schema_version": schema["schema_version"],
        "preset": schema["preset_name"],
        "engine": schema["engine"],
        "parameter_base": "natural-v3 defaults" if use_defaults else "all zero",
        "parameters": parameters,
        "locks": schema["locks"],
        "quality_limits": schema["quality_limits"],
        "workflow": schema["workflow"],
        "planned_model_calls": 0,
        "stages": stages,
        "zero_edit_behavior": (
            "Return the original image unchanged without processing."
            if not stages
            else None
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a deterministic natural-v3 portrait-retouch workflow."
    )
    parser.add_argument(
        "--config", type=Path, help="JSON config with optional preset and beauty keys."
    )
    parser.add_argument(
        "--set",
        dest="assignments",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Override one beauty parameter. Repeat as needed.",
    )
    parser.add_argument(
        "--zero-unspecified",
        action="store_true",
        help="Start all beauty parameters at zero before applying overrides.",
    )
    parser.add_argument(
        "--only-stage",
        choices=["lighting", "skin"],
        help="Compile only one deterministic stage.",
    )
    parser.add_argument("--output", type=Path, help="Write the plan to this JSON file.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        schema = load_schema()
        overrides = load_config_overrides(args.config)
        overrides.update(parse_assignments(args.assignments))
        plan = compile_plan(
            schema,
            overrides=overrides,
            use_defaults=not args.zero_unspecified,
            only_stage=args.only_stage,
        )
        payload = json.dumps(
            plan, ensure_ascii=False, indent=2 if args.pretty else None
        ) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        else:
            sys.stdout.write(payload)
        return 0
    except (ConfigError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
