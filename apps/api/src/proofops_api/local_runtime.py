"""Explicit, local-only RunService configuration; absent input stays fail-closed."""

from __future__ import annotations

import json
import math
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from proofops.application.budget import BudgetLimits, RoleLimit
from proofops.application.claim_scope import MAX_EXTRACTION_BATCH_CALLS
from proofops.application.claims import ExtractionProfile
from proofops.application.ingest.graph_fusion import ParserProfile
from proofops.application.ports.models import ModelBinding
from proofops.application.runs import validate_raster_policy
from proofops.application.supply_chain import verify_supply_chain
from proofops.application.tagging.service import TaggingSettings

_MANIFEST_ID = "00000000-0000-4000-8000-000000000000"
_MAX_CONFIG_BYTES = 65_536
_SETTINGS_FIELDS = frozenset(
    {
        "build_root",
        "budget_limits",
        "extraction_profile",
        "extraction_year_notation",
        "extraction_context",
        "extraction_table_context",
        "extraction_source_ids",
        "extraction_assertion_prompt",
        "extraction_complete_selection",
        "extraction_content_bounds",
        "position_context_order",
        "tagging_settings",
        "preliminary_settings",
        "relation_settings",
        "input_reservation_policy",
        "extraction_limits",
        "claim_source_policy",
        "raster_runtime_binding_id",
        "raster_policy",
        "report_level_link",
    }
)
_REQUIRED_SETTINGS_FIELDS = frozenset({"build_root", "budget_limits"})
_BUDGET_FIELDS = frozenset({"input_tokens", "output_tokens", "max_attempts", "roles"})
_ROLE_FIELDS = frozenset(
    {"role", "max_calls", "max_input_tokens", "max_output_tokens", "max_context_tokens"}
)
_REQUIRED_TAGGING_FIELDS = frozenset(
    {
        "binding",
        "model_id",
        "model_profile",
        "region",
        "system_prompt",
        "schema_json",
    }
)
_OPTIONAL_TAGGING_FIELDS = frozenset(
    {
        "max_tokens",
        "temperature",
        "extraction_epoch",
        "max_response_bytes",
        "wire_policy_version",
    }
)
_TAGGING_FIELDS = _REQUIRED_TAGGING_FIELDS | _OPTIONAL_TAGGING_FIELDS
_BINDING_FIELDS = frozenset({"binding_id", "role", "synthetic"})
_EXTRACTION_FIELDS = frozenset(
    {
        "model_sha256",
        "prompt_sha256",
        "rule_sha256",
        "synthetic",
        "replicate_id",
        "extraction_epoch",
    }
)
_RASTER_FIELDS = frozenset({"raster_runtime_binding_id", "raster_policy"})


def _invalid() -> ValueError:
    return ValueError("LOCAL_RUNTIME_CONFIG_INVALID")


