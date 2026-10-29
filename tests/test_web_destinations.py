"""Portable named output locations, confined folders and persistent job identities."""

import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr import cli, pipeline, web
from synthocr.verify import verify


def app_client(config, **options):
    return TestClient(web.create_app(config, **options), base_url="http://127.0.0.1:8000")


def wait_job(client, identifier):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{identifier}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in web.ACTIVE:
            return job
        time.sleep(0.01)
    pytest.fail("Destination smoke job did not finish")


def start_job(client, **settings):
    response = client.post("/api/jobs", json={"samples": 2, **settings})
    assert response.status_code == 202, response.text
    result = wait_job(client, response.json()["id"])
    assert result["status"] == "completed", result
    return result


def symlink(path, target):
    try:
        path.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Symbolic links are unavailable: {error}")


def test_named_folder_generation_verification_download_and_restart(config, tmp_path):
    root = tmp_path / "training-drive"
    with app_client(config, output_locations={"training-drive": root}) as client:
        locations = client.get("/api/info").json()["output_locations"]
        assert locations == [
            {"id": "default", "label": "Dataset storage"},
            {"id": "training-drive", "label": "Training drive"},
        ]
        created = client.post(
            "/api/directories", json={"location": "training-drive", "name": "Kurdish datasets"}
        )
        assert created.status_code == 201, created.text
        assert created.json() == {
            "location": "training-drive",
            "path": "Kurdish datasets",
            "parent": "",
            "directories": [],
            "truncated": False,
            "root": str(root),
            "absolute_path": str(root / "Kurdish datasets"),
        }
        assert (
            client.post(
                "/api/directories",
                json={"location": "training-drive", "path": "Kurdish datasets", "name": "سۆرانی"},
            ).status_code
            == 201
        )
        parent = "Kurdish datasets/سۆرانی"
        job = start_job(
            client, name="saved-dataset", output_location="training-drive", output_folder=parent
        )
        destination = root / parent / "saved-dataset"
        assert job["output"] == str(destination)
        assert job["output_root"] == str(root)
        assert job["output_location"] == "training-drive"
        assert job["output_folder"] == parent
        assert verify(destination)["records"] == 2
        assert (client.app.state.studio.metadata / f"{job['id']}.json").is_file()
        assert not list((root / ".jobs").glob("*.json"))
        response = client.get(f"/api/jobs/{job['id']}/download?format=dataset")
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert "manifest.jsonl" in archive.namelist()
            assert "origins.jsonl" in archive.namelist()
        conflict = client.post(
            "/api/jobs",
            json={
                "name": job["name"],
                "samples": 2,
                "output_location": "training-drive",
                "output_folder": parent,
            },
        )
        assert conflict.status_code == 409
    with app_client(config, output_locations={"training-drive": root}) as client:
        restored = client.get(f"/api/jobs/{job['id']}")
        assert restored.status_code == 200, restored.text
        assert restored.json()["output"] == str(destination)
        assert client.get(f"/api/jobs/{job['id']}/download?format=manifest").status_code == 200
        assert client.get("/api/jobs").json()["jobs"][0]["id"] == job["id"]


def test_default_and_legacy_jobs_keep_the_existing_storage_contract(config):
    with app_client(config) as client:
        job = start_job(client, name="legacy-compatible")
        manager = client.app.state.studio
        assert Path(job["output"]) == manager.output_root / job["name"]
        metadata = manager.metadata / f"{job['id']}.json"
        legacy = json.loads(metadata.read_text())
        for key in ("output_root", "output_location", "output_folder"):
            legacy.pop(key)
        legacy["output"] = "/untrusted/path/is/not/used"
        metadata.write_text(json.dumps(legacy))
    with app_client(config) as client:
        assert client.get(f"/api/jobs/{job['id']}/download?format=manifest").status_code == 200
        assert client.get("/api/directories").json()["directories"] == []


