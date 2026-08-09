#!/usr/bin/env python3
"""Deterministic, geometry-preserving raster operations for portrait retouching."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageCms, ImageFilter, ImageOps


class RetouchError(ValueError):
    """Raised when an image cannot be processed without violating V3 locks."""


def _load_pixels(path: Path) -> tuple[Image.Image, np.ndarray, np.ndarray | None]:
    # Normalize EXIF orientation in memory while preserving the immutable source
    # file on disk. This supports ordinary phone JPEGs without asking callers to
    # destructively rotate or re-encode their original first.
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()
        alpha = None
        if "A" in image.getbands():
            alpha = np.asarray(image.getchannel("A"), dtype=np.uint8).copy()
        embedded_profile = image.info.get("icc_profile")
        output_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        if embedded_profile:
            try:
                input_profile = ImageCms.ImageCmsProfile(
                    io.BytesIO(embedded_profile)
                )
                color_source = image if image.mode != "RGBA" else image.convert("RGB")
                color_source = ImageCms.profileToProfile(
                    color_source,
                    input_profile,
                    output_profile,
                    outputMode="RGB",
                )
            except (OSError, TypeError, ValueError, ImageCms.PyCMSError) as exc:
                raise RetouchError(
                    "embedded ICC profile cannot be converted safely to sRGB"
                ) from exc
        else:
            # Untagged JPEG/PNG/WebP is treated as sRGB by the documented MVP
            # input contract.
            color_source = image.convert("RGB")
        rgb = np.asarray(color_source, dtype=np.uint8).copy()
        image.info["icc_profile"] = output_profile.tobytes()
    return image, rgb, alpha


def _to_float(rgb: np.ndarray) -> np.ndarray:
    return rgb.astype(np.float32) / 255.0


def _to_uint8(rgb: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(rgb * 255.0), 0, 255).astype(np.uint8)


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return (
        rgb[..., 0] * 0.2126
        + rgb[..., 1] * 0.7152
        + rgb[..., 2] * 0.0722
    )


def apply_lighting(rgb_u8: np.ndarray, value: float) -> tuple[np.ndarray, dict[str, Any]]:
    if value <= 0:
        return rgb_u8.copy(), {"operation": "lighting", "strength": value}

    strength = min(max(value / 0.5, 0.0), 1.0)
    rgb = _to_float(rgb_u8)
    luma = _luminance(rgb)
    valid = (luma > 0.04) & (luma < 0.96)
    sample = rgb[valid] if np.any(valid) else rgb.reshape(-1, 3)

    channel_means = np.maximum(sample.mean(axis=0), 1e-4)
    gray_mean = float(channel_means.mean())
    raw_gains = np.clip(gray_mean / channel_means, 0.92, 1.08)
    gains = 1.0 + (raw_gains - 1.0) * (0.55 * strength)
    balanced = np.clip(rgb * gains, 0.0, 1.0)

    balanced_luma = _luminance(balanced)
    median = float(np.median(balanced_luma[valid])) if np.any(valid) else 0.5
    target = 0.46
    ev = float(np.clip(np.log2(target / max(median, 0.05)), -0.35, 0.35))
    exposure_gain = 2.0 ** (ev * 0.45 * strength)
    exposed = np.clip(balanced * exposure_gain, 0.0, 1.0)

    exposed_luma = _luminance(exposed)
    low, high = np.percentile(exposed_luma, [1.0, 99.0])
    if high - low > 0.08:
        stretched = np.clip((exposed - low) / (high - low), 0.0, 1.0)
        contrast_mix = 0.08 * strength
        exposed = exposed * (1.0 - contrast_mix) + stretched * contrast_mix

    output = _to_uint8(exposed)
    return output, {
        "operation": "bounded_global_tone",
        "strength": round(value, 4),
        "white_balance_gains": [round(float(item), 5) for item in gains],
        "exposure_ev_applied": round(ev * 0.45 * strength, 5),
    }


def apply_light_balance(
    rgb_u8: np.ndarray, value: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """Balance whole-image luminance without changing white balance.

    V6 deliberately separates tone from color. This stage therefore uses one
    bounded exposure/contrast curve for the entire frame and never isolates or
    relights the face.
    """

    if value <= 0:
        return rgb_u8.copy(), {
            "operation": "light_balance_no_change",
            "strength": value,
        }
    strength = min(max(value / 0.5, 0.0), 1.0)
    rgb = _to_float(rgb_u8)
    luma = _luminance(rgb)
    valid = (luma > 0.03) & (luma < 0.97)
    median = float(np.median(luma[valid])) if np.any(valid) else 0.5
    target = 0.46
    ev = float(np.clip(np.log2(target / max(median, 0.05)), -0.3, 0.3))
    applied_ev = ev * 0.4 * strength
    exposure_gain = 2.0**applied_ev
    if exposure_gain >= 1.0:
        curved = 1.0 - np.power(1.0 - rgb, exposure_gain)
        # Fade the lift through the top 18% of luminance. This retains highlight
        # separation in skies/windows instead of multiplying them into clipping.
        shoulder = np.clip((1.0 - luma) / 0.18, 0.0, 1.0) ** 0.75
        exposed = rgb + (curved - rgb) * shoulder[..., None]
    else:
        curved = np.power(rgb, 1.0 / max(exposure_gain, 1e-6))
        # The matching shadow toe avoids turning near-black hair/clothing into
        # a featureless black patch when a frame needs a slight reduction.
        toe = np.clip(luma / 0.18, 0.0, 1.0) ** 0.75
        exposed = rgb + (curved - rgb) * toe[..., None]
    exposed = np.clip(exposed, 0.0, 1.0)
    return _to_uint8(exposed), {
        "operation": "highlight_protected_whole_image_light_balance",
        "strength": round(value, 4),
        "exposure_ev_applied": round(applied_ev, 5),
        "source_luma_median": round(median, 6),
        "highlight_shoulder_start_luma": 0.82,
        "shadow_toe_end_luma": 0.18,
    }


def apply_color_balance(
    rgb_u8: np.ndarray,
    white_balance: float,
    style_strength: float,
    mode: str,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply bounded whole-image white balance and the selected V6 recipe."""

    if mode not in {"natural", "fresh", "warm-film"}:
        raise RetouchError(f"unsupported V6 color mode: {mode}")
    if max(white_balance, style_strength) <= 0:
        return rgb_u8.copy(), {
            "operation": "color_balance_no_change",
            "mode": mode,
        }

    rgb = _to_float(rgb_u8)
    luma = _luminance(rgb)
    valid = (luma > 0.04) & (luma < 0.96)
    sample = rgb[valid] if np.any(valid) else rgb.reshape(-1, 3)
    means = np.maximum(sample.mean(axis=0), 1e-4)
    gray = float(means.mean())
    wb_scale = min(max(white_balance / 0.4, 0.0), 1.0)
    raw_gains = np.clip(gray / means, 0.94, 1.06)
    gains = 1.0 + (raw_gains - 1.0) * (0.6 * wb_scale)
    balanced = np.clip(rgb * gains, 0.0, 1.0)

    style_scale = min(max(style_strength / 0.3, 0.0), 1.0)
    style_gains = np.ones(3, dtype=np.float32)
    saturation_factor = 1.0
    if mode == "fresh":
        saturation_factor = 1.0 - 0.045 * style_scale
    elif mode == "warm-film":
        style_gains = np.array(
            [1.0 + 0.035 * style_scale, 1.0 + 0.008 * style_scale, 1.0 - 0.04 * style_scale],
            dtype=np.float32,
        )
        saturation_factor = 1.0 - 0.075 * style_scale
    styled = np.clip(balanced * style_gains, 0.0, 1.0)
    styled_luma = _luminance(styled)
    styled = np.clip(
        styled_luma[..., None]
        + (styled - styled_luma[..., None]) * saturation_factor,
        0.0,
        1.0,
    )
    return _to_uint8(styled), {
        "operation": "bounded_whole_image_color_balance",
        "mode": mode,
        "white_balance_strength": round(white_balance, 4),
        "style_strength": round(style_strength, 4),
        "white_balance_gains": [round(float(item), 5) for item in gains],
        "style_gains": [round(float(item), 5) for item in style_gains],
        "saturation_factor": round(float(saturation_factor), 5),
    }


