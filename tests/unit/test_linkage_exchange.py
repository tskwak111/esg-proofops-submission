"""Focused tests for the A-side linkage-exchange packet builder (R08a).

Exactly the three cases the task called for:
1. one synthetic full roundtrip (build_packet -> real strict1.1 schema
   validation passes -> the existing handoff/team-v3/contract/validate.py
   return-mode structural gate accepts it against a synthetic policy/output).
2. one negative source-byte-mismatch rejection (a financial artifact whose
   real bytes do not match the declared hash is rejected, never silently
   accepted).
3. a real cached run's claim with no financial context -> BlockedPacket
   (never a fabricated packet).

No paid/API calls, no network. `synthetic=True` throughout; nothing here
claims real DART/financial-statement verification.
"""

from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
from proofops.application.linkage_exchange import (
    TRIGGER_TAG_MAP,
    BlockedPacket,
    C3Context,
    FinancialContext,
    FinancialFact,
    FinancialSource,
    _verified_triggers,
    build_packet,
)
from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags
from proofops.domain.values import SourceRef

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
MANIFEST = "33333333-3333-4333-8333-333333333333"
CLAIM = "66666666-6666-4666-8666-666666666666"
SR_SOURCE = "77777777-7777-4777-8777-777777777777"

FIXTURE_CONTRACT_DIR = Path(__file__).resolve().parents[2] / "tests/fixtures/linkage-contract"
CONTRACT_DIR = FIXTURE_CONTRACT_DIR


def _source_ref(**overrides) -> SourceRef:
    values = dict(
        source_id=SR_SOURCE,
        document_version_id=VERSION,
        parse_manifest_id=MANIFEST,
        page_num=5,
        printed_page_label=None,
        bbox=None,
        raw_text_sha256="a" * 64,
        quote="공시 대상 법인은 가상법인 A와 B입니다.",
        char_start=0,
        char_end=10,
        location_quality="unlocated",
        verification_state="candidate",
    )
    values.update(overrides)
    return SourceRef(**values)


def _claim(**overrides) -> Claim:
    ref = _source_ref()
    profile = ExtractionProfile(
        model_sha256="b" * 64, prompt_sha256="c" * 64, rule_sha256="d" * 64, synthetic=True
    )
    receipt = ExtractionReceipt(
        source_id=ref.source_id,
        packet_sha256="e" * 64,
        response_sha256=None,
        raw_response_json=None,
        profile=profile,
        status="succeeded",
    )
    values = dict(
        claim_id=CLAIM,
        tenant_id=TENANT,
        document_version_id=VERSION,
        parse_manifest_id=MANIFEST,
        source_sha256="f" * 64,
        quote=ref.quote,
        source_refs=(ref,),
        source_quality="unverified",
        topic_ids=("environment",),
        receipt=receipt,
    )
    values.update(overrides)
    return Claim(**values)


def _confirmed_tags(**overrides) -> ConfirmedTags:
    ref = _source_ref()
    fact = ConfirmedFact(
        name="organizational_boundary",
        state="present",
        evidence_refs=(ref,),
        source_tenant_id=TENANT,
        citation_verified=True,
        binding_accepted=True,
        search_coverage_verified=False,
        normalized_value='["A","B"]',
    )
    values = dict(
        tenant_id=TENANT,
        document_version_id=VERSION,
        claim_id=CLAIM,
        track="performance",
        facts=(fact,),
        tag_revision=1,
        packet_sha256="1" * 64,
        model_sha256="2" * 64,
        prompt_sha256="3" * 64,
        replicate_hashes=("4" * 64, "5" * 64, "6" * 64),
        ontology_version="v1",
    )
    values.update(overrides)
    return ConfirmedTags(**values)


