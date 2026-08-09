#!/usr/bin/env python3
"""Apply non-semantic geometry gates to a V4 model candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import check_geometry


def compare_model_candidate(
    original: Path,
    candidate: Path,
    maximum_ratio_delta: float = 0.005,
    minimum_short_edge: int = 768,
) -> dict[str, Any]:
    if not 0 <= maximum_ratio_delta <= 0.05:
        raise ValueError("maximum_ratio_delta must be between 0 and 0.05")
    if minimum_short_edge < 1:
        raise ValueError("minimum_short_edge must be positive")
    original_width, original_height = check_geometry.read_dimensions(original)
    candidate_width, candidate_height = check_geometry.read_dimensions(candidate)
    original_ratio = original_width / original_height
    candidate_ratio = candidate_width / candidate_height
    ratio_delta = abs(candidate_ratio / original_ratio - 1.0)
    ratio_ok = ratio_delta <= maximum_ratio_delta
    resolution_ok = min(candidate_width, candidate_height) >= minimum_short_edge
    accepted = ratio_ok and resolution_ok
    return {
        "decision": "visual_review_required" if accepted else "reject",
        "automated_gates_passed": accepted,
        "reason": (
            "aspect ratio and minimum resolution pass; identity and naturalness still require visual review"
            if accepted
            else "candidate failed aspect-ratio or minimum-resolution gate"
        ),
        "original": {
            "width": original_width,
            "height": original_height,
            "aspect_ratio": round(original_ratio, 8),
        },
        "candidate": {
            "width": candidate_width,
            "height": candidate_height,
            "aspect_ratio": round(candidate_ratio, 8),
        },
        "relative_aspect_ratio_delta": round(ratio_delta, 8),
        "relative_aspect_ratio_delta_max": maximum_ratio_delta,
        "minimum_short_edge": minimum_short_edge,
        "ratio_gate": ratio_ok,
        "resolution_gate": resolution_ok,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check V4 model candidate aspect ratio and minimum resolution."
    )
    parser.add_argument("original", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--max-ratio-delta", type=float, default=0.005)
    parser.add_argument("--minimum-short-edge", type=int, default=768)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = compare_model_candidate(
            args.original,
            args.candidate,
            maximum_ratio_delta=args.max_ratio_delta,
            minimum_short_edge=args.minimum_short_edge,
        )
    except (OSError, ValueError, check_geometry.GeometryError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result["automated_gates_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
