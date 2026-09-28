"""Exercise the actual search guard rather than inferring isolation from saved hashes."""

from dataclasses import replace
from uuid import uuid4

import pytest

from evaluation.section_pipeline import SectionSearch
from tests.acceptance.test_claims import COMPOUND, TENANT, graph_of
from tests.integration.test_section_pipeline import section_map


@pytest.mark.parametrize(
    "field", ["tenant_id", "document_version_id", "parse_manifest_id", "index_generation"]
)
def test_foreign_scope_cannot_search_same_document_index(field):
    graph = graph_of(COMPOUND, "other", "배출량 데이터", "범위", "보증")
    search = SectionSearch(graph, section_map(graph), tenant_id=TENANT)
    assert search.search(search.scope, "배출량").hits
    foreign = replace(search.scope, **{field: str(uuid4())})
    with pytest.raises(ValueError, match="section search scope mismatch"):
        search.search(foreign, "배출량")
