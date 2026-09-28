"""Independent OCR may corroborate only an otherwise valid native paragraph."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.adapters.local import source_verification
from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_parsing import TENANT, candidate, pdf


@pytest.mark.parametrize(
    "fault", ["none", "native_text", "clipped", "external_text", "native_receipt"]
)
def test_visibility_requires_both_native_gates_and_exact_external_reading(monkeypatch, fault):
    from evaluation.native_raster_visibility import corroborate_native_visibility
    from evaluation.raster_ocr import prepare_raster_ocr

    source = pdf()
    text = "Page 1 emissions 1234 tCO2e"
    box = (80, 710, 300, 740) if fault == "clipped" else (70, 710, 300, 740)
    batch = replace(
        candidate(
            "fixture",
            [
                (
                    "p",
                    "paragraph",
                    text if fault != "native_text" else text.replace("1234", "1235"),
                    box,
                    (),
                )
            ],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    monkeypatch.setattr(
        source_verification,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="unresolved OCR"),
    )
    native = source_verification.attest_native_sources(
        graph, source, tenant_id=TENANT, geometry_mode="glyph"
    )
    assert native["records"][0]["status"] == "unresolved"
    submitted, request = prepare_raster_ocr(
        graph, source, (graph.blocks[0].source_id,), tenant_id=TENANT
    )
    body = dict(
        model="document-parse-260128",
        mode="standard",
        pdf_sha256=sha256(submitted).hexdigest(),
        pages=1,
        bytes_len=len(submitted),
    )
    raw = dict(
        model=body["model"],
        usage=dict(pages=1, standard=[1]),
        elements=[
            dict(
                id=0,
                page=1,
                content=dict(
                    text=text if fault != "external_text" else text.replace("1234", "1235")
                ),
            )
        ],
    )
    receipt = dict(
        model=body["model"],
        provider_model=body["model"],
        mode="standard",
        pages=1,
        request_sha256=canonical_hash(body),
        response_sha256=canonical_hash(raw),
        raw_response=raw,
    )
    pins = dict(
        request_sha256=canonical_hash(request),
        receipt_sha256=canonical_hash(receipt),
        tenant_id=TENANT,
    )
    if fault == "native_receipt":
        native = deepcopy(native)
        native["records"][0]["reason"] = "made_up"
    if fault in {"native_text", "clipped", "native_receipt"}:
        with pytest.raises(ValueError):
            corroborate_native_visibility(native, request, receipt, graph, source, **pins)
    else:
        result, proof = corroborate_native_visibility(
            native, request, receipt, graph, source, **pins
        )
        assert result.blocks[0].quality == ("verified" if fault == "none" else "unverified")
        assert graph.blocks[0].quality == "unverified"
        assert proof["corroborated_source_ids"] == (
            [graph.blocks[0].source_id] if fault == "none" else []
        )
        assert result.blocks[0].raw_text == graph.blocks[0].raw_text

        assert (
            canonical_hash({k: v for k, v in proof.items() if k != "artifact_sha256"})
            == proof["artifact_sha256"]
        )
