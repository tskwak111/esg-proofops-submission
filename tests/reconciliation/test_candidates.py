from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from proofops.adapters.dart import ArtifactStore, build_collection_manifest, create_artifact_entry
from proofops.adapters.dart.candidates import (
    MAX_DECLARED_ROWSPAN,
    CandidatePreparationError,
    add_operator_sr_sources,
    build_candidate_catalog,
)
from proofops.adapters.reconciliation import FileSourceReader
from proofops.adapters.reconciliation.files import SourceReadError

from evaluation.reconciliation_prepare import prepare_review_draft

CORP = "00126380"
FY = 2024
RECEIPT = "20250311001085"
VERSION = "fs-v1"


def zipped(entries: list[tuple[str, bytes]]) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return result.getvalue()


def collection(tmp_path: Path, *, document: bytes | None = None, statements: bytes | None = None):
    store = ArtifactStore(tmp_path / "originals")
    statements = (
        statements
        or json.dumps(
            {
                "status": "000",
                "message": "OK",
                "list": [
                    {
                        "rcept_no": RECEIPT,
                        "reprt_code": "11011",
                        "bsns_year": str(FY),
                        "corp_code": CORP,
                        "sj_div": "BS",
                        "sj_nm": "Balance sheet",
                        "account_id": "ifrs-full_Assets",
                        "account_nm": "Assets",
                        "account_detail": "-",
                        "thstrm_nm": "2024",
                        "thstrm_amount": "123",
                        "frmtrm_nm": "2023",
                        "frmtrm_amount": "100",
                        "bfefrmtrm_nm": "2022",
                        "bfefrmtrm_amount": "90",
                        "ord": "1",
                        "currency": "KRW",
                    }
                ],
            },
            allow_nan=False,
        ).encode()
    )
    document = document or zipped(
        [(f"{RECEIPT}.xml", "<?xml version='1.0'?><doc><p>원문 문장</p></doc>".encode())]
    )
    xbrl = zipped(
        [
            (
                "entity00126380_2024-12-31.xbrl",
                b"<xbrl><Amount id='fact-1' contextRef='c1' unitRef='KRW'>123</Amount></xbrl>",
            )
        ]
    )
    entries = []
    for kind, payload, ext in (
        ("statements", statements, "json"),
        ("document", document, "zip"),
        ("xbrl", xbrl, "zip"),
    ):
        digest = store.store(payload, ext=ext)
        entries.append(
            create_artifact_entry(
                f"{VERSION}:{kind}",
                VERSION,
                CORP,
                FY,
                artifact_sha256=digest,
                rcept_no=RECEIPT,
                consolidation="consolidated",
                locator=store.path_for(digest, ext=ext).relative_to(store.root).as_posix(),
                fetched_at="2026-09-21T00:00:00Z",
            )
        )
    manifest = build_collection_manifest(
        "manifest-1",
        "package-1",
        entries,
        synthetic=False,
        fetched_at="2026-09-21T00:00:00Z",
    )
    return store, manifest


def prepare(store, manifest, **kwargs):
    return build_candidate_catalog(
        manifest,
        store,
        corp_code=CORP,
        fy=FY,
        rcept_no=RECEIPT,
        consolidation="consolidated",
        **kwargs,
    )


def test_actual_xml_without_ids_is_an_explicit_derived_utf8_projection(tmp_path):
    store, manifest = collection(tmp_path)
    result = prepare(store, manifest)
    candidate = next(
        c for c in result.catalog["candidates"] if c["candidate_type"] == "document_element"
    )
    assert candidate["lineage"]["representation"] == "derived"
    assert (
        candidate["lineage"]["original_artifact_sha256"]
        == manifest["artifacts"][1]["artifact_sha256"]
    )
    assert candidate["lineage"]["zip_member"] == f"{RECEIPT}.xml"
    assert candidate["lineage"]["transformation_locator"].startswith("xml-path:/")
    assert candidate["source"]["locator"].startswith("chars:")
    assert "original_locator" not in candidate["lineage"]
    artifact = next(
        a for a in result.artifacts if a.document_id == candidate["source"]["document_id"]
    )
    assert artifact.payload.decode("utf-8")
    assert artifact.sha256 == candidate["source"]["artifact_sha256"]


