"""Trusted raster runtime settings stay opt-in and validate before run creation."""

from dataclasses import asdict
from uuid import uuid4

import pytest

from tests.integration.test_local_runtime_config import (
    _budget_limits,
    _parser_snapshot,
    _write_json,
)


def _policy():
    from proofops.adapters.local.raster_visibility import raster_ocr_policy

    return raster_ocr_policy(mode="standard", max_pages=1, max_calls=1)


def _settings(tmp_path, *, raster=True):
    from proofops_agent.upstage_extraction import _profile

    settings = {
        "build_root": str(tmp_path),
        "budget_limits": _budget_limits(),
        "extraction_profile": asdict(_profile()),
        "extraction_limits": {"max_calls": 1, "max_output_tokens": 1024},
    }
    if raster:
        settings.update(raster_runtime_binding_id=str(uuid4()), raster_policy=_policy())
    return settings


def _load(tmp_path, settings, *, mode="upstage_probe"):
    from proofops_api.local_runtime import load_local_runtime

    parser = _write_json(tmp_path / "parser.json", _parser_snapshot())
    config = _write_json(tmp_path / "runtime.json", settings)
    env = {
        "LOCAL_PARSER_PROFILE_PATH": str(parser),
        "LOCAL_RUN_SETTINGS_PATH": str(config),
    }
    if mode is not None:
        env["LOCAL_EXTRACTION_MODE"] = mode
    return load_local_runtime(env)


def test_complete_raster_settings_become_trusted_run_service_kwargs(tmp_path):
    settings = _settings(tmp_path)

    runtime = _load(tmp_path, settings)

    assert runtime["raster_runtime_binding_id"] == settings["raster_runtime_binding_id"]
    assert runtime["raster_policy"] == settings["raster_policy"]
    assert runtime["extraction_mode"] == "upstage_probe"


@pytest.mark.parametrize("field", ["raster_runtime_binding_id", "raster_policy"])
def test_partial_raster_settings_are_rejected(tmp_path, field):
    settings = _settings(tmp_path)
    settings.pop(field)

    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        _load(tmp_path, settings)


@pytest.mark.parametrize("fault", ["uuid", "empty_uuid", "policy", "hash"])
def test_malformed_raster_settings_are_rejected(tmp_path, fault):
    settings = _settings(tmp_path)
    if fault == "uuid":
        settings["raster_runtime_binding_id"] = uuid4().hex
    elif fault == "empty_uuid":
        settings["raster_runtime_binding_id"] = ""
    elif fault == "policy":
        settings["raster_policy"].pop("reader_versions")
    else:
        settings["raster_policy"]["native_policy_sha256"] = "x" * 64

    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        _load(tmp_path, settings)


@pytest.mark.parametrize("mode", [None, "", "local_synthetic"])
def test_raster_settings_require_explicit_upstage_extraction_mode(tmp_path, mode):
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        _load(tmp_path, _settings(tmp_path), mode=mode)


def test_default_runtime_settings_are_unaffected(tmp_path):
    runtime = _load(tmp_path, _settings(tmp_path, raster=False))

    assert runtime["extraction_mode"] == "upstage_probe"
    assert "raster_runtime_binding_id" not in runtime
    assert "raster_policy" not in runtime
