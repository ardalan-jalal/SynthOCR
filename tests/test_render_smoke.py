"""Bounded visual smoke matrix across the repository's usable font faces."""

from pathlib import Path

import pytest
from test_fonts_and_text import ARABIC, SAMPLES

from synthocr.config import Generation
from synthocr.fonts import Coverage, discover
from synthocr.render import Renderer
from synthocr.text import required_codepoints

ROOT = Path(__file__).resolve().parents[1]
FACES = discover([ROOT / "fonts"])
COVERAGE = Coverage(128)


@pytest.mark.parametrize("face", FACES, ids=[face.name for face in FACES])
def test_font_augmentation_smoke_matrix(face):
    family = Path(face.path).parent.name
    default = "سڵاو جیهان ١٢٣ ۱۲۳" if family in ARABIC or family == "fonts" else "Çav baş! jÁy 123"
    candidates = [
        SAMPLES.get(family, default),
        "مرحبا بالعالم ١٢٣",
        "Çav baş! jÁy 123",
        "0123456789",
    ]
    text = next(
        (text for text in candidates if not COVERAGE.missing(face, required_codepoints(text))), None
    )
    assert text is not None, f"Add a script-appropriate smoke probe for {face.name}"
    for padding in (0, 1, 12):
        for size in (12, 48):
            renderer = Renderer(Generation(augmentation=1, padding=padding, font_size=size), 2)
            for seed in (0, 1, 4, 42):
                image, metadata = renderer.render(text, face, seed)
                try:
                    left, top, right, bottom = metadata["bbox"]
                    assert 0 < left < right < image.width, (padding, size, seed)
                    assert 0 < top < bottom < image.height, (padding, size, seed)
                    low, high = image.getextrema()
                    # Paper noise alone spans about 12 gray levels. Require
                    # enough extra contrast to distinguish visible ink.
                    assert high - low > 16, (padding, size, seed)
                finally:
                    image.close()
