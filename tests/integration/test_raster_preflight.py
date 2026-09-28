"""A text-model grant must never implicitly authorize raster document upload."""

import pytest
from proofops.application.authorization import TenantNotFoundError
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_preflight import AUTH, NOW
from tests.integration.test_upstage_runtime import profiles


def configured():
    binding, consent = profiles()
    binding.update(
        schema="local_upstage_raster_binding_v1",
        role="vision",
        model_id="document-parse-260128",
        endpoint="https://api.upstage.ai/v1/document-digitization",
        mode="standard",
        max_pages=10,
        accepts_images=True,
        image_input_verified=True,
    )
    consent["allow_raster_upload"] = True
    return dict(
        binding=binding,
        consent=consent,
        auth=AUTH,
        checked_at=NOW,
        source_sha256="a" * 64,
        document_rights="report-test",
        model_sha256=canonical_hash(
            dict(model=binding["model_id"], provider="upstage", transport="UpstageParseProbe")
        ),
    )


def check(**args):
    from proofops.application.preflight import check_local_upstage_raster

    return check_local_upstage_raster(**args)


def test_distinct_raster_authorization_keeps_region_and_probe_unverified():
    from proofops.application.preflight import check_local_upstage_binding

    args = configured()
    result = check(**args)
    assert result.ready
    assert {c.name for c in result.checks if c.status == "not_run"} == {
        "processing_regions",
        "live_model_probe",
    }
    assert not check_local_upstage_binding(
        **{k: v for k, v in args.items() if k != "document_rights"}
    ).ready
    assert not check(**(args | dict(include_live_model_probe=True))).ready


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "extractor"),
        ("role", "tagger"),
        ("model_id", "document-parse"),
        ("mode", "auto"),
        ("max_pages", True),
        ("max_pages", 11),
        ("schema", "wrong"),
        ("status", "revoked"),
        ("expires_at", NOW),
        ("accepts_images", False),
        ("image_input_verified", False),
        ("endpoint", "https://api.upstage.ai/v1/chat/completions"),
        ("fallback_bindings", ["other"]),
    ],
)
def test_bad_raster_binding_is_denied(field, value):
    args = configured()
    args["binding"][field] = value
    assert not check(**args).ready


@pytest.mark.parametrize(
    "fault", ["image_consent", "source", "rights", "rights_missing", "model_hash"]
)
def test_raster_requires_explicit_image_and_actual_document_scope(fault):
    args = configured()
    if fault == "image_consent":
        args["consent"].pop("allow_raster_upload")
    elif fault == "source":
        args["source_sha256"] = "b" * 64
    elif fault == "rights":
        args["document_rights"] = "different-rights"
    elif fault == "rights_missing":
        args["consent"]["allowed_document_rights"] = None
    else:
        args["model_sha256"] = "0" * 64
    assert not check(**args).ready


def test_raster_foreign_profile_is_hidden():
    args = configured()
    args["binding"]["tenant_id"] = "00000000-0000-4000-8000-000000000099"
    with pytest.raises(TenantNotFoundError):
        check(**args)


def test_raster_contract_matches_pinned_transport_without_dependency_reversal():
    from proofops.adapters.local.upstage_parse import MAX_PAGES, PARSE_MODEL_PINNED

    binding = configured()["binding"]
    assert binding["model_id"] == PARSE_MODEL_PINNED
    assert binding["max_pages"] == MAX_PAGES
