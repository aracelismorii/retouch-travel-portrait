#!/usr/bin/env python3
"""Render deterministic visual-review proofs with Pillow and NumPy only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError


OUTPUT_NAMES = {
    "comparison": "comparison.png",
    "difference_heatmap": "difference-heatmap.png",
    "thumbnail_720": "thumbnail-720.png",
    "mobile_preview_1080": "mobile-preview-1080.png",
    "center_detail_100_percent": "detail-100-percent.png",
}

VIEW_FILE_KEYS = {
    "100_percent": "center_detail_100_percent",
    "fit_view": "comparison",
    "thumbnail": "thumbnail_720",
    "phone_normal_brightness": "mobile_preview_1080",
    "side_by_side": "comparison",
    "difference_heatmap": "difference_heatmap",
}

PROOF_SCHEMA_VERSION = 2
PROOF_GENERATOR = "portrait-retouch-render-proof-v6"
MANIFEST_NAME = "manifest.json"


class ProofError(ValueError):
    """Raised when visual-review proofs cannot be rendered safely."""


def _load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()
        return image.convert("RGB")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
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
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fit(image: Image.Image, box: tuple[int, int]) -> Image.Image:
    copy = image.copy()
    copy.thumbnail(box, Image.Resampling.LANCZOS)
    return copy


def _labeled_panel(image: Image.Image, label: str, width: int, height: int) -> Image.Image:
    panel = Image.new("RGB", (width, height + 38), (26, 28, 32))
    fitted = _fit(image, (width, height))
    x = (width - fitted.width) // 2
    y = 38 + (height - fitted.height) // 2
    panel.paste(fitted, (x, y))
    draw = ImageDraw.Draw(panel)
    draw.text((12, 12), label, fill=(240, 240, 240))
    return panel


def _save_comparison(original: Image.Image, candidate: Image.Image, path: Path) -> None:
    panel_width = 640
    panel_height = 720
    left = _labeled_panel(original, "ORIGINAL", panel_width, panel_height)
    right = _labeled_panel(candidate, "CANDIDATE", panel_width, panel_height)
    canvas = Image.new("RGB", (panel_width * 2 + 4, panel_height + 38), (8, 8, 8))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (panel_width + 4, 0))
    canvas.save(path)


def _heatmap_color(intensity: np.ndarray) -> np.ndarray:
    value = np.clip(intensity, 0.0, 1.0)
    red = np.clip(value * 3.0, 0.0, 1.0)
    green = np.clip((value - 0.25) * 2.2, 0.0, 1.0)
    blue = np.clip(0.34 - value * 0.55, 0.0, 1.0)
    return np.stack([red, green, blue], axis=-1)


def _save_heatmap(original: Image.Image, candidate: Image.Image, path: Path) -> dict[str, float]:
    compare_original = original.copy()
    if max(compare_original.size) > 1600:
        compare_original.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    compare_candidate = candidate.resize(original.size, Image.Resampling.LANCZOS)
    compare_candidate = compare_candidate.resize(compare_original.size, Image.Resampling.LANCZOS)
    left = np.asarray(compare_original, dtype=np.float32) / 255.0
    right = np.asarray(compare_candidate, dtype=np.float32) / 255.0
    difference = np.mean(np.abs(right - left), axis=2)
    intensity = np.clip(difference * 4.0, 0.0, 1.0)
    heatmap = Image.fromarray(np.uint8(_heatmap_color(intensity) * 255.0))
    header = 42
    canvas = Image.new("RGB", (heatmap.width, heatmap.height + header), (22, 24, 28))
    canvas.paste(heatmap, (0, header))
    draw = ImageDraw.Draw(canvas)
    mean = float(np.mean(difference))
    p95 = float(np.percentile(difference, 95.0))
    draw.text(
        (12, 14),
        f"ABSOLUTE DIFFERENCE  mean={mean:.4f}  p95={p95:.4f}",
        fill=(242, 242, 242),
    )
    canvas.save(path)
    return {"mean_absolute_rgb_difference": mean, "p95_absolute_rgb_difference": p95}


def _save_thumbnail(candidate: Image.Image, path: Path) -> None:
    thumbnail = _fit(candidate, (720, 720))
    canvas = Image.new("RGB", (720, 720), (32, 34, 38))
    canvas.paste(thumbnail, ((720 - thumbnail.width) // 2, (720 - thumbnail.height) // 2))
    canvas.save(path)


def _save_mobile_preview(candidate: Image.Image, path: Path) -> None:
    width, height = 1080, 1920
    canvas = Image.new("RGB", (width, height), (28, 30, 34))
    fitted = _fit(candidate, (984, 1728))
    x = (width - fitted.width) // 2
    y = (height - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((x - 2, y - 2, x + fitted.width + 1, y + fitted.height + 1), outline=(86, 88, 92), width=2)
    draw.text((48, 42), "1080 MOBILE VIEW - inspect at normal display brightness", fill=(230, 230, 230))
    canvas.save(path)


def _focus_point(image: Image.Image, crop_size: int) -> tuple[float, float]:
    """Estimate an upper-body skin-detail focus without claiming face detection."""

    preview = image.copy()
    preview.thumbnail((240, 240), Image.Resampling.BILINEAR)
    array = np.asarray(preview, dtype=np.float32)
    red, green, blue = array[..., 0], array[..., 1], array[..., 2]
    cb = 128.0 - 0.168736 * red - 0.331264 * green + 0.5 * blue
    cr = 128.0 + 0.5 * red - 0.418688 * green - 0.081312 * blue
    skin = (
        (red > 75)
        & (green > 35)
        & (blue > 20)
        & (red > green * 1.025)
        & (red > blue * 1.06)
        & ((red - blue) > 12)
        & (
            (
                np.maximum.reduce([red, green, blue])
                - np.minimum.reduce([red, green, blue])
            )
            > 12
        )
        & (cb >= 80)
        & (cb <= 135)
        & (cr >= 135)
        & (cr <= 180)
    )
    skin[: int(preview.height * 0.12), :] = False
    skin[int(preview.height * 0.72) :, :] = False

    window_width = max(
        8, min(preview.width, round(crop_size / image.width * preview.width))
    )
    window_height = max(
        8, min(preview.height, round(crop_size / image.height * preview.height))
    )
    if not np.any(skin) or window_width >= preview.width or window_height >= preview.height:
        return 0.5, 0.42

    integral = (
        np.pad(skin.astype(np.float32), ((1, 0), (1, 0)))
        .cumsum(0)
        .cumsum(1)
    )
    window_sums = (
        integral[window_height:, window_width:]
        - integral[:-window_height, window_width:]
        - integral[window_height:, :-window_width]
        + integral[:-window_height, :-window_width]
    )
    upper_limit = max(1, int(preview.height * 0.72) - window_height + 1)
    window_sums = window_sums[:upper_limit]
    if (
        window_sums.size == 0
        or float(window_sums.max()) < window_width * window_height * 0.01
    ):
        return 0.5, 0.42

    # Prefer an actual skin-dense patch; weak positional priors only resolve ties.
    ys, xs = np.indices(window_sums.shape)
    x_center = (xs + window_width / 2) / preview.width
    y_center = (ys + window_height / 2) / preview.height
    positional_prior = 1.0 - 0.035 * np.abs(x_center - 0.5) - 0.025 * y_center
    score = window_sums * positional_prior
    best_y, best_x = np.unravel_index(int(np.argmax(score)), score.shape)
    focus_x = float((best_x + window_width / 2) / preview.width)
    # Skin-density maxima tend to land on a forehead/highlight. Shift slightly
    # downward so the evidence crop is more likely to include eyes, nose and lips.
    focus_y = min(
        0.72 - crop_size / image.height / 2,
        float((best_y + window_height / 2) / preview.height) + 0.055,
    )
    return focus_x, focus_y


def _focused_crop(
    image: Image.Image, size: int, normalized_focus: tuple[float, float]
) -> Image.Image:
    focus_x = round(normalized_focus[0] * image.width)
    focus_y = round(normalized_focus[1] * image.height)
    left = max(0, min(image.width - size, focus_x - size // 2))
    top = max(0, min(image.height - size, focus_y - size // 2))
    return image.crop((left, top, left + size, top + size))


def _save_detail(
    original: Image.Image, candidate: Image.Image, path: Path
) -> tuple[int, list[tuple[str, tuple[float, float]]]]:
    size = min(512, original.width, original.height, candidate.width, candidate.height)
    if size < 1:
        raise ProofError("images have no renderable detail")
    focuses = [
        ("skin_density_primary", _focus_point(original, size)),
        ("upper_center_fallback", (0.5, 0.30)),
    ]
    header = 38
    row_gap = 4
    row_height = size + header
    canvas = Image.new(
        "RGB", (size * 2 + 4, row_height * len(focuses) + row_gap), (8, 8, 8)
    )
    draw = ImageDraw.Draw(canvas)
    for index, (role, focus) in enumerate(focuses):
        row_y = index * row_height + (row_gap if index else 0)
        original_crop = _focused_crop(original, size, focus)
        candidate_crop = _focused_crop(candidate, size, focus)
        canvas.paste(original_crop, (0, row_y + header))
        canvas.paste(candidate_crop, (size + 4, row_y + header))
        label = role.replace("_", " ").upper()
        draw.text(
            (12, row_y + 12),
            f"ORIGINAL - 100% {label}",
            fill=(240, 240, 240),
        )
        draw.text(
            (size + 16, row_y + 12),
            f"CANDIDATE - 100% {label}",
            fill=(240, 240, 240),
        )
    canvas.save(path)
    return size, focuses


def render_proofs(original: Path, candidate: Path, output_dir: Path) -> dict[str, Any]:
    """Create all five required proof images and persist their hash manifest."""

    original = original.resolve()
    candidate = candidate.resolve()
    output_dir = output_dir.resolve()

    try:
        original_hash = _sha256(original)
        candidate_hash = _sha256(candidate)
        original_image = _load_rgb(original)
        candidate_image = _load_rgb(candidate)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ProofError(f"cannot read proof input: {exc}") from exc

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    paths = {key: output_dir / name for key, name in OUTPUT_NAMES.items()}
    _save_comparison(original_image, candidate_image, paths["comparison"])
    difference = _save_heatmap(original_image, candidate_image, paths["difference_heatmap"])
    _save_thumbnail(candidate_image, paths["thumbnail_720"])
    _save_mobile_preview(candidate_image, paths["mobile_preview_1080"])
    detail_size, detail_focuses = _save_detail(
        original_image, candidate_image, paths["center_detail_100_percent"]
    )
    for path in paths.values():
        os.chmod(path, 0o600)

    # Do not publish evidence for inputs that changed while the views rendered.
    if _sha256(original) != original_hash or _sha256(candidate) != candidate_hash:
        raise ProofError("proof input changed while evidence was being rendered")

    file_hashes = {key: _sha256(path) for key, path in paths.items()}
    views = {
        view: str(paths[file_key]) for view, file_key in VIEW_FILE_KEYS.items()
    }
    view_hashes = {
        view: file_hashes[file_key] for view, file_key in VIEW_FILE_KEYS.items()
    }

    manifest = {
        "schema_version": PROOF_SCHEMA_VERSION,
        "generator": PROOF_GENERATOR,
        "original": str(original),
        "candidate": str(candidate),
        # Keep the top-level hashes for existing batch-run compatibility while
        # also recording the complete, explicit input binding below.
        "original_sha256": original_hash,
        "candidate_sha256": candidate_hash,
        "inputs": {
            "original": {
                "path": str(original),
                "sha256": original_hash,
                "width": original_image.width,
                "height": original_image.height,
            },
            "candidate": {
                "path": str(candidate),
                "sha256": candidate_hash,
                "width": candidate_image.width,
                "height": candidate_image.height,
            },
        },
        "files": {key: str(path) for key, path in paths.items()},
        "file_sha256": file_hashes,
        "views": views,
        "view_file_keys": dict(VIEW_FILE_KEYS),
        "view_sha256": view_hashes,
        "required_views": list(VIEW_FILE_KEYS),
        "detail_crop_pixels": detail_size,
        "detail_focus_normalized": {
            "x": round(detail_focuses[0][1][0], 6),
            "y": round(detail_focuses[0][1][1], 6),
            "method": "upper_body_skin_density_heuristic",
        },
        "detail_focus_points_normalized": [
            {
                "x": round(focus[0], 6),
                "y": round(focus[1], 6),
                "role": role,
            }
            for role, focus in detail_focuses
        ],
        "difference": {key: round(value, 8) for key, value in difference.items()},
    }
    _write_json_atomic(output_dir / MANIFEST_NAME, manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render five deterministic portrait-retouch review proofs."
    )
    parser.add_argument("original", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = render_proofs(args.original, args.candidate, args.output_dir)
    except (ProofError, OSError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
