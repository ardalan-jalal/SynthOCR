"""Appearance validation and real CLI outputs share the rendering configuration."""

import json
from dataclasses import replace

import pytest
from PIL import Image

from synthocr import __version__, cli
from synthocr.config import AUGMENTATION_FIELDS, DOCUMENT_LAYERS, augmentation_contrast
from synthocr.experiments import PRESETS
from synthocr.verify import verify


@pytest.mark.parametrize(
    "field,value",
    [
        ("augmentation", True),
        ("texture_strength", float("nan")),
        ("rotation", float("inf")),
        ("blur", -1),
        ("contrast", 0.51),
        ("rotation", "2"),
        ("background", "photograph"),
        ("color_mode", "CMYK"),
        ("background_color", "#fff"),
        ("background_color_end", "#nothex"),
        ("text_color", None),
    ],
)
def test_invalid_appearance_is_rejected_before_generation(config, field, value):
    with pytest.raises(ValueError, match=field):
        replace(config, generation=replace(config.generation, **{field: value})).validate()


@pytest.mark.parametrize("color_mode", ["grayscale", "rgb"])
def test_gradient_rejects_intermediate_collision_even_with_contrasting_endpoints(
    config, color_mode
):
    settings = replace(
        config.generation,
        augmentation=1,
        color_mode=color_mode,
        background="gradient",
        background_color="#ffffff",
        background_color_end="#000000",
        text_color="#767676",
    )
    assert augmentation_contrast(replace(settings, background="plain")) > 3
    assert (
        augmentation_contrast(replace(settings, background="plain", background_color="#000000")) > 3
    )
    with pytest.raises(ValueError, match="3:1 base contrast"):
        replace(config, generation=settings).validate()
    # Disabled settings may be retained while generating clean images.
    replace(config, generation=replace(settings, augmentation=0)).validate()


@pytest.mark.parametrize("command", ["generate", "kurdish"])
def test_cli_options_reach_images_and_recorded_run(config, monkeypatch, capsys, command):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    original = (config.root / "raw/sample.jsonl").read_bytes()
    values = {
        "augmentation": 1.0,
        "background": "gradient",
        "color_mode": "rgb",
        "background_color": "#f4ddb4",
        "background_color_end": "#bbd3f4",
        "text_color": "#172338",
        "texture_strength": 0.12,
        "rotation": 0.0,
        "blur": 0.0,
        "contrast": 0.0,
    }
    args = [command, "--samples", "1", "--output", "chosen-output"]
    for key, value in values.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    output = config.root / "chosen-output"
    assert result["generated"] == verify(output)["records"] == 1
    run = json.loads((output / "run.json").read_text())
    assert run["version"] == __version__
    expected = {key: getattr(config.generation, key) for key in AUGMENTATION_FIELDS}
    expected.update(values)
    assert {key: run["config"]["generation"][key] for key in AUGMENTATION_FIELDS} == expected
    row = json.loads((output / "manifest.jsonl").read_text())
    assert row["augmentation"]["background"] == "gradient"
    assert row["augmentation"]["color_mode"] == "rgb"
    assert row["augmentation"]["background_color_end"] == values["background_color_end"]
    with Image.open(output / row["image"]) as image:
        image.load()
        assert image.mode == "RGB"
        assert all(
            abs(actual - expected) <= 2
            for actual, expected in zip(image.getpixel((0, 0)), (244, 221, 180), strict=True)
        )
    assert (config.root / "raw/sample.jsonl").read_bytes() == original


def test_experiment_cli_creates_a_portable_bounded_report(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert cli.main(["experiment", "--output", "evidence", "--samples", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    output = config.root / "evidence"
    assert report["output"] == str(output)
    assert (output / "report.json").is_file()
    assert (output / "contact-sheet.png").is_file()
    rows = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
    assert len(rows) == 2 * len(PRESETS)
    assert {row["language"] for row in rows} == {"ckb", "kmr"}
    assert cli.main(["experiment", "--output", "evidence", "--samples", "2"]) == 1


def test_cli_combines_document_types_and_effect_overrides(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    args = [
        "generate",
        "--output",
        "mixed-documents",
        "--samples",
        "8",
        "--augmentation",
        "1",
        "--background-preset",
        "office-scan",
        "--background-preset",
        "notebook",
        "--no-preset-colors",
        "--color-mode",
        "rgb",
    ]
    overrides = dict(zip(DOCUMENT_LAYERS, (0.3, 0.15, 0.2, 0, 0.1, 0.25, 0.3, 0.2, 0), strict=True))
    for name, strength in overrides.items():
        args.extend(["--" + name.replace("_", "-"), str(strength)])
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    output = config.root / "mixed-documents"
    assert result["generated"] == verify(output)["records"] == 8
    run = json.loads((output / "run.json").read_text())["config"]["generation"]
    assert run["background_presets"] == ["office-scan", "notebook"]
    assert run["preset_colors"] is False
    with (output / "manifest.jsonl").open() as stream:
        records = [json.loads(line) for line in stream]
    assert {row["augmentation"]["background_preset"] for row in records} == {
        "office-scan",
        "notebook",
    }
    assert all(row["augmentation"]["layers"] == overrides for row in records)
    assert all(row["augmentation"]["color_mode"] == "rgb" for row in records)