def _financial_context(**overrides) -> FinancialContext:
    values = dict(
        synthetic=True,
        company_id="fixture-company",
        package_id="fixture-package",
        dart_corp_code="00000000",
        financial_document_version="fs-v1",
        financial_fiscal_year=2024,
        consolidation="consolidated",
        financial_period_start="2024-01-01",
        financial_period_end="2024-12-31",
        financial_published_at="2025-03-31",
        rcept_no="synthetic-receipt",
        as_of_date="2025-06-30",
        financial=FinancialFact(
            raw="가상법인 A, B",
            normalized='["A","B"]',
            kind="entity_set",
            unit="entity",
            source_id="fs-scope",
        ),
        financial_sources=(
            FinancialSource(
                source_id="fs-scope",
                document_id="fs-v1",
                artifact_sha256="2" * 64,
                locator="physical_page=1",
                quote="공시 대상 법인은 가상법인 A와 B입니다.",
            ),
        ),
    )
    values.update(overrides)
    return FinancialContext(**values)


def _build(**overrides):
    kwargs = dict(
        claim=_claim(),
        tags=_confirmed_tags(),
        financial_context=_financial_context(),
        item="C1",
        tenant_id=TENANT,
        period_start="2024-01-01",
        period_end="2024-12-31",
        sr_published_at="2025-06-30",
    )
    kwargs.update(overrides)
    return build_packet(**kwargs)


def test_c2_period_is_bound_to_confirmed_evidence_and_rejects_cross_range_dates():
    from dataclasses import replace

    from proofops.application.linkage_exchange import C2PeriodContext, period_from_literal
    from proofops.domain.errors import DomainValidationError

    quote = "배출량 10 tCO2e, 보고년도 : 2025년 1월 1일 ~ 2025년 12월 31일"
    period = "2025-01-01/2025-12-31"
    ref = _source_ref(quote=quote)
    fact = replace(
        _confirmed_tags().facts[0],
        name="quantitative_or_qualified_ordinal",
        evidence_refs=(ref,),
        normalized_value=quote,
    )
    kwargs = dict(
        item="C2",
        claim=_claim(quote=quote, source_refs=(ref,)),
        tags=_confirmed_tags(facts=(fact,)),
        financial_context=_financial_context(
            financial=FinancialFact(quote, period, "period", None, "fs-scope")
        ),
    )
    context = C2PeriodContext(period, SR_SOURCE, quote)
    result = _build(**kwargs, c2_period_context=context)
    assert not isinstance(result, BlockedPacket), result
    assert result["sustainability"]["kind"] == "period"
    assert result["sustainability"]["normalized"] == period
    assert isinstance(_build(**kwargs), BlockedPacket)
    categorical = replace(
        fact, normalized_value="ISO 14001 인증, 2025년 1월 1일 ~ 2025년 12월 31일"
    )
    assert isinstance(
        _build(
            **(kwargs | {"tags": _confirmed_tags(facts=(categorical,))}), c2_period_context=context
        ),
        BlockedPacket,
    )
    for invalid in (
        replace(context, source_id="foreign"),
        replace(context, normalized="2024-01-01/2024-12-31"),
        replace(context, quote=quote + " changed"),
    ):
        assert isinstance(_build(**kwargs, c2_period_context=invalid), BlockedPacket)
    assert isinstance(
        _build(
            **(kwargs | {"tenant_id": "99999999-9999-4999-8999-999999999999"}),
            c2_period_context=context,
        ),
        BlockedPacket,
    )
    assert period_from_literal("2025년 01월 01일 부터 2025년 12월 31일 까지") == period
    for text in (
        quote + " / 2024년 1월 1일 ~ 2024년 12월 31일",
        "발행일 2025년 1월 1일; 수정일 2025년 12월 31일",
        "2025년 12월 31일 ~ 2025년 1월 1일",
        "2025-02-30/2025-12-31",
    ):
        with pytest.raises((DomainValidationError, ValueError)):
            period_from_literal(text)


