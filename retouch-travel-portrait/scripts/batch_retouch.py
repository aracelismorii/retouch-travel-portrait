#!/usr/bin/env python3
"""Run isolated V3 portrait-retouch jobs for every supported image in a folder."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import compile_workflow
import execute_run
import prepare_run


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class BatchError(ValueError):
    """Raised when a batch cannot be started safely."""


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _safe_stem(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-._")
    return stem[:80] or "portrait"


def _discover_images(input_dir: Path, recursive: bool) -> list[Path]:
    iterator = input_dir.rglob("*") if recursive else input_dir.iterdir()
    return sorted(
        (
            path
            for path in iterator
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda path: str(path.relative_to(input_dir)).lower(),
    )


def _skin_operation(manifest: dict[str, Any]) -> str | None:
    for event in manifest.get("events", []):
        if event.get("type") == "stage_accepted" and event.get("stage") == "skin":
            return (
                event.get("metrics", {}).get("details", {}).get("operation")
            )
    return None


def _summary(
    input_dir: Path,
    output_dir: Path,
    items: list[dict[str, Any]],
    target_rate: float,
) -> dict[str, Any]:
    total = len(items)
    succeeded = sum(item["classification"] == "success" for item in items)
    degraded = sum(item["classification"] == "safe_degraded" for item in items)
    failed = sum(item["classification"] == "failed" for item in items)
    full_rate = succeeded / total if total else 0.0
    usable_rate = (succeeded + degraded) / total if total else 0.0
    return {
        "schema_version": 3,
        "preset": "natural-v3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "counts": {
            "total": total,
            "success": succeeded,
            "safe_degraded": degraded,
            "failed": failed,
        },
        "rates": {
            "full_success": round(full_rate, 6),
            "usable_output": round(usable_rate, 6),
            "target": round(target_rate, 6),
            "target_met": full_rate >= target_rate,
        },
        "planned_model_calls": 0,
        "items": items,
    }


def run_batch(
    input_dir: Path,
    output_dir: Path,
    overrides: dict[str, float] | None = None,
    use_defaults: bool = True,
    recursive: bool = False,
    target_rate: float = 0.9,
) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if not input_dir.is_dir():
        raise BatchError(f"input directory does not exist: {input_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise BatchError(f"output path is not a directory: {output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BatchError(f"output directory must be new or empty: {output_dir}")
    if not 0.0 <= target_rate <= 1.0:
        raise BatchError("target rate must be between 0 and 1")

    sources = _discover_images(input_dir, recursive)
    if not sources:
        raise BatchError("input directory contains no supported images")

    runs_dir = output_dir / "runs"
    finals_dir = output_dir / "finals"
    runs_dir.mkdir(parents=True, exist_ok=True)
    finals_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    items: list[dict[str, Any]] = []

    for index, source in enumerate(sources, start=1):
        item_id = f"{index:04d}-{_safe_stem(source)}"
        run_dir = runs_dir / item_id
        item: dict[str, Any] = {
            "id": item_id,
            "source": str(source),
            "classification": "failed",
        }
        try:
            prepared = prepare_run.prepare_run(
                source,
                run_dir,
                overrides=overrides,
                use_defaults=use_defaults,
            )
            result = execute_run.execute_run(Path(str(prepared["run_dir"])))
            manifest = json.loads(
                (run_dir / "run.json").read_text(encoding="utf-8")
            )
            final_source = Path(str(result["final"]))
            final_name = item_id + final_source.suffix.lower()
            final_output = finals_dir / final_name
            shutil.copy2(final_source, final_output)
            skin_operation = _skin_operation(manifest)
            expected_stages = [stage["id"] for stage in json.loads(
                (run_dir / "plan.json").read_text(encoding="utf-8")
            ).get("stages", [])]
            accepted_stages = list(result.get("accepted_stages", []))
            full_success = (
                result.get("status") == "complete"
                and accepted_stages == expected_stages
                and skin_operation != "skin_stage_skipped"
            )
            item.update(
                {
                    "classification": (
                        "success" if full_success else "safe_degraded"
                    ),
                    "run_status": result.get("status"),
                    "accepted_stages": accepted_stages,
                    "skin_operation": skin_operation,
                    "geometry_pass_rate": result.get("geometry_pass_rate"),
                    "final": str(final_output),
                    "run_dir": str(run_dir),
                }
            )
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            item["error"] = str(exc)
        items.append(item)
        _write_json(
            summary_path,
            _summary(input_dir, output_dir, items, target_rate),
        )

    return _summary(input_dir, output_dir, items, target_rate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retouch a folder of portraits with isolated V3 runs."
    )
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--recursive", action="store_true", help="Include nested input folders."
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
        help="Start all parameters at zero before applying overrides.",
    )
    parser.add_argument(
        "--target-rate",
        type=float,
        default=0.9,
        help="Required full-success rate written to summary.json (default: 0.9).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_batch(
            args.input_dir,
            args.output_dir,
            overrides=compile_workflow.parse_assignments(args.assignments),
            use_defaults=not args.zero_unspecified,
            recursive=args.recursive,
            target_rate=args.target_rate,
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result["rates"]["target_met"] else 2
    except (
        BatchError,
        compile_workflow.ConfigError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