def test_unique_xbrl_id_uses_unchanged_member_bytes(tmp_path):
    store, manifest = collection(tmp_path)
    result = prepare(store, manifest)
    fact = next(c for c in result.catalog["candidates"] if c["candidate_type"] == "xbrl_fact")
    assert fact["source"]["locator"] == "id:fact-1"
    assert fact["lineage"]["representation"] == "original_member"
    artifact = next(a for a in result.artifacts if a.document_id == fact["source"]["document_id"])
    assert artifact.payload.startswith(b"<xbrl>")
    assert artifact.sha256 == fact["lineage"]["member_sha256"]


@pytest.mark.parametrize(
    "document",
    [
        zipped([("../escape.xml", b"<x/>")]),
        zipped([("same.xml", b"<x/>"), ("same.xml", b"<y/>")]),
    ],
)
def test_malicious_or_duplicate_zip_members_are_rejected(tmp_path, document):
    store, manifest = collection(tmp_path, document=document)
    with pytest.raises(CandidatePreparationError, match="zip_rejected"):
        prepare(store, manifest)


def test_original_source_hash_tamper_is_rejected(tmp_path):
    store, manifest = collection(tmp_path)
    entry = manifest["artifacts"][0]
    store.path_for(entry["artifact_sha256"], ext="json").write_bytes(b"tampered")
    with pytest.raises(CandidatePreparationError, match="artifact_hash_or_read_failure"):
        prepare(store, manifest)


@pytest.mark.parametrize("field,value", [("corp_code", "00000000"), ("rcept_no", "20240101000000")])
def test_manifest_company_and_receipt_must_match_pinned_identity(tmp_path, field, value):
    store, manifest = collection(tmp_path)
    changed = copy.deepcopy(manifest)
    changed["artifacts"][0][field] = value
    with pytest.raises(CandidatePreparationError, match="identity_or_status_mismatch"):
        prepare(store, changed)


def test_nonfinite_statement_number_is_rejected_not_normalized(tmp_path):
    payload = (
        b'{"status":"000","list":[{"corp_code":"00126380",'
        b'"bsns_year":"2024","rcept_no":"20250311001085","thstrm_amount":NaN}]}'
    )
    store, manifest = collection(tmp_path, statements=payload)
    with pytest.raises(CandidatePreparationError, match="non_finite_json_number"):
        prepare(store, manifest)


def test_missing_operator_choices_stay_candidate_only_without_false_trust(tmp_path):
    store, manifest = collection(tmp_path)
    result = prepare(store, manifest)
    draft = prepare_review_draft(result, {"schema_version": "reconciliation-operator-selections-1"})
    assert draft["status"] == "candidate_only"
    assert draft["review_state"] == "pending"
    assert draft["policy_approved"] is False
    assert "packet" not in draft
    assert result.catalog["trust"] == {
        "reviewed": False,
        "policy_approved": False,
        "search_complete": False,
        "notice": "Candidates and normalization suggestions require operator review.",
    }


def test_candidate_limit_is_enforced_and_reported(tmp_path):
    store, manifest = collection(tmp_path)
    result = prepare(store, manifest, max_candidates=1)
    assert len(result.catalog["candidates"]) == 1
    assert result.catalog["limits"] == {
        "max_candidates": 1,
        "truncated": True,
        "max_row_span_candidates": 200,
        "row_span_candidate_count": 0,
    }
    with pytest.raises(CandidatePreparationError, match="candidate_limit_invalid"):
        prepare(store, manifest, max_candidates=0)


def test_unknown_or_unreadable_xml_encoding_is_rejected(tmp_path):
    document = zipped(
        [(f"{RECEIPT}.xml", b"<?xml version='1.0' encoding='made-up'?><doc>text</doc>")]
    )
    store, manifest = collection(tmp_path, document=document)
    with pytest.raises(CandidatePreparationError, match="xml_encoding_unknown"):
        prepare(store, manifest)


def sr_input(tmp_path: Path, identity: dict | None = None):
    payload = b"Samsung Electronics and its consolidated subsidiaries"
    path = tmp_path / "sr.txt"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    document = {
        "tenant_id": "tenant-1",
        "company_id": "company-1",
        "package_id": "package-1",
        "document_version_id": "sr-v1",
        "document_role": "sustainability",
        "artifact_sha256": digest,
        "corp_code": CORP,
        "fiscal_year": FY,
        "rcept_no": None,
        "consolidation": "consolidated",
        "published_at": "2025-06-27",
        "available_on": "2025-06-27",
        "as_of_date": "2025-06-30",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "synthetic": False,
    }
    if identity:
        document.update(identity)
    return {
        "schema_version": "reconciliation-sr-sources-1",
        "artifacts": {"sr-v1": {"path": "sr.txt", "format": "text", "sha256": digest}},
        "documents": {"sr-v1": document},
        "sources": [
            {
                "source_id": "sr-scope",
                "document_id": "sr-v1",
                "artifact_sha256": digest,
                "locator": f"chars:0:{len(payload.decode())}",
                "quote": payload.decode(),
                "lineage": {
                    "representation": "original",
                    "original_artifact_sha256": digest,
                    "transformation": "none",
                    "transformation_locator": "whole-file UTF-8 source",
                },
            }
        ],
    }