@pytest.mark.skipif(not CONTRACT_DIR.is_dir(), reason="handoff contract dir not present")
def test_synthetic_full_roundtrip_passes_real_schema_and_validate_py_return_mode(tmp_path):
    """Case 1: synthetic full roundtrip through the EXISTING strict1.1 validator."""
    sys.path.insert(0, str(CONTRACT_DIR))
    from validate import load_schema_validators  # type: ignore[import-not-found]

    result = _build()
    assert not isinstance(result, BlockedPacket), result

    validators = load_schema_validators()
    validators["input"].validate(result)  # raises on any schema violation

    # Now exercise validate.py's full return-mode gate (schema + linkage +
    # hash + source-existence), using a synthetic policy/output pair.
    policy = {
        "schema_version": "1.1",
        "version": "synthetic-demo-only",
        "approved": True,
        "synthetic_only": True,
        "current_stage": 1,
        "enabled_items": ["C1", "C2", "C3", "C4"],
        "c1_identity_rule": "exact_verified_entity_set",
        "c3_threshold": None,
        "c3_account_mapping_approved": False,
        "approved_by": "synthetic-fixture-author",
        "approved_on": "2026-09-18",
        "source_policy_sha256": "3" * 64,
        "allowed_capex_account_ids": [],
        "coverage_policy_id": "synthetic-coverage-only",
        "allowed_difference_types": ["operational_control_vs_control"],
        "c2_timing_rule": "same_period_or_verified_explanation",
        "c4_required_explanations": ["definition", "calculation_basis"],
    }

    def digest(value):
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode()).hexdigest()

    output = {
        "schema_version": "1.1",
        "claim_id": result["identity"]["claim_id"],
        "item": result["item"],
        "execution_state": "completed",
        "status": "matched",
        "review_required": False,
        "reason_codes": ["same_verified_entity_set"],
        "source_ids": [s["source_id"] for s in result["sources"]],
        "explanation_source_id": None,
        "sustainability_value": result["sustainability"]["raw"],
        "financial_value": result["financial"]["raw"],
        "packet_sha256": digest(result),
        "policy_sha256": digest(policy),
        "synthetic": True,
        "engine_version": "synthetic-expected-not-engine",
    }

    input_path = tmp_path / "input.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps(result, ensure_ascii=False))
    policy_path.write_text(json.dumps(policy, ensure_ascii=False))
    output_path.write_text(json.dumps(output, ensure_ascii=False))

    from validate import validate_return  # type: ignore[import-not-found]

    errors = validate_return(str(input_path), str(policy_path), str(output_path))
    assert errors == [], errors


def test_negative_source_byte_mismatch_is_rejected(tmp_path):
    """Case 2: a financial artifact whose real bytes don't match the declared hash."""
    from proofops.adapters.local.linkage_reader import (
        LinkageVerificationError,
        verify_financial_source,
    )

    # Write a real local PDF-like file whose bytes do NOT hash to the value
    # the packet declares -- this must be rejected, never silently accepted
    # just because it opens.
    fake_pdf = tmp_path / "fake-financial.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 not a real financial statement byte stream")
    wrong_hash_source = FinancialSource(
        source_id="fs-scope",
        document_id="fs-v1",
        artifact_sha256="2" * 64,  # does not match the real bytes above
        locator="physical_page=1",
        quote="anything",
    )
    with pytest.raises(LinkageVerificationError, match="byte hash mismatch"):
        verify_financial_source(wrong_hash_source, local_path=fake_pdf)


def test_unsupported_locator_format_is_rejected_even_with_correct_hash(tmp_path):
    """Hash-alone is never sufficient: an XBRL-style locator this reader
    cannot parse must be explicitly rejected, not silently accepted."""
    from proofops.adapters.local.linkage_reader import (
        LinkageVerificationError,
        verify_financial_source,
    )

    content = b"%PDF-1.4 real bytes for this test"
    real_hash = sha256(content).hexdigest()
    artifact = tmp_path / "real-financial.pdf"
    artifact.write_bytes(content)
    xbrl_style_source = FinancialSource(
        source_id="fs-scope",
        document_id="fs-v1",
        artifact_sha256=real_hash,  # correct hash
        locator="fact=Assets;context=FY2024",  # unsupported locator format
        quote="anything",
    )
    with pytest.raises(LinkageVerificationError, match="unsupported_locator_format"):
        verify_financial_source(xbrl_style_source, local_path=artifact)


