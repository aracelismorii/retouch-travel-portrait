#!/usr/bin/env python3
"""Prepare a non-destructive local V3 portrait-retouch run directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import compile_workflow
import check_geometry


class RunError(ValueError):
    """Raised when a local retouch run cannot be prepared safely."""


def detect_image_type(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    raise RunError("input is not a recognized PNG, JPEG, or WebP image")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_run(
    source: Path,
    run_dir: Path,
    overrides: dict[str, float] | None = None,
    use_defaults: bool = True,
) -> dict[str, object]:
    source = source.resolve()
    run_dir = run_dir.resolve()
    if not source.is_file():
        raise RunError(f"source image does not exist: {source}")
    image_type = detect_image_type(source)
    source_width, source_height = check_geometry.read_dimensions(source)
    extension = source.suffix.lower()
    if extension == ".jpeg":
        extension = ".jpg"
    if extension != image_type:
        raise RunError(
            f"file extension {source.suffix or '<none>'} does not match "
            f"detected image type {image_type}"
        )

    schema = compile_workflow.load_schema()
    plan = compile_workflow.compile_plan(
        schema,
        overrides=overrides,
        use_defaults=use_defaults,
    )
    for index, stage in enumerate(plan["stages"], start=1):
        stage["suggested_output_name"] = f"stage-{index:02d}-{stage['id']}.png"

    if run_dir.exists() and not run_dir.is_dir():
        raise RunError(f"run directory path is not a directory: {run_dir}")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RunError(f"run directory must be new or empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    original_name = f"original{image_type}"
    original_path = run_dir / original_name
    if original_path.exists():
        raise RunError(f"refusing to overwrite existing original: {original_path}")
    shutil.copy2(source, original_path)

    plan_path = run_dir / "plan.json"
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema_version": 3,
        "status": "ready" if plan["stages"] else "no-edit-needed",
        "preset": plan["preset"],
        "engine": plan["engine"]["name"],
        "source_path": str(source),
        "source_sha256": sha256_file(source),
        "source_dimensions": {
            "width": source_width,
            "height": source_height,
            "aspect_ratio": round(source_width / source_height, 8),
        },
        "original_copy": original_name,
        "current_accepted_image": original_name,
        "plan": "plan.json",
        "planned_stages": [stage["id"] for stage in plan["stages"]],
        "accepted_stages": [],
        "events": [
            {
                "type": "prepared",
                "message": "Original preserved and deterministic V3 plan compiled.",
            }
        ],
    }
    manifest_path = run_dir / "run.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "run_dir": str(run_dir),
        "original": str(original_path),
        "plan": str(plan_path),
        "manifest": str(manifest_path),
        "planned_model_calls": plan["planned_model_calls"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a local natural-v3 portrait-retouch run."
    )
    parser.add_argument("source_image", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        overrides = compile_workflow.parse_assignments(args.assignments)
        result = prepare_run(
            args.source_image,
            args.run_dir,
            overrides=overrides,
            use_defaults=not args.zero_unspecified,
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (
        RunError,
        compile_workflow.ConfigError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
