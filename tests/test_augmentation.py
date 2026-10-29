import io
from pathlib import Path

import pytest
from PIL import Image

from synthocr.config import Generation
from synthocr.fonts import Font
from synthocr.render import Renderer

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("text", ["Paper edges", "سڵاو جیهان"])
def test_rotated_paper_covers_corners_without_a_white_frame(seed, text):
    # A clear margin above the ink gives an interior paper reference. The same
    # paper must continue into all four expanded corners, within texture noise.
    font = Font(str(ROOT / "fonts/open/notonaskharabic/NotoNaskhArabic[wght].ttf"))
    image, metadata = Renderer(
        Generation(augmentation=1, padding=24, max_width=600, max_height=180), 2
    ).render(text, font, seed)
    try:
        assert metadata["augmentation"]["rotation"] != 0
        background = image.getpixel((image.width // 2, metadata["bbox"][1] - 4))
        for corner in (
            (0, 0),
            (image.width - 1, 0),
            (0, image.height - 1),
            (image.width - 1, image.height - 1),
        ):
            assert abs(image.getpixel(corner) - background) <= 14
    finally:
        image.close()


@pytest.mark.parametrize("padding", [0, 1])
@pytest.mark.parametrize("seed", [0, 3, 5])
@pytest.mark.parametrize(
    "filename,text",
    [
        ("notonaskharabic/NotoNaskhArabic[wght].ttf", "سڵاو جیهان ١٢٣ ۱۲۳"),
        ("notonastaliqurdu/NotoNastaliqUrdu[wght].ttf", "کتب خانہ سب کے لیے کھلا ہے"),
        ("notosans/NotoSans[wdth,wght].ttf", "Paper edges jÁy"),
        ("notosansdevanagari/NotoSansDevanagari[wdth,wght].ttf", "नमस्ते दुनिया"),
    ],
)
def test_small_padding_keeps_a_clear_margin_around_augmented_ink(filename, text, seed, padding):
    face = Font(str(ROOT / "fonts/open" / filename))
    image, metadata = Renderer(Generation(augmentation=1, padding=padding), 2).render(
        text, face, seed
    )
    try:
        left, top, right, bottom = metadata["bbox"]
        assert 0 < left < right < image.width
        assert 0 < top < bottom < image.height
    finally:
        image.close()


def test_too_small_augmented_canvas_fails_before_allocating_or_clipping():
    face = Font(str(ROOT / "fonts/open/notonaskharabic/NotoNaskhArabic[wght].ttf"))
    settings = Generation(augmentation=1, padding=0, max_width=8, max_height=8, max_pixels=64)
    with pytest.raises(ValueError, match="safety margin"):
        Renderer(settings, 2).render("ب", face, 0)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "TIFF"])
def test_encoded_augmented_background_has_no_white_corners(format):
    face = Font(str(ROOT / "fonts/open/notonaskharabic/NotoNaskhArabic[wght].ttf"))
    image, metadata = Renderer(Generation(augmentation=1, padding=24), 2).render(
        "سڵاو جیهان", face, 1, "ckb"
    )
    try:
        with io.BytesIO() as stream:
            options = {"quality": 95} if format == "JPEG" else {}
            image.save(stream, format=format, **options)
            stream.seek(0)
            with Image.open(stream) as decoded, decoded.convert("L") as gray:
                gray.load()
                assert gray.size == image.size
                reference = gray.getpixel((gray.width // 2, metadata["bbox"][1] - 4))
                for corner in (
                    (0, 0),
                    (gray.width - 1, 0),
                    (0, gray.height - 1),
                    (gray.width - 1, gray.height - 1),
                ):
                    assert abs(gray.getpixel(corner) - reference) <= 18
    finally:
        image.close()
