#!/usr/bin/env python3
"""Audit a retouched portrait without pretending pixels can replace visual review.

The automatic checks deliberately use only Pillow and NumPy.  They can reject
extreme, high-confidence failures, but an automatic pass never accepts a model
candidate.  Acceptance requires a complete manual review of all ten checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError


CHECK_IDS = (
    "skin_texture_natural",
    "face_neck_color_consistent",
    "sclera_teeth_not_blue",
    "lip_saturation_natural",
    "background_lines_straight",
    "hair_edges_clean",
    "subject_background_brightness_cohesive",
    "zoom_and_thumbnail_coherent",
    "change_not_excessive_vs_original",
    "phone_viewing_comfortable",
)

VALID_STATUSES = {
    "pass",
    "review",
    "reject",
    "not_evaluable",
    "not_applicable",
}

MANUAL_STATUS_ALIASES = {
    "pass": "pass",
    "passed": "pass",
    "ok": "pass",
    "fail": "fail",
    "failed": "fail",
    "reject": "fail",
    "not-applicable": "not_applicable",
    "not_applicable": "not_applicable",
    "n/a": "not_applicable",
    "na": "not_applicable",
}

PROOF_SCHEMA_VERSION = 2
PROOF_GENERATOR = "portrait-retouch-render-proof-v6"
PROOF_OUTPUT_NAMES = {
    "comparison": "comparison.png",
    "difference_heatmap": "difference-heatmap.png",
    "thumbnail_720": "thumbnail-720.png",
    "mobile_preview_1080": "mobile-preview-1080.png",
    "center_detail_100_percent": "detail-100-percent.png",
}
PROOF_VIEW_FILE_KEYS = {
    "100_percent": "center_detail_100_percent",
    "fit_view": "comparison",
    "thumbnail": "thumbnail_720",
    "phone_normal_brightness": "mobile_preview_1080",
    "side_by_side": "comparison",
    "difference_heatmap": "difference_heatmap",
}

PLACEHOLDER_NOTE_FRAGMENTS = (
    "describe ",
    "replace_with",
    "replace with",
    "placeholder",
    "template",
    "todo",
    "tbd",
    "fill in",
    "insert note",
    "add note",
    "evidence here",
    "your note",
    "image-specific",
    "looks good",
    "looks fine",
    "passes this test",
    "no issue",
    "待填写",
    "请填写",
    "占位",
    "模板",
    "替换为",
)

GENERIC_NOTES = {
    "pass",
    "fail",
    "passed",
    "failed",
    "ok",
    "okay",
    "looks good",
    "looks fine",
    "no issue",
    "no issues",
    "not applicable",
    "n/a",
    "na",
    "通过",
    "失败",
    "没问题",
    "看起来不错",
    "不适用",
}

CHECK_NOTE_TERMS = {
    "skin_texture_natural": ("skin", "texture", "pore", "fine line", "皮肤", "纹理", "毛孔", "细纹"),
    "face_neck_color_consistent": ("face", "neck", "tone", "undertone", "脸", "面部", "颈", "肤色", "色调"),
    "sclera_teeth_not_blue": ("sclera", "eye white", "teeth", "eye", "closed", "visible", "眼白", "牙", "眼睛", "闭眼", "可见"),
    "lip_saturation_natural": ("lip", "saturation", "color", "edge", "唇", "饱和", "颜色", "边缘"),
    "background_lines_straight": ("line", "frame", "wall", "door", "horizon", "geometry", "直线", "背景", "门框", "墙", "地平", "几何"),
    "hair_edges_clean": ("hair", "edge", "flyaway", "halo", "smear", "发", "边缘", "碎发", "光晕", "涂抹"),
    "subject_background_brightness_cohesive": ("subject", "background", "brightness", "lighting", "exposure", "人物", "主体", "背景", "亮度", "光线", "曝光"),
    "zoom_and_thumbnail_coherent": ("100%", "zoom", "thumbnail", "reduced", "full size", "detail", "缩略", "放大", "原尺寸", "细节"),
    "change_not_excessive_vs_original": ("original", "candidate", "difference", "heatmap", "side-by-side", "change", "原图", "候选", "差异", "热图", "对比", "变化"),
    "phone_viewing_comfortable": ("phone", "mobile", "1080", "screen", "brightness", "shadow", "highlight", "手机", "移动", "屏幕", "亮度", "阴影", "高光"),
}

DEFAULT_GATES: dict[str, Any] = {
    "schema_version": 6,
    "required_keys": list(CHECK_IDS),
    "not_applicable_requires_note": True,
    "required_views": [
        "100_percent",
        "fit_view",
        "thumbnail",
        "phone_normal_brightness",
    ],
    "minimum_short_edge": 768,
    "maximum_aspect_ratio_delta": 0.005,
    "analysis_max_long_edge": 1024,
    "global_change": {
        "modes": {
            "natural": {
                "review_rgb_mae": 0.10,
                "reject_rgb_mae": 0.28,
                "review_luma_mae": 0.08,
                "reject_luma_mae": 0.24,
            },
            "fresh": {
                "review_rgb_mae": 0.14,
                "reject_rgb_mae": 0.32,
                "review_luma_mae": 0.11,
                "reject_luma_mae": 0.27,
            },
            "warm-film": {
                "review_rgb_mae": 0.14,
                "reject_rgb_mae": 0.32,
                "review_luma_mae": 0.11,
                "reject_luma_mae": 0.27,
            },
        }
    },
    "multiscale": {
        "review_luma_correlation": 0.90,
        "reject_luma_correlation": 0.70,
        "review_thumbnail_correlation": 0.92,
        "reject_thumbnail_correlation": 0.75,
        "review_sharpness_ratio_low": 0.60,
        "reject_sharpness_ratio_low": 0.20,
        "review_sharpness_ratio_high": 1.80,
        "reject_sharpness_ratio_high": 5.00,
        "minimum_source_sharpness": 0.001,
    },
    "mobile": {
        "review_white_clip_increase": 0.02,
        "reject_white_clip_increase": 0.25,
        "review_black_clip_increase": 0.03,
        "reject_black_clip_increase": 0.25,
        "review_median_luma_shift": 0.12,
        "reject_median_luma_shift": 0.25,
    },
}


class AuditConfigError(ValueError):
    """Raised for an invalid mode, gates file, or manual-review document."""


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_gates(path: Path | None = None) -> dict[str, Any]:
    """Load optional V6 gates while keeping safe defaults for missing keys."""

    default_path = Path(__file__).resolve().parents[1] / "references" / "audit-gates.json"
    selected = path if path is not None else default_path
    if not selected.exists():
        return deepcopy(DEFAULT_GATES)
    try:
        raw = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditConfigError(f"cannot read audit gates: {exc}") from exc
    if not isinstance(raw, dict):
        raise AuditConfigError("audit gates must be a JSON object")
    return _deep_merge(DEFAULT_GATES, raw)


def _threshold(
    gates: Mapping[str, Any],
    section: str,
    key: str,
    default: float,
) -> float:
    """Read both the local schema and a checks.<id> compatible schema."""

    local = gates.get(section, {})
    if isinstance(local, Mapping) and key in local:
        return float(local[key])
    checks = gates.get("checks", {})
    if isinstance(checks, Mapping):
        aliases = {
            "global_change": "change_not_excessive_vs_original",
            "multiscale": "zoom_and_thumbnail_coherent",
            "mobile": "phone_viewing_comfortable",
        }
        check = checks.get(aliases.get(section, section), {})
        if isinstance(check, Mapping) and key in check:
            return float(check[key])
    return float(default)


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


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _load_proof_document(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AuditConfigError(f"cannot read proof manifest: {exc}") from exc
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditConfigError(f"proof manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise AuditConfigError("proof manifest must be a JSON object")
    return document, hashlib.sha256(payload).hexdigest()


def _proof_path(
    raw_path: Any,
    *,
    manifest_dir: Path,
    label: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise AuditConfigError(f"proof manifest {label} must be a non-empty path")
    path = Path(raw_path)
    resolved = (manifest_dir / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(manifest_dir)
    except ValueError as exc:
        raise AuditConfigError(
            f"proof manifest {label} must stay inside the proof directory"
        ) from exc
    if not resolved.is_file():
        raise AuditConfigError(f"proof manifest {label} file is missing: {resolved}")
    return resolved


def _proof_binding(
    manifest_path: Path | str | None,
    *,
    original_sha256: str,
    candidate_sha256: str,
    required_views: list[str],
) -> dict[str, Any]:
    """Verify a render_proof manifest and return an audit-stable binding."""

    if manifest_path is None:
        return {
            "status": "missing",
            "verified": False,
            "reason": "A render_proof manifest is required before acceptance.",
            "required_views": list(required_views),
        }

    selected = Path(manifest_path).resolve()
    if not selected.is_file():
        raise AuditConfigError(f"proof manifest does not exist: {selected}")
    manifest, manifest_hash = _load_proof_document(selected)
    manifest_dir = selected.parent.resolve()

    if manifest.get("schema_version") != PROOF_SCHEMA_VERSION:
        raise AuditConfigError(
            f"proof manifest must use schema_version {PROOF_SCHEMA_VERSION}"
        )
    if manifest.get("generator") != PROOF_GENERATOR:
        raise AuditConfigError("proof manifest was not generated by render_proof V6")
    if manifest.get("original_sha256") != original_sha256:
        raise AuditConfigError("proof manifest is stale for the original image")
    if manifest.get("candidate_sha256") != candidate_sha256:
        raise AuditConfigError("proof manifest is stale for the candidate image")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise AuditConfigError("proof manifest must contain input bindings")
    for label, expected_hash in (
        ("original", original_sha256),
        ("candidate", candidate_sha256),
    ):
        record = inputs.get(label)
        if not isinstance(record, Mapping) or record.get("sha256") != expected_hash:
            raise AuditConfigError(f"proof manifest {label} input binding is invalid")

    files = manifest.get("files")
    file_hashes = manifest.get("file_sha256")
    if not isinstance(files, Mapping) or not isinstance(file_hashes, Mapping):
        raise AuditConfigError("proof manifest must contain files and file_sha256 objects")

    verified_files: dict[str, dict[str, str]] = {}
    for file_key, expected_name in PROOF_OUTPUT_NAMES.items():
        file_path = _proof_path(
            files.get(file_key),
            manifest_dir=manifest_dir,
            label=f"files.{file_key}",
        )
        if file_path.name != expected_name:
            raise AuditConfigError(
                f"proof manifest files.{file_key} must name {expected_name}"
            )
        expected_hash = file_hashes.get(file_key)
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise AuditConfigError(
                f"proof manifest file_sha256.{file_key} is missing or invalid"
            )
        actual_hash = _sha256(file_path)
        if actual_hash != expected_hash:
            raise AuditConfigError(f"proof file was modified after rendering: {file_key}")
        verified_files[file_key] = {
            "path": str(file_path),
            "sha256": actual_hash,
        }

    views = manifest.get("views")
    view_file_keys = manifest.get("view_file_keys")
    view_hashes = manifest.get("view_sha256")
    declared_required = manifest.get("required_views")
    if (
        not isinstance(views, Mapping)
        or not isinstance(view_file_keys, Mapping)
        or not isinstance(view_hashes, Mapping)
        or not isinstance(declared_required, list)
    ):
        raise AuditConfigError(
            "proof manifest must contain views, view_file_keys, view_sha256, and required_views"
        )
    if any(view not in declared_required for view in required_views):
        raise AuditConfigError("proof manifest does not declare every required audit view")

    verified_views: dict[str, dict[str, str]] = {}
    for view in required_views:
        expected_file_key = PROOF_VIEW_FILE_KEYS.get(view)
        if expected_file_key is None:
            raise AuditConfigError(f"audit gates contain an unsupported proof view: {view}")
        if view_file_keys.get(view) != expected_file_key:
            raise AuditConfigError(f"proof view {view} is bound to the wrong proof file")
        view_path = _proof_path(
            views.get(view),
            manifest_dir=manifest_dir,
            label=f"views.{view}",
        )
        file_record = verified_files[expected_file_key]
        if view_path != Path(file_record["path"]):
            raise AuditConfigError(f"proof view {view} does not reference its rendered file")
        expected_hash = view_hashes.get(view)
        if expected_hash != file_record["sha256"]:
            raise AuditConfigError(f"proof view {view} has an invalid file hash")
        verified_views[view] = {
            "file_key": expected_file_key,
            "path": str(view_path),
            "sha256": file_record["sha256"],
        }

    return {
        "status": "verified",
        "verified": True,
        "generator": PROOF_GENERATOR,
        "schema_version": PROOF_SCHEMA_VERSION,
        "manifest": {"path": str(selected), "sha256": manifest_hash},
        "inputs": {
            "original_sha256": original_sha256,
            "candidate_sha256": candidate_sha256,
        },
        "required_views": list(required_views),
        "files": verified_files,
        "views": verified_views,
    }


def _resize_for_analysis(image: Image.Image, max_long_edge: int) -> Image.Image:
    width, height = image.size
    long_edge = max(width, height)
    if long_edge <= max_long_edge:
        return image.copy()
    scale = max_long_edge / long_edge
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _rgb_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image, dtype=np.float32) / 255.0


def _luma(rgb: np.ndarray) -> np.ndarray:
    return (
        rgb[..., 0] * 0.2126
        + rgb[..., 1] * 0.7152
        + rgb[..., 2] * 0.0722
    )


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    a = left.astype(np.float64, copy=False).ravel()
    b = right.astype(np.float64, copy=False).ravel()
    a_std = float(a.std())
    b_std = float(b.std())
    if a_std < 1e-8 or b_std < 1e-8:
        return 1.0 if float(np.mean(np.abs(a - b))) < 0.01 else 0.0
    value = float(np.corrcoef(a, b)[0, 1])
    return value if np.isfinite(value) else 0.0


def _sharpness(luma: np.ndarray) -> float:
    source = Image.fromarray(np.uint8(np.clip(luma * 255.0, 0, 255)))
    blurred = np.asarray(source.filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32)
    residual = np.abs(luma - blurred / 255.0)
    return float(np.mean(residual))


def _round_metrics(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, dict):
        return {key: _round_metrics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_metrics(item) for item in value]
    return value


def _check(
    check_id: str,
    status: str,
    automation: str,
    reason: str,
    metrics: Mapping[str, Any] | None = None,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise AuditConfigError(f"invalid check status: {status}")
    return {
        "id": check_id,
        "status": status,
        "automatic_status": status,
        "manual_status": None,
        "automation": automation,
        "reason": reason,
        "metrics": _round_metrics(dict(metrics or {})),
        "thresholds": _round_metrics(dict(thresholds or {})),
    }


def _semantic_checks() -> dict[str, dict[str, Any]]:
    reasons = {
        "skin_texture_natural": "A skin-detail mask or complete manual review is required.",
        "face_neck_color_consistent": "Reliable face-skin and neck-skin regions are unavailable.",
        "sclera_teeth_not_blue": "Visible sclera and teeth require semantic visibility review.",
        "lip_saturation_natural": "A reliable lip region is unavailable.",
        "background_lines_straight": "Straight background structures require visual annotation or review.",
        "hair_edges_clean": "A reliable hair-boundary region is unavailable.",
        "subject_background_brightness_cohesive": "Reliable person and adjacent-background regions are unavailable.",
    }
    return {
        check_id: _check(
            check_id,
            "not_evaluable",
            "manual_or_semantic_regions_required",
            reason,
        )
        for check_id, reason in reasons.items()
    }


def _validate_manual_note(check_id: str, note: Any) -> str:
    if not isinstance(note, str):
        raise AuditConfigError(
            f"manual review for {check_id} must include a text note"
        )
    normalized = " ".join(note.strip().split())
    lowered = normalized.casefold()
    if len(normalized) < 16:
        raise AuditConfigError(
            f"manual review note for {check_id} is too short to be image-specific"
        )
    if lowered in GENERIC_NOTES or any(
        fragment in lowered for fragment in PLACEHOLDER_NOTE_FRAGMENTS
    ):
        raise AuditConfigError(
            f"manual review note for {check_id} is a placeholder or generic judgment"
        )
    if not any(term.casefold() in lowered for term in CHECK_NOTE_TERMS[check_id]):
        raise AuditConfigError(
            f"manual review note for {check_id} must describe check-specific visible evidence"
        )
    return normalized


def _normalize_manual_review(
    document: Mapping[str, Any] | None,
    *,
    not_applicable_requires_note: bool = True,
) -> tuple[dict[str, dict[str, Any]], bool]:
    if document is None:
        return {}, False
    raw_checks: Any = document.get("checks", document)
    if not isinstance(raw_checks, Mapping):
        raise AuditConfigError("manual review checks must be a JSON object")
    unknown = sorted(set(raw_checks) - set(CHECK_IDS))
    if unknown:
        raise AuditConfigError(f"unknown manual-review checks: {', '.join(unknown)}")

    normalized: dict[str, dict[str, Any]] = {}
    for check_id, value in raw_checks.items():
        if isinstance(value, str):
            raise AuditConfigError(
                f"manual review for {check_id} must be an object with status and image-specific note"
            )
        if not isinstance(value, Mapping):
            raise AuditConfigError(f"manual review for {check_id} must be an object")
        raw_status = value.get("status", value.get("decision"))
        if not isinstance(raw_status, str):
            raise AuditConfigError(f"manual review for {check_id} has no status")
        status = MANUAL_STATUS_ALIASES.get(raw_status.strip().lower())
        if status is None:
            raise AuditConfigError(
                f"manual review for {check_id} has unsupported status {raw_status!r}"
            )
        # Notes are mandatory for every outcome.  The legacy gate flag remains
        # accepted for file compatibility but cannot weaken this V6 invariant.
        _ = not_applicable_requires_note
        note = _validate_manual_note(check_id, value.get("note"))
        normalized[check_id] = {"status": status, "note": note}
    return normalized, set(normalized) == set(CHECK_IDS)


def load_manual_review(value: str | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    candidate = Path(value)
    try:
        text = candidate.read_text(encoding="utf-8") if candidate.is_file() else value
    except OSError as exc:
        raise AuditConfigError(f"cannot read manual review: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuditConfigError(f"manual review is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise AuditConfigError("manual review must be a JSON object")
    return document


def _failed_result(
    original: Path,
    candidate: Path,
    mode: str,
    reason: str,
) -> dict[str, Any]:
    checks = _semantic_checks()
    for check_id in CHECK_IDS:
        if check_id not in checks:
            checks[check_id] = _check(
                check_id,
                "not_evaluable",
                "automatic_preflight_unavailable",
                "Automatic analysis could not run because image preflight failed.",
            )
    return {
        "schema_version": 6,
        "checker": "portrait-retouch-audit-v6",
        "mode": mode,
        "inputs": {
            "original": {"path": str(original)},
            "candidate": {"path": str(candidate)},
        },
        "preflight": {"readable": False, "status": "reject", "reason": reason},
        "proof": {
            "status": "unverified",
            "verified": False,
            "reason": "Proof cannot be verified because image preflight failed.",
        },
        "checks": {check_id: checks[check_id] for check_id in CHECK_IDS},
        "summary": {
            "automated_precheck": "reject",
            "manual_review_complete": False,
            "proof_verified": False,
            "hard_rejects": ["preflight"],
            "review_flags": [],
            "not_evaluable": list(CHECK_IDS),
            "decision": "reject",
            "may_auto_accept": False,
        },
        "decision": "reject",
    }


def audit_images(
    original: Path,
    candidate: Path,
    *,
    mode: str = "natural",
    manual_review: Mapping[str, Any] | None = None,
    proof_manifest: Path | str | None = None,
    gates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fixed ten-check audit for one original/candidate pair."""

    gate_values = load_gates() if gates is None else _deep_merge(DEFAULT_GATES, gates)
    configured_keys = gate_values.get("required_keys", list(CHECK_IDS))
    if configured_keys != list(CHECK_IDS):
        raise AuditConfigError(
            "audit gates required_keys do not match the fixed V6 review contract"
        )
    mode_tables = gate_values.get("global_change", {}).get("modes", {})
    if mode not in mode_tables:
        raise AuditConfigError(f"unknown mode: {mode}")
    configured_views = gate_values.get("required_views")
    if (
        not isinstance(configured_views, list)
        or not configured_views
        or any(not isinstance(view, str) or not view for view in configured_views)
        or len(configured_views) != len(set(configured_views))
    ):
        raise AuditConfigError("audit gates required_views must be a unique non-empty list")
    unsupported_views = sorted(set(configured_views) - set(PROOF_VIEW_FILE_KEYS))
    if unsupported_views:
        raise AuditConfigError(
            "audit gates contain unsupported proof views: " + ", ".join(unsupported_views)
        )

    try:
        original_image = _load_rgb(original)
        candidate_image = _load_rgb(candidate)
        original_hash = _sha256(original)
        candidate_hash = _sha256(candidate)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        return _failed_result(original, candidate, mode, str(exc))

    proof = _proof_binding(
        proof_manifest,
        original_sha256=original_hash,
        candidate_sha256=candidate_hash,
        required_views=configured_views,
    )

    original_width, original_height = original_image.size
    candidate_width, candidate_height = candidate_image.size
    original_ratio = original_width / original_height
    candidate_ratio = candidate_width / candidate_height
    ratio_delta = abs(candidate_ratio / original_ratio - 1.0)

    preflight_cfg = gate_values.get("preflight", {})
    minimum_short_edge = int(
        preflight_cfg.get(
            "minimum_short_edge", gate_values.get("minimum_short_edge", 768)
        )
    )
    maximum_ratio_delta = float(
        preflight_cfg.get(
            "maximum_aspect_ratio_delta",
            gate_values.get("maximum_aspect_ratio_delta", 0.005),
        )
    )
    ratio_ok = ratio_delta <= maximum_ratio_delta
    resolution_ok = min(candidate_width, candidate_height) >= minimum_short_edge
    preflight_ok = ratio_ok and resolution_ok

    maximum_edge = int(gate_values.get("analysis_max_long_edge", 1024))
    original_analysis = _resize_for_analysis(original_image, maximum_edge)
    candidate_aligned = candidate_image.resize(
        original_image.size, Image.Resampling.LANCZOS
    )
    candidate_analysis = candidate_aligned.resize(
        original_analysis.size, Image.Resampling.LANCZOS
    )
    original_rgb = _rgb_array(original_analysis)
    candidate_rgb = _rgb_array(candidate_analysis)
    original_luma = _luma(original_rgb)
    candidate_luma = _luma(candidate_rgb)

    rgb_mae = float(np.mean(np.abs(candidate_rgb - original_rgb)))
    luma_mae = float(np.mean(np.abs(candidate_luma - original_luma)))
    luma_correlation = _correlation(original_luma, candidate_luma)

    thumbnail_size = (192, 192)
    original_thumb = _rgb_array(original_analysis.resize(thumbnail_size, Image.Resampling.LANCZOS))
    candidate_thumb = _rgb_array(candidate_analysis.resize(thumbnail_size, Image.Resampling.LANCZOS))
    thumbnail_correlation = _correlation(_luma(original_thumb), _luma(candidate_thumb))

    original_sharpness = _sharpness(original_luma)
    candidate_sharpness = _sharpness(candidate_luma)
    minimum_sharpness = _threshold(
        gate_values,
        "multiscale",
        "minimum_source_sharpness",
        DEFAULT_GATES["multiscale"]["minimum_source_sharpness"],
    )
    sharpness_evaluable = original_sharpness >= minimum_sharpness
    sharpness_ratio = (
        candidate_sharpness / original_sharpness if sharpness_evaluable else None
    )

    review_corr = _threshold(
        gate_values,
        "multiscale",
        "review_luma_correlation",
        DEFAULT_GATES["multiscale"]["review_luma_correlation"],
    )
    reject_corr = _threshold(
        gate_values,
        "multiscale",
        "reject_luma_correlation",
        DEFAULT_GATES["multiscale"]["reject_luma_correlation"],
    )
    review_thumb = _threshold(
        gate_values,
        "multiscale",
        "review_thumbnail_correlation",
        DEFAULT_GATES["multiscale"]["review_thumbnail_correlation"],
    )
    reject_thumb = _threshold(
        gate_values,
        "multiscale",
        "reject_thumbnail_correlation",
        DEFAULT_GATES["multiscale"]["reject_thumbnail_correlation"],
    )
    review_sharp_low = _threshold(
        gate_values,
        "multiscale",
        "review_sharpness_ratio_low",
        DEFAULT_GATES["multiscale"]["review_sharpness_ratio_low"],
    )
    reject_sharp_low = _threshold(
        gate_values,
        "multiscale",
        "reject_sharpness_ratio_low",
        DEFAULT_GATES["multiscale"]["reject_sharpness_ratio_low"],
    )
    review_sharp_high = _threshold(
        gate_values,
        "multiscale",
        "review_sharpness_ratio_high",
        DEFAULT_GATES["multiscale"]["review_sharpness_ratio_high"],
    )
    reject_sharp_high = _threshold(
        gate_values,
        "multiscale",
        "reject_sharpness_ratio_high",
        DEFAULT_GATES["multiscale"]["reject_sharpness_ratio_high"],
    )

    multiscale_reject = luma_correlation < reject_corr or thumbnail_correlation < reject_thumb
    multiscale_review = luma_correlation < review_corr or thumbnail_correlation < review_thumb
    if sharpness_ratio is not None:
        multiscale_reject = multiscale_reject or not (
            reject_sharp_low <= sharpness_ratio <= reject_sharp_high
        )
        multiscale_review = multiscale_review or not (
            review_sharp_low <= sharpness_ratio <= review_sharp_high
        )
    if multiscale_reject:
        multiscale_status = "reject"
        multiscale_reason = "Extreme structural correlation or clarity loss/gain was detected."
    elif multiscale_review:
        multiscale_status = "review"
        multiscale_reason = "Global structure or clarity changed enough to require visual review."
    else:
        multiscale_status = "pass"
        multiscale_reason = "Automatic multiscale risk indicators remain inside conservative bounds."

    mode_cfg = mode_tables[mode]
    review_rgb = float(mode_cfg.get("review_rgb_mae", 0.10))
    reject_rgb = float(mode_cfg.get("reject_rgb_mae", 0.28))
    review_luma = float(mode_cfg.get("review_luma_mae", 0.08))
    reject_luma = float(mode_cfg.get("reject_luma_mae", 0.24))
    if rgb_mae > reject_rgb or luma_mae > reject_luma:
        change_status = "reject"
        change_reason = "The candidate differs from the original beyond the extreme-change guardrail."
    elif rgb_mae > review_rgb or luma_mae > review_luma:
        change_status = "review"
        change_reason = "The amount of change is mode-valid only after visual comparison."
    else:
        change_status = "pass"
        change_reason = "Global pixel-change risk remains inside the mode-specific guardrail."

    original_white = float(np.mean(original_luma >= 0.98))
    candidate_white = float(np.mean(candidate_luma >= 0.98))
    original_black = float(np.mean(original_luma <= 0.02))
    candidate_black = float(np.mean(candidate_luma <= 0.02))
    white_increase = candidate_white - original_white
    black_increase = candidate_black - original_black
    median_shift = float(np.median(candidate_luma) - np.median(original_luma))
    absolute_median_shift = abs(median_shift)

    mobile_defaults = DEFAULT_GATES["mobile"]
    review_white = _threshold(
        gate_values, "mobile", "review_white_clip_increase", mobile_defaults["review_white_clip_increase"]
    )
    reject_white = _threshold(
        gate_values, "mobile", "reject_white_clip_increase", mobile_defaults["reject_white_clip_increase"]
    )
    review_black = _threshold(
        gate_values, "mobile", "review_black_clip_increase", mobile_defaults["review_black_clip_increase"]
    )
    reject_black = _threshold(
        gate_values, "mobile", "reject_black_clip_increase", mobile_defaults["reject_black_clip_increase"]
    )
    review_median = _threshold(
        gate_values, "mobile", "review_median_luma_shift", mobile_defaults["review_median_luma_shift"]
    )
    reject_median = _threshold(
        gate_values, "mobile", "reject_median_luma_shift", mobile_defaults["reject_median_luma_shift"]
    )
    if (
        white_increase > reject_white
        or black_increase > reject_black
        or absolute_median_shift > reject_median
    ):
        mobile_status = "reject"
        mobile_reason = "Extreme clipping or luminance displacement was detected."
    elif (
        white_increase > review_white
        or black_increase > review_black
        or absolute_median_shift > review_median
    ):
        mobile_status = "review"
        mobile_reason = "Exposure changes require inspection in the 1080 mobile proof."
    else:
        mobile_status = "pass"
        mobile_reason = "Automatic clipping and luminance-shift risks remain bounded."

    checks = _semantic_checks()
    checks["zoom_and_thumbnail_coherent"] = _check(
        "zoom_and_thumbnail_coherent",
        multiscale_status,
        "automatic_risk_gate_plus_manual_review",
        multiscale_reason,
        {
            "luma_correlation": luma_correlation,
            "thumbnail_luma_correlation": thumbnail_correlation,
            "source_sharpness": original_sharpness,
            "candidate_sharpness": candidate_sharpness,
            "sharpness_ratio": sharpness_ratio,
            "sharpness_evaluable": sharpness_evaluable,
        },
        {
            "review_luma_correlation_below": review_corr,
            "reject_luma_correlation_below": reject_corr,
            "review_thumbnail_correlation_below": review_thumb,
            "reject_thumbnail_correlation_below": reject_thumb,
            "review_sharpness_ratio_range": [review_sharp_low, review_sharp_high],
            "reject_sharpness_ratio_range": [reject_sharp_low, reject_sharp_high],
        },
    )
    checks["change_not_excessive_vs_original"] = _check(
        "change_not_excessive_vs_original",
        change_status,
        "automatic_risk_gate_plus_manual_review",
        change_reason,
        {"rgb_mae": rgb_mae, "luma_mae": luma_mae},
        {
            "review_rgb_mae_above": review_rgb,
            "reject_rgb_mae_above": reject_rgb,
            "review_luma_mae_above": review_luma,
            "reject_luma_mae_above": reject_luma,
        },
    )
    checks["phone_viewing_comfortable"] = _check(
        "phone_viewing_comfortable",
        mobile_status,
        "automatic_exposure_risk_plus_manual_review",
        mobile_reason,
        {
            "original_white_clip_fraction": original_white,
            "candidate_white_clip_fraction": candidate_white,
            "white_clip_increase": white_increase,
            "original_black_clip_fraction": original_black,
            "candidate_black_clip_fraction": candidate_black,
            "black_clip_increase": black_increase,
            "median_luma_shift": median_shift,
        },
        {
            "review_white_clip_increase_above": review_white,
            "reject_white_clip_increase_above": reject_white,
            "review_black_clip_increase_above": review_black,
            "reject_black_clip_increase_above": reject_black,
            "review_absolute_median_luma_shift_above": review_median,
            "reject_absolute_median_luma_shift_above": reject_median,
        },
    )

    normalized_manual, manual_complete = _normalize_manual_review(
        manual_review,
        not_applicable_requires_note=bool(
            gate_values.get("not_applicable_requires_note", True)
        ),
    )
    for check_id, manual in normalized_manual.items():
        check = checks[check_id]
        manual_status = manual["status"]
        check["manual_status"] = manual_status
        if manual.get("note") is not None:
            check["manual_note"] = manual["note"]
        if manual_status == "fail":
            check["status"] = "reject"
            check["reason"] = "Manual review rejected this check."
        elif check["automatic_status"] == "reject":
            check["status"] = "reject"
        else:
            check["status"] = manual_status
            check["reason"] = "Resolved by manual review."

    hard_rejects: list[str] = []
    if not ratio_ok:
        hard_rejects.append("aspect_ratio")
    if not resolution_ok:
        hard_rejects.append("minimum_short_edge")
    hard_rejects.extend(
        check_id for check_id in CHECK_IDS if checks[check_id]["status"] == "reject"
    )
    review_flags = [
        check_id for check_id in CHECK_IDS if checks[check_id]["status"] == "review"
    ]
    not_evaluable = [
        check_id
        for check_id in CHECK_IDS
        if checks[check_id]["status"] == "not_evaluable"
    ]

    manual_accepts_all = manual_complete and all(
        normalized_manual[check_id]["status"] in {"pass", "not_applicable"}
        for check_id in CHECK_IDS
    )
    if hard_rejects:
        decision = "reject"
    elif preflight_ok and manual_accepts_all and proof["verified"]:
        decision = "accept"
    else:
        decision = "visual_review_required"

    automatic_reject = (not preflight_ok) or any(
        checks[check_id]["automatic_status"] == "reject" for check_id in CHECK_IDS
    )
    automatic_review = any(
        checks[check_id]["automatic_status"] in {"review", "not_evaluable"}
        for check_id in CHECK_IDS
    )
    automated_precheck = (
        "reject" if automatic_reject else "pass_with_review" if automatic_review else "pass"
    )

    result = {
        "schema_version": 6,
        "checker": "portrait-retouch-audit-v6",
        "mode": mode,
        "inputs": {
            "original": {
                "path": str(original),
                "width": original_width,
                "height": original_height,
                "sha256": original_hash,
            },
            "candidate": {
                "path": str(candidate),
                "width": candidate_width,
                "height": candidate_height,
                "sha256": candidate_hash,
            },
        },
        "preflight": {
            "readable": True,
            "status": "pass" if preflight_ok else "reject",
            "relative_aspect_ratio_delta": round(ratio_delta, 8),
            "maximum_aspect_ratio_delta": maximum_ratio_delta,
            "ratio_gate": ratio_ok,
            "candidate_short_edge": min(candidate_width, candidate_height),
            "minimum_short_edge": minimum_short_edge,
            "resolution_gate": resolution_ok,
        },
        "proof": proof,
        "checks": {check_id: checks[check_id] for check_id in CHECK_IDS},
        "summary": {
            "automated_precheck": automated_precheck,
            "manual_review_complete": manual_complete,
            "proof_verified": proof["verified"],
            "hard_rejects": hard_rejects,
            "review_flags": review_flags,
            "not_evaluable": not_evaluable,
            "decision": decision,
            "may_auto_accept": False,
        },
        "decision": decision,
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a conservative ten-check portrait-retouch audit."
    )
    parser.add_argument("original", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument(
        "--mode",
        choices=("natural", "fresh", "warm-film"),
        default="natural",
    )
    parser.add_argument(
        "--manual-review",
        help="Inline JSON or a path to JSON containing manual statuses for the ten checks.",
    )
    parser.add_argument(
        "--proof-manifest",
        required=True,
        type=Path,
        help="Path to the manifest.json generated by render_proof.py.",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gates", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        gates = load_gates(args.gates)
        manual = load_manual_review(args.manual_review)
        result = audit_images(
            args.original,
            args.candidate,
            mode=args.mode,
            manual_review=manual,
            proof_manifest=args.proof_manifest,
            gates=gates,
        )
        _write_private_json(args.output, result)
    except (AuditConfigError, OSError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    if result["decision"] == "accept":
        return 0
    if result["decision"] == "reject":
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
