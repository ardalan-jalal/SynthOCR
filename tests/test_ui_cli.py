import builtins
import json

import pytest

from synthocr.cli import main

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


def test_ui_uses_config_relative_output_root_and_one_server_worker(config, monkeypatch):
    path = config.root / "config.toml"
    path.write_text(
        f"[fonts]\ndirectories={json.dumps(config.fonts.directories)}\n", encoding="utf-8"
    )
    captured = {}
    app = object()

    def create_app(selected, output_root=None, *, cache_mb=256, output_locations=None):
        captured.update(
            root=selected.root,
            output_root=output_root,
            cache_mb=cache_mb,
            output_locations=output_locations,
        )
        return app

    def serve(application, **settings):
        captured.update(application=application, **settings)

    monkeypatch.setattr("synthocr.web.create_app", create_app)
    monkeypatch.setattr("uvicorn.run", serve)
    assert (
        main(
            [
                "--config",
                str(path),
                "ui",
                "--no-browser",
                "--port",
                "8085",
                "--output-root",
                "outputs",
            ]
        )
        == 0
    )
    assert captured == {
        "root": config.root,
        "output_root": config.root / "outputs",
        "application": app,
        "host": "127.0.0.1",
        "port": 8085,
        "workers": 1,
        "cache_mb": 256,
        "output_locations": None,
    }


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_ui_rejects_invalid_port_without_starting(config, monkeypatch, port):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid port must not start a server")

    monkeypatch.setattr("uvicorn.run", unexpected)
    assert main(["ui", "--no-browser", "--port", port]) == 1


def test_ui_stays_on_loopback_by_default():
    with pytest.raises(SystemExit) as error:
        main(["ui", "--host", "0.0.0.0"])
    assert error.value.code == 2


@pytest.mark.parametrize("cache_mb", ["0", "-1"])
def test_ui_rejects_invalid_cache_limit(monkeypatch, cache_mb):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid cache limit must not start a server")

    monkeypatch.setattr("uvicorn.run", unexpected)
    assert main(["ui", "--no-browser", "--cache-mb", cache_mb]) == 1


def test_ui_explains_how_to_install_optional_dependencies(monkeypatch, caplog):
    original = builtins.__import__

    def without_web(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("Optional dependency is absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_web)
    assert main(["ui", "--no-browser"]) == 1
    assert "uv run --extra web synthocr ui" in caplog.text


def test_ui_static_assets_are_bundled():
    from importlib.resources import files

    for name in ("index.html", "app.css", "app.js", "theme.js", "brand.svg"):
        assert files("synthocr").joinpath("static", name).is_file()
