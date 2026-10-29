"""Visible, reproducible augmentation controls across scripts and output formats."""

import io
import json
import random
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageColor

from synthocr.config import Generation
from synthocr.fonts import Font
from synthocr.pipeline import generate
from synthocr.render import Renderer
from synthocr.verify import verify

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = [
    ("notonaskharabic/NotoNaskhArabic[wght].ttf", "سڵاو جیهان؛ ڕێگای هەولێر ١٢٣", "ckb"),
    ("notosans/NotoSans[wdth,wght].ttf", "Çav baş! Êdî ji Îranê 123", "kmr"),
]


def settings(**changes):
    return replace(
        Generation(
            augmentation=1,
            color_mode="rgb",
            background_color="#eacb9d",
            background_color_end="#a9cae9",
            text_color="#253052",
            padding=20,
        ),
        **changes,
    )


def render(generation, sample=SAMPLES[0], seed=17):
    filename, text, language = sample
    face = Font(str(ROOT / "fonts/open" / filename))
    return Renderer(generation, 2).render(text, face, seed, language)


@pytest.mark.parametrize("background", ["plain", "paper", "gradient", "noise"])
@pytest.mark.parametrize("color_mode", ["grayscale", "rgb"])
@pytest.mark.parametrize("sample", SAMPLES, ids=["sorani", "kurmanji"])
def test_styles_reproduce_pixels_and_preserve_shaping_metadata(background, color_mode, sample):
    generation = settings(background=background, color_mode=color_mode)
    image, metadata = render(generation, sample)
    repeated, other = render(generation, sample)
    try:
        assert metadata == other
        assert ImageChops.difference(image, repeated).getbbox() is None
        assert image.mode == ("RGB" if color_mode == "rgb" else "L")
        assert metadata["direction"] == ("rtl" if sample[2] == "ckb" else "ltr")
        assert metadata["language"] == sample[2]
        augmentation = metadata["augmentation"]
        assert augmentation["background"] == background
        assert augmentation["color_mode"] == color_mode
        assert -generation.rotation <= augmentation["rotation"] <= generation.rotation
        assert 0 <= augmentation["blur"] <= generation.blur
        assert 1 - generation.contrast <= augmentation["contrast"] <= 1 + generation.contrast
        left, top, right, bottom = metadata["bbox"]
        assert 0 < left < right < image.width
        assert 0 < top < bottom < image.height
        if background == "gradient":
            assert augmentation["background_color_end"] == generation.background_color_end
    finally:
        image.close()
        repeated.close()


@pytest.mark.parametrize("seed", [0, 17, 42])
def test_changing_background_keeps_geometry_blur_and_contrast_matched(seed):
    effects = []
    for background in ("plain", "paper", "gradient", "noise"):
        image, metadata = render(settings(background=background), seed=seed)
        try:
            effects.append(
                {
                    "dimensions": image.size,
                    "bbox": metadata["bbox"],
                    **{
                        key: metadata["augmentation"][key]
                        for key in ("rotation", "blur", "contrast")
                    },
                }
            )
        finally:
            image.close()
    assert all(effect == effects[0] for effect in effects[1:])


def test_plain_color_controls_produce_the_selected_colors_without_distortion():
    generation = settings(background="plain", rotation=0, blur=0, contrast=0)
    image, metadata = render(generation)
    try:
        assert image.getpixel((0, 0)) == ImageColor.getrgb(generation.background_color)
        assert ImageColor.getrgb(generation.text_color) in {
            color for _, color in image.getcolors(maxcolors=256)
        }
        assert metadata["augmentation"]["rotation"] == 0
        assert metadata["augmentation"]["blur"] == 0
        assert metadata["augmentation"]["contrast"] == 1
        assert metadata["augmentation"]["texture_strength"] == 0
    finally:
        image.close()


def test_gradient_reaches_both_selected_colors_over_the_expanded_canvas():
    generation = settings(background="gradient", rotation=8, blur=0, contrast=0)
    image, _ = render(generation)
    try:
        for position, value in (
            ((0, 0), generation.background_color),
            ((image.width - 1, image.height - 1), generation.background_color_end),
        ):
            assert all(
                abs(actual - expected) <= 2
                for actual, expected in zip(
                    image.getpixel(position), ImageColor.getrgb(value), strict=True
                )
            )
    finally:
        image.close()


@pytest.mark.parametrize("background", ["paper", "noise"])
def test_zero_texture_removes_background_noise(background):
    generation = settings(background=background, texture_strength=0, rotation=0, blur=0, contrast=0)
    image, _ = render(generation)
    plain, _ = render(replace(generation, background="plain"))
    try:
        assert ImageChops.difference(image, plain).getbbox() is None
    finally:
        image.close()
        plain.close()