# The shape of the real Samsung FY2024 cover: one ROWSPAN="2" label cell over a
# first row holding the start date and a second row holding the end date.
COVER = (
    "<?xml version='1.0'?>"
    "<DOCUMENT><BODY><COVER><TABLE-GROUP><TABLE><TBODY>"
    '<TR ACOPY=N><TD ROWSPAN="2" ACLASS=NORMAL>사업연도</TD>'
    '<TU AUNIT="PERIODFROM">2024년 01월 01일</TU><TD>부터</TD></TR>'
    '<TR ACOPY=N><TU AUNIT="PERIODTO">2024년 12월 31일</TU><TD>까지</TD></TR>'
    "<TR ACOPY=N><TD>회사명</TD><TE>주식회사 예시</TE></TR>"
    "</TBODY></TABLE></TABLE-GROUP></COVER></BODY></DOCUMENT>"
)


def proprietary(tmp_path: Path, markup: str = COVER):
    """A DART-style member that is not well-formed XML, so the text projection runs."""
    return collection(tmp_path, document=zipped([(f"{RECEIPT}.xml", markup.encode("utf-8"))]))


def spans(result):
    return [
        item
        for item in result.catalog["candidates"]
        if item["candidate_type"] == "document_row_span"
    ]


def test_split_period_row_is_covered_by_one_exact_contiguous_span(tmp_path):
    """Both disclosed dates live in one quote whose locator and hash still verify."""
    store, manifest = proprietary(tmp_path / "dart")
    result = prepare(store, manifest)
    period = next(item for item in spans(result) if "사업연도" in item["source"]["quote"])
    quote = period["source"]["quote"]
    assert "2024년 01월 01일" in quote
    assert "2024년 12월 31일" in quote
    assert "회사명" not in quote and "주식회사 예시" not in quote
    assert period["verification_state"] == "candidate"
    assert period["normalization_suggestions"] == {}
    assert period["lineage"]["representation"] == "derived"
    assert period["lineage"]["zip_member"] == f"{RECEIPT}.xml"
    assert period["lineage"]["transformation_locator"].startswith("proprietary-markup-row-span:")
    assert period["raw"]["element_path"].endswith("/tr")
    assert period["raw"]["cell_texts"] == [
        "사업연도",
        "2024년 01월 01일",
        "부터",
        "2024년 12월 31일",
        "까지",
    ]

    root = tmp_path / "artifacts"
    root.mkdir()
    for artifact in result.artifacts:
        target = root / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.payload)
    reader = FileSourceReader(root, result.artifact_index)
    assert reader.validate(period["source"])

    projection = (root / result.artifact_index[period["source"]["document_id"]]["path"]).read_text(
        encoding="utf-8"
    )
    start, end = (int(part) for part in period["source"]["locator"].split(":")[1:])
    assert projection[start:end] == quote

    tampered = dict(period["source"])
    tampered["locator"] = f"chars:{start + 1}:{end}"
    with pytest.raises(SourceReadError):
        reader.validate(tampered)


def test_row_spans_never_join_two_rows_and_never_displace_existing_candidates(tmp_path):
    """Each disclosed row gets its own span, and the bounded catalogue is unchanged."""
    store, manifest = proprietary(tmp_path / "dart")
    result = prepare(store, manifest)
    quotes = [item["source"]["quote"] for item in spans(result)]
    assert len(quotes) == 2
    period, company = quotes
    assert "사업연도" in period and "회사명" not in period
    assert "회사명" in company and "사업연도" not in company
    assert result.catalog["limits"]["row_span_candidate_count"] == 2

    without = prepare(store, manifest, max_row_spans=0)
    assert spans(without) == []
    assert without.catalog["limits"]["row_span_candidate_count"] == 0
    identity = lambda catalog: [  # noqa: E731
        (item["candidate_id"], item["source"]["source_id"], item["source"]["locator"])
        for item in catalog["candidates"]
        if item["candidate_type"] != "document_row_span"
    ]
    assert identity(result.catalog) == identity(without.catalog)
    assert [item.sha256 for item in result.artifacts] == [item.sha256 for item in without.artifacts]


