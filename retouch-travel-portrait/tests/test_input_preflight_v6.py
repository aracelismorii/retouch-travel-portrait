from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageCms


SKILL_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = SKILL_ROOT / "scripts" / "input_preflight.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load_module("input_preflight", PREFLIGHT_PATH)


def save_rgb(
    path: Path,
    *,
    size: tuple[int, int] = (768, 900),
    image_format: str | None = None,
    orientation: int | None = None,
    icc_profile: bytes | None = None,
) -> None:
    image = Image.new("RGB", size, (82, 104, 126))
    save_options: dict[str, object] = {}
    if orientation is not None:
        exif = image.getexif()
        exif[274] = orientation
        save_options["exif"] = exif
    if icc_profile is not None:
        save_options["icc_profile"] = icc_profile
    image.save(path, format=image_format, **save_options)


class InputPreflightV6Tests(unittest.TestCase):
    def assert_rejected(self, path: Path, code: str) -> preflight.PreflightError:
        with self.assertRaises(preflight.PreflightError) as raised:
            preflight.preflight_image(path)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_valid_jpeg_returns_private_structured_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "portrait.JPEG"
            save_rgb(path)
            original = path.read_bytes()

            result = preflight.preflight_image(path)

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["decision"], "accept")
            self.assertEqual(result["source"]["name"], "portrait.JPEG")
            self.assertNotIn(str(path.parent), json.dumps(result))
            self.assertEqual(result["source"]["format"], "JPEG")
            self.assertEqual(result["source"]["mode"], "RGB")
            self.assertEqual(result["source"]["frame_count"], 1)
            self.assertEqual(
                result["source"]["icc"],
                {
                    "present": False,
                    "description": None,
                    "color_space_signature": None,
                    "requires_srgb_conversion": False,
                    "assumption": "untagged image is treated as sRGB",
                },
            )
            self.assertEqual(
                result["source"]["sha256"], hashlib.sha256(original).hexdigest()
            )
            self.assertEqual(path.read_bytes(), original)

    def test_valid_png_grayscale_and_webp_rgb_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grayscale = root / "grayscale.png"
            webp = root / "portrait.webp"
            Image.new("L", (768, 800), 91).save(grayscale)
            save_rgb(webp)

            gray_result = preflight.preflight_image(grayscale)
            webp_result = preflight.preflight_image(webp)

            self.assertEqual(gray_result["source"]["mode"], "L")
            self.assertEqual(gray_result["source"]["format"], "PNG")
            self.assertEqual(webp_result["source"]["format"], "WEBP")

    def test_exif_rotations_use_display_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orientation_three = root / "three.jpg"
            orientation_six = root / "six.jpg"
            orientation_eight = root / "eight.jpg"
            save_rgb(orientation_three, size=(768, 900), orientation=3)
            save_rgb(orientation_six, size=(900, 768), orientation=6)
            save_rgb(orientation_eight, size=(900, 768), orientation=8)

            three = preflight.preflight_image(orientation_three)["source"]
            six = preflight.preflight_image(orientation_six)["source"]
            eight = preflight.preflight_image(orientation_eight)["source"]

            self.assertEqual(three["encoded_dimensions"], {"width": 768, "height": 900})
            self.assertEqual(three["display_dimensions"], {"width": 768, "height": 900})
            self.assertEqual(six["encoded_dimensions"], {"width": 900, "height": 768})
            self.assertEqual(six["display_dimensions"], {"width": 768, "height": 900})
            self.assertEqual(eight["display_dimensions"], {"width": 768, "height": 900})

    def test_mirrored_exif_orientations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for orientation in (2, 4, 5, 7):
                with self.subTest(orientation=orientation):
                    path = root / f"mirror-{orientation}.jpg"
                    save_rgb(path, orientation=orientation)
                    error = self.assert_rejected(path, "mirrored_exif_orientation")
                    self.assertEqual(error.details["exif_orientation"], orientation)

    def test_invalid_exif_orientation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid-orientation.jpg"
            save_rgb(path, orientation=9)
            self.assert_rejected(path, "unsupported_exif_orientation")

    def test_extension_and_encoded_format_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disguised = root / "disguised.jpg"
            unsupported = root / "portrait.bmp"
            save_rgb(disguised, image_format="PNG")
            save_rgb(unsupported)

            error = self.assert_rejected(disguised, "format_extension_mismatch")
            self.assertEqual(error.details["actual_format"], "PNG")
            self.assert_rejected(unsupported, "unsupported_extension")

    def test_missing_and_non_image_sources_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.jpg"
            text = root / "fake.png"
            text.write_text("not an image", encoding="utf-8")
            self.assert_rejected(missing, "source_not_found")
            self.assert_rejected(text, "image_open_failed")

    def test_truncated_image_must_fully_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "truncated.jpg"
            save_rgb(path)
            encoded = path.read_bytes()
            path.write_bytes(encoded[: len(encoded) // 2])
            self.assert_rejected(path, "image_decode_failed")

    def test_display_short_edge_must_be_at_least_768(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "small.jpg"
            save_rgb(path, size=(767, 1400))
            error = self.assert_rejected(path, "resolution_too_small")
            self.assertEqual(error.details["display_short_edge"], 767)

    def test_pixel_limit_is_checked_before_raster_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "too-many-pixels.jpg"
            save_rgb(path)
            with mock.patch.object(preflight, "MAX_PIXELS", 600_000):
                error = self.assert_rejected(path, "pixel_limit_exceeded")
            self.assertEqual(error.details["pixel_count"], 768 * 900)
            self.assertEqual(error.details["max_pixels"], 600_000)

    def test_pillow_decompression_bomb_is_a_stable_preflight_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pillow-bomb.jpg"
            save_rgb(path)
            with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 100_000):
                self.assert_rejected(path, "pixel_limit_exceeded")

    def test_animation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "animated.webp"
            first = Image.new("RGB", (768, 800), (10, 20, 30))
            second = Image.new("RGB", (768, 800), (30, 20, 10))
            first.save(path, save_all=True, append_images=[second], duration=100, loop=0)
            error = self.assert_rejected(path, "multiple_frames_not_supported")
            self.assertEqual(error.details["frame_count"], 2)

    def test_alpha_and_transparency_metadata_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rgba = root / "alpha.png"
            transparency = root / "transparency.png"
            Image.new("RGBA", (768, 800), (1, 2, 3, 240)).save(rgba)
            Image.new("RGB", (768, 800), (1, 2, 3)).save(
                transparency, transparency=(1, 2, 3)
            )
            self.assert_rejected(rgba, "transparency_not_supported")
            self.assert_rejected(transparency, "transparency_not_supported")

    def test_cmyk_16bit_and_palette_modes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cmyk = root / "cmyk.jpg"
            sixteen_bit = root / "sixteen-bit.png"
            palette = root / "palette.png"
            Image.new("CMYK", (768, 800), (1, 2, 3, 4)).save(cmyk)
            Image.new("I;16", (768, 800), 1024).save(sixteen_bit)
            Image.new("P", (768, 800), 3).save(palette)
            for path in (cmyk, sixteen_bit, palette):
                with self.subTest(mode=path.stem):
                    self.assert_rejected(path, "unsupported_pixel_mode")

    def test_srgb_icc_is_recorded_without_conversion_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "srgb.jpg"
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            save_rgb(path, icc_profile=profile.tobytes())

            icc = preflight.preflight_image(path)["source"]["icc"]

            self.assertTrue(icc["present"])
            self.assertIn("sRGB", icc["description"])
            self.assertEqual(icc["color_space_signature"], "RGB")
            self.assertFalse(icc["requires_srgb_conversion"])

    def test_profile_incompatible_with_rgb_pixels_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lab-profile.jpg"
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
            save_rgb(path, icc_profile=profile.tobytes())

            self.assert_rejected(path, "incompatible_icc_profile")

    def test_malformed_icc_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad-icc.jpg"
            save_rgb(path, icc_profile=b"not-an-icc-profile")
            self.assert_rejected(path, "invalid_icc_profile")

    def test_cli_emits_json_and_uses_exit_two_for_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid.png"
            invalid = root / "invalid.png"
            save_rgb(valid)
            save_rgb(invalid, size=(400, 600))

            accepted = subprocess.run(
                [sys.executable, str(PREFLIGHT_PATH), str(valid)],
                check=False,
                capture_output=True,
                text=True,
            )
            rejected = subprocess.run(
                [sys.executable, str(PREFLIGHT_PATH), str(invalid)],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(accepted.returncode, 0)
            self.assertEqual(json.loads(accepted.stdout)["decision"], "accept")
            self.assertEqual(rejected.returncode, 2)
            rejection = json.loads(rejected.stdout)
            self.assertEqual(rejection["decision"], "reject")
            self.assertEqual(rejection["error"]["code"], "resolution_too_small")
            self.assertEqual(rejection["source"], {"name": "invalid.png"})
            self.assertEqual(rejected.stderr, "")


if __name__ == "__main__":
    unittest.main()
