"""Synthetic C1-C4 drafts anchored to the browser harness's verified claim.

Only the test fixture substitutes disclosed facts. No review or policy approval
is issued here. The original G/P/M claim and tag records are never changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evaluation.reconciliation_fixtures import build_case


def _replace(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_replace(item, mapping) for item in value]
    if isinstance(value, dict):
        return {mapping.get(key, key): _replace(item, mapping) for key, item in value.items()}
    return value


def seed_reconciliation_cases(composition, auth, run_id, claim_id, root: Path):
    claim = composition.claims.get(auth.tenant_id, run_id, claim_id)
    assert claim.source_quality == "verified"
    snapshot = composition.runs.store.snapshot(auth.tenant_id, run_id)
    inputs = composition.tags.load_inputs(auth.tenant_id, run_id, claim_id)
    track = inputs.packet.to_dict()["track"]
    company_id = snapshot["document"]["company"]["company_id"]
    results = []
    for name in (
        "c1-same-entities",
        "c2-same-period",
        "c3-policy-unresolved",
        "c4-definition-and-method",
    ):
        artifacts = root / name / "artifacts"
        bundle = build_case(name, artifacts)
        bundle = _replace(
            bundle,
            {
                "fixture-tenant": auth.tenant_id,
                "fixture-company": company_id,
                "fixture-claim": claim_id,
                "sr-v1": claim.document_version_id,
            },
        )
        packet = bundle["packet"]
        packet["claim"]["quote"], packet["claim"]["track"] = claim.quote, track
        claim_source = next(
            s for s in packet["sources"] if s["source_id"] == packet["claim"]["source_id"]
        )
        claim_source["quote"] = claim.quote
        document_id = claim_source["document_id"]
        refs = [s for s in packet["sources"] if s["document_id"] == document_id]
        text = "SYNTHETIC CROSS-DISCLOSURE TEST ONLY\n"
        for ref in refs:
            start = len(text)
            text += ref["quote"]
            ref["locator"] = f"chars:{start}:{len(text)}"
            text += "\n"
        payload = text.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        artifact = bundle["artifacts"][document_id]
        (artifacts / artifact["path"]).write_bytes(payload)
        artifact["sha256"] = digest
        document = bundle["documents"][document_id]
        document["artifact_sha256"] = digest
        for ref in refs:
            ref["artifact_sha256"] = digest
            binding = document["source_bindings"][ref["source_id"]]
            binding.update(locator=ref["locator"], quote=ref["quote"])
        for document in bundle["documents"].values():
            document["decision_binding"]["claim"] = json.loads(json.dumps(packet["claim"]))
        bundle["policies"] = {}
        results.append(
            composition.reconciliation.register_case(
                auth,
                run_id,
                claim_id,
                bundle,
                artifacts,
            )
        )
    return results