def test_remapped_location_cannot_serve_old_job_or_recovery_data(config, tmp_path):
    first, replacement = tmp_path / "first", tmp_path / "replacement"
    with app_client(config, output_locations={"archive": first}) as client:
        job = start_job(client, output_location="archive")
    with app_client(config, output_locations={"archive": replacement}) as client:
        for suffix in ("", "/samples", "/download?format=manifest"):
            assert client.get(f"/api/jobs/{job['id']}{suffix}").status_code == 409
        assert client.get("/api/jobs").json()["jobs"] == []
    with app_client(config, output_locations={"archive": first}) as client:
        assert client.get(f"/api/jobs/{job['id']}/download?format=manifest").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "a/../b",
        "a//b",
        "a/",
        "a\\b",
        "C:/outside",
        ".jobs",
        "a/.cache",
        "a/CON",
        "a/COM¹",
        "a/trailing.",
        "a/trailing ",
        "a\x00b",
        "/".join(["a"] * 9),
    ],
)
def test_unportable_or_traversing_paths_cannot_be_browsed_created_or_generated(config, path):
    with app_client(config) as client:
        assert client.get("/api/directories", params={"path": path}).status_code == 400
        assert (
            client.post("/api/directories", json={"path": path, "name": "new"}).status_code == 422
        )
        assert (
            client.post("/api/jobs", json={"samples": 1, "output_folder": path}).status_code == 422
        )
        assert client.get("/api/jobs").json()["jobs"] == []


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".jobs",
        "nested/child",
        "nested\\child",
        "CON",
        "LPT2.txt",
        "ending.",
        "ending ",
        "bad?name",
        "a" * 81,
    ],
)
def test_folder_creation_requires_a_single_portable_name(config, name):
    with app_client(config) as client:
        assert client.post("/api/directories", json={"name": name}).status_code == 422


def test_existing_folder_is_selected_but_never_overwritten(config):
    with app_client(config) as client:
        assert client.post("/api/directories", json={"name": "training"}).status_code == 201
        assert client.post("/api/directories", json={"name": "training"}).status_code == 409
        assert client.get("/api/directories").json()["directories"] == [
            {"name": "training", "path": "training"}
        ]
        assert client.get("/api/directories", params={"path": "missing"}).status_code == 400
        assert (
            client.post("/api/jobs", json={"samples": 1, "output_folder": "missing"}).status_code
            == 400
        )
        assert not (client.app.state.studio.output_root / "missing").exists()


def test_joined_folder_depth_and_utf8_filename_limit_fail_before_mkdir(config):
    with app_client(config) as client:
        root = client.app.state.studio.output_root
        parent = "/".join(["nested"] * 8)
        (root / parent).mkdir(parents=True)
        response = client.post("/api/directories", json={"path": parent, "name": "too-deep"})
        assert response.status_code == 422, response.text
        assert not (root / parent / "too-deep").exists()
        for name in ("📝" * 80, "bad\ud800name"):
            response = client.post(
                "/api/directories",
                content=json.dumps({"name": name}),
                headers={"content-type": "application/json"},
            )
            assert response.status_code in {400, 422}, response.text
        assert [path.name for path in root.iterdir() if not path.name.startswith(".")] == ["nested"]


def test_symbolic_links_and_dataset_internals_are_not_directory_destinations(config, tmp_path):
    with app_client(config) as client:
        root = client.app.state.studio.output_root
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink(root / "linked", outside)
        (root / "container").mkdir()
        symlink(root / "container/linked", outside)
        for path in ("linked", "container/linked"):
            assert client.get("/api/directories", params={"path": path}).status_code == 400
            assert (
                client.post("/api/directories", json={"path": path, "name": "child"}).status_code
                == 400
            )
            assert (
                client.post("/api/jobs", json={"samples": 1, "output_folder": path}).status_code
                == 400
            )
        job = start_job(client, name="existing-dataset")
        for path in (job["name"], job["name"] + "/images"):
            assert client.get("/api/directories", params={"path": path}).status_code == 400
            assert (
                client.post("/api/jobs", json={"samples": 1, "output_folder": path}).status_code
                == 400
            )
        assert client.get("/api/directories").json()["directories"] == [
            {"name": "container", "path": "container"}
        ]