def test_quote_not_on_declared_page_is_rejected_despite_hash_match(tmp_path):
    """Hash-alone is never sufficient: even a correct hash + supported
    locator format must still find the literal quote on that real page."""
    from io import BytesIO

    import pdfplumber
    from proofops.adapters.local.linkage_reader import (
        LinkageVerificationError,
        verify_financial_source,
    )
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 700 Td (Unrelated financial statement text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    content = buffer.getvalue()

    pdf_path = tmp_path / "one-page.pdf"
    pdf_path.write_bytes(content)
    with pdfplumber.open(pdf_path) as pdf:
        assert len(pdf.pages) == 1  # sanity: single real page

    real_hash = sha256(content).hexdigest()
    mismatched_quote_source = FinancialSource(
        source_id="fs-scope",
        document_id="fs-v1",
        artifact_sha256=real_hash,
        locator="physical_page=1",
        quote="공시 대상 법인은 가상법인 A와 B입니다.",  # not actually on this page
    )
    with pytest.raises(LinkageVerificationError, match="quote not found on physical_page"):
        verify_financial_source(mismatched_quote_source, local_path=pdf_path)


def test_missing_financial_context_blocks_rather_than_fabricates():
    """Case 3: a claim exists but no caller-supplied FinancialContext -> blocked."""
    result = _build(financial_context=None)
    assert isinstance(result, BlockedPacket)
    assert result.reason == "missing_financial_context"
    as_dict = result.to_dict()
    assert as_dict["execution_state"] == "blocked"
    assert as_dict["claim_id"] == CLAIM
    assert as_dict["item"] == "C1"


def test_c5_is_always_blocked():
    result = _build(item="C5")
    assert isinstance(result, BlockedPacket)
    assert result.reason == "prohibited_item"


def test_tenant_mismatch_blocks():
    other_tenant = "88888888-8888-4888-8888-888888888888"
    result = _build(tenant_id=other_tenant)
    assert isinstance(result, BlockedPacket)
    assert result.reason == "tenant_mismatch"


def test_no_verified_trigger_blocks_when_fact_not_present():
    unresolved_tags = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="organizational_boundary",
                state="unknown",
                evidence_refs=(),
                source_tenant_id=None,
                citation_verified=False,
                binding_accepted=False,
                search_coverage_verified=True,
            ),
        )
    )
    result = _build(tags=unresolved_tags)
    assert isinstance(result, BlockedPacket)
    assert result.reason == "no_verified_trigger"


def test_the_canonical_org_boundary_fact_name_maps_to_the_contract_trigger():
    """`org_boundary` is what management.M2/goal.G4 actually emit.

    The older `organizational_boundary` spelling stays mapped (every other case
    in this module still uses it), and neither spelling changes the verification
    guards a trigger has to clear.
    """
    canonical = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="org_boundary",
                state="present",
                evidence_refs=(_source_ref(),),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value='["A","B"]',
            ),
        )
    )
    assert TRIGGER_TAG_MAP["org_boundary"] == "organizational_boundary"
    assert TRIGGER_TAG_MAP["organizational_boundary"] == "organizational_boundary"
    triggers = _verified_triggers(canonical)
    assert [(t.fact_name, t.trigger_element) for t in triggers] == [
        ("org_boundary", "organizational_boundary")
    ]


def test_an_unproven_boundary_or_scope_primitive_stays_an_explicit_gap():
    """No blind widening: a calculation boundary or a GHG `scope` is not a trigger.

    `calculation_boundary` (performance.P3) and `scope` (goal.G4's other
    primitive) are deliberately absent from the map, so a present+verified fact
    under either name still produces no trigger at all.
    """
    for name in ("calculation_boundary", "scope"):
        assert name not in TRIGGER_TAG_MAP
        assert (
            _verified_triggers(
                _confirmed_tags(
                    facts=(
                        ConfirmedFact(
                            name=name,
                            state="present",
                            evidence_refs=(_source_ref(),),
                            source_tenant_id=TENANT,
                            citation_verified=True,
                            binding_accepted=True,
                            search_coverage_verified=False,
                            normalized_value="Scope 1",
                        ),
                    )
                )
            )
            == ()
        )


