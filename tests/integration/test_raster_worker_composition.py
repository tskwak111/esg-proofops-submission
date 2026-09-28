"""Raster worker composition is explicit, local, and remains call-free."""

import json
import sqlite3

import pytest


def _configure(monkeypatch, tmp_path):
    from proofops.application.ingest.graph_fusion import ParserProfile
    from proofops_worker import composition

    monkeypatch.setattr(
        composition, "__file__", str(tmp_path / "apps/worker/src/proofops_worker/composition.py")
    )
    parser = tmp_path / "parser.json"
    parser.write_text(
        json.dumps(ParserProfile("00000000-0000-4000-8000-000000000000").config_snapshot())
    )
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")
    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("LOCAL_PARSER_PROFILE_PATH", str(parser))
    return composition, tmp_path / ".local/upstage/budget.sqlite3"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stage": "extract", "verify_paragraphs": True, "raster_ocr": True},
        {"stage": "parse", "verify_paragraphs": False, "raster_ocr": True},
        {"stage": "parse", "verify_paragraphs": 1, "raster_ocr": True},
        {"stage": "parse", "verify_paragraphs": True, "raster_ocr": 1},
    ],
)
def test_raster_composition_rejects_invalid_options_before_runtime(tmp_path, monkeypatch, kwargs):
    composition, _ledger = _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        composition,
        "build_proofops_composition",
        lambda **_kwargs: pytest.fail("invalid raster options built runtime"),
    )

    with pytest.raises(ValueError, match="RASTER_OCR_REQUIRE_NATIVE_PARSE_STAGE"):
        composition.build_composition(**kwargs)


def test_raster_composition_requires_existing_shared_ledger(tmp_path, monkeypatch):
    composition, ledger = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("UPSTAGE_API_KEY", "test-secret")

    with pytest.raises(ValueError, match="SHARED_BUDGET_LEDGER_REQUIRED"):
        composition.build_composition(stage="parse", verify_paragraphs=True, raster_ocr=True)

    assert not ledger.exists()


def test_raster_composition_uses_shared_ledger_without_provider_request(tmp_path, monkeypatch):
    from proofops.adapters.local.upstage_parse import UpstageParseProbe

    composition, ledger = _configure(monkeypatch, tmp_path)
    ledger.parent.mkdir(parents=True)
    ledger.touch()
    monkeypatch.setenv("UPSTAGE_API_KEY", "test-secret")
    monkeypatch.setattr(
        UpstageParseProbe,
        "parse",
        lambda *_args, **_kwargs: pytest.fail("composition sent a provider request"),
    )

    runner = composition.build_composition(stage="parse", verify_paragraphs=True, raster_ocr=True)

    assert runner.raster_ledger == ledger
    assert runner.raster_probe.ledger == ledger
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT COUNT(*) FROM probe_calls").fetchone()[0] == 0
    runner.uploads.close()
    runner.uploads.registry.close()
