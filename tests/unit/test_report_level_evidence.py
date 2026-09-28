"""Deterministic report-level M2/M3 crediting (user decision 2026-09-25, original §6 2-4)."""

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import pytest
from proofops.application.evidence.report_level import (
    GRI_ASSURED_PAGE_V1,
    REPORT_SCOPE_V1,
    check_report_level,
)
from proofops.domain.values import SourceRef

DOC = "22222222-2222-4222-8222-222222222222"


def ref(page, quote, *, label=None):
    return SourceRef(
        str(uuid5(NAMESPACE_URL, f"{page}:{quote}")),
        DOC,
        DOC,
        page,
        str(page) if label is None else label,
        (0, 0, 1, 1),
        "a" * 64,
        quote,
        0,
        len(quote),
        "located",
        "verified",
    )


CLAIM = (ref(83, "ESG위원회는 환경 및 기후 관련 안건을 포함해 최소 연3회 이상 정기적으로 개최"),)
PAGES = {
    230: [
        "GRI Index",
        "Material Topics",
        "중대 토픽 3-3 중대 토픽 관리 83-95 에너지 302-1 조직 내부",
    ],
    242: [
        "제3자 검증의견서",
        "AA1000AS v3",
        "중대성 주제 3-1 ~ 3-3",
        "에너지 302-1, 302-2, 302-3, 302-4",
    ],
    2: [
        "보고서 개요",
        "보고 범위",
        "ESG·지속가능성 관련 성과는 네이버 주식회사 개별 기업을 기준으로 작성",
    ],
}
INDEX = ref(230, "3-3 중대 토픽 관리 83-95")
COVERAGE = ref(242, "중대성 주제 3-1 ~ 3-3")
STANDARD = ref(242, "AA1000AS v3")
SCOPE = ref(2, "ESG·지속가능성 관련 성과는 네이버 주식회사 개별 기업을 기준으로 작성")


def m3(refs, claim=CLAIM, pages=PAGES, credited=None):
    return check_report_level(
        "M3",
        GRI_ASSURED_PAGE_V1,
        tuple(refs),
        claim_refs=claim,
        claim_quote=" ".join(r.quote for r in claim),
        page_texts=pages,
        credited_from=credited if credited is not None else refs[1].source_id,
    )


def test_gri_index_chain_credits_external_verification():
    assert m3((INDEX, COVERAGE, STANDARD)) is True


@pytest.mark.parametrize(
    "index,coverage",
    [
        (ref(230, "3-3 중대 토픽 관리 96-112"), COVERAGE),  # page outside the listed range
        (ref(230, "3-3 중대 토픽 관리 302-1 83-95"), COVERAGE),  # another disclosure in between
        (INDEX, ref(242, "중대성 주제 3-1 ~ 3-2")),  # disclosure not assured
        (INDEX, ref(242, "공급업체 환경평가 308-2")),
    ],
)
def test_broken_chain_is_rejected(index, coverage):
    assert m3((index, coverage, STANDARD)) is False


def test_explicit_disclosure_list_is_accepted():
    index = ref(230, "302-4 에너지소비 절감 86-91, 225")
    coverage = ref(242, "에너지 302-1, 302-2, 302-3, 302-4")
    claim = (ref(90, "물 리스크 평가 체계 구축"),)
    assert m3((index, coverage, STANDARD), claim=claim) is True