def test_c3_requires_c3_context():
    c3_tags = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="currency_amount",
                state="present",
                evidence_refs=(_source_ref(),),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value="1000000000",
            ),
        )
    )
    result = _build(item="C3", tags=c3_tags)
    assert isinstance(result, BlockedPacket)
    assert result.reason == "missing_c3_context"

    with_c3 = _build(
        item="C3",
        tags=c3_tags,
        financial_context=_financial_context(
            financial=FinancialFact(
                raw="10억원",
                normalized="1000000000",
                kind="currency_amount",
                unit="KRW",
                source_id="fs-scope",
            ),
            c3_context=C3Context(
                currency="KRW",
                target_period_start="2024-01-01",
                target_period_end="2024-12-31",
                capex_period_start="2024-01-01",
                capex_period_end="2024-12-31",
                capex_account_ids=("account-1",),
                commitment_source_id=None,
                funding_plan_source_id=None,
            ),
        ),
    )
    assert not isinstance(with_c3, BlockedPacket)


def test_organizational_boundary_does_not_displace_currency_amount_for_c3():
    """CONTRACT item-specific trigger: C3 chooses currency_amount, never organizational_boundary."""
    ref = _source_ref()
    both_tags = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="organizational_boundary",
                state="present",
                evidence_refs=(ref,),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value='["A","B"]',
            ),
            ConfirmedFact(
                name="currency_amount",
                state="present",
                evidence_refs=(ref,),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value="50000000",
            ),
        )
    )
    res = _build(
        item="C3",
        tags=both_tags,
        financial_context=_financial_context(
            financial=FinancialFact(
                raw="5천만원",
                normalized="50000000",
                kind="currency_amount",
                unit="KRW",
                source_id="fs-scope",
            ),
            c3_context=C3Context(
                currency="KRW",
                target_period_start="2024-01-01",
                target_period_end="2024-12-31",
                capex_period_start="2024-01-01",
                capex_period_end="2024-12-31",
                capex_account_ids=("account-1",),
                commitment_source_id=None,
                funding_plan_source_id=None,
            ),
        ),
    )
    assert not isinstance(res, BlockedPacket)
    assert res["sustainability"]["kind"] == "currency_amount"
    assert res["sustainability"]["normalized"] == "50000000"


def test_company_mismatch_rejects():
    """Finding 1: Company mismatch rejects."""
    result = _build(
        trusted_company_id="real-corp-uuid",
        financial_context=_financial_context(company_id="mismatched-corp-uuid"),
    )
    assert isinstance(result, BlockedPacket)
    assert result.reason == "company_mismatch"


def test_period_mismatch_rejects():
    """Finding 1: Reversed or invalid period rejects."""
    result = _build(period_start="2024-12-31", period_end="2024-01-01")
    assert isinstance(result, BlockedPacket)
    assert result.reason == "period_mismatch"

    fin_reversed = _build(
        financial_context=_financial_context(
            financial_period_start="2024-12-31", financial_period_end="2024-01-01"
        )
    )
    assert isinstance(fin_reversed, BlockedPacket)
    assert fin_reversed.reason == "period_mismatch"


def test_version_mismatch_rejects():
    """Finding 1: Document version mismatch rejects."""
    other_version = "99999999-9999-4999-8999-999999999999"
    tag_mismatch = _build(tags=_confirmed_tags(document_version_id=other_version))
    assert isinstance(tag_mismatch, BlockedPacket)
    assert tag_mismatch.reason == "version_mismatch"


def test_all_trigger_evidence_refs_preserved_and_deduplicated():
    """Finding 2: trigger fact evidence refs are preserved in sources[]."""
    ref1 = _source_ref(
        source_id="11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", page_num=2, quote="인용문 1"
    )
    ref2 = _source_ref(
        source_id="22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb", page_num=3, quote="인용문 2"
    )
    tags = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="organizational_boundary",
                state="present",
                evidence_refs=(ref1, ref2),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value='["A","B"]',
            ),
        )
    )
    result = _build(tags=tags)
    assert not isinstance(result, BlockedPacket)
    source_ids = {s["source_id"] for s in result["sources"]}
    assert "sr-" + ref1.source_id in source_ids
    assert "sr-" + ref2.source_id in source_ids
    # verify locator and quote preserved
    entry1 = next(s for s in result["sources"] if s["source_id"] == "sr-" + ref1.source_id)
    assert entry1["locator"] == f"physical_page=2;source_id={ref1.source_id}"
    assert entry1["quote"] == "인용문 1"


