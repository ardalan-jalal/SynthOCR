import json
from pathlib import Path

import pytest

from synthocr.config import Config, Data, Fonts, Generation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/sample.jsonl").write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in [
                {"text": "Hello world", "genre": "prose", "language": "en"},
                {"text": "سڵاو جیهان", "genre": "prose", "language": "ckb"},
                {"text": "١٢٣٤ 5678", "genre": "number"},
                {"text": "A quiet morning", "language": "en"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return Config(
        root=tmp_path,
        data=Data(raw="raw", processed="processed", max_chars=30, write_jsonl=True),
        fonts=Fonts(directories=[str(ROOT / "fonts")]),
        generation=Generation(output="output", samples=4, seed=17, augmentation=0.5),
    ).validate()
