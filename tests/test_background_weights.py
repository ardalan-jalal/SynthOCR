"""Relative background shares stay bounded, reproducible and shared by all entry points."""

import json
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from synthocr import cli
from synthocr.config import (
    DOCUMENT_PRESETS,
    Generation,
    background_distribution,
    load_config,
    validate_augmentation,
)
from synthocr.experiments import run_experiment
from synthocr.pipeline import generate
from synthocr.render import Renderer, _effect_rng
from synthocr.verify import verify

POOL = ["office-scan", "aged-paper", "phone-photo"]


def weighted(**changes):
    return replace(Generation(augmentation=1, background_presets=POOL), **changes)


def test_relative_shares_include_omitted_defaults_and_explicit_zero():
    assert background_distribution(Generation()) == {}
    assert background_distribution(weighted()) == dict.fromkeys(POOL, 1 / 3)
    generation = weighted(background_weights={"office-scan": 3, "aged-paper": 0})
    validate_augmentation(generation)
    assert background_distribution(generation) == {
        "office-scan": 0.75,
        "aged-paper": 0,
        "phone-photo": 0.25,
    }


@pytest.mark.parametrize(
    "weights",
    [
        None,
        [],
        [("office-scan", 2)],
        {"unknown": 2},
        {"notebook": 2},
        {1: 2},
        {"office-scan": True},
        {"office-scan": "2"},
        {"office-scan": None},
        {"office-scan": -1},
        {"office-scan": 1_000_001},
        {"office-scan": 10**1000},
        {"office-scan": float("nan")},
        {"office-scan": float("inf")},
        dict.fromkeys(POOL, 0),
        dict.fromkeys((str(index) for index in range(len(DOCUMENT_PRESETS) + 1)), 1),
    ],
)
@pytest.mark.parametrize("augmentation", [0, 1])
def test_invalid_weights_are_rejected_even_when_effects_disabled(weights, augmentation):
    with pytest.raises(ValueError, match="background_weights"):
        validate_augmentation(weighted(augmentation=augmentation, background_weights=weights))


def test_weights_need_selected_recipes_and_accept_finite_upper_bound():
    with pytest.raises(ValueError, match="selected background_presets"):
        validate_augmentation(Generation(background_weights={"office-scan": 1}))
    validate_augmentation(weighted(background_weights={"office-scan": 1_000_000}))


def test_default_seeded_recipe_choices_are_unchanged():
    renderer = Renderer(weighted(), 1)
    for seed in range(128):
        assert renderer._background_preset(seed) == _effect_rng(seed, "preset").choice(POOL)


def test_weighted_choices_are_deterministic_and_approach_requested_shares():
    settings = weighted(background_weights={"office-scan": 3, "aged-paper": 0})
    first, repeated = Renderer(settings, 1), Renderer(settings, 1)
    observed = Counter()
    for seed in range(10_000):
        chosen = first._background_preset(seed)
        assert chosen == repeated._background_preset(seed)
        observed[chosen] += 1
    assert observed["aged-paper"] == 0
    assert set(observed) == {"office-scan", "phone-photo"}
    assert observed["office-scan"] / 10_000 == pytest.approx(0.75, abs=0.02)


@pytest.mark.parametrize("augmentation", [0.5, 1])
def test_weighted_datasets_match_across_worker_counts_and_record_shares(config, augmentation):
    generation = replace(
        config.generation,
        augmentation=augmentation,
        samples=12,
        background_presets=POOL,
        background_weights={"office-scan": 3, "aged-paper": 0},
    )
    manifests = []
    for workers in (1, 2):
        current = replace(
            config, generation=replace(generation, output=f"weighted-{workers}", workers=workers)
        )
        assert generate(current)["generated"] == 12
        output = current.path(current.generation.output)
        assert verify(output)["records"] == 12
        records = [
            json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()
        ]
        assert any(record["augmentation"] for record in records)
        assert all(record["augmentation"] for record in records) == (augmentation == 1)
        for record in records:
            actual = record["augmentation"]
            if not actual:
                continue
            assert actual["background_preset"] != "aged-paper"
            assert (
                actual["background_probability"]
                == background_distribution(generation)[actual["background_preset"]]
            )
        manifests.append(records)
    assert manifests[0] == manifests[1]


def test_toml_configuration_retains_weights(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[generation]\nbackground_presets = ["office-scan", "phone-photo"]\n'
        "background_weights = { office-scan = 3, phone-photo = 1 }\n"
    )
    settings = load_config(path).generation
    assert settings.background_weights == {"office-scan": 3, "phone-photo": 1}
    assert background_distribution(settings) == {"office-scan": 0.75, "phone-photo": 0.25}


@pytest.mark.parametrize("command", ["generate", "kurdish", "benchmark"])
def test_cli_weight_flag_is_available_on_all_generation_commands(command):
    args = cli.parser().parse_args(
        [command, "--background-weight", "office-scan=3", "--background-weight", "phone-photo=1"]
    )
    assert args.background_weights == {"office-scan": 3, "phone-photo": 1}


