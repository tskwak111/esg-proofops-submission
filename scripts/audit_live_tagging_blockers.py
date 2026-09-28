"""Inspect immutable live-tagging checkpoints offline; never infer source approval.

Usage: uv run python scripts/audit_live_tagging_blockers.py STATE_DIRECTORY [...]
This verifies checkpoint/packet hashes, not PDF geometry or semantic correctness.
"""

import json
import sqlite3
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path
from unicodedata import normalize

from proofops.application.evidence.binding import relation_tags_for
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef


def normalized(text):
    return " ".join(normalize("NFC", text).split())


def relation_diagnostic(records):
    """Compare retained candidates, not semantic correctness or provider independence."""
    validated = [r for r in records if r.get("status") == "validated_candidate"]
    complete = len(records) == len(validated) == 3 and {r["replicate_id"] for r in records} == {
        1,
        2,
        3,
    }
    differences = []
    values = [r["values"] for r in validated]
    for source_id in sorted(set().union(*(v.keys() for v in values))):
        roles = [v.get(source_id, {}) for v in values]
        for dimension in sorted(set().union(*(r.keys() for r in roles))):
            selections = [
                dict(present=dimension in role, value=role.get(dimension)) for role in roles
            ]
            if len({canonical_hash(v) for v in selections}) > 1:
                differences.append(
                    dict(
                        source_id=source_id,
                        dimension=dimension,
                        replicas=[
                            dict(replicate_id=record["replicate_id"], **selection)
                            for record, selection in zip(validated, selections, strict=True)
                        ],
                    )
                )
    return dict(
        all_three_validated=complete,
        value_consensus=complete and len({canonical_hash(v) for v in values}) == 1,
        records=[{k: v for k, v in r.items() if k != "values"} for r in records],
        differences=differences,
    )


def audit(state):
    database = (Path(state) / "state.sqlite3").resolve()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        runs = db.execute(
            "SELECT tenant_id,run_id,value FROM job_records WHERE kind='run' AND record_id='META'"
        ).fetchall()
        if len(runs) != 1:
            raise ValueError("one pilot run per state required")
        tenant, run_id, metadata = runs[0]
        expected = json.loads(metadata)["tag_snapshot_sha256"]
        matches = [
            raw
            for (raw,) in db.execute(
                "SELECT value FROM job_records WHERE kind='artifact' AND tenant_id=? AND run_id=?",
                (tenant, run_id),
            )
            if sha256(raw).hexdigest() == expected
        ]
    if len(matches) != 1:
        raise ValueError("tag checkpoint missing, duplicated or digest mismatch")
    checkpoint = json.loads(matches[0])
    assert checkpoint["synthetic"] is False and checkpoint["run_id"] == run_id
    counts = Counter()
    claims = []
    relation_claims = []
    for item in checkpoint["claims"]:
        if "relation_records" in item:
            relation_claims.append(
                dict(
                    claim_id=item["claim_id"],
                    reason=item.get("reason"),
                    **relation_diagnostic(item["relation_records"]),
                )
            )
        review = item.get("review_inputs")
        if review is None:
            counts["claims_without_review"] += 1
            continue
        packet = review["packet"]
        assert (
            canonical_hash({k: v for k, v in packet.items() if k != "packet_sha256"})
            == review["packet_sha256"]
        )
        missing = sorted(
            key
            for key in ("entity", "metric", "reporting_period")
            if not review["dimensions"].get(key)
        )
        relations = {
            key: {role: SourceRef(**ref) if ref else None for role, ref in roles.items()}
            for key, roles in review["relation_tags"].items()
        }
        entries = []
        for run in review["tag_runs"]:
            if not run["raw_response_json"] or not run["guarded"]:
                counts["invalid_or_failed_replies"] += 1
                continue
            raw = json.loads(run["raw_response_json"])
            guarded = {e["element_id"]: e for e in run["guarded"]["elements"]}
            for element in raw["elements"]:
                if element["state"] != "present":
                    continue
                counts["raw_present_element_votes"] += 1
                refs = element["evidence_refs"]
                roles_missing = [
                    ref["source_id"]
                    for ref in refs
                    if not relation_tags_for(SourceRef(**ref), relations)
                ]
                mismatch = element["normalized_value"] is not None and not any(
                    normalized(element["normalized_value"]) == normalized(ref["quote"])
                    for ref in refs
                )
                state_after = guarded[element["element_id"]]["state"]
                counts["raw_present_to_" + state_after] += 1
                counts["present_votes_with_no_relation_roles"] += bool(roles_missing)
                counts["present_votes_with_literal_value_mismatch"] += mismatch
                entries.append(
                    dict(
                        replicate_id=raw["replicate_id"],
                        element_id=element["element_id"],
                        guarded_state=state_after,
                        missing_relation_source_ids=roles_missing,
                        literal_value_mismatch=mismatch,
                    )
                )
        claims.append(
            dict(
                claim_id=item["claim_id"],
                quote=review["claim"]["quote"],
                missing_required_claim_dimensions=missing,
                relation_source_count=len(review["relation_tags"]),
                present_votes=entries,
            )
        )
    return dict(
        run_id=run_id,
        source_sha256=checkpoint["source_sha256"],
        checkpoint_sha256=expected,
        state_path=str(state),
        additional_model_calls=0,
        counts=dict(counts),
        claims=claims,
        relation_claims=relation_claims,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(
        json.dumps(
            dict(schema="live_tagging_blocker_audit_v1", runs=[audit(p) for p in sys.argv[1:]]),
            ensure_ascii=False,
            indent=2,
        )
    )