def _base_skin_mask(rgb: np.ndarray) -> np.ndarray:
    red = rgb[..., 0] * 255.0
    green = rgb[..., 1] * 255.0
    blue = rgb[..., 2] * 255.0
    luma = _luminance(rgb)
    cb = 128.0 - 0.168736 * red - 0.331264 * green + 0.5 * blue
    cr = 128.0 + 0.5 * red - 0.418688 * green - 0.081312 * blue

    cb_confidence = np.clip(1.0 - np.abs(cb - 102.0) / 28.0, 0.0, 1.0)
    cr_confidence = np.clip(1.0 - np.abs(cr - 151.0) / 26.0, 0.0, 1.0)
    chroma_range = np.maximum.reduce([red, green, blue]) - np.minimum.reduce(
        [red, green, blue]
    )
    hard = (
        (cb >= 74.0)
        & (cb <= 132.0)
        & (cr >= 125.0)
        & (cr <= 180.0)
        & (red > 45.0)
        & (red >= green * 0.88)
        & (red >= blue * 0.92)
        & (chroma_range > 7.0)
        & (luma > 0.10)
        & (luma < 0.97)
    )
    raw = cb_confidence * cr_confidence * hard.astype(np.float32)
    feather = Image.fromarray(_to_uint8(np.repeat(raw[..., None], 3, axis=2))).convert(
        "L"
    )
    feathered = np.asarray(feather.filter(ImageFilter.GaussianBlur(radius=1.2))) / 255.0
    return feathered.astype(np.float32) * hard.astype(np.float32)


