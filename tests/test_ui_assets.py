"""Cross-file UI contracts that must hold before any browser interaction."""

import re
from collections import Counter
from html.parser import HTMLParser
from importlib.resources import files
from typing import get_args

from synthocr.config import IMAGE_PRESETS
from synthocr.web import DOWNLOAD_FORMAT


class AssetDocument(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []
        self.scripts = []
        self.inputs = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "label" and "for" in attributes:
            self.labels.append(attributes["for"])
        if tag == "script":
            self.scripts.append(attributes)
        if tag == "input":
            self.inputs.append(attributes)
        if tag == "img":
            self.images.append(attributes)


def ui_assets():
    assets = files("synthocr").joinpath("static")
    document = AssetDocument()
    document.feed(assets.joinpath("index.html").read_text(encoding="utf-8"))
    return document, assets.joinpath("app.js").read_text(encoding="utf-8")


def test_ui_javascript_targets_exist_before_startup():
    document, script = ui_assets()
    references = set(re.findall(r"""\$\(\s*["']([^"']+)["']\s*\)""", script))
    # Every literal lookup must be backed by the initial document. A missing
    # target in synchronous startup code stops initialization entirely.
    assert not (missing := references - set(document.ids)), (
        f"JavaScript targets absent from the page: {sorted(missing)}"
    )
    assert any(
        item.get("src") == "/assets/app.js" and "defer" in item for item in document.scripts
    ), "The application must start after its document has been parsed"


def test_ui_controls_have_unique_ids_and_connected_labels():
    document, _ = ui_assets()
    assert not (duplicates := [key for key, count in Counter(document.ids).items() if count > 1]), (
        f"Duplicate UI IDs: {duplicates}"
    )
    assert not (missing := set(document.labels) - set(document.ids)), (
        f"Labels without controls: {sorted(missing)}"
    )


def test_styles_and_download_formats_match_the_server():
    document, _ = ui_assets()
    # Each style is a server preset sent unchanged; the page has no effect editor.
    styles = {item["value"] for item in document.inputs if item.get("name") == "style"}
    assert styles == set(IMAGE_PRESETS)
    formats = {item["value"] for item in document.inputs if item.get("name") == "download-format"}
    # Labels-only JSONL stays in the API; the page saves complete datasets.
    assert formats == set(get_args(DOWNLOAD_FORMAT)) - {"manifest"}
    assert not any("data-layer-toggle" in item for item in document.inputs)


def test_simple_form_leaves_tuning_to_the_cli():
    document, script = ui_assets()
    for identifier in ("train-ratio", "validation-ratio", "workers", "seed", "font-size"):
        assert identifier not in document.ids
    assert "train_ratio: 1" in script and "validation_ratio: 0" in script


def test_download_flow_has_no_folder_picker_or_in_memory_archive():
    document, script = ui_assets()
    assert not any("destination" in identifier for identifier in document.ids)
    assert {"download-button", "download-error", "save-button", "save-error"} <= set(document.ids)
    assert "/api/directories" not in script and "/api/output-folder" not in script
    assert "createObjectURL" not in script and ".blob()" not in script


def test_every_effect_layer_has_a_plain_label_for_the_page():
    from synthocr.config import DOCUMENT_LAYER_LABELS, DOCUMENT_LAYERS

    assert tuple(DOCUMENT_LAYER_LABELS) == DOCUMENT_LAYERS
    assert all(label and description for label, description in DOCUMENT_LAYER_LABELS.values())
    _, script = ui_assets()
    # The page builds its effect rows from /api/info rather than a second list.
    assert not any(f'"{name}"' in script for name in DOCUMENT_LAYERS)
