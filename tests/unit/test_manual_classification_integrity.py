"""Reviewed classification cannot change its pinned source or borrow another row."""

from copy import deepcopy
from dataclasses import asdict

import pytest
from proofops.application.tagging.manual_classification import (
    ClassificationRejected,
    classification_override,
    classification_snapshot,
    validate_manual_classification,
)
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_binding import DIMENSIONS, corpus, span
from tests.acceptance.test_citations import TENANT
from tests.unit.test_manual_classification import body


@pytest.mark.parametrize("change", ["digest", "graph", "document", "borrowed_row"])
def test_replay_rejects_changed_pins_and_outside_atomic_source(change):
    graph, claim, refs = corpus()
    result = validate_manual_classification(claim, graph, body(claim), tenant_id=TENANT)
    record = classification_snapshot(
        result,
        tenant_id=TENANT,
        run_id=claim.claim_id,
        claim=claim,
        graph=graph,
        lineage_checkpoint_sha256="a" * 64,
        origin="human_classification",
        classified_by="reviewer",
        reason="original source reviewed",
    )
    changed = deepcopy(record)
    if change == "digest":
        changed["record_sha256"] = "0" * 64
    else:
        if change == "graph":
            changed["graph_sha256"] = "b" * 64
        elif change == "document":
            changed["document_version_id"] = claim.claim_id
        else:
            changed["dimensions"]["metric"] = asdict(span(refs[1], DIMENSIONS["metric"]))
        changed["record_sha256"] = canonical_hash(
            {k: v for k, v in changed.items() if k != "record_sha256"}
        )
    with pytest.raises(ClassificationRejected):
        classification_override(changed, claim, graph, tenant_id=TENANT)