def _adaptive_portrait_mask(
    rgb: np.ndarray,
    base_mask: np.ndarray,
) -> tuple[np.ndarray | None, dict[str, Any]]:
    """Contain an over-broad color mask around the most portrait-like region.

    This fallback deliberately uses only pixel-space evidence. It does not infer,
    move, or regenerate facial structure. A broad mask is rescued only when a
    sufficiently detailed skin-colored region can be located; otherwise the skin
    stage remains a safe no-op.
    """

    height, width = base_mask.shape
    luma = _luminance(rgb)
    dx = np.zeros_like(luma)
    dy = np.zeros_like(luma)
    dx[:, 1:] = np.abs(luma[:, 1:] - luma[:, :-1])
    dy[1:, :] = np.abs(luma[1:, :] - luma[:-1, :])
    detail = np.maximum(dx, dy)
    detail_radius = float(np.clip(min(width, height) / 700.0, 1.0, 3.0))
    detail_context = np.asarray(
        Image.fromarray(
            np.clip(detail * 255.0, 0, 255).astype(np.uint8)
        ).filter(ImageFilter.GaussianBlur(radius=detail_radius)),
        dtype=np.float32,
    ) / 255.0
    skin_detail = detail_context[base_mask > 0.12]
    detail_p98 = float(np.percentile(skin_detail, 98.0)) if skin_detail.size else 0.0
    if detail_p98 < 0.012:
        return None, {
            "reason": "broad color mask had insufficient portrait detail",
            "portrait_detail_p98": round(detail_p98, 6),
        }

    detail_scale = max(float(np.percentile(skin_detail, 95.0)), 0.02)
    normalized_detail = np.clip(detail_context / detail_scale, 0.0, 1.0)
    score = base_mask * (0.12 + 0.88 * normalized_detail)

    scale = min(1.0, 192.0 / max(width, height))
    score_width = max(24, int(round(width * scale)))
    score_height = max(24, int(round(height * scale)))
    score_image = Image.fromarray(
        np.clip(score * 255.0, 0, 255).astype(np.uint8)
    )
    if (score_width, score_height) != (width, height):
        score_image = score_image.resize(
            (score_width, score_height), Image.Resampling.BILINEAR
        )
    blur_radius = max(2.0, min(score_width, score_height) * 0.035)
    pooled = np.asarray(
        score_image.filter(ImageFilter.GaussianBlur(radius=blur_radius)),
        dtype=np.float32,
    ) / 255.0

    y_grid, x_grid = np.mgrid[0:score_height, 0:score_width]
    x_normalized = (x_grid + 0.5) / score_width
    y_normalized = (y_grid + 0.5) / score_height
    horizontal_prior = 0.55 + 0.45 * np.exp(
        -(((x_normalized - 0.5) / 0.42) ** 2)
    )
    vertical_prior = 0.18 + 0.82 * np.exp(
        -(((y_normalized - 0.36) / 0.30) ** 2)
    )
    center_prior = horizontal_prior * vertical_prior
    focus_y, focus_x = np.unravel_index(
        int(np.argmax(pooled * center_prior)), pooled.shape
    )
    center_x = (float(focus_x) + 0.5) * width / score_width
    center_y = (float(focus_y) + 0.5) * height / score_height

    radius_x = max(width * 0.18, min(width, height) * 0.22)
    radius_y = max(height * 0.28, min(width, height) * 0.32)
    full_y, full_x = np.mgrid[0:height, 0:width]
    ellipse_distance = (
        ((full_x - center_x) / radius_x) ** 2
        + ((full_y - center_y) / radius_y) ** 2
    )
    spatial_prior = np.clip(1.0 - ellipse_distance, 0.0, 1.0) ** 0.4
    rescued = base_mask * spatial_prior.astype(np.float32)
    rescued_fraction = float(np.mean(rescued > 0.02))
    if rescued_fraction < 0.01 or rescued_fraction > 0.45:
        return None, {
            "reason": "adaptive portrait region was not safely bounded",
            "portrait_detail_p98": round(detail_p98, 6),
            "rescued_mask_fraction": round(rescued_fraction, 6),
        }
    return rescued.astype(np.float32), {
        "mask_strategy": "adaptive_spatial_rescue",
        "portrait_detail_p98": round(detail_p98, 6),
        "portrait_region_center": [
            round(center_x / width, 6),
            round(center_y / height, 6),
        ],
        "rescued_mask_fraction": round(rescued_fraction, 6),
    }


