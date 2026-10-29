"""Users choose and add fonts; every entry point uses the same selection."""

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from synthocr.cli import parser
from synthocr.fonts import discover, font_ids, select
from synthocr.pipeline import generate
from synthocr.verify import verify

ROOT = Path(__file__).resolve().parents[1]
NOTO_SANS = ROOT / "fonts/open/notosans"
NOTO_NASKH = ROOT / "fonts/open/notonaskharabic"
CAIRO = next((ROOT / "fonts/open/cairo").glob("*.ttf"))


def test_select_by_file_name_face_name_or_id():
    fonts = discover([NOTO_SANS, NOTO_NASKH])
    ids = font_ids(fonts)
    first, second = fonts[0], fonts[-1]
    assert select(fonts, []) == fonts
    assert select(fonts, [Path(first.path).name], ids) == [
        font for font in fonts if font.path == first.path
    ]
    assert select(fonts, [second.name], ids) == [second]
    assert select(fonts, [ids[second], "missing.ttf"], ids) == [second]
    with pytest.raises(ValueError, match="None of the chosen fonts"):
        select(fonts, ["missing.ttf"], ids)


def test_invalid_font_selection_is_rejected(config):
    for include in ("Cairo.ttf", [""], ["x" * 301]):
        with pytest.raises(ValueError, match="fonts.include"):
            replace(config, fonts=replace(config.fonts, include=include)).validate()


def test_generation_uses_only_the_chosen_fonts(config):
    fonts = discover([NOTO_SANS, NOTO_NASKH])
    chosen = next(font for font in fonts if "NotoSans[" in font.name)
    config = replace(
        config,
        fonts=replace(
            config.fonts,
            directories=[str(NOTO_SANS), str(NOTO_NASKH)],
            include=[Path(chosen.path).name],
        ),
        generation=replace(config.generation, languages=["en"], samples=2),
    ).validate()
    generate(config)
    dataset = config.path(config.generation.output)
    verify(dataset)
    run = json.loads((dataset / "run.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in run["font_files"]] == [font_ids([chosen])[chosen]]
    for line in (dataset / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        assert Path(json.loads(line)["font"]).name == Path(chosen.path).name


def test_cli_collects_fonts():
    args = parser().parse_args(["generate", "--font", "a.ttf", "--font", "b.otf#0"])
    assert args.fonts == ["a.ttf", "b.otf#0"]


pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402

from synthocr import web  # noqa: E402


@pytest.fixture
def client(config):
    config = replace(
        config, fonts=replace(config.fonts, directories=[str(NOTO_SANS), str(NOTO_NASKH)])
    ).validate()
    with TestClient(web.create_app(config), base_url="http://127.0.0.1:8000") as client:
        yield client


def add(client, path, name=None):
    return client.post(
        f"/api/fonts?filename={name or path.name}",
        content=path.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
    )


def test_font_catalog_names_fonts_and_their_languages(client):
    fonts = client.get("/api/fonts").json()["fonts"]
    assert fonts and all(web.FONT_ID.fullmatch(font["id"]) for font in fonts)
    assert not any(font["user"] for font in fonts)
    naskh = [font for font in fonts if "Naskh" in font["name"]]
    assert naskh and all({"ckb", "ar"} <= set(font["languages"]) for font in naskh)
    sans = [font for font in fonts if font["file"].startswith("NotoSans[")]
    assert sans and all("en" in font["languages"] for font in sans)


def test_added_font_is_used_and_can_be_removed(client):
    response = add(client, CAIRO, "My Cairo.ttf")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["added"] is True
    font = body["fonts"][0]
    assert font["user"] and font["file"].startswith("My-Cairo-")
    studio = client.app.state.studio
    assert (studio.user_fonts / font["file"]).is_file()
    assert not (ROOT / "fonts" / font["file"]).exists()
    # The same file again is recognised rather than stored twice.
    again = add(client, CAIRO, "copy.ttf").json()
    assert again["added"] is False and again["fonts"][0]["id"] == font["id"]
    assert len(list(studio.user_fonts.glob("*.ttf"))) == 1

    preview = client.post(
        "/api/preview",
        json={"text": "مرحبا", "language": "ar", "fonts": [font["id"]]},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["font"].startswith("My-Cairo-")

    job = client.post(
        "/api/jobs",
        json={"samples": 2, "languages": ["en"], "fonts": [font["id"]], "source": "corpus"},
    )
    assert job.status_code == 202, job.text

    digest = font["id"].split(":")[0]
    for _ in range(1500):
        status = client.get(f"/api/jobs/{job.json()['id']}").json()
        if status["status"] not in web.ACTIVE:
            break
        time.sleep(0.01)
    assert status["status"] == "completed", status
    manifest = studio.dataset(status) / "manifest.jsonl"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        assert json.loads(line)["font_id"] == font["id"]

    assert client.delete(f"/api/fonts/{digest}").status_code == 200
    assert client.delete(f"/api/fonts/{digest}").status_code == 404
    assert not list(studio.user_fonts.glob("*.ttf"))


def test_bundled_fonts_cannot_be_removed_and_bad_files_are_refused(client):
    bundled = client.get("/api/fonts").json()["fonts"][0]["id"].split(":")[0]
    assert client.delete(f"/api/fonts/{bundled}").status_code == 404
    assert client.delete("/api/fonts/not-a-hash").status_code in {404, 413, 422}
    bad = client.post(
        "/api/fonts?filename=fake.ttf",
        content=b"not a font",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert bad.status_code == 422
    wrong = client.post(
        "/api/fonts?filename=notes.txt",
        content=b"x",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert wrong.status_code == 422
    huge = client.post(
        "/api/fonts?filename=big.ttf",
        content=b"",
        headers={"Content-Length": str(web.FONT_UPLOAD_BYTES + 1)},
    )
    assert huge.status_code == 413
    studio = client.app.state.studio
    assert not list(studio.user_fonts.iterdir())


def test_requests_reject_unknown_font_choices(client):
    assert (
        client.post("/api/preview", json={"text": "abc", "fonts": ["Cairo.ttf"]}).status_code == 422
    )
    missing = "0" * 64 + ":0"
    response = client.post("/api/preview", json={"text": "abc", "fonts": [missing]})
    assert response.status_code == 422
    assert "None of the chosen fonts" in response.text
