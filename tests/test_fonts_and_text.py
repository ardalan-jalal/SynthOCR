import json
import re
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

import pytest

from synthocr.config import Generation
from synthocr.fonts import Coverage, Font
from synthocr.render import Renderer
from synthocr.text import generate_text, normalize, prepare, records, required_codepoints

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "synthocr/font_catalog.json").read_text())["fonts"]
SAMPLES = {
    "notosanssc": "图书馆欢迎您",
    "notosansjp": "図書館へようこそ",
    "notosanskr": "도서관에 오신 것을 환영합니다",
    "notonastaliqurdu": "کتب خانہ سب کے لیے کھلا ہے",
    "notosansgujarati": "પુસ્તકાલય",
    "notosansgurmukhi": "ਪੁਸਤਕਾਲਾ",
    "notosansmalayalam": "പുസ്തകശാല",
    "notosanstelugu": "గ్రంథాలయం",
    "notosanskannada": "ಗ್ರಂಥಾಲಯ",
    "notosanssinhala": "පුස්තකාලය",
    "notosanskhmer": "បណ្ណាល័យ",
    "notosanslao": "ຫໍສະໝຸດ",
    "notosansmyanmar": "စာကြည့်တိုက်",
    "notosanssyriac": "ܫܠܡܐ",
    "notoseriftibetan": "བོད་ཡིག",
    "notosansdevanagari": "नमस्ते दुनिया",
    "notosansbengali": "সবার জন্য",
    "notosanstamil": "தமிழ் நூலகம்",
    "notosanshebrew": "שלום עולם",
    "notosansthai": "ห้องสมุด",
    "notosansethiopic": "ቤተ መጻሕፍት",
    "notosansarmenian": "Գրադարան",
    "notosansgeorgian": "ბიბლიოთეკა",
    "notosanssymbols2": "🂡",
}
ARABIC = {
    "notosansarabic",
    "notonaskharabic",
    "amiri",
    "scheherazadenew",
    "cairo",
    "tajawal",
    "almarai",
    "lateef",
    "harmattan",
    "markazitext",
    "readexpro",
    "reemkufi",
    "ibmplexsansarabic",
}


@pytest.mark.parametrize("entry", CATALOG, ids=[e["family"] for e in CATALOG])
@pytest.mark.parametrize("augmentation", [0, 1], ids=["clean", "augmented"])
def test_every_open_family_renders_its_script(entry, augmentation):
    family = entry["family"]
    text = SAMPLES.get(family, "مرحبا بالعالم" if family in ARABIC else "Hello 123")
    font = Font(str(ROOT / "fonts" / entry["file"]["name"]))
    assert not Coverage().missing(font, required_codepoints(text))
    image, metadata = Renderer(Generation(augmentation=augmentation), 2).render(text, font, 42)
    try:
        assert image.getextrema()[0] < 200
        assert metadata["bbox"][2] > metadata["bbox"][0]
        assert metadata["bbox"][3] > metadata["bbox"][1]
    finally:
        image.close()


@pytest.mark.parametrize("language", ["en", "ckb", "ar", "fa", "de", "fr", "es", "tr"])
def test_receipt_totals_are_consistent(tmp_path, language):
    path = tmp_path / "receipt.jsonl"
    generate_text(path, 100, 20, "receipt", language)
    for line in path.read_text().splitlines():
        text = json.loads(line)["text"]
        _, quantity, unit, total = text.split(" | ")
        q = int(quantity.split(": ")[1])
        u = Decimal(unit.split(": ")[1].replace(",", ""))
        t = Decimal(total.split(": ")[1].replace(",", ""))
        assert q * u == t


@pytest.mark.parametrize("language", ["en", "ckb", "ar", "fa"])
def test_arithmetic_answers_are_correct(tmp_path, language):
    path = tmp_path / "arithmetic.jsonl"
    generate_text(path, 100, 31, "arithmetic", language)
    for line in path.read_text().splitlines():
        text = json.loads(line)["text"]
        match = re.fullmatch(r"(-?\d+) ([+×-]) (\d+) = (-?\d+)", text)
        assert match
        a, b, result = map(int, (match[1], match[3], match[4]))
        assert result == (a + b if match[2] == "+" else a - b if match[2] == "-" else a * b)


@pytest.mark.parametrize(
    "content", ["[]", "null", '{"artifacts": []}', '{"artifacts": {"../x": "bad"}}']
)
def test_malformed_preparation_metadata_is_rebuilt(config, content):
    prepare(config)
    (config.root / "processed/stats.json").write_text(content)
    assert prepare(config)["records"] == 4


def test_csv_blank_lines_and_multiline_fields_preserve_line_provenance(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text('text,language\n\n"first\nsecond",en\n\nthird,en\n')
    assert [number for number, _ in records(path, tmp_path, 100)] == [3, 6]


@pytest.mark.parametrize(
    "content,error", [("text,text\na,b\n", "unique"), ("text\na,b\n", "unexpected CSV")]
)
def test_ambiguous_csv_is_rejected(tmp_path, content, error):
    path = tmp_path / "data.csv"
    path.write_text(content)
    with pytest.raises(ValueError, match=error):
        list(records(path, tmp_path, 100))


def test_csv_field_limit_matches_configuration_and_is_restored(tmp_path):
    import csv

    old_limit = csv.field_size_limit()
    path = tmp_path / "large.csv"
    path.write_text("text\n" + "x" * 200000 + "\n")
    assert len(list(records(path, tmp_path, 300000))[0][1]["text"]) == 200000
    assert csv.field_size_limit() == old_limit


def test_json_surrogate_has_an_actionable_error_and_preserves_previous_data(config):
    prepare(config)
    raw = config.root / "raw/sample.jsonl"
    raw.write_text('{"text":"abc\\ud800def"}\n')
    with pytest.raises(ValueError, match="sample.jsonl:1:.*surrogate"):
        prepare(config)
    assert (config.root / "processed/texts.sqlite3").exists()


def test_lexicons_are_packaged_resources():
    values = json.loads(files("synthocr").joinpath("lexicons.json").read_text())
    assert set(values) == {"en", "ckb", "ar", "fa", "de", "fr", "es", "tr"}


def test_unassigned_scalars_cannot_bypass_glyph_checks():
    assert 0x378 in required_codepoints("Hello\u0378")
    assert (
        Coverage().choose(
            [Font(str(ROOT / "fonts/open/notosans/NotoSans[wdth,wght].ttf"))], "Hello\u0378", 0
        )
        is None
    )


def test_processed_labels_strip_nonvisual_c1_controls():
    assert normalize("Hello\x80world\x9f\x85again") == "Helloworld again"
