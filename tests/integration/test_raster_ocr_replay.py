"""Raster correspondence is replayable without giving OCR source-approval authority."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.domain.provenance import canonical_hash
from pypdf import PdfReader

from tests.acceptance.test_parsing import FOREIGN, TENANT, candidate, pdf


def test_raster_request_and_receipt_replay_reject_tampering(monkeypatch):
    from evaluation.raster_ocr import prepare_raster_ocr, replay_raster_ocr

    source = pdf()
    batch = replace(
        candidate(
            "fixture", [("p", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    ids = (graph.blocks[0].source_id,)
    submitted, request = prepare_raster_ocr(graph, source, ids, tenant_id=TENANT)
    assert not PdfReader(BytesIO(submitted)).pages[0].extract_text()
    assert prepare_raster_ocr(graph, source, ids, tenant_id=TENANT) == (submitted, request)
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
        elements=[dict(id=0, page=1, content=dict(text=graph.blocks[0].raw_text))],
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
    result = replay_raster_ocr(request, receipt, graph, source, **pins)
    assert result[0]["exact_normalized_match"] is True
    assert graph.blocks[0].quality == "unverified"
    for target, key, value in [
        ("request", "source_sha256", "0" * 64),
        ("request", "rows", []),
        ("receipt", "response_sha256", "0" * 64),
        ("receipt", "provider_model", "wrong"),
    ]:
        a, b = deepcopy(request), deepcopy(receipt)
        (a if target == "request" else b)[key] = value
        with pytest.raises(ValueError):
            replay_raster_ocr(a, b, graph, source, **pins)
    with pytest.raises(ValueError):
        replay_raster_ocr(request, receipt, graph, source + b"changed", **pins)
    with pytest.raises(ValueError):
        replay_raster_ocr(request, receipt, graph, source, **(pins | dict(tenant_id=FOREIGN)))
    for fault in (
        "wrong_page",
        "duplicate_element",
        "text_change",
        "model",
        "model_alias",
        "billing_bool",
    ):
        altered = deepcopy(receipt)
        raw = altered["raw_response"]
        if fault == "wrong_page":
            raw["elements"][0]["page"] = True
        elif fault == "duplicate_element":
            raw["elements"] *= 2
        elif fault == "text_change":
            raw["elements"][0]["content"]["text"] = "Page 1 emissions 1235 tCO2e"
        elif fault == "model":
            raw["model"] = "wrong"
        elif fault == "model_alias":
            raw["model"] = altered["provider_model"] = "document-parse"
        else:
            raw["usage"]["standard"] = [True]
        altered["response_sha256"] = canonical_hash(raw)
        newpins = pins | dict(receipt_sha256=canonical_hash(altered))
        if fault == "text_change":
            assert (
                replay_raster_ocr(request, altered, graph, source, **newpins)[0][
                    "exact_normalized_match"
                ]
                is False
            )
        else:
            with pytest.raises(ValueError):
                replay_raster_ocr(request, altered, graph, source, **newpins)

    for fault in (
        "raw_none",
        "usage_none",
        "element_none",
        "oversized",
        "response_oversized",
        "mode_list",
        "provider_list",
    ):
        altered = deepcopy(receipt)
        if fault == "raw_none":
            altered["raw_response"] = None
        elif fault == "usage_none":
            altered["raw_response"]["usage"] = None
        elif fault == "element_none":
            altered["raw_response"]["elements"] = [None]
        elif fault == "response_oversized":
            altered["raw_response"]["extra"] = "X" * 1048577
        elif fault == "mode_list":
            altered["mode"] = []
        elif fault == "provider_list":
            altered["provider_model"] = []
        else:
            altered["raw_response"]["elements"][0]["content"]["text"] = "X" * 200001
        altered["response_sha256"] = canonical_hash(altered["raw_response"])
        with pytest.raises(ValueError):
            replay_raster_ocr(
                request,
                altered,
                graph,
                source,
                **(pins | dict(receipt_sha256=canonical_hash(altered))),
            )

    from contextlib import nullcontext
    from types import SimpleNamespace

    import proofops.adapters.local.raster_ocr as raster

    for width in (float("nan"), float("inf"), True):
        fake = SimpleNamespace(
            pages=[SimpleNamespace(width=width, height=800, bbox=(0, 0, width, 800), rotation=0)]
        )
        with monkeypatch.context() as patch:
            patch.setattr(raster.pdfplumber, "open", lambda *a, **k: nullcontext(fake))
            with pytest.raises(ValueError):
                prepare_raster_ocr(graph, source, ids, tenant_id=TENANT)