@pytest.mark.parametrize(
    "change",
    [
        "no_index_heading",
        "no_assurance_heading",
        "standard_other_page",
        "unknown_standard",
        "printed_label_missing",
        "credited_elsewhere",
    ],
)
def test_m3_guards_fail_closed(change):
    pages = {k: list(v) for k, v in PAGES.items()}
    refs = [INDEX, COVERAGE, STANDARD]
    claim, credited = CLAIM, None
    if change == "no_index_heading":
        pages[230] = [text for text in pages[230] if "GRI" not in text]
    elif change == "no_assurance_heading":
        pages[242] = [text for text in pages[242] if "검증의견서" not in text]
    elif change == "standard_other_page":
        refs[2] = ref(241, "AA1000AS v3")
        pages[241] = ["AA1000AS v3"]
    elif change == "unknown_standard":
        refs[2] = ref(242, "KPCQA ESG Assurance Protocol")
        pages[242].append("KPCQA ESG Assurance Protocol")
    elif change == "printed_label_missing":
        claim = (replace(CLAIM[0], printed_page_label=None),)
    elif change == "credited_elsewhere":
        credited = INDEX.source_id
    assert m3(refs, claim=claim, pages=pages, credited=credited) is False


def m2(refs, claim_quote, pages=PAGES, credited=None):
    claim = (ref(184, claim_quote),)
    return check_report_level(
        "M2",
        REPORT_SCOPE_V1,
        tuple(refs),
        claim_refs=claim,
        claim_quote=claim_quote,
        page_texts=pages,
        credited_from=credited if credited is not None else refs[0].source_id,
    )


def test_report_scope_credits_claims_without_their_own_scope():
    assert m2((SCOPE,), "ESG위원회는 최소 연3회 이상 정기적으로 개최") is True


@pytest.mark.parametrize(
    "claim_quote",
    [
        "춘천 사업장 특성을 고려하여 핵심 요인을 선정",
        "국내 사업장을 대상으로 리스크 수준을 검토",
        "해외 법인의 환경경영 체계 구축",
        "데이터센터 냉각수 사용 관리",
    ],
)
def test_claims_with_their_own_scope_are_not_given_the_report_scope(claim_quote):
    assert m2((SCOPE,), claim_quote) is False


def test_report_scope_requires_scope_heading_entity_and_credit():
    pages = {k: list(v) for k, v in PAGES.items()}
    pages[2] = [text for text in pages[2] if text != "보고 범위"]
    assert m2((SCOPE,), "ESG위원회 개최", pages=pages) is False
    vague = ref(2, "일부 성과의 경우 이전 3개년 이상의 데이터를 제시")
    pages = {k: list(v) for k, v in PAGES.items()}
    pages[2].append(vague.quote)
    assert m2((vague,), "ESG위원회 개최", pages=pages) is False
    assert m2((SCOPE,), "ESG위원회 개최", credited="other") is False


def test_unknown_policy_or_element_is_rejected():
    kwargs = dict(
        claim_refs=CLAIM,
        claim_quote=CLAIM[0].quote,
        page_texts=PAGES,
        credited_from=COVERAGE.source_id,
    )
    assert (
        check_report_level("M1", GRI_ASSURED_PAGE_V1, (INDEX, COVERAGE, STANDARD), **kwargs)
        is False
    )
    assert check_report_level("M3", REPORT_SCOPE_V1, (INDEX, COVERAGE, STANDARD), **kwargs) is False
    assert check_report_level("M3", "OTHER_V1", (INDEX, COVERAGE, STANDARD), **kwargs) is False


@pytest.mark.parametrize(
    "heading_blocks,other_page_blocks,expected",
    [
        (["GRI", "Index"], [], True),
        (["Index", "GRI Standards"], [], True),
        ([" \tiNdEx\n", " gri\n\tSTANDARDS "], [], True),
        (["Index"], [], False),
        (["GRI"], [], False),
        (["GRI Standards"], [], False),
        (["GRI", "See the Index for details"], [], False),
        (["Index", "Prepared using GRI Standards"], [], False),
        (["GRI"], ["Index"], False),
        (["Index"], ["GRI Standards"], False),
        ([], ["GRI", "Index"], False),
    ],
)
def test_m3_split_index_heading_requires_whole_blocks_on_index_page(
    heading_blocks, other_page_blocks, expected
):
    pages = {**PAGES, 230: heading_blocks + PAGES[230][1:], 229: other_page_blocks}
    assert m3((INDEX, COVERAGE, STANDARD), pages=pages) is expected