@pytest.mark.parametrize("background", ["paper", "noise"])
def test_texture_uses_a_bounded_random_buffer(background, monkeypatch):
    sizes = []
    original = random.Random.randbytes

    def record(self, size):
        sizes.append(size)
        return original(self, size)

    monkeypatch.setattr(random.Random, "randbytes", record)
    image, _ = render(settings(background=background), (SAMPLES[1][0], "Çav baş! " * 100, "kmr"))
    image.close()
    assert sizes and max(sizes) <= 256 * 256
    if background == "noise":
        assert len(sizes) > 1


def test_noise_does_not_stretch_grain_into_blocks_on_wide_lines():
    generation = settings(background="noise", rotation=0, blur=0, contrast=0)
    image, _ = render(generation, (SAMPLES[1][0], "Çav baş! " * 100, "kmr"))
    try:
        assert len({image.getpixel((x, 0)) for x in range(50)}) > 5
    finally:
        image.close()


@pytest.mark.parametrize("background", ["plain", "paper", "gradient", "noise"])
@pytest.mark.parametrize("sample", SAMPLES, ids=["sorani", "kurmanji"])
@pytest.mark.parametrize("seed", [0, 3, 5])
def test_strong_blur_and_rotation_keep_ink_inside_the_canvas(background, sample, seed):
    image, metadata = render(
        settings(background=background, padding=0, rotation=10, blur=2), sample, seed
    )
    try:
        left, top, right, bottom = metadata["bbox"]
        assert 0 < left < right < image.width
        assert 0 < top < bottom < image.height
        # Colored backgrounds must fill rotated corners, rather than exposing white.
        assert image.getpixel((0, 0)) != (255, 255, 255)
    finally:
        image.close()


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "TIFF"])
def test_rgb_augmentation_can_be_encoded_in_every_supported_format(format):
    image, _ = render(settings(background="gradient"))
    try:
        with io.BytesIO() as stream:
            image.save(stream, format=format)
            stream.seek(0)
            with Image.open(stream) as decoded:
                decoded.load()
                assert decoded.mode == "RGB"
                assert decoded.size == image.size
    finally:
        image.close()


def test_disabled_augmentation_still_produces_clean_black_and_white():
    image, metadata = render(settings(augmentation=0, background="gradient", text_color="#eacb9d"))
    try:
        assert image.mode == "L"
        assert image.getpixel((0, 0)) == 255
        assert image.getextrema() == (0, 255)
        assert metadata["augmentation"] == {}
    finally:
        image.close()


@pytest.mark.parametrize("color_mode", ["grayscale", "rgb"])
def test_direct_renderer_rejects_unreadable_base_colors(color_mode):
    with pytest.raises(ValueError, match="3:1 base contrast"):
        Renderer(settings(color_mode=color_mode, text_color="#eacb9d"))


@pytest.mark.parametrize("sample", SAMPLES, ids=["sorani", "kurmanji"])
def test_maximum_rotation_and_blur_fit_tight_bounds_without_clipping(sample, monkeypatch):
    monkeypatch.setattr(random.Random, "uniform", lambda self, start, end: end)
    generation = settings(
        background="paper", rotation=10, blur=2, padding=0, max_width=500, max_height=120
    )
    image, metadata = render(generation, sample)
    try:
        assert metadata["augmentation"]["rotation"] == 10
        assert metadata["augmentation"]["blur"] == 2
        left, top, right, bottom = metadata["bbox"]
        assert 0 < left < right < image.width <= generation.max_width
        assert 0 < top < bottom < image.height <= generation.max_height
    finally:
        image.close()


@pytest.mark.parametrize("format", ["png", "jpg", "webp", "tif"])
def test_rgb_datasets_verify_and_reproduce_across_worker_counts(config, format):
    serial = replace(
        config,
        generation=replace(
            config.generation,
            augmentation=1,
            color_mode="rgb",
            background="gradient",
            rotation=10,
            blur=2,
            format=format,
        ),
    )
    parallel = replace(serial, generation=replace(serial.generation, output="parallel", workers=2))
    manifests = []
    for current in (serial, parallel):
        assert generate(current)["generated"] == 4
        output = current.path(current.generation.output)
        assert verify(output)["records"] == 4
        with (output / "manifest.jsonl").open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream]
        for record in records:
            with Image.open(output / record["image"]) as image:
                assert image.mode == "RGB"
                image.load()
            assert record["augmentation"]["background"] == "gradient"
            assert record["augmentation"]["color_mode"] == "rgb"
        manifests.append(records)
    assert manifests[0] == manifests[1]