def test_folder_replaced_by_symlink_cannot_redirect_existing_job(config, tmp_path):
    with app_client(config) as client:
        assert client.post("/api/directories", json={"name": "training"}).status_code == 201
        job = start_job(client, output_folder="training")
        root = client.app.state.studio.output_root
        (root / "training").rename(root / "moved-training")
        symlink(root / "training", root / "moved-training")
        assert client.get(f"/api/jobs/{job['id']}/download?format=manifest").status_code == 400


def test_directory_listing_bounds_results_and_scanning(config, monkeypatch):
    with app_client(config) as client:
        root = client.app.state.studio.output_root
        for index in range(web.DIRECTORY_LIMIT + 1):
            (root / f"folder-{index:03d}").mkdir()
        listing = client.get("/api/directories").json()
        assert len(listing["directories"]) == web.DIRECTORY_LIMIT
        assert listing["truncated"] is True
        assert listing["directories"] == sorted(
            listing["directories"], key=lambda item: item["name"].casefold()
        )
        monkeypatch.setattr(web, "DIRECTORY_SCAN_LIMIT", 5)
        limited = client.get("/api/directories").json()
        assert len(limited["directories"]) <= 5
        assert limited["truncated"] is True


@pytest.mark.parametrize("id", ["default", "Uppercase", "with space", "../escape", "", "a" * 33])
def test_named_root_ids_are_validated(config, tmp_path, id):
    with pytest.raises(ValueError, match="named output locations"):
        web.create_app(config, output_locations={id: tmp_path / "extra"})


def test_protected_duplicate_and_overlapping_locations_are_rejected_before_locking(
    config, tmp_path
):
    default = config.path("dataset/ui")
    for location in (
        config.path(config.data.raw),
        config.path(config.data.processed),
        config.path(config.fonts.directories[0]),
        config.root,
    ):
        with pytest.raises(ValueError, match="separate"):
            web.create_app(config, output_locations={"invalid": location})
    for location in (default, default / "nested", default.parent):
        with pytest.raises(ValueError, match="overlap"):
            web.create_app(config, output_locations={"invalid": location})
    with pytest.raises(ValueError, match="eight"):
        web.create_app(
            config, output_locations={f"place-{index}": tmp_path / str(index) for index in range(9)}
        )
    with pytest.raises(ValueError, match="named output locations"):
        web.create_app(config, output_locations={"invalid": None})
    with app_client(config) as client:
        assert client.get("/api/directories").status_code == 200


def test_all_roots_are_locked_and_partial_startup_failure_releases_earlier_locks(config, tmp_path):
    occupied_root = tmp_path / "occupied"
    with app_client(config, output_root=occupied_root):
        with pytest.raises(ValueError, match="Another SynthOCR studio"):
            web.create_app(config, output_locations={"occupied": occupied_root})
        with app_client(config) as client:
            assert client.get("/api/directories").status_code == 200
    with app_client(config, output_locations={"occupied": occupied_root}):
        with pytest.raises(ValueError, match="Another SynthOCR studio"):
            web.create_app(config, output_root=occupied_root)
    with app_client(config, output_root=occupied_root) as client:
        assert client.get("/api/directories").status_code == 200


def test_configured_roots_cannot_be_datasets_dataset_children_or_metadata(config, tmp_path):
    previous = tmp_path / "previous-dataset"
    (previous / "images").mkdir(parents=True)
    (previous / "run.json").write_text("{}")
    for root in (
        previous,
        previous / "images",
        tmp_path / "another/.jobs",
        tmp_path / "another/.jobs/cache",
    ):
        with pytest.raises(ValueError, match="outside existing datasets and job metadata"):
            web.create_app(config, output_locations={"invalid": root})


