#!/usr/bin/env python3
"""Reject portrait candidates whose encoded canvas geometry changed."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

from PIL import Image, UnidentifiedImageError


class GeometryError(ValueError):
    """Raised when image dimensions cannot be read safely."""


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise GeometryError("invalid PNG header")
    return struct.unpack(">II", data[16:24])


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    position = 2
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while position < len(data):
        while position < len(data) and data[position] != 0xFF:
            position += 1
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[position : position + 2])[0]
        if segment_length < 2 or position + segment_length > len(data):
            break
        if marker in start_of_frame:
            if segment_length < 7:
                break
            height, width = struct.unpack(">HH", data[position + 3 : position + 7])
            return width, height
        position += segment_length
    raise GeometryError("JPEG dimensions were not found")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    position = 12
    while position + 8 <= len(data):
        fourcc = data[position : position + 4]
        chunk_size = struct.unpack("<I", data[position + 4 : position + 8])[0]
        payload = data[position + 8 : position + 8 + chunk_size]
        if len(payload) != chunk_size:
            break
        if fourcc == b"VP8X" and len(payload) >= 10:
            width = 1 + int.from_bytes(payload[4:7], "little")
            height = 1 + int.from_bytes(payload[7:10], "little")
            return width, height
        if fourcc == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
            height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
            return width, height
        if fourcc == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            width = 1 + (bits & 0x3FFF)
            height = 1 + ((bits >> 14) & 0x3FFF)
            return width, height
        position += 8 + chunk_size + (chunk_size % 2)
    raise GeometryError("WebP dimensions were not found")


def read_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = _png_dimensions(data)
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _webp_dimensions(data)
    else:
        raise GeometryError("unsupported or unrecognized image format")
    if width <= 0 or height <= 0:
        raise GeometryError("image dimensions must be positive")

    # Geometry gates must compare the canvas a person actually sees. Phone and
    # camera JPEGs commonly store landscape pixels with an EXIF orientation tag
    # that presents them as portraits. Treating the encoded dimensions as the
    # display dimensions makes a correctly normalized candidate look stretched.
    try:
        with Image.open(path) as image:
            orientation = image.getexif().get(274, 1)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise GeometryError(f"image metadata could not be read: {exc}") from exc
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    return width, height


def compare_geometry(original: Path, candidate: Path) -> dict[str, object]:
    original_width, original_height = read_dimensions(original)
    candidate_width, candidate_height = read_dimensions(candidate)
    original_ratio = original_width / original_height
    candidate_ratio = candidate_width / candidate_height
    relative_ratio_delta = abs(candidate_ratio / original_ratio - 1.0)
    exact_match = (
        original_width == candidate_width and original_height == candidate_height
    )
    return {
        "decision": "accept" if exact_match else "reject",
        "reason": (
            "encoded canvas dimensions match exactly"
            if exact_match
            else "candidate dimensions differ from the immutable original"
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
        "relative_aspect_ratio_delta": round(relative_ratio_delta, 8),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reject a retouch candidate if its canvas geometry changed."
    )
    parser.add_argument("original", type=Path)
    parser.add_argument("candidate", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = compare_geometry(args.original, args.candidate)
    except (GeometryError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result["decision"] == "accept" else 2


if __name__ == "__main__":
    raise SystemExit(main())
