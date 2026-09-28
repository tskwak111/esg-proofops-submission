"""Local API run configuration is explicit, typed, and fail-closed."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


def test_config_read_is_bounded_even_if_file_grows(tmp_path, monkeypatch):
    from proofops_api.local_runtime import _json_file

    path = _write_json(tmp_path / "growing.json", {})

    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 65_537, "unbounded configuration read"
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *_a, **_k: GrowingFile(b" " * 65_538))
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        _json_file(str(path))


def _parser_snapshot() -> dict[str, object]:
    from proofops.application.ingest.graph_fusion import ParserProfile

    return ParserProfile(
        "00000000-0000-4000-8000-000000000000", java_executable="/usr/bin/java"
    ).config_snapshot()


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _budget_limits() -> dict[str, object]:
    return {
        "input_tokens": 1000,
        "output_tokens": 500,
        "max_attempts": 3,
        "roles": [
            {
                "role": "tagger",
                "max_calls": 3,
                "max_input_tokens": 1000,
                "max_output_tokens": 500,
                "max_context_tokens": 1500,
            }
        ],
    }


def _extraction_profile() -> dict[str, object]:
    return {
        "model_sha256": "1" * 64,
        "prompt_sha256": "2" * 64,
        "rule_sha256": "3" * 64,
        "synthetic": True,
        "replicate_id": 1,
        "extraction_epoch": 1,
    }


def test_absent_parser_profile_preserves_run_service_fail_closed_defaults() -> None:
    from proofops_api.local_runtime import load_local_runtime

    assert load_local_runtime({}) == {}


def test_parser_profile_and_budget_limits_are_typed_from_strict_snapshots(tmp_path: Path) -> None:
    from proofops.application.budget import BudgetLimits
    from proofops.application.ingest.graph_fusion import ParserProfile
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings_path = _write_json(
        tmp_path / "run.json",
        {
            "build_root": str(tmp_path),
            "budget_limits": _budget_limits(),
        },
    )

    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
            "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
        }
    )

    assert runtime["parser_profile"] == _parser_snapshot()
    assert (
        runtime["parser_profile_hash"]
        == ParserProfile("00000000-0000-4000-8000-000000000000", **_parser_snapshot()).config_hash()
    )
    assert isinstance(runtime["budget_limits"], BudgetLimits)


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": True},
        {"build_result": {"passed": True}},
        {"budget_limits": {"input_tokens": 1}},
        {
            "budget_limits": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "max_attempts": 3,
                "roles": [
                    {
                        "role": "tagger",
                        "max_calls": 3,
                        "max_input_tokens": 1000,
                        "max_output_tokens": 500,
                        "max_context_tokens": 1500,
                    },
                    {
                        "role": "ignored",
                        "max_calls": 1,
                        "max_input_tokens": 1,
                        "max_output_tokens": 1,
                        "max_context_tokens": 1,
                        "unknown": True,
                    },
                ],
            }
        },
    ],
)
def test_run_settings_reject_unknown_or_incomplete_values(tmp_path: Path, settings: dict) -> None:
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    if "budget_limits" in settings:
        settings = {**settings, "build_root": str(tmp_path)}
    settings_path = _write_json(tmp_path / "run.json", settings)

    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(
            {
                "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
                "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
            }
        )


def test_supplied_build_root_is_rechecked_instead_of_accepting_ready_json(tmp_path: Path) -> None:
    from proofops.application.supply_chain import SupplyChainResult
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings_path = _write_json(
        tmp_path / "run.json", {"build_root": str(tmp_path), "build_result": {"passed": True}}
    )

    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(
            {
                "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
                "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
            }
        )

    settings_path = _write_json(
        tmp_path / "run.json", {"build_root": str(tmp_path), "budget_limits": _budget_limits()}
    )
    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
            "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
        }
    )
    assert isinstance(runtime["build_result"], SupplyChainResult)
    assert runtime["build_result"].passed is False


@pytest.mark.parametrize("name", ["LOCAL_EXTRACTION_MODE", "LOCAL_TAGGING_MODE"])
def test_modes_require_explicit_supported_local_synthetic_configuration(
    tmp_path: Path, name: str
) -> None:
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime({"LOCAL_PARSER_PROFILE_PATH": str(parser_path), name: "unexpected"})


def test_extraction_profile_uses_shared_application_type_without_an_agent_import(
    tmp_path: Path,
) -> None:
    from proofops.application.claims import ExtractionProfile
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings_path = _write_json(
        tmp_path / "run.json",
        {
            "build_root": str(tmp_path),
            "budget_limits": _budget_limits(),
            "extraction_profile": _extraction_profile(),
        },
    )

    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
            "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
            "LOCAL_EXTRACTION_MODE": "local_synthetic",
        }
    )
    assert isinstance(runtime["extraction_profile"], ExtractionProfile)
    assert runtime["extraction_profile"].synthetic is True
    assert runtime["extraction_mode"] == "local_synthetic"


def test_tagging_mode_requires_explicit_typed_settings_without_defaults(tmp_path: Path) -> None:
    from proofops.application.tagging.service import TaggingSettings
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    missing_path = _write_json(tmp_path / "missing.json", {})
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(
            {
                "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
                "LOCAL_RUN_SETTINGS_PATH": str(missing_path),
                "LOCAL_TAGGING_MODE": "local_synthetic",
            }
        )

    settings_path = _write_json(
        tmp_path / "tagging.json",
        {
            "build_root": str(tmp_path),
            "budget_limits": _budget_limits(),
            "extraction_profile": _extraction_profile(),
            "tagging_settings": {
                "binding": {"binding_id": "runtime-binding", "role": "tagger", "synthetic": True},
                "model_id": "synthetic-local-model",
                "model_profile": "synthetic-local-profile",
                "region": "local",
                "system_prompt": "tag only",
                "schema_json": '{"type":"object"}',
                "max_tokens": 100,
                "temperature": 0.0,
                "extraction_epoch": 1,
                "max_response_bytes": 1024,
            },
        },
    )
    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
            "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
            "LOCAL_EXTRACTION_MODE": "local_synthetic",
            "LOCAL_TAGGING_MODE": "local_synthetic",
        }
    )
    assert isinstance(runtime["tagging_settings"], TaggingSettings)
    assert runtime["tagging_settings"].binding.synthetic is True
    assert runtime["tagging_mode"] == "local_synthetic"


def test_m2_auto_link_error_survives_local_settings_wrapper(tmp_path: Path) -> None:
    from proofops.application.tagging.report_level_link import POLICY, POLICY_HASH
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings_path = _write_json(
        tmp_path / "run.json",
        {
            "build_root": str(tmp_path),
            "budget_limits": _budget_limits(),
            "report_level_link": {
                "policy": POLICY,
                "policy_hash": POLICY_HASH,
                "refs": {"M2": [{}]},
            },
        },
    )
    with pytest.raises(ValueError, match="REPORT_LEVEL_LINK_M3_ONLY"):
        load_local_runtime(
            {
                "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
                "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
                "LOCAL_TAGGING_MODE": "local_synthetic",
            }
        )


def test_settings_reject_duplicate_json_keys(tmp_path: Path) -> None:
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings_path = tmp_path / "run.json"
    settings_path.write_text(
        '{"build_root":"' + str(tmp_path) + '","build_root":"' + str(tmp_path) + '"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(
            {
                "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
                "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
            }
        )


def _probe_extraction_profile() -> dict[str, object]:
    profile = dict(_extraction_profile())
    profile["synthetic"] = False
    return profile


def test_run_settings_accept_approved_extraction_opt_ins(tmp_path: Path) -> None:
    """R03d/R03f/R14: the pilot-written year/context/source-id flags validate."""
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings_path = _write_json(
        tmp_path / "run.json",
        {
            "build_root": str(tmp_path),
            "budget_limits": _budget_limits(),
            "extraction_profile": _probe_extraction_profile(),
            "extraction_limits": {"max_calls": 2, "max_output_tokens": 128},
            "extraction_year_notation": True,
            "extraction_context": True,
            "extraction_source_ids": True,
        },
    )
    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
            "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
            "LOCAL_EXTRACTION_MODE": "upstage_probe",
        }
    )
    assert runtime["extraction_mode"] == "upstage_probe"
    assert runtime["extraction_profile"].synthetic is False


@pytest.mark.parametrize("content_bounds", [False, True])
@pytest.mark.parametrize("complete_selection", [False, True])
def test_run_settings_accept_the_assertion_prompt_opt_in(
    tmp_path: Path, complete_selection, content_bounds
) -> None:
    """R20 fix 2: assertion prompt validates when paired with source-ids in probe mode."""
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings_path = _write_json(
        tmp_path / "run.json",
        {
            "build_root": str(tmp_path),
            "budget_limits": _budget_limits(),
            "extraction_profile": _probe_extraction_profile(),
            "extraction_limits": {"max_calls": 2, "max_output_tokens": 128},
            "extraction_source_ids": True,
            "extraction_assertion_prompt": True,
            **({"extraction_complete_selection": True} if complete_selection else {}),
            **({"extraction_content_bounds": True} if content_bounds else {}),
        },
    )
    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
            "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
            "LOCAL_EXTRACTION_MODE": "upstage_probe",
        }
    )
    assert runtime["extraction_mode"] == "upstage_probe"
    assert runtime["extraction_profile"].synthetic is False


@pytest.mark.parametrize(
    "settings,mode",
    [
        ({"extraction_year_notation": False}, "upstage_probe"),
        ({"extraction_year_notation": "yes"}, "upstage_probe"),
        ({"extraction_context": 1}, "upstage_probe"),
        ({"extraction_source_ids": False}, "upstage_probe"),
        ({"extraction_source_ids": "yes"}, "upstage_probe"),
        ({"extraction_source_ids": True}, "local_synthetic"),
        ({"extraction_year_notation": True}, "local_synthetic"),
        ({"extraction_context": True}, "local_synthetic"),
        ({"extraction_context": True}, ""),
        # R20 fix 2: assertion prompt is a real-probe opt-in that requires
        # source-id selection; every invalid shape fails closed.
        ({"extraction_assertion_prompt": "yes", "extraction_source_ids": True}, "upstage_probe"),
        ({"extraction_assertion_prompt": False, "extraction_source_ids": True}, "upstage_probe"),
        ({"extraction_assertion_prompt": True, "extraction_source_ids": True}, "local_synthetic"),
        ({"extraction_assertion_prompt": True}, "upstage_probe"),
        ({"extraction_content_bounds": True}, "upstage_probe"),
        ({"extraction_content_bounds": False, "extraction_source_ids": True}, "upstage_probe"),
        ({"extraction_content_bounds": "yes", "extraction_source_ids": True}, "upstage_probe"),
        ({"extraction_content_bounds": True, "extraction_source_ids": True}, "local_synthetic"),
        ({"extraction_complete_selection": True}, "upstage_probe"),
        ({"extraction_complete_selection": True, "extraction_source_ids": True}, "upstage_probe"),
        ({"extraction_complete_selection": False}, "upstage_probe"),
        ({"extraction_complete_selection": "yes"}, "upstage_probe"),
        (
            {
                "extraction_complete_selection": True,
                "extraction_source_ids": True,
                "extraction_assertion_prompt": True,
            },
            "local_synthetic",
        ),
    ],
)
def test_run_settings_reject_bad_extraction_opt_ins(
    tmp_path: Path, settings: dict, mode: str
) -> None:
    """Non-True values, or opt-ins outside the real probe mode, fail closed."""
    from proofops_api.local_runtime import load_local_runtime

    parser_path = _write_json(tmp_path / "parser.json", _parser_snapshot())
    base = {
        "build_root": str(tmp_path),
        "budget_limits": _budget_limits(),
        "extraction_profile": _probe_extraction_profile(),
        "extraction_limits": {"max_calls": 2, "max_output_tokens": 128},
    }
    base.update(settings)
    settings_path = _write_json(tmp_path / "run.json", base)
    env = {
        "LOCAL_PARSER_PROFILE_PATH": str(parser_path),
        "LOCAL_RUN_SETTINGS_PATH": str(settings_path),
        "LOCAL_EXTRACTION_MODE": mode,
    }
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(env)