def test_one_active_job_is_shared_across_locations(config, tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = web.Studio._run

    def delayed(self, *args):
        started.set()
        release.wait(10)
        return original(self, *args)

    monkeypatch.setattr(web.Studio, "_run", delayed)
    with app_client(config, output_locations={"other": tmp_path / "other"}) as client:
        response = client.post("/api/jobs", json={"name": "reserved", "samples": 1})
        assert response.status_code == 202
        assert started.wait(5)
        try:
            assert (
                client.post(
                    "/api/jobs", json={"samples": 1, "output_location": "other"}
                ).status_code
                == 409
            )
            assert client.post("/api/directories", json={"name": "reserved"}).status_code == 409
            for path in ("reserved", "reserved/images"):
                assert client.get("/api/directories", params={"path": path}).status_code == 409
                assert (
                    client.post(
                        "/api/directories", json={"path": path, "name": "child"}
                    ).status_code
                    == 409
                )
        finally:
            release.set()
        assert wait_job(client, response.json()["id"])["status"] == "completed"


def test_active_output_is_reserved_before_dataset_markers_are_written(
    config, tmp_path, monkeypatch
):
    ready, release = threading.Event(), threading.Event()
    original = pipeline._font_name

    def pause_font_metadata(*args):
        if not ready.is_set():
            ready.set()
            release.wait(10)
        return original(*args)

    monkeypatch.setattr(pipeline, "_font_name", pause_font_metadata)
    root = tmp_path / "training-drive"
    with app_client(config, output_locations={"training": root}) as client:
        assert (
            client.post(
                "/api/directories", json={"location": "training", "name": "collection"}
            ).status_code
            == 201
        )
        response = client.post(
            "/api/jobs",
            json={
                "name": "live",
                "samples": 2,
                "output_location": "training",
                "output_folder": "collection",
            },
        )
        assert response.status_code == 202, response.text
        assert ready.wait(5)
        destination = root / "collection/live"
        try:
            assert (destination / "images").is_dir()
            assert not (destination / "run.json").exists()
            assert not (destination / "manifest.jsonl").exists()
            listing = client.get(
                "/api/directories", params={"location": "training", "path": "collection"}
            )
            assert listing.status_code == 200
            assert listing.json()["directories"] == []
            for path in ("collection/live", "collection/live/images"):
                assert (
                    client.get(
                        "/api/directories", params={"location": "training", "path": path}
                    ).status_code
                    == 409
                )
                blocked = client.post(
                    "/api/directories",
                    json={"location": "training", "path": path, "name": "00000000.png"},
                )
                assert blocked.status_code == 409, blocked.text
            assert not (destination / "images/00000000.png").exists()
            assert (
                client.post(
                    "/api/directories",
                    json={"location": "training", "path": "collection", "name": "next-collection"},
                ).status_code
                == 201
            )
        finally:
            release.set()
        job = wait_job(client, response.json()["id"])
        assert job["status"] == "completed", job
        assert verify(destination)["records"] == 2
        assert client.get(f"/api/jobs/{job['id']}/download?format=manifest").status_code == 200


@pytest.mark.parametrize(
    "arguments",
    [
        ["missing"],
        ["default=somewhere"],
        ["bad name=somewhere"],
        ["empty="],
        ["a=first", "a=second"],
        [f"place-{index}=path-{index}" for index in range(9)],
    ],
)
def test_cli_rejects_malformed_duplicate_or_excess_locations(arguments):
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(
            ["ui", *(item for argument in arguments for item in ("--output-location", argument))]
        )
    assert error.value.code == 2


def test_cli_resolves_additional_locations_from_config_and_keeps_one_server_worker(
    config, monkeypatch
):
    captured = {}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        web, "create_app", lambda selected, **options: captured.update(options) or "app"
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **options: captured.update(options))
    assert (
        cli.main(
            [
                "ui",
                "--no-browser",
                "--output-root",
                "primary",
                "--output-location",
                "training-drive=other",
            ]
        )
        == 0
    )
    assert captured["output_root"] == config.root / "primary"
    assert captured["output_locations"] == {"training-drive": config.root / "other"}
    assert captured["workers"] == 1
    assert captured["host"] == "127.0.0.1"
