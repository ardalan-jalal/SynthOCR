"""Installed copies run outside a project folder from a personal workspace."""

import json
import shutil
from pathlib import Path

import pytest

from synthocr import cli, home

ROOT = Path(__file__).resolve().parents[1]


def fake_fetch(catalog, destination, families):
    # Two open families stand in for the pinned downloads.
    for family in ("notosans", "notonaskharabic"):
        shutil.copytree(
            ROOT / "fonts/open" / family, destination / "open" / family, dirs_exist_ok=True
        )
    return {"families": list(families or []), "destination": str(destination)}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHOCR_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(home, "fetch", fake_fetch)
    return tmp_path / "home"


@pytest.mark.parametrize(
    ("platform", "variables", "expected"),
    [
        ("darwin", {}, "Library/Application Support/SynthOCR"),
        ("linux", {"XDG_DATA_HOME": "/data/xdg"}, "/data/xdg/synthocr"),
        ("linux", {}, ".local/share/synthocr"),
        ("win32", {"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, "AppData/Local/SynthOCR"),
    ],
)
def test_workspace_follows_each_platform(monkeypatch, platform, variables, expected):
    monkeypatch.delenv("SYNTHOCR_HOME", raising=False)
    for name in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(home.sys, "platform", platform)
    assert home.home().as_posix().endswith(expected)


def test_config_comes_from_the_folder_then_the_workspace(workspace, tmp_path):
    with pytest.raises(home.NotSetUp, match="synthocr setup"):
        home.resolve_config(None)
    (workspace).mkdir()
    (workspace / "config.toml").write_text(home.CONFIG, encoding="utf-8")
    config, personal = home.resolve_config(None)
    assert personal and config.root == workspace.resolve()
    (tmp_path / "config.toml").write_text(home.CONFIG, encoding="utf-8")
    config, personal = home.resolve_config(None)
    assert not personal and config.root == tmp_path.resolve()


def test_offline_setup_prepares_fonts_and_text_and_can_run_again(workspace):
    result = home.setup(limit=200, tatoeba=False)
    assert result["workspace"] == str(workspace.resolve())
    assert result["templates"] == list(home.SETUP_TEMPLATES)
    assert result["texts"] > 0
    assert result["fonts"] == list(home.SETUP_FONTS)
    config, personal = home.resolve_config(None)
    assert personal and home.ready(config)
    texts = sorted((workspace / "data/raw/synthetic").glob("*.jsonl"))
    before = [path.read_bytes() for path in texts]
    home.setup(limit=200, tatoeba=False)
    assert [path.read_bytes() for path in texts] == before


def test_failed_tatoeba_download_still_leaves_a_working_workspace(workspace, monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("network unavailable")

    monkeypatch.setattr(home, "snapshot", offline)
    result = home.setup(limit=100)
    assert "network unavailable" in result["tatoeba_skipped"]
    assert result["texts"] > 0


def test_cli_setup_and_generation_use_the_workspace(workspace, capsys):
    assert cli.main(["setup", "--offline", "--limit", "100"]) == 0
    assert json.loads(capsys.readouterr().out)["workspace"] == str(workspace.resolve())
    # A relative output is in the current folder, not inside the workspace.
    assert cli.main(["kurdish", "--samples", "2", "--output", "first"]) == 0
    assert (workspace.parent / "first" / "manifest.jsonl").is_file()
    assert cli.main(["verify", "--dataset", "first"]) == 0


def test_commands_explain_how_to_set_up(workspace, caplog):
    assert cli.main(["kurdish", "--samples", "1"]) == 1
    assert "Run: synthocr setup" in caplog.text