def test_ambiguous_source_duplicate_is_rejected():
    """Deduplication check: Same source_id with conflicting quote or locator must reject."""
    same_uuid = "88888888-8888-4888-8888-888888888888"
    ref1 = _source_ref(source_id=same_uuid, page_num=2, quote="인용문 A")
    ref2 = _source_ref(source_id=same_uuid, page_num=2, quote="인용문 B (다른 스팬)")
    tags = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="organizational_boundary",
                state="present",
                evidence_refs=(ref1, ref2),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value='["A","B"]',
            ),
        )
    )
    res = _build(tags=tags)
    assert isinstance(res, BlockedPacket)
    assert res.reason == "ambiguous_source_duplicate"


def test_unsupported_trigger_kind_or_missing_normalization_blocks():
    """Finding 3: sustainability value requires supported typed kind and normalized_value."""
    ref = _source_ref()
    missing_norm_tags = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="organizational_boundary",
                state="present",
                evidence_refs=(ref,),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value=None,
            ),
        )
    )
    res2 = _build(tags=missing_norm_tags)
    assert isinstance(res2, BlockedPacket)
    assert res2.reason == "unsupported_trigger_value"


def test_return_reader_rejects_network_path_and_bad_tenant_version(tmp_path):
    """Finding 4: reader rejects network URL, bad tenant, and bad version."""
    from proofops.adapters.local.linkage_reader import (
        LinkageVerificationError,
        verify_financial_source,
        verify_packet_sources,
    )

    url_source = FinancialSource(
        source_id="fs-1",
        document_id="fs-v1",
        artifact_sha256="a" * 64,
        locator="physical_page=1",
        quote="quote",
    )
    with pytest.raises(LinkageVerificationError, match="refusing arbitrary/network path"):
        verify_financial_source(url_source, local_path="http://malicious.host/file.pdf")

    packet = _build()
    assert not isinstance(packet, BlockedPacket)

    # Bad tenant
    with pytest.raises(LinkageVerificationError, match="tenant_mismatch"):
        verify_packet_sources(
            packet,
            uploads=None,
            tenant_id="00000000-0000-4000-8000-000000000000",
            financial_local_paths={},
        )


def test_whole_quote_entity_set_is_refused_at_the_build_boundary():
    """A boundary sentence is not a normalized entity set.

    The review path can store a confirmed ``org_boundary`` value that is the
    literal evidence quote. Mapped to C1 that value used to leave this module as
    ``sustainability.kind='entity_set'`` with a sentence in ``normalized``, i.e.
    a quote presented as a typed identifier set. The reconciliation engine's own
    ``parse_entity_set`` refuses that shape, so the packet was never admissible;
    only the refusal happened late, after A had already emitted it. It is now
    refused here, by the same shared validator, with no value repaired or
    substituted.
    """
    ref = _source_ref()
    whole_quote_tags = _confirmed_tags(
        facts=(
            ConfirmedFact(
                name="org_boundary",
                state="present",
                evidence_refs=(ref,),
                source_tenant_id=TENANT,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=False,
                normalized_value=ref.quote,
            ),
        )
    )
    result = _build(tags=whole_quote_tags)
    assert isinstance(result, BlockedPacket)
    assert result.reason == "invalid_reconciliation_packet"
    assert "sustainability.normalized" in result.detail


def test_typed_entity_set_still_builds_after_the_shared_validator_gate():
    """The valid typed path is unchanged: the gate refuses shape, not content."""
    packet = _build()
    assert not isinstance(packet, BlockedPacket)
    assert packet["sustainability"]["kind"] == "entity_set"
    assert packet["sustainability"]["normalized"] == '["A","B"]'

    from proofops.domain.reconciliation.common import validate_packet

    validate_packet(packet)  # the same validator the boundary now applies


def test_malformed_financial_typed_value_is_refused_at_the_same_boundary():
    """The shared validator covers the financial side too, not only sustainability."""
    result = _build(
        financial_context=_financial_context(
            financial=FinancialFact(
                raw="12,035,007,218,975",
                normalized="12,035,007,218,975",
                kind="currency_amount",
                unit="KRW",
                source_id="fs-scope",
            )
        )
    )
    assert isinstance(result, BlockedPacket)
    assert result.reason == "invalid_reconciliation_packet"
    assert "financial.normalized" in result.detail