def _skin_mask(rgb: np.ndarray) -> tuple[np.ndarray | None, dict[str, Any]]:
    base_mask = _base_skin_mask(rgb)
    base_fraction = float(np.mean(base_mask > 0.02))
    if base_fraction <= 0.65:
        return base_mask, {
            "mask_strategy": "color_mask",
            "base_skin_mask_fraction": round(base_fraction, 6),
        }
    rescued, details = _adaptive_portrait_mask(rgb, base_mask)
    details["base_skin_mask_fraction"] = round(base_fraction, 6)
    return rescued, details


def _edge_protection(luma: np.ndarray) -> np.ndarray:
    dx = np.zeros_like(luma)
    dy = np.zeros_like(luma)
    dx[:, 1:] = np.abs(luma[:, 1:] - luma[:, :-1])
    dy[1:, :] = np.abs(luma[1:, :] - luma[:-1, :])
    gradient = np.maximum(dx, dy)
    return np.exp(-((gradient / 0.055) ** 2)).astype(np.float32)


def _blur_rgb(rgb: np.ndarray, radius: float) -> np.ndarray:
    image = Image.fromarray(_to_uint8(rgb))
    return _to_float(np.asarray(image.filter(ImageFilter.GaussianBlur(radius=radius))))


def apply_skin(
    rgb_u8: np.ndarray,
    smoothing: float,
    blemish: float,
    tone_evenness: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    if max(smoothing, blemish, tone_evenness) <= 0:
        return rgb_u8.copy(), {
            "operation": "skin",
            "skin_mask_fraction": 0.0,
        }

    rgb = _to_float(rgb_u8)
    mask, mask_details = _skin_mask(rgb)
    if mask is None:
        return rgb_u8.copy(), {
            "operation": "skin_stage_skipped",
            "reason": mask_details["reason"],
            "skin_mask_fraction": mask_details.get(
                "rescued_mask_fraction",
                mask_details["base_skin_mask_fraction"],
            ),
            **mask_details,
        }
    mask_fraction = float(np.mean(mask > 0.02))
    protected_mask = mask * _edge_protection(_luminance(rgb))
    height, width = rgb.shape[:2]
    short_side = min(width, height)

    if smoothing > 0:
        smoothing_scale = min(max(smoothing / 0.5, 0.0), 1.0)
        radius = float(np.clip(short_side / 650.0, 0.8, 3.0))
        blurred = _blur_rgb(rgb, radius)
        alpha = protected_mask[..., None] * (0.28 * smoothing_scale)
        rgb = rgb * (1.0 - alpha) + blurred * alpha

    if tone_evenness > 0:
        tone_scale = min(max(tone_evenness / 0.45, 0.0), 1.0)
        radius = float(np.clip(short_side * 0.012, 5.0, 22.0))
        low_frequency = _blur_rgb(rgb, radius)
        luma_delta = _luminance(rgb) - _luminance(low_frequency)
        chroma_smoothed = np.clip(low_frequency + luma_delta[..., None], 0.0, 1.0)
        alpha = protected_mask[..., None] * (0.20 * tone_scale)
        rgb = rgb * (1.0 - alpha) + chroma_smoothed * alpha

    redness_fraction = 0.0
    if blemish > 0:
        blemish_scale = min(max(blemish / 0.5, 0.0), 1.0)
        median = _to_float(
            np.asarray(
                Image.fromarray(_to_uint8(rgb)).filter(
                    ImageFilter.MedianFilter(size=3)
                )
            )
        )
        red_excess = rgb[..., 0] - 0.5 * (rgb[..., 1] + rgb[..., 2])
        local_red_excess = median[..., 0] - 0.5 * (
            median[..., 1] + median[..., 2]
        )
        anomaly = np.clip((red_excess - local_red_excess - 0.025) / 0.08, 0.0, 1.0)
        anomaly *= mask
        anomaly_image = Image.fromarray(np.clip(anomaly * 255.0, 0, 255).astype(np.uint8))
        anomaly = np.asarray(
            anomaly_image.filter(ImageFilter.GaussianBlur(radius=0.65)),
            dtype=np.float32,
        ) / 255.0
        alpha = anomaly[..., None] * (0.35 * blemish_scale)
        rgb = rgb * (1.0 - alpha) + median * alpha
        redness_fraction = float(np.mean(anomaly > 0.02))

    output = _to_uint8(rgb)
    return output, {
        "operation": "masked_skin_retouch",
        "skin_mask_fraction": round(mask_fraction, 6),
        **mask_details,
        "temporary_redness_fraction": round(redness_fraction, 6),
        "strengths": {
            "skin_smoothing": round(smoothing, 4),
            "blemish_reduction": round(blemish, 4),
            "skin_tone_evenness": round(tone_evenness, 4),
        },
    }


def _save_pixels(
    output: Path,
    rgb: np.ndarray,
    alpha: np.ndarray | None,
    source: Image.Image,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RetouchError(f"refusing to overwrite existing output: {output}")
    result = Image.fromarray(rgb)
    if alpha is not None:
        result.putalpha(Image.fromarray(alpha))
    save_options: dict[str, Any] = {"format": "PNG", "compress_level": 6}
    if source.info.get("icc_profile"):
        save_options["icc_profile"] = source.info["icc_profile"]
    if source.info.get("dpi"):
        save_options["dpi"] = source.info["dpi"]
    result.save(output, **save_options)
    os.chmod(output, 0o600)


def delta_stats(before: np.ndarray, after: np.ndarray) -> dict[str, float]:
    delta = np.abs(after.astype(np.int16) - before.astype(np.int16))
    changed = np.any(delta > 0, axis=2)
    return {
        "mean_absolute_channel_delta": round(float(delta.mean()), 6),
        "changed_pixel_fraction": round(float(changed.mean()), 6),
        "maximum_channel_delta": int(delta.max()),
    }


def apply_stage(
    source_path: Path,
    output_path: Path,
    stage_id: str,
    parameters: dict[str, float],
) -> dict[str, Any]:
    source, rgb, alpha = _load_pixels(source_path)
    if stage_id == "lighting":
        edited, details = apply_lighting(
            rgb, float(parameters.get("lighting_improvement", 0.0))
        )
    elif stage_id == "skin":
        edited, details = apply_skin(
            rgb,
            float(parameters.get("skin_smoothing", 0.0)),
            float(parameters.get("blemish_reduction", 0.0)),
            float(parameters.get("skin_tone_evenness", 0.0)),
        )
    else:
        raise RetouchError(f"unsupported deterministic stage: {stage_id}")
    _save_pixels(output_path, edited, alpha, source)
    return {
        "stage": stage_id,
        "source_dimensions": {"width": source.width, "height": source.height},
        "output_dimensions": {"width": source.width, "height": source.height},
        "delta": delta_stats(rgb, edited),
        "details": details,
    }


def apply_v6_stage(
    source_path: Path,
    output_path: Path,
    stage_id: str,
    parameters: dict[str, Any],
    mode: str = "natural",
) -> dict[str, Any]:
    """Apply a geometry-safe V6 deterministic stage.

    Semantic stages that cannot be localized reliably with Pillow/NumPy are
    emitted as explicit inspection-only no-ops. They must still be recorded and
    visually reviewed by the V6 state machine; silently skipping them is not
    allowed.
    """

    source, rgb, alpha = _load_pixels(source_path)
    if stage_id == "photo_correction":
        edited = rgb.copy()
        details: dict[str, Any] = {
            "operation": "validated_geometry_safe_no_change",
            "reason": "No safe affine correction was requested or inferred.",
        }
    elif stage_id == "light_balance":
        edited, details = apply_light_balance(
            rgb, float(parameters.get("lighting_improvement", 0.0))
        )
    elif stage_id == "color_balance":
        edited, details = apply_color_balance(
            rgb,
            float(parameters.get("white_balance_correction", 0.0)),
            float(parameters.get("style_strength", 0.0)),
            mode,
        )
    elif stage_id == "skin_cleanup":
        edited, details = apply_skin(
            rgb,
            float(parameters.get("skin_smoothing", 0.0)),
            float(parameters.get("blemish_reduction", 0.0)),
            float(parameters.get("skin_tone_evenness", 0.0)),
        )
        details["under_eye_deferred_to_facial_features"] = True
    elif stage_id in {"facial_features", "hair_clothing", "background"}:
        edited = rgb.copy()
        details = {
            "operation": "inspection_only_no_change",
            "reason": (
                "No reliable semantic mask is available for deterministic editing; "
                "record visual inspection or evaluate one isolated model candidate."
            ),
        }
    else:
        raise RetouchError(f"unsupported V6 deterministic stage: {stage_id}")
    _save_pixels(output_path, edited, alpha, source)
    return {
        "schema_version": 6,
        "stage": stage_id,
        "mode": mode,
        "source_dimensions": {"width": source.width, "height": source.height},
        "output_dimensions": {"width": source.width, "height": source.height},
        "delta": delta_stats(rgb, edited),
        "details": details,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply one deterministic V3 portrait-retouch stage."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--stage", choices=["lighting", "skin"], required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        parameters = json.loads(args.parameters.read_text(encoding="utf-8"))
        if not isinstance(parameters, dict):
            raise RetouchError("parameters file must contain a JSON object")
        result = apply_stage(args.source, args.output, args.stage, parameters)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (OSError, json.JSONDecodeError, RetouchError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