NESTED = (
    "<?xml version='1.0'?>"
    "<DOCUMENT><BODY><COVER><TABLE><TBODY>"
    "<TR ACOPY=N><TD>바깥 왼쪽</TD>"
    "<TD><TABLE><TBODY><TR><TD>안쪽 왼쪽</TD><TD>안쪽 오른쪽</TD></TR></TBODY></TABLE></TD>"
    "<TD>바깥 오른쪽</TD></TR>"
    "</TBODY></TABLE></COVER></BODY></DOCUMENT>"
)


def two_rows(rowspan: str, tag: str = "TD") -> str:
    return (
        "<?xml version='1.0'?>"
        "<DOCUMENT><BODY><COVER><TABLE><TBODY>"
        f"<TR ACOPY=N><{tag} ROWSPAN={rowspan} ACLASS=NORMAL>사업연도</{tag}>"
        "<TU>2024년 01월 01일</TU></TR>"
        "<TR ACOPY=N><TU>2024년 12월 31일</TU></TR>"
        "</TBODY></TABLE></COVER></BODY></DOCUMENT>"
    )


def test_a_row_interrupted_by_a_nested_table_is_never_spanned(tmp_path):
    """Discontiguous cells are not a complete row, so only the inner row spans."""
    store, manifest = proprietary(tmp_path / "dart", NESTED)
    quotes = [item["source"]["quote"] for item in spans(prepare(store, manifest))]
    assert not any("바깥 왼쪽" in quote and "바깥 오른쪽" in quote for quote in quotes)
    assert not any("바깥" in quote for quote in quotes)
    assert [quote for quote in quotes if "안쪽 왼쪽" in quote and "안쪽 오른쪽" in quote]


def test_only_a_well_formed_bounded_cell_rowspan_binds_two_rows(tmp_path):
    """A declared ROWSPAN joins rows; malformed, oversized or non-cell ones do not."""
    joined = spans(prepare(*proprietary(tmp_path / "ok", two_rows('"2"'))))
    assert len(joined) == 1
    assert "2024년 01월 01일" in joined[0]["source"]["quote"]
    assert "2024년 12월 31일" in joined[0]["source"]["quote"]

    for name, markup in (
        ("malformed", two_rows('"two"')),
        ("oversized", f'"{MAX_DECLARED_ROWSPAN + 1}"'),
        ("non_cell", two_rows('"2"', tag="DIV")),
    ):
        if name == "oversized":
            markup = two_rows(markup)
        store, manifest = proprietary(tmp_path / name, markup)
        quotes = [item["source"]["quote"] for item in spans(prepare(store, manifest))]
        assert not any(
            "2024년 01월 01일" in quote and "2024년 12월 31일" in quote for quote in quotes
        ), name


def short_rowspan(declared: int, closed: bool) -> str:
    """A ROWSPAN that promises more rows than the markup actually carries."""
    tail = "</TBODY></TABLE></COVER></BODY></DOCUMENT>" if closed else ""
    return (
        "<?xml version='1.0'?>"
        "<DOCUMENT><BODY><COVER><TABLE><TBODY>"
        f'<TR ACOPY=N><TD ROWSPAN="{declared}" ACLASS=NORMAL>사업연도</TD>'
        "<TU>2024년 01월 01일</TU></TR>"
        "<TR ACOPY=N><TU>2024년 12월 31일</TU></TR>"
        f"{tail}"
    )


def test_a_rowspan_left_unsatisfied_at_table_close_or_eof_yields_no_span(tmp_path):
    """Two rows do not satisfy ROWSPAN=3, so the partial group is never published."""
    for name, closed in (("closed", True), ("eof", False)):
        store, manifest = proprietary(tmp_path / name, short_rowspan(3, closed))
        result = prepare(store, manifest)
        quotes = [item["source"]["quote"] for item in spans(result)]
        assert not any(
            "2024년 01월 01일" in quote and "2024년 12월 31일" in quote for quote in quotes
        ), name
        assert not any("사업연도" in quote for quote in quotes), name
        # The per-cell candidates are still there; only the false row is withheld.
        cells = [
            item["source"]["quote"]
            for item in result.catalog["candidates"]
            if item["candidate_type"] == "document_element"
        ]
        assert any("2024년 01월 01일" in quote for quote in cells), name
        assert any("2024년 12월 31일" in quote for quote in cells), name

    # The same markup with a satisfied ROWSPAN=2 does produce one span.
    satisfied = spans(prepare(*proprietary(tmp_path / "satisfied", short_rowspan(2, True))))
    assert len(satisfied) == 1
    assert "2024년 01월 01일" in satisfied[0]["source"]["quote"]
    assert "2024년 12월 31일" in satisfied[0]["source"]["quote"]


