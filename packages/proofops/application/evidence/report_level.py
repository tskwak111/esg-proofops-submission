"""Deterministic report-level crediting of M2/M3 (original §6 2-4; user decision 2026-09-25).

The original allows 적용범위 and 외부검증 to be credited from elsewhere in the same
document, with the location kept in ``credited_from``. It does not define what binds
a report-level statement to one claim, so each binding rule is a named, versioned
policy the user approved. A reviewer proposes exact spans; this module only checks
the literal chain against the verified page texts and never searches or infers.
Numbers and target years never use this path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from unicodedata import normalize

from proofops.domain.values import SourceRef

# M3: claim page -> GRI Index row (disclosure + page list) -> disclosure listed in a
# third-party assurance statement that names its standard on the same page.
# The index heading may also be separate whole blocks "Index" and "GRI" or
# "GRI Standards" on the row's page, after NFC/whitespace normalization, ignoring case.
GRI_ASSURED_PAGE_V1 = "GRI_ASSURED_PAGE_V1"
# M2: the report's own "보고 범위" statement, only for claims that state no scope.
REPORT_SCOPE_V1 = "REPORT_SCOPE_V1"
POLICIES = {"M3": GRI_ASSURED_PAGE_V1, "M2": REPORT_SCOPE_V1}

_DISCLOSURE = r"\d{1,3}-\d{1,2}"
_INDEX_ROW = re.compile(
    rf"^(?P<code>{_DISCLOSURE})\s+(?P<label>\D*?)\s*(?P<pages>\d+(?:\s*[-–]\s*\d+)?"
    r"(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)$"
)
_RANGE = re.compile(rf"(?P<start>{_DISCLOSURE})\s*~\s*(?P<end>{_DISCLOSURE})")
_STANDARD = re.compile(r"AA1000\s*AS|ISAE\s*3000|ISSA\s*5000", re.IGNORECASE)
_INDEX_HEADING = re.compile(r"GRI(?:\s+Content)?\s+Index|GRI\s*인덱스", re.IGNORECASE)
_ASSURANCE_HEADING = re.compile(
    r"제3자\s*검증|검증\s*의견서|Independent\s+Assurance|Assurance\s+Statement", re.IGNORECASE
)
_SCOPE_HEADING = re.compile(r"보고\s*범위|Reporting\s+(?:Scope|Boundary)", re.IGNORECASE)
_ENTITY = re.compile(r"주식회사|㈜|\(주\)|개별\s*기업|연결\s*(?:기준|대상)|법인|사업장")
# A claim naming its own organisational or site scope must be credited locally (M2).
_OWN_SCOPE = re.compile(
    r"사업장|공장|데이터\s*센터|센터|사옥|본사|지사|법인|자회사|계열사|국내|해외|글로벌|지역|"
    r"춘천|세종|전\s*사업장|전사"
)


def _text(value: str) -> str:
    return " ".join(normalize("NFC", value).split())


def _code(value: str) -> tuple[int, int]:
    left, right = value.split("-")
    return int(left), int(right)


def _pages(expression: str) -> set[int]:
    pages: set[int] = set()
    for part in expression.split(","):
        bounds = [int(x) for x in re.split(r"\s*[-–]\s*", part.strip())]
        if len(bounds) == 2 and bounds[0] <= bounds[1]:
            pages.update(range(bounds[0], bounds[1] + 1))
        elif len(bounds) == 1:
            pages.add(bounds[0])
    return pages


def _covers(coverage: str, code: str) -> bool:
    target = _code(code)
    for match in _RANGE.finditer(coverage):
        start, end = _code(match["start"]), _code(match["end"])
        if start[0] == end[0] == target[0] and start[1] <= target[1] <= end[1]:
            return True
    listed = _RANGE.sub(" ", coverage)
    return code in re.findall(_DISCLOSURE, listed)


def _on_page(ref: SourceRef, page_texts: Mapping[int, Sequence[str]], pattern: re.Pattern) -> bool:
    return any(pattern.search(_text(text)) for text in page_texts.get(ref.page_num, ()))


def _gri_assured_page(refs, claim_refs, page_texts, credited_from) -> bool:
    if len(refs) != 3:
        return False
    index, coverage, standard = refs
    labels = {ref.printed_page_label for ref in claim_refs}
    if len(labels) != 1 or not next(iter(labels), None) or not next(iter(labels)).isdigit():
        return False
    row = _INDEX_ROW.match(_text(index.quote))
    if row is None or int(next(iter(labels))) not in _pages(row["pages"]):
        return False
    index_page_blocks = {_text(text).casefold() for text in page_texts.get(index.page_num, ())}
    return (
        (
            _on_page(index, page_texts, _INDEX_HEADING)
            or ("index" in index_page_blocks and bool(index_page_blocks & {"gri", "gri standards"}))
        )
        and _covers(_text(coverage.quote), row["code"])
        and bool(_STANDARD.search(_text(standard.quote)))
        and standard.page_num == coverage.page_num
        and _on_page(coverage, page_texts, _ASSURANCE_HEADING)
        and credited_from == coverage.source_id
    )


def _report_scope(refs, claim_quote, page_texts, credited_from) -> bool:
    if len(refs) != 1:
        return False
    (scope,) = refs
    return (
        not _OWN_SCOPE.search(_text(claim_quote))
        and bool(_ENTITY.search(_text(scope.quote)))
        and _on_page(scope, page_texts, _SCOPE_HEADING)
        and credited_from == scope.source_id
    )


def check_report_level(
    element_id: str,
    policy: str | None,
    refs: Sequence[SourceRef],
    *,
    claim_refs: Sequence[SourceRef],
    claim_quote: str,
    page_texts: Mapping[int, Sequence[str]],
    credited_from: str | None,
) -> bool:
    """True only when the named policy's literal chain holds; any doubt is False."""
    if POLICIES.get(element_id) != policy or not refs or not claim_refs:
        return False
    if any(ref.verification_state != "verified" for ref in (*refs, *claim_refs)):
        return False
    if policy == GRI_ASSURED_PAGE_V1:
        return _gri_assured_page(tuple(refs), tuple(claim_refs), page_texts, credited_from)
    return _report_scope(tuple(refs), claim_quote, page_texts, credited_from)
