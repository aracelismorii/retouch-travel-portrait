#!/usr/bin/env python3
"""Strict V6 input preflight for the travel-portrait retouch workflow.

The preflight is intentionally conservative.  It accepts only source images
whose raster representation can be handled without an implicit mirror,
palette expansion, alpha compositing, frame selection, or untracked color
conversion.  It does not modify the source file.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms, UnidentifiedImageError


SCHEMA_VERSION = 1
CHECKER_NAME = "retouch-travel-portrait-input-preflight-v6"
MIN_SHORT_EDGE = 768
# Covers current high-resolution phone and full-frame camera photographs while
# rejecting unexpectedly huge rasters before their pixel buffers are decoded.
MAX_PIXELS = 80_000_000
SUPPORTED_EXTENSIONS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
SUPPORTED_MODES = {"RGB", "L"}
MIRRORED_ORIENTATIONS = {2, 4, 5, 7}
SUPPORTED_ORIENTATIONS = {1, 3, 6, 8}
SWAPPED_ORIENTATIONS = {6, 8}


class PreflightError(ValueError):
    """A stable, machine-classifiable input rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_profile_text(value: str) -> str:
    """Make an ICC label safe and compact enough for a JSON run record."""

    return " ".join(value.replace("\x00", " ").split())[:256]


def _inspect_icc(raw_profile: object, source_mode: str) -> dict[str, object]:
    if raw_profile is None:
        return {
            "present": False,
            "description": None,
            "color_space_signature": None,
            "requires_srgb_conversion": False,
            "assumption": "untagged image is treated as sRGB",
        }
    if not isinstance(raw_profile, bytes) or not raw_profile:
        raise PreflightError(
            "invalid_icc_profile",
            "embedded ICC profile is empty or is not a byte sequence",
        )
    try:
        profile = ImageCms.ImageCmsProfile(io.BytesIO(raw_profile))
        description = _clean_profile_text(ImageCms.getProfileDescription(profile))
        color_space = _clean_profile_text(profile.profile.xcolor_space).upper()
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise PreflightError(
            "invalid_icc_profile",
            "embedded ICC profile could not be parsed",
        ) from exc

    # Profile descriptions are the portable signal exposed by Pillow/lcms for
    # distinguishing standard sRGB from RGB working spaces such as Display P3
    # or Adobe RGB.  Unknown but parseable profiles are conservatively routed
    # through an explicit sRGB conversion step by the caller.
    normalized_description = description.casefold().replace("-", "")
    is_srgb = color_space == "RGB" and "srgb" in normalized_description
    try:
        ImageCms.buildTransformFromOpenProfiles(
            profile,
            ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")),
            source_mode,
            "RGB",
        )
    except (OSError, TypeError, ValueError, ImageCms.PyCMSError) as exc:
        raise PreflightError(
            "incompatible_icc_profile",
            "embedded ICC profile is incompatible with the source pixel mode",
            details={"mode": source_mode, "profile_color_space": color_space or None},
        ) from exc
    return {
        "present": True,
        "description": description or "unnamed ICC profile",
        "color_space_signature": color_space or None,
        "requires_srgb_conversion": not is_srgb,
        "assumption": None,
    }


def _reject_if_transparent(image: Image.Image) -> None:
    if "A" in image.getbands() or "transparency" in image.info:
        raise PreflightError(
            "transparency_not_supported",
            "alpha channels and transparency metadata are not supported by V6 MVP",
            details={"mode": image.mode},
        )