def _json_file(raw_path: str) -> dict[str, Any]:
    if not isinstance(raw_path, str) or not raw_path:
        raise _invalid()
    try:
        path = Path(raw_path)
        status = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(status.st_mode)
            or status.st_size > _MAX_CONFIG_BYTES
        ):
            raise _invalid()
        with path.open("rb") as source:
            raw = source.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES or len(raw) != status.st_size:
            raise _invalid()
        value = json.loads(raw, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _invalid()


def _strict_int(value: object, *, minimum: int = 1, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _budget(value: object) -> BudgetLimits:
    if not isinstance(value, dict) or set(value) != _BUDGET_FIELDS:
        raise _invalid()
    roles = value["roles"]
    if not isinstance(roles, list) or not 1 <= len(roles) <= 16:
        raise _invalid()
    if any(not isinstance(item, dict) or set(item) != _ROLE_FIELDS for item in roles):
        raise _invalid()
    try:
        return BudgetLimits(
            _strict_int(value["input_tokens"]),
            _strict_int(value["output_tokens"]),
            tuple(
                RoleLimit(
                    item["role"],
                    _strict_int(item["max_calls"]),
                    _strict_int(item["max_input_tokens"]),
                    _strict_int(item["max_output_tokens"]),
                    _strict_int(item["max_context_tokens"]),
                )
                for item in roles
            ),
            _strict_int(value["max_attempts"], maximum=3),
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid() from None


def _tagging(value: object, *, synthetic: bool = True) -> TaggingSettings:
    if (
        not isinstance(value, dict)
        or not _REQUIRED_TAGGING_FIELDS <= set(value)
        or set(value) - _TAGGING_FIELDS
    ):
        raise _invalid()
    binding = value["binding"]
    if (
        not isinstance(binding, dict)
        or set(binding) != _BINDING_FIELDS
        or binding.get("role") != "tagger"
        or binding.get("synthetic") is not synthetic
    ):
        raise _invalid()
    try:
        options = {key: value[key] for key in _OPTIONAL_TAGGING_FIELDS if key in value}
        if "max_tokens" in options:
            options["max_tokens"] = _strict_int(options["max_tokens"], maximum=1_048_576)
        if "extraction_epoch" in options:
            options["extraction_epoch"] = _strict_int(options["extraction_epoch"])
        if "wire_policy_version" in options:
            options["wire_policy_version"] = _strict_int(options["wire_policy_version"], maximum=2)
        if "max_response_bytes" in options:
            options["max_response_bytes"] = _strict_int(
                options["max_response_bytes"], maximum=1_048_576
            )
        if "temperature" in options:
            temperature = options["temperature"]
            if (
                type(temperature) not in (int, float)
                or not math.isfinite(temperature)
                or temperature != 0
            ):
                raise _invalid()
            options["temperature"] = float(temperature)
        fields = {key: value[key] for key in _REQUIRED_TAGGING_FIELDS if key != "binding"}
        return TaggingSettings(ModelBinding(**binding), **(fields | options))
    except (KeyError, TypeError, ValueError):
        raise _invalid() from None


def _reservation_policy(value: object) -> dict[str, Any]:
    """Freeze an explicit capacity reservation; absence stays fail-closed downstream."""
    if not isinstance(value, dict) or not value:
        raise _invalid()
    try:
        frozen = json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError):
        raise _invalid() from None
    if not isinstance(frozen, dict) or frozen != dict(value):
        raise _invalid()
    return frozen


def _extraction(value: object, *, synthetic=True) -> ExtractionProfile:
    if (
        not isinstance(value, dict)
        or set(value) != _EXTRACTION_FIELDS
        or value.get("synthetic") is not synthetic
    ):
        raise _invalid()
    try:
        return ExtractionProfile(**value)
    except (TypeError, ValueError):
        raise _invalid() from None


def _raster(settings: Mapping[str, Any], extraction_mode: str) -> dict[str, Any]:
    present = _RASTER_FIELDS & set(settings)
    if not present:
        return {}
    if present != _RASTER_FIELDS or extraction_mode != "upstage_probe":
        raise _invalid()
    binding_id = settings["raster_runtime_binding_id"]
    try:
        if (
            not isinstance(binding_id, str)
            or not binding_id
            or str(UUID(binding_id)) != binding_id.lower()
        ):
            raise ValueError
        policy = validate_raster_policy(settings["raster_policy"])
    except (TypeError, ValueError):
        raise _invalid() from None
    return {"raster_runtime_binding_id": binding_id, "raster_policy": policy}


def load_local_runtime(env: Mapping[str, str]) -> dict[str, Any]:
    """Return only trusted RunService kwargs; no config leaves its gate closed."""
    parser_path = env.get("LOCAL_PARSER_PROFILE_PATH")
    settings_path = env.get("LOCAL_RUN_SETTINGS_PATH")
    extraction_mode = env.get("LOCAL_EXTRACTION_MODE", "")
    tagging_mode = env.get("LOCAL_TAGGING_MODE", "")
    fact_assembly_profile = env.get("LOCAL_FACT_ASSEMBLY_PROFILE", "strict-v1")
    if fact_assembly_profile not in ("strict-v1", "partial-facts-v1"):
        raise _invalid()
    if not parser_path:
        if settings_path or extraction_mode or tagging_mode or fact_assembly_profile != "strict-v1":
            raise _invalid()
        return {}
    if extraction_mode not in {"", "local_synthetic", "upstage_probe"} or tagging_mode not in {
        "",
        "local_synthetic",
        "upstage_local",
    }:
        raise _invalid()
    profile = _json_file(parser_path)
    try:
        parsed = ParserProfile(_MANIFEST_ID, **profile)
    except (TypeError, ValueError):
        raise _invalid() from None
    if parsed.config_snapshot() != profile:
        raise _invalid()
    runtime: dict[str, Any] = {
        "parser_profile": profile,
        "parser_profile_hash": parsed.config_hash(),
    }
    settings = _json_file(settings_path) if settings_path else {}
    if set(settings) - _SETTINGS_FIELDS or (
        settings_path and not _REQUIRED_SETTINGS_FIELDS <= set(settings)
    ):
        raise _invalid()
    runtime.update(_raster(settings, extraction_mode))
    if "report_level_link" in settings:
        from proofops.application.tagging.report_level_link import validate_config

        if not tagging_mode:
            raise _invalid()
        try:
            runtime["report_level_link"] = validate_config(settings["report_level_link"])
        except ValueError as exc:
            if str(exc) == "REPORT_LEVEL_LINK_M3_ONLY":
                raise ValueError(
                    "LOCAL_RUNTIME_CONFIG_INVALID: REPORT_LEVEL_LINK_M3_ONLY"
                ) from None
            raise _invalid() from None
    if "claim_source_policy" in settings:
        from proofops.adapters.local.claim_source_policies import claim_source_reader

        if extraction_mode != "upstage_probe":
            raise _invalid()
        try:
            claim_source_reader(settings["claim_source_policy"])
        except ValueError:
            raise _invalid() from None
        runtime["claim_source_policy"] = settings["claim_source_policy"]
    if "build_root" in settings:
        root = settings["build_root"]
        if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
            raise _invalid()
        runtime["build_result"] = verify_supply_chain(
            root_dir=root,
            env={"ENABLE_LEGACY_PYMUPDF": env.get("ENABLE_LEGACY_PYMUPDF", "false")},
        )
    for flag in (
        "extraction_year_notation",
        "extraction_context",
        "extraction_table_context",
        "extraction_source_ids",
        "extraction_assertion_prompt",
        "extraction_complete_selection",
        "extraction_content_bounds",
    ):
        # Approved extraction opt-ins: explicit True only, and
        # only with the real probe mode. Validated here so the gate never rejects
        # a pilot-written value; the worker composition (not this runtime dict)
        # reconstructs the matching extractor profile from the settings file.
        if flag in settings and (settings[flag] is not True or extraction_mode != "upstage_probe"):
            raise _invalid()
    if "extraction_table_context" in settings and settings.get("extraction_context") is not True:
        # Table context refines the context profile; alone it has no wire.
        raise _invalid()
    if (
        "extraction_assertion_prompt" in settings
        and settings.get("extraction_source_ids") is not True
    ):
        # The assertion prompt refines source-ID selection; alone it has no wire.
        raise _invalid()
    if (
        "extraction_content_bounds" in settings
        and settings.get("extraction_source_ids") is not True
    ):
        raise _invalid()
    if "extraction_complete_selection" in settings and (
        settings.get("extraction_source_ids") is not True
        or settings.get("extraction_assertion_prompt") is not True
    ):
        raise _invalid()
    if "position_context_order" in settings:
        from proofops.application.tagging.preliminary import CONTEXT_POSITION_ORDER

        if (
            settings["position_context_order"] != CONTEXT_POSITION_ORDER
            or settings.get("extraction_context") is not True
            or (
                tagging_mode == "upstage_local"
                and (
                    not isinstance(settings.get("preliminary_settings"), dict)
                    or settings["preliminary_settings"].get("model_profile")
                    not in {
                        "upstage-preliminary-source-quotes-table-role-v1-position-v1",
                        "upstage-preliminary-source-quotes-table-role-v2-p2-position-v1",
                    }
                )
            )
        ):
            raise _invalid()
        runtime["position_context_order"] = CONTEXT_POSITION_ORDER
    if "budget_limits" in settings:
        runtime["budget_limits"] = _budget(settings["budget_limits"])
    if extraction_mode:
        if "extraction_profile" not in settings:
            raise _invalid()
        runtime["extraction_profile"] = _extraction(
            settings["extraction_profile"], synthetic=extraction_mode != "upstage_probe"
        )
        runtime["extraction_mode"] = extraction_mode
    elif "extraction_profile" in settings:
        raise _invalid()
    if extraction_mode == "upstage_probe":
        limits = settings.get("extraction_limits")
        if (
            tagging_mode not in ("", "upstage_local")
            or not isinstance(limits, dict)
            or set(limits)
            not in (
                {"max_calls", "max_output_tokens"},
                {
                    "max_calls",
                    "max_output_tokens",
                    "claim_pages",
                },
            )
        ):
            raise _invalid()
        runtime["extraction_limits"] = {
            "max_calls": _strict_int(limits["max_calls"], maximum=MAX_EXTRACTION_BATCH_CALLS),
            "max_output_tokens": _strict_int(limits["max_output_tokens"], maximum=1024),
        }
        if "claim_pages" in limits:
            # Structural check only; RunService verifies claim_pages ⊆ selected_pages
            # once the run body's declared subset is known.
            pages = limits["claim_pages"]
            if not isinstance(pages, list) or not pages or sorted(set(pages)) != pages:
                raise _invalid()
            runtime["extraction_limits"]["claim_pages"] = [_strict_int(page) for page in pages]
        if tagging_mode == "upstage_local":
            if (
                "preliminary_settings" not in settings
                or "tagging_settings" not in settings
                or "input_reservation_policy" not in settings
            ):
                raise _invalid()
            runtime["preliminary_settings"] = _tagging(
                settings["preliminary_settings"], synthetic=False
            )
            runtime["tagging_settings"] = _tagging(settings["tagging_settings"], synthetic=False)
            if "relation_settings" in settings:
                runtime["relation_settings"] = _tagging(
                    settings["relation_settings"], synthetic=False
                )
            runtime["input_reservation_policy"] = _reservation_policy(
                settings["input_reservation_policy"]
            )
            runtime["tagging_mode"] = tagging_mode
    elif "extraction_limits" in settings:
        raise _invalid()
    if tagging_mode == "upstage_local":
        if extraction_mode != "upstage_probe":
            raise _invalid()
    elif tagging_mode:
        if not extraction_mode or "tagging_settings" not in settings:
            raise _invalid()
        runtime["tagging_settings"] = _tagging(settings["tagging_settings"])
        runtime["tagging_mode"] = tagging_mode
    elif "tagging_settings" in settings or "relation_settings" in settings:
        raise _invalid()
    if fact_assembly_profile != "strict-v1":
        if not tagging_mode:
            raise _invalid()
        runtime["fact_assembly_profile"] = fact_assembly_profile
    if tagging_mode != "upstage_local" and (
        "preliminary_settings" in settings
        or "relation_settings" in settings
        or "input_reservation_policy" in settings
    ):
        raise _invalid()
    return runtime
