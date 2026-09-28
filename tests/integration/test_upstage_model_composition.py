"""Explicit model/profile selection through the real worker composition; no network."""

import json
from dataclasses import asdict

import pytest
from proofops.application.ingest.graph_fusion import ParserProfile
from proofops_agent.upstage_extraction import _profile


@pytest.mark.parametrize("model", ["solar-pro3", "solar-pro4", "unknown"])
def test_composition_selects_frozen_model_or_refuses_before_transport(tmp_path, monkeypatch, model):
    from proofops.adapters.local import upstage
    from proofops_worker import composition

    monkeypatch.setattr(
        composition, "__file__", str(tmp_path / "apps/worker/src/worker/composition.py")
    )
    ledger = tmp_path / ".local/upstage/budget.sqlite3"
    ledger.parent.mkdir(parents=True)
    ledger.touch()  # existence only; fake transport never reads/writes this file
    parser = tmp_path / "parser.json"
    parser.write_text(
        json.dumps(ParserProfile("00000000-0000-4000-8000-000000000000").config_snapshot())
    )
    settings = tmp_path / "settings.json"
    profile = asdict(_profile(model if model != "unknown" else "solar-pro3"))
    if model == "unknown":
        profile["model_sha256"] = "a" * 64
    settings.write_text(
        json.dumps(dict(extraction_profile=profile, extraction_limits=dict(max_output_tokens=1024)))
    )
    for key, value in dict(
        APP_ENV="local",
        MODEL_ADAPTER="synthetic",
        LOCAL_EXTRACTION_MODE="upstage_probe",
        LOCAL_DATABASE_PATH=str(tmp_path / "state.sqlite3"),
        LOCAL_PARSER_PROFILE_PATH=str(parser),
        LOCAL_RUN_SETTINGS_PATH=str(settings),
    ).items():
        monkeypatch.setenv(key, value)
    calls = []

    class Probe:
        def __init__(self, key, path, *, model):
            self.model = model
            calls.append((path, model))

        def complete(self, *args, **kwargs):
            raise AssertionError("composition must not invoke model")

    monkeypatch.setattr(upstage, "UpstageProbe", Probe)
    if model == "unknown":
        with pytest.raises(ValueError, match="EXTRACTION_PROFILE_MISMATCH"):
            composition.build_composition(stage="extract")
        assert calls == []
    else:
        runner = composition.build_composition(stage="extract")
        assert runner.extractor.profile == _profile(model)
        assert calls == [(ledger, model)]
        runner.uploads.close()
        runner.uploads.registry.close()
    assert ledger.read_bytes() == b""


@pytest.mark.parametrize("mode", ["upstage_probe", "misspelled-mode"])
def test_unsupported_tagging_mode_is_rejected_before_building_runtime(monkeypatch, mode):
    from proofops_worker.composition import build_composition

    monkeypatch.setenv("LOCAL_TAGGING_MODE", mode)
    monkeypatch.delenv("LOCAL_PARSER_PROFILE_PATH", raising=False)
    with pytest.raises(ValueError, match="LOCAL_TAGGING_MODE_UNSUPPORTED"):
        build_composition(stage="tag")


def _extract_env(tmp_path, monkeypatch, settings_extra):
    """Real extract composition wiring with a fake probe; returns nothing built yet."""
    from proofops.adapters.local import upstage
    from proofops_agent.upstage_extraction import _profile_with_options
    from proofops_worker import composition

    monkeypatch.setattr(
        composition, "__file__", str(tmp_path / "apps/worker/src/worker/composition.py")
    )
    ledger = tmp_path / ".local/upstage/budget.sqlite3"
    ledger.parent.mkdir(parents=True)
    ledger.touch()
    parser = tmp_path / "parser.json"
    parser.write_text(
        json.dumps(ParserProfile("00000000-0000-4000-8000-000000000000").config_snapshot())
    )
    settings = tmp_path / "settings.json"
    # The frozen profile is always a VALID combination; an invalid settings combo
    # (e.g. assertion without source-ids) is what composition must reject, not the
    # profile builder here. So drop the assertion flag from the frozen profile
    # when source-ids is off.
    frozen_source_ids = settings_extra.get("extraction_source_ids", False)
    frozen_assertion = (
        settings_extra.get("extraction_assertion_prompt", False) and frozen_source_ids
    )
    profile = asdict(
        _profile_with_options(
            "solar-pro3",
            source_ids=frozen_source_ids,
            assertion_prompt=frozen_assertion,
        )
    )
    settings.write_text(
        json.dumps(
            dict(
                extraction_profile=profile,
                extraction_limits=dict(max_output_tokens=1024),
                **settings_extra,
            )
        )
    )
    for key, value in dict(
        APP_ENV="local",
        MODEL_ADAPTER="synthetic",
        LOCAL_EXTRACTION_MODE="upstage_probe",
        LOCAL_DATABASE_PATH=str(tmp_path / "state.sqlite3"),
        LOCAL_PARSER_PROFILE_PATH=str(parser),
        LOCAL_RUN_SETTINGS_PATH=str(settings),
    ).items():
        monkeypatch.setenv(key, value)

    class Probe:
        def __init__(self, key, path, *, model="solar-pro3"):
            self.model = model

        def complete(self, *args, **kwargs):
            raise AssertionError("composition must not invoke model")

    monkeypatch.setattr(upstage, "UpstageProbe", Probe)
    return composition


def test_composition_builds_the_assertion_extractor_from_settings(tmp_path, monkeypatch):
    """R20 fix 2: the real extract composition path honours the assertion flag.

    A twin-``_profile_with_options`` test cannot catch composition dropping the
    new flag, so this builds the actual extractor and asserts its wired profile
    and sent system prompt carry the assertion suffix.
    """
    from proofops.domain.provenance import canonical_hash
    from proofops_agent.upstage_extraction import (
        ASSERTION_SYSTEM_SUFFIX,
        SOURCE_ID_SYSTEM_PROMPT,
        _profile_with_options,
    )

    composition = _extract_env(
        tmp_path,
        monkeypatch,
        {"extraction_source_ids": True, "extraction_assertion_prompt": True},
    )
    runner = composition.build_composition(stage="extract")
    try:
        assert runner.extractor.profile == _profile_with_options(
            "solar-pro3", source_ids=True, assertion_prompt=True
        )
        # The extractor really composes the proven wire, not just a matching hash:
        # with no context enabled, the pinned prompt is exactly source-ID + suffix.
        assert runner.extractor._assertion_prompt is True
        assert runner.extractor._source_ids is True
        assert runner.extractor.profile.prompt_sha256 == canonical_hash(
            SOURCE_ID_SYSTEM_PROMPT + ASSERTION_SYSTEM_SUFFIX
        )
    finally:
        runner.uploads.close()
        runner.uploads.registry.close()


def test_composition_rejects_assertion_prompt_without_source_ids(tmp_path, monkeypatch):
    """A lone assertion opt-in (no source-ids) fails closed before any transport."""
    composition = _extract_env(tmp_path, monkeypatch, {"extraction_assertion_prompt": True})
    with pytest.raises(ValueError, match="EXTRACTION_PROFILE_MISMATCH"):
        composition.build_composition(stage="extract")