def later_rowspan(total_rows: int) -> str:
    """ROWSPAN=2 on the first row, then ROWSPAN=3 on the second: reaches row 4."""
    extra = "".join(f"<TR ACOPY=N><TU>R{number}</TU></TR>" for number in range(3, total_rows + 1))
    return (
        "<?xml version='1.0'?>"
        "<DOCUMENT><BODY><COVER><TABLE><TBODY>"
        '<TR ACOPY=N><TD ROWSPAN="2" ACLASS=NORMAL>사업연도</TD><TU>R1</TU></TR>'
        '<TR ACOPY=N><TD ROWSPAN="3" ACLASS=NORMAL>감사기간</TD><TU>R2</TU></TR>'
        f"{extra}"
        "</TBODY></TABLE></COVER></BODY></DOCUMENT>"
    )


def test_a_rowspan_declared_on_a_later_row_promises_from_that_row(tmp_path):
    """ROWSPAN=3 on row two reaches row four, so three rows are still incomplete."""
    short = spans(prepare(*proprietary(tmp_path / "short", later_rowspan(3))))
    assert short == []

    satisfied = spans(prepare(*proprietary(tmp_path / "satisfied", later_rowspan(4))))
    assert len(satisfied) == 1
    quote = satisfied[0]["source"]["quote"]
    assert satisfied[0]["raw"]["cell_texts"] == [
        "사업연도",
        "R1",
        "감사기간",
        "R2",
        "R3",
        "R4",
    ]
    assert quote.count("\n") == 5


def test_a_row_cut_by_the_candidate_bound_produces_no_span(tmp_path):
    """A partially retained row is never spanned; an incomplete row is not a row."""
    store, manifest = proprietary(tmp_path / "dart")
    result = prepare(store, manifest, max_candidates=2)
    assert spans(result) == []
    with pytest.raises(CandidatePreparationError, match="row_span_limit_invalid"):
        prepare(store, manifest, max_row_spans=-1)


