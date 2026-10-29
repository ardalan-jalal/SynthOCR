import hashlib
import json
import shutil
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest
from PIL import Image

from synthocr.cli import main, parser
from synthocr.fonts import load_profile
from synthocr.preview import preview
from synthocr.text import required_codepoints

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_reference_keeps_original_bytes_and_loads_outside_checkout(tmp_path, monkeypatch):
    reference = files("synthocr").joinpath("kurdish_characters.json").read_bytes()
    assert hashlib.sha256(reference).hexdigest() == (
        "add629e50aa72be491979d18367c05db81e6b70ffd29922a570dc2b5d2571ff7"
    )
    monkeypatch.chdir(tmp_path)
    characters = load_profile("kurdish")
    assert len(characters) == 88
    assert len(required_codepoints(characters)) == 86
    assert set("ەیێڕڵ٠١٢٣٤٥٦٧٨٩0123456789") <= set(characters)
    with pytest.raises(ValueError, match="Unknown font profile"):
        load_profile("missing")


def test_profile_cli_reports_missing_glyphs_and_enforces_explicit_audit(tmp_path, capsys):
    config = tmp_path / "config.toml"
    directory = json.dumps(str(ROOT / "fonts/open/notosans"))
    config.write_text(f"[fonts]\ndirectories = [{directory}]\n")
    args = ["--config", str(config), "fonts", "check", "--profile", "kurdish"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["profile"] == "kurdish"
    assert report["characters"] == 86
    assert report["fonts"] == 1
    assert "U+06D5" in report["results"][0]["missing"]
    assert main([*args, "--require-all"]) == 1
    assert json.loads(capsys.readouterr().out) == report


def test_font_character_sources_cannot_silently_override_each_other():
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["fonts", "check", "--profile", "kurdish", "--text", "Hello"])
    assert error.value.code == 2


def test_preview_is_reproducible_and_refuses_to_overwrite(config):
    first, second = config.root / "a.png", config.root / "b.png"
    result = preview(config, first)
    assert result["samples"] == 12
    assert len(set(result["languages"])) == 12
    with Image.open(first) as image:
        image.load()
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (1600, 1260)
        assert image.getextrema()[0][0] < image.getextrema()[0][1]
    original = first.read_bytes()
    preview(config, second)
    assert original == second.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        preview(config, first)
    assert first.read_bytes() == original


def test_preview_requires_licensed_fonts_and_complete_sample_coverage(config):
    directory = config.root / "fonts"
    directory.mkdir()
    shutil.copyfile(ROOT / "fonts/open/notosans/NotoSans[wdth,wght].ttf", directory / "font.ttf")
    limited = replace(config, fonts=replace(config.fonts, directories=[str(directory)]))
    destination = config.root / "preview.png"
    with pytest.raises(ValueError, match="OFL.txt"):
        preview(limited, destination)
    shutil.copyfile(ROOT / "fonts/open/notosans/OFL.txt", directory / "OFL.txt")
    with pytest.raises(ValueError, match="No licensed font covers"):
        preview(limited, destination)
    assert not destination.exists()