def preflight_image(path: Path | str) -> dict[str, object]:
    """Validate one immutable source and return JSON-serializable metadata.

    Raises:
        PreflightError: if the source is outside the V6 MVP input contract.
    """

    source = Path(path)
    suffix = source.suffix.lower()
    expected_format = SUPPORTED_EXTENSIONS.get(suffix)
    if expected_format is None:
        raise PreflightError(
            "unsupported_extension",
            "only .jpg, .jpeg, .png, and .webp source files are supported",
            details={"suffix": suffix or None},
        )
    if not source.is_file():
        raise PreflightError("source_not_found", "source image is not a regular file")

    try:
        with warnings.catch_warnings():
            # Our explicit limit is lower than Pillow's default warning and
            # keeps the classification stable if Pillow changes that default.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            image_context = Image.open(source)
    except Image.DecompressionBombError as exc:
        raise PreflightError(
            "pixel_limit_exceeded",
            "source raster exceeds Pillow's decompression safety limit",
            details={"max_pixels": MAX_PIXELS},
        ) from exc
    except (OSError, UnidentifiedImageError) as exc:
        raise PreflightError(
            "image_open_failed", "source is not a readable supported image"
        ) from exc

    try:
        with image_context as image:
            actual_format = (image.format or "").upper()
            if actual_format != expected_format:
                raise PreflightError(
                    "format_extension_mismatch",
                    "encoded image format does not match the filename extension",
                    details={
                        "suffix": suffix,
                        "expected_format": expected_format,
                        "actual_format": actual_format or None,
                    },
                )

            width, height = image.size
            if width <= 0 or height <= 0:
                raise PreflightError(
                    "invalid_dimensions", "encoded dimensions must be positive"
                )
            pixel_count = width * height
            if pixel_count > MAX_PIXELS:
                raise PreflightError(
                    "pixel_limit_exceeded",
                    "source raster exceeds the V6 decompression safety limit",
                    details={"pixel_count": pixel_count, "max_pixels": MAX_PIXELS},
                )

            try:
                frame_count = int(getattr(image, "n_frames", 1))
            except (OSError, ValueError) as exc:
                raise PreflightError(
                    "frame_count_failed", "source frame count could not be read"
                ) from exc
            if frame_count != 1:
                raise PreflightError(
                    "multiple_frames_not_supported",
                    "animated and multi-frame images are not supported by V6 MVP",
                    details={"frame_count": frame_count},
                )

            _reject_if_transparent(image)
            if image.mode not in SUPPORTED_MODES:
                raise PreflightError(
                    "unsupported_pixel_mode",
                    "V6 MVP accepts only 8-bit RGB or 8-bit grayscale source pixels",
                    details={"mode": image.mode},
                )
            source_mode = image.mode

            try:
                orientation_value = image.getexif().get(274, 1)
                orientation = int(orientation_value)
            except (OSError, TypeError, ValueError, SyntaxError) as exc:
                raise PreflightError(
                    "invalid_exif_orientation",
                    "EXIF orientation could not be interpreted safely",
                ) from exc
            if orientation in MIRRORED_ORIENTATIONS:
                raise PreflightError(
                    "mirrored_exif_orientation",
                    "mirrored EXIF orientations require an identity-affecting flip",
                    details={"exif_orientation": orientation},
                )
            if orientation not in SUPPORTED_ORIENTATIONS:
                raise PreflightError(
                    "unsupported_exif_orientation",
                    "EXIF orientation must be 1, 3, 6, or 8",
                    details={"exif_orientation": orientation},
                )

            if orientation in SWAPPED_ORIENTATIONS:
                display_width, display_height = height, width
            else:
                display_width, display_height = width, height
            if min(display_width, display_height) < MIN_SHORT_EDGE:
                raise PreflightError(
                    "resolution_too_small",
                    "display short edge is below the V6 minimum",
                    details={
                        "display_short_edge": min(display_width, display_height),
                        "min_short_edge": MIN_SHORT_EDGE,
                    },
                )

            icc = _inspect_icc(image.info.get("icc_profile"), source_mode)

            # load() forces raster decoding now instead of allowing a corrupt
            # source to fail halfway through a later retouch stage.
            try:
                image.load()
            except (OSError, ValueError, SyntaxError, EOFError) as exc:
                raise PreflightError(
                    "image_decode_failed", "source raster could not be fully decoded"
                ) from exc
    except PreflightError:
        raise
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError, EOFError) as exc:
        raise PreflightError(
            "image_decode_failed", "source raster or metadata could not be fully decoded"
        ) from exc

    try:
        source_hash = _sha256(source)
    except OSError as exc:
        raise PreflightError(
            "source_read_failed", "source could not be read after raster validation"
        ) from exc

    return {
        "schema_version": SCHEMA_VERSION,
        "checker": CHECKER_NAME,
        "decision": "accept",
        "source": {
            "name": source.name,
            "sha256": source_hash,
            "suffix": suffix,
            "format": actual_format,
            "mode": source_mode,
            "frame_count": frame_count,
            "exif_orientation": orientation,
            "encoded_dimensions": {"width": width, "height": height},
            "display_dimensions": {
                "width": display_width,
                "height": display_height,
            },
            "pixel_count": pixel_count,
            "icc": icc,
        },
        "limits": {
            "min_short_edge": MIN_SHORT_EDGE,
            "max_pixels": MAX_PIXELS,
            "supported_modes": sorted(SUPPORTED_MODES),
        },
    }


def rejection_result(path: Path | str, error: PreflightError) -> dict[str, object]:
    source = Path(path)
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "checker": CHECKER_NAME,
        "decision": "reject",
        "source": {"name": source.name},
        "error": {"code": error.code, "message": error.message},
    }
    if error.details:
        result["error"]["details"] = error.details  # type: ignore[index]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one source against the V6 MVP image input contract."
    )
    parser.add_argument("source", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = preflight_image(args.source)
    except PreflightError as exc:
        result = rejection_result(args.source, exc)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