def test_operator_sr_source_is_byte_hash_and_locator_verified(tmp_path):
    store, manifest = collection(tmp_path / "dart")
    sr_manifest = sr_input(tmp_path)
    prepared = add_operator_sr_sources(prepare(store, manifest), sr_manifest, tmp_path)
    candidate = next(
        item
        for item in prepared.catalog["candidates"]
        if item["candidate_type"] == "sustainability_source"
    )
    assert candidate["source"]["source_id"] == "sr-scope"
    assert candidate["verification_state"] == "candidate"
    assert prepared.artifact_index["sr-v1"]["sha256"] == candidate["source"]["artifact_sha256"]
    (tmp_path / "sr.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(CandidatePreparationError, match="sr_source_verification_failed"):
        add_operator_sr_sources(prepare(store, manifest), sr_manifest, tmp_path)


def test_operator_sr_sources_report_their_own_bound_beside_the_dart_limit(tmp_path):
    """A real collection fills the DART bound, so the extra SR source must stay visible."""
    store, manifest = collection(tmp_path / "dart")
    prepared = add_operator_sr_sources(
        prepare(store, manifest, max_candidates=1), sr_input(tmp_path), tmp_path
    )
    limits = prepared.catalog["limits"]
    assert limits["max_candidates"] == 1
    assert limits["truncated"] is True
    assert limits["max_operator_sources"] == 100
    assert limits["operator_source_count"] == 1
    dart = [
        item
        for item in prepared.catalog["candidates"]
        if item["candidate_type"] != "sustainability_source"
    ]
    assert len(dart) == limits["max_candidates"]
    assert len(prepared.catalog["candidates"]) == (
        limits["max_candidates"] + limits["operator_source_count"]
    )


def test_repeated_operator_sr_addition_keeps_the_reported_count_truthful(tmp_path):
    """The reported operator count follows the catalogue, not one manifest's length."""
    store, manifest = collection(tmp_path / "dart")
    first = sr_input(tmp_path)
    prepared = add_operator_sr_sources(prepare(store, manifest, max_candidates=1), first, tmp_path)
    assert prepared.catalog["limits"]["operator_source_count"] == 1

    second = copy.deepcopy(first)
    second["sources"][0] = second["sources"][0] | {
        "source_id": "sr-scope-2",
        "locator": "chars:0:7",
        "quote": "Samsung",
    }
    prepared = add_operator_sr_sources(prepared, second, tmp_path)
    operator = [
        item
        for item in prepared.catalog["candidates"]
        if item["candidate_type"] == "sustainability_source"
    ]
    limits = prepared.catalog["limits"]
    assert [item["source"]["source_id"] for item in operator] == ["sr-scope", "sr-scope-2"]
    assert limits["operator_source_count"] == len(operator) == 2
    assert limits["max_candidates"] == 1
    assert limits["max_operator_sources"] == 100
    assert len(prepared.catalog["candidates"]) == (
        limits["max_candidates"] + limits["operator_source_count"]
    )


def test_complete_selection_is_pending_unapproved_and_cross_disclosure(tmp_path):
    store, manifest = collection(tmp_path / "dart")
    prepared = add_operator_sr_sources(prepare(store, manifest), sr_input(tmp_path), tmp_path)
    sr = next(
        c for c in prepared.catalog["candidates"] if c["candidate_type"] == "sustainability_source"
    )
    financial = next(
        c for c in prepared.catalog["candidates"] if c["candidate_type"] == "statement_row"
    )
    identity = {
        "tenant_id": "tenant-1",
        "company_id": "company-1",
        "claim_id": "claim-1",
        "package_id": "package-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "sustainability_document_version": "sr-v1",
        "financial_document_version": financial["source"]["document_id"],
        "dart_corp_code": CORP,
        "financial_fiscal_year": FY,
        "consolidation": "consolidated",
        "financial_period_start": "2024-01-01",
        "financial_period_end": "2024-12-31",
        "sr_published_at": "2025-06-27",
        "financial_published_at": "2025-03-11",
        "rcept_no": RECEIPT,
        "as_of_date": "2025-06-30",
    }
    selections = {
        "schema_version": "reconciliation-operator-selections-1",
        "identity": identity,
        "item": "C1",
        "claim": {
            "candidate_id": sr["candidate_id"],
            "track": "performance",
            "trigger_elements": ["organizational_boundary"],
            "fiscal_year": FY,
        },
        "comparability": "unknown",
        "sustainability": {
            "candidate_id": sr["candidate_id"],
            "raw": sr["source"]["quote"],
            "normalized": '["Samsung Electronics and its consolidated subsidiaries"]',
            "kind": "entity_set",
            "unit": None,
        },
        "financial": {
            "candidate_id": financial["candidate_id"],
            "raw": financial["raw"]["account_detail"],
            "normalized": '["-"]',
            "kind": "entity_set",
            "unit": None,
        },
        "explanation": {"candidate_id": None, "search_complete": False},
        "search": {
            "state": "not_run",
            "coverage_policy_id": None,
            "required_document_ids": [],
            "reviewed_source_ids": [],
            "failed_document_ids": [],
            "receipt_id": None,
        },
        "c3_context": None,
        "c4_context": None,
        "policy": {
            "schema_version": "1.1",
            "version": "operator-draft-1",
            "approved": False,
            "synthetic_only": False,
            "current_stage": 1,
            "enabled_items": ["C1"],
            "c1_identity_rule": "exact_verified_entity_set",
            "c3_threshold": None,
            "c3_account_mapping_approved": False,
            "approved_by": None,
            "approved_on": None,
            "source_policy_sha256": "a" * 64,
            "allowed_capex_account_ids": [],
            "coverage_policy_id": "coverage-draft-1",
            "allowed_difference_types": [],
            "c2_timing_rule": "same_period_or_verified_explanation",
            "c4_required_explanations": ["definition", "calculation_basis"],
        },
    }
    draft = prepare_review_draft(prepared, selections)
    assert draft["status"] == "draft_pending_review"
    assert draft["review_state"] == "pending"
    assert draft["policy_approved"] is False
    assert draft["coverage"]["state"] == "not_run"
    assert draft["coverage"]["reviewed_source_ids"] == []
    assert draft["documents"]["sr-v1"]["binding_state"] == "draft"
    assert draft["packet"]["sustainability"]["source_id"] == "sr-scope"
    assert draft["packet"]["financial"]["source_id"] != "sr-scope"
