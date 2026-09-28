"""Lossless scoped sharing of export search coverage."""

import json

import pytest
from proofops.application.exports import (
    MAX_EXPORT_BYTES,
    REVISION_RECORDS_V2,
    SEARCH_COVERAGE_V1,
    decode_revision_records,
    encode_revision_record,
    encode_search_coverages,
)
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json

SCOPE = {
    "tenant_id": "tenant-a",
    "document_version_id": "doc-a",
    "parse_manifest_id": "parse-a",
    "source_sha256": "a" * 64,
}


def _manifest(extra_coverage=None):
    quality = {"source-1": {"state": "ok", "payload": "q" * 100}}
    issues = [{"kind": "unreadable", "source_ids": ["source-2"]}]
    coverages, documents, records, originals = {}, {}, {}, {}
    for index in range(3):
        coverage = {
            "source_quality": quality,
            "quality_issues": issues,
            "unprocessed_source_ids": [f"source-{index}"] * 100,
        } | (extra_coverage or {})
        packet = SCOPE | {"search_coverage": coverage, "claim_id": f"claim-{index}"}
        inputs = {
            "claim": SCOPE | {"claim_id": f"claim-{index}"},
            "original": SCOPE,
            "original_packet": packet,
            "packet": dict(packet),
        }
        tag = {"inputs": inputs, "input_snapshot_sha256": canonical_hash(inputs)}
        raw = encode_revision_record(tag, None, inputs)
        originals[f"claim-{index}"] = {
            "tag": tag,
            "decision": None,
            "original_inputs": inputs,
        }
        encoded, new_coverages, new_documents = encode_search_coverages(raw, identity=SCOPE)
        records[f"claim-{index}"] = encoded
        coverages.update(new_coverages)
        documents.update(new_documents)
    return (
        SCOPE
        | {
            "revision_records_encoding": REVISION_RECORDS_V2,
            "revision_records": records,
            "search_coverage_encoding": SEARCH_COVERAGE_V1,
            "search_coverages": coverages,
            "coverage_documents": documents,
        },
        originals,
    )


def test_shared_coverage_round_trip_and_dedupe():
    manifest, originals = _manifest()
    assert len(manifest["search_coverages"]) == 3
    assert len(manifest["coverage_documents"]) == 2
    for record in manifest["revision_records"].values():
        assert "search_coverage" not in record["tag"]["inputs"]["packet"]
    decoded = decode_revision_records(manifest)
    assert canonical_json(decoded) == canonical_json(originals)
    for claim_id, record in decoded.items():
        assert canonical_hash(record["tag"]) == canonical_hash(originals[claim_id]["tag"])
        assert canonical_hash(record["original_inputs"]) == canonical_hash(
            originals[claim_id]["original_inputs"]
        )
        assert canonical_hash(record["tag"]["inputs"]["packet"]) == canonical_hash(
            originals[claim_id]["tag"]["inputs"]["packet"]
        )
    assert MAX_EXPORT_BYTES == 33_554_432


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "tamper",
        "scope",
        "extra",
        "inline",
        "document_missing",
        "document_tamper",
        "document_scope",
        "document_extra",
        "packet_scope",
        "bad_ref",
        "unknown_encoding",
    ],
)
def test_shared_coverage_rejects_invalid_tables(damage):
    manifest, _ = _manifest()
    manifest = json.loads(canonical_json(manifest))
    digest = next(iter(manifest["search_coverages"]))
    doc_digest = next(iter(manifest["coverage_documents"]))
    if damage == "missing":
        del manifest["search_coverages"][digest]
    elif damage == "tamper":
        manifest["search_coverages"][digest]["value"]["unprocessed_source_ids"].append("bad")
    elif damage == "scope":
        manifest["search_coverages"][digest]["scope"]["tenant_id"] = "other"
    elif damage == "extra":
        manifest["search_coverages"]["0" * 64] = manifest["search_coverages"][digest]
    elif damage == "inline":
        packet = next(iter(manifest["revision_records"].values()))["tag"]["inputs"]["packet"]
        packet["search_coverage"] = {}
    elif damage == "document_missing":
        del manifest["coverage_documents"][doc_digest]
    elif damage == "document_tamper":
        manifest["coverage_documents"][doc_digest]["value"] = "bad"
    elif damage == "document_scope":
        manifest["coverage_documents"][doc_digest]["scope"]["source_sha256"] = "b" * 64
    elif damage == "document_extra":
        manifest["coverage_documents"]["0" * 64] = manifest["coverage_documents"][doc_digest]
    elif damage == "packet_scope":
        next(iter(manifest["revision_records"].values()))["tag"]["inputs"]["packet"][
            "tenant_id"
        ] = "other"
    elif damage == "bad_ref":
        next(iter(manifest["revision_records"].values()))["tag"]["inputs"]["packet"][
            "search_coverage_ref"
        ] = "sha256:bad"
    else:
        manifest["search_coverage_encoding"] = "unknown"
    with pytest.raises(ValueError):
        decode_revision_records(manifest)


def test_legacy_inline_coverage_still_decodes():
    manifest, originals = _manifest()
    old = {"revision_records": originals}
    assert canonical_json(decode_revision_records(old)) == canonical_json(originals)
    del manifest["search_coverage_encoding"]
    with pytest.raises(ValueError):
        decode_revision_records(manifest)
    with pytest.raises(ValueError):
        decode_revision_records({"revision_records_encoding": "unknown", "revision_records": {}})


@pytest.mark.parametrize(
    "extra",
    [
        {"source_quality_ref": "original quality reference"},
        {"quality_issues_ref": {"original": ["issue reference"]}},
        {"__shared_coverage__": None},
        {
            "source_quality_ref": "original quality reference",
            "quality_issues_ref": "original issue reference",
            "__shared_coverage__": {"refs": {"source_quality": "user data"}},
        },
    ],
)
def test_shared_coverage_preserves_reserved_names_and_rejects_tampering(extra):
    manifest, originals = _manifest(extra)
    assert canonical_json(decode_revision_records(manifest)) == canonical_json(originals)
    digest = next(iter(manifest["search_coverages"]))
    envelope = manifest["search_coverages"][digest]["value"]["__shared_coverage__"]
    envelope["refs"]["source_quality"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="missing coverage table entry"):
        decode_revision_records(manifest)