@pytest.mark.parametrize(
    "arguments,message",
    [
        (["office-scan"], "expected a supported background preset and weight"),
        (["imaginary=1"], "expected a supported background preset and weight"),
        (["office-scan=true"], "weight must be a finite number"),
        (["office-scan=nan"], "weight must be between"),
        (["office-scan=inf"], "weight must be between"),
        (["office-scan=-1"], "weight must be between"),
        (["office-scan=1000001"], "weight must be between"),
        (["office-scan=1", "office-scan=2"], "duplicate weight for office-scan"),
    ],
)
def test_cli_reports_malformed_or_duplicate_weights(arguments, message, capsys):
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(
            ["generate", *(item for value in arguments for item in ("--background-weight", value))]
        )
    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("command", ["generate", "kurdish"])
def test_cli_weights_reach_generated_images_and_run_configuration(config, monkeypatch, command):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert (
        cli.main(
            [
                command,
                "--samples",
                "2",
                "--output",
                "weighted-cli",
                "--augmentation",
                "1",
                "--background-preset",
                "office-scan",
                "--background-preset",
                "phone-photo",
                "--background-weight",
                "office-scan=0",
                "--background-weight",
                "phone-photo=3",
            ]
        )
        == 0
    )
    output = config.root / "weighted-cli"
    assert verify(output)["records"] == 2
    run = json.loads((output / "run.json").read_text())
    assert run["config"]["generation"]["background_weights"] == {
        "office-scan": 0,
        "phone-photo": 3,
    }
    for line in (output / "manifest.jsonl").read_text().splitlines():
        applied = json.loads(line)["augmentation"]
        assert applied["background_preset"] == "phone-photo"
        assert applied["background_probability"] == 1


def test_comparison_experiment_does_not_inherit_custom_weight_mapping(config):
    current = replace(
        config,
        generation=replace(
            config.generation,
            background_presets=POOL,
            background_weights={"office-scan": 3, "aged-paper": 0},
        ),
    )
    destination = config.root / "weighted-experiment"
    result = run_experiment(current, destination, samples=2)
    assert result["status"] == "complete"
    settings = json.loads((destination / "config.json").read_text())
    assert all(value["background_weights"] == {} for value in settings["presets"].values())


@pytest.fixture
def client(config):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx2")
    from fastapi.testclient import TestClient

    from synthocr.web import create_app

    with TestClient(create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


@pytest.mark.parametrize("compare", [False, True])
def test_preview_compares_all_variants_with_configured_shares(client, compare):
    response = client.post(
        "/api/preview",
        json={
            "text": "سڵاو جیهان",
            "augmentation": 1,
            "background_presets": POOL,
            "background_weights": {"office-scan": 0, "aged-paper": 3},
            "compare": compare,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["augmentation"]["background_preset"] == "aged-paper"
    assert result["augmentation"]["background_probability"] == 0.75
    assert len(result["variants"]) == (len(DOCUMENT_PRESETS) if compare else len(POOL))
    for variant in result["variants"]:
        applied = variant["augmentation"]
        assert applied["background_preset"] == variant["preset"]
        assert applied["background_probability"] == {
            "office-scan": 0,
            "aged-paper": 0.75,
            "phone-photo": 0.25,
        }.get(variant["preset"], 0)


@pytest.mark.parametrize(
    "weights",
    [
        {"office-scan": True},
        {"office-scan": "3"},
        {"office-scan": -1},
        {"office-scan": 1_000_001},
        {"notebook": 3},
        dict.fromkeys(POOL, 0),
    ],
)
@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
def test_api_rejects_invalid_weight_values(client, weights, endpoint):
    response = client.post(
        endpoint, json={"background_presets": POOL, "background_weights": weights}
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
def test_api_rejects_nonfinite_weights_with_a_valid_client_error(client, value, endpoint):
    response = client.post(
        endpoint,
        content=json.dumps(
            {"background_presets": POOL, "background_weights": {"office-scan": value}}
        ),
        headers={"content-type": "application/json"},
    )
    assert response.status_code in {400, 422}, response.text
    assert response.json()["detail"]
    assert client.get("/api/jobs").json()["jobs"] == []


def test_job_carries_weights_to_disk_and_effective_annotations(client):
    weights = {"office-scan": 0, "aged-paper": 0, "phone-photo": 4}
    response = client.post(
        "/api/jobs",
        json={
            "source": "custom",
            "language": "kurmanji",
            "text": "Roj baş, cîhan!",
            "samples": 4,
            "augmentation": 1,
            "background_presets": POOL,
            "background_weights": weights,
        },
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{identifier}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    else:
        pytest.fail("Weighted preview job did not complete")
    assert job["status"] == "completed", job
    output = Path(job["output"])
    assert verify(output)["records"] == 4
    run = json.loads((output / "run.json").read_text())
    assert run["config"]["generation"]["background_weights"] == weights
    for line in (output / "manifest.jsonl").read_text().splitlines():
        record = json.loads(line)
        assert record["text"] == "Roj baş, cîhan!"
        assert record["augmentation"]["background_preset"] == "phone-photo"
        assert record["augmentation"]["background_probability"] == 1
