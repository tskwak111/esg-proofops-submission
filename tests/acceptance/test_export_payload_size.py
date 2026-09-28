"""Export payload-size contract: shared original input packets and compressed bundles.

Covers the observed blocker where a published tag whose input packet is stored twice in
one revision record doubled the captured bytes and tripped EXPORT_SIZE_LIMIT before any
format was rendered. The 32 MiB caps are unchanged and are never patched away here.
"""

import json
from hashlib import sha256
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
from proofops.application.exports import (
    MAX_EXPORT_BYTES,
    REVISION_RECORDS_V1,
    REVISION_RECORDS_V2,
    SHARED_ORIGINAL_INPUTS,
    SOURCE_RECEIPTS_V1,
    decode_revision_record,
    decode_revision_records,
    encode_report_level_source_receipts,
    encode_revision_record,
)
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json

from tests.acceptance.test_exports import archive, create, exports
from tests.integration.test_revision_coverage import resolve


def test_identical_original_packet_is_stored_once_and_restores_exact_bytes():
    inputs = {"claim": {"claim_id": "c"}, "payload": ["x" * 64, {"n": 1}]}
    tag = {"tag_revision": 1, "inputs": json.loads(canonical_json(inputs))}
    record = encode_revision_record(tag, None, inputs)
    assert record["original_inputs_ref"] == SHARED_ORIGINAL_INPUTS
    assert "original_inputs" not in record
    assert len(canonical_json(record).encode()) < len(
        canonical_json(dict(tag=tag, decision=None, original_inputs=inputs)).encode()
    )
    restored = decode_revision_record(record, encoding=REVISION_RECORDS_V2)
    assert canonical_json(restored["original_inputs"]) == canonical_json(inputs)
    assert canonical_json(restored["tag"]) == canonical_json(tag)
    assert canonical_json(restored) == canonical_json(
        dict(tag=tag, decision=None, original_inputs=inputs)
    )


def test_unequal_packets_are_both_preserved_verbatim():
    inputs = {"claim": {"claim_id": "c"}, "payload": [1, 2, 3]}
    tag = {"tag_revision": 2, "inputs": {"claim": {"claim_id": "c"}, "payload": [1, 2, 4]}}
    record = encode_revision_record(tag, None, inputs)
    assert "original_inputs_ref" not in record
    assert canonical_json(record["original_inputs"]) == canonical_json(inputs)
    assert canonical_json(record["tag"]["inputs"]) == canonical_json(tag["inputs"])
    assert decode_revision_record(record, encoding=REVISION_RECORDS_V2) == record


def test_reviewed_tag_without_embedded_inputs_stays_inline_under_v2():
    inputs = {"claim": {"claim_id": "c"}}
    record = encode_revision_record({"tag_revision": 3}, None, inputs)
    assert record["original_inputs"] == inputs and "original_inputs_ref" not in record
    assert decode_revision_record(record, encoding=REVISION_RECORDS_V2) == record
    assert decode_revision_record(record, encoding=REVISION_RECORDS_V1) == record


def test_version_dispatch_defaults_to_legacy_and_refuses_mismatched_records():
    legacy = dict(tag={"inputs": {"a": 1}}, decision=None, original_inputs={"a": 2})
    assert decode_revision_record(legacy) == legacy
    assert REVISION_RECORDS_V1 != REVISION_RECORDS_V2
    shared = dict(
        tag={"inputs": {"a": 1}}, decision=None, original_inputs_ref=SHARED_ORIGINAL_INPUTS
    )
    # A v1 manifest can never carry a reference record, so the default encoding refuses it.
    with pytest.raises(ValueError):
        decode_revision_record(shared)
    with pytest.raises(ValueError):
        decode_revision_record(shared, encoding="shared_original_inputs_v3")
    with pytest.raises(ValueError):
        decode_revision_record(legacy, encoding="shared_original_inputs_v3")
    assert decode_revision_record(shared, encoding=REVISION_RECORDS_V2)["original_inputs"] == {
        "a": 1
    }


def test_malformed_records_raise_instead_of_losing_the_packet():
    ref = SHARED_ORIGINAL_INPUTS
    for broken in (
        dict(tag={}, decision=None),
        dict(tag={"inputs": {}}, decision=None, original_inputs_ref="tag"),
        dict(tag={"inputs": {}}, decision=None, original_inputs={}, original_inputs_ref=ref),
        dict(tag={}, decision=None, original_inputs_ref=ref),
        dict(tag={"inputs": None}, decision=None, original_inputs_ref=ref),
        dict(tag={"inputs": "not a packet"}, decision=None, original_inputs_ref=ref),
        dict(tag={"inputs": []}, decision=None, original_inputs_ref=ref),
        dict(tag=None, decision=None, original_inputs_ref=ref),
        dict(tag={"inputs": {}}, decision=None, original_inputs_ref=None),
    ):
        with pytest.raises(ValueError):
            decode_revision_record(broken, encoding=REVISION_RECORDS_V2)
    with pytest.raises(ValueError):
        decode_revision_record(["not", "an", "object"], encoding=REVISION_RECORDS_V2)


def test_http_bundle_is_deflated_and_declares_the_shared_packet_encoding(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    resolve(ws)
    result = create(ws).json()
    bundle, ticket, content = archive(ws, result)
    assert set(bundle.namelist()) == {"manifest.json", "report.json", "report.csv", "report.html"}
    assert {info.compress_type for info in bundle.infolist()} == {ZIP_DEFLATED}
    assert len(content) <= MAX_EXPORT_BYTES
    assert sha256(content).hexdigest() == ticket["sha256"]
    manifest = json.loads(bundle.read("manifest.json"))
    encoding = manifest["revision_records_encoding"]
    assert encoding == REVISION_RECORDS_V2
    records = manifest["revision_records"]
    assert records
    for record in records.values():
        restored = decode_revision_record(record, encoding=encoding)
        assert canonical_json(restored["original_inputs"]) == canonical_json(
            record["tag"]["inputs"]
        )


def test_stored_and_deflated_bundles_round_trip_the_same_member_bytes():
    from io import BytesIO

    payload = canonical_json({"x": ["y" * 512] * 32}).encode()
    archives = {}
    for compression in (ZIP_STORED, ZIP_DEFLATED):
        stream = BytesIO()
        with ZipFile(stream, "w", compression=compression) as bundle:
            info = ZipInfo("manifest.json")
            info.compress_type = compression
            bundle.writestr(info, payload)
        archives[compression] = stream.getvalue()
    assert len(archives[ZIP_DEFLATED]) < len(archives[ZIP_STORED])
    for raw in archives.values():
        with ZipFile(BytesIO(raw)) as bundle:
            assert bundle.read("manifest.json") == payload


def _source_receipt(identity):
    receipt = {
        "schema": "context_source_attestation_v1",
        **{
            key: identity[key]
            for key in ("tenant_id", "document_version_id", "parse_manifest_id", "source_sha256")
        },
        "records": [
            {
                "status": "verified",
                "ref": {"source_id": "source-1", "verification_state": "candidate"},
            }
        ],
    }
    return receipt | {"artifact_sha256": canonical_hash(receipt)}


def _receipt_manifest(count=10):
    identity = {
        "tenant_id": "tenant-a",
        "document_version_id": "version-a",
        "parse_manifest_id": "parse-a",
        "source_sha256": "a" * 64,
    }
    receipt = _source_receipt(identity)
    records = {
        f"claim-{index}": encode_revision_record(
            {
                "inputs": {
                    "claim": identity | {"claim_id": f"claim-{index}"},
                    "original": identity,
                },
                "elements": [
                    {
                        "element_id": "M3",
                        "state": "present",
                        "reason_code": "report-source-v1",
                        "credited_from": "source-1",
                        "evidence_refs": [
                            {"source_id": "source-1", "verification_state": "verified"}
                        ],
                    }
                ],
                "report_level_review": [
                    {
                        "element_id": "M3",
                        "policy": "report-source-v1",
                        "refs": [{"source_id": "source-1", "verification_state": "candidate"}],
                        "credited_from": "source-1",
                        "source_receipt": receipt,
                    }
                ],
            },
            None,
            {
                "claim": identity | {"claim_id": f"claim-{index}"},
                "original": identity,
            },
        )
        for index in range(count)
    }
    original = {
        claim_id: {
            "tag": record["tag"],
            "decision": record["decision"],
            "original_inputs": record["tag"]["inputs"],
        }
        for claim_id, record in records.items()
    }
    compacted, receipts = encode_report_level_source_receipts(records, identity=identity)
    return (
        identity
        | {
            "revision_records_encoding": REVISION_RECORDS_V2,
            "revision_records": compacted,
            "source_receipts_encoding": SOURCE_RECEIPTS_V1,
            "source_receipts": receipts,
        },
        original,
    )


def test_identical_report_level_receipts_are_stored_once_and_restore_exact_records():
    manifest, original = _receipt_manifest()
    assert len(manifest["source_receipts"]) == 1
    refs = {
        record["tag"]["report_level_review"][0]["source_receipt_ref"]
        for record in manifest["revision_records"].values()
    }
    assert len(refs) == 1
    assert all(
        "source_receipt" not in record["tag"]["report_level_review"][0]
        for record in manifest["revision_records"].values()
    )
    assert canonical_json(decode_revision_records(manifest)) == canonical_json(original)
    assert all(
        "source_receipt_ref" in record["tag"]["report_level_review"][0]
        for record in manifest["revision_records"].values()
    )


def test_report_level_receipt_export_validation_rejects_bad_references():
    manifest, _ = _receipt_manifest(count=1)
    digest = next(iter(manifest["source_receipts"]))
    receipt = manifest["source_receipts"][digest]

    tampered = json.loads(canonical_json(manifest))
    tampered["source_receipts"][digest]["records"][0]["status"] = "unverified"

    missing = json.loads(canonical_json(manifest))
    del missing["source_receipts"][digest]

    mismatched = json.loads(canonical_json(manifest))
    wrong = dict(receipt, tenant_id="tenant-b")
    wrong.pop("artifact_sha256")
    wrong["artifact_sha256"] = canonical_hash(wrong)
    wrong_digest = canonical_hash(wrong)
    mismatched["source_receipts"] = {wrong_digest: wrong}
    mismatched["revision_records"]["claim-0"]["tag"]["report_level_review"][0][
        "source_receipt_ref"
    ] = f"sha256:{wrong_digest}"

    mismatched_source = json.loads(canonical_json(manifest))
    wrong_source = dict(receipt, source_sha256="b" * 64)
    wrong_source.pop("artifact_sha256")
    wrong_source["artifact_sha256"] = canonical_hash(wrong_source)
    wrong_source_digest = canonical_hash(wrong_source)
    mismatched_source["source_receipts"] = {wrong_source_digest: wrong_source}
    mismatched_source["revision_records"]["claim-0"]["tag"]["report_level_review"][0][
        "source_receipt_ref"
    ] = f"sha256:{wrong_source_digest}"

    from proofops.application.exports import ExportRejected, build_export

    for broken in (tampered, missing, mismatched, mismatched_source):
        with pytest.raises(ExportRejected, match="EXPORT_INTEGRITY_FAILED"):
            build_export({"manifest": broken, "decisions": {}}, ["json"])


def test_swapped_same_document_receipt_must_match_review_refs():
    from proofops.application.exports import ExportRejected, build_export

    manifest, original = _receipt_manifest(count=1)
    digest = next(iter(manifest["source_receipts"]))
    wrong = json.loads(canonical_json(manifest["source_receipts"][digest]))
    wrong["records"][0]["ref"]["source_id"] = "source-2"
    wrong.pop("artifact_sha256")
    wrong["artifact_sha256"] = canonical_hash(wrong)
    wrong_digest = canonical_hash(wrong)
    manifest["source_receipts"] = {wrong_digest: wrong}
    manifest["revision_records"]["claim-0"]["tag"]["report_level_review"][0][
        "source_receipt_ref"
    ] = f"sha256:{wrong_digest}"
    with pytest.raises(ValueError, match="source receipt review mismatch"):
        decode_revision_records(manifest)
    with pytest.raises(ExportRejected, match="EXPORT_INTEGRITY_FAILED"):
        build_export({"manifest": manifest, "decisions": {}}, ["json"])

    original["claim-0"]["tag"]["report_level_review"][0]["source_receipt"] = wrong
    with pytest.raises(ValueError, match="source receipt review mismatch"):
        encode_report_level_source_receipts(original, identity=manifest)


def test_referenced_receipt_review_metadata_must_match_stored_element():
    manifest, _ = _receipt_manifest(count=1)
    for field, value in (
        ("element_id", "M4"),
        ("policy", "different-policy"),
        ("credited_from", "source-2"),
    ):
        broken = json.loads(canonical_json(manifest))
        review = broken["revision_records"]["claim-0"]["tag"]["report_level_review"][0]
        review[field] = value
        with pytest.raises(ValueError, match="source receipt review mismatch"):
            decode_revision_records(broken)


def test_old_inline_receipt_snapshot_still_validates_without_new_encoding():
    old = {
        "tenant_id": "tenant-a",
        "document_version_id": "version-a",
        "parse_manifest_id": "parse-a",
        "source_sha256": "a" * 64,
        "revision_records": {
            "claim-0": {
                "tag": {"report_level_review": [{"source_receipt": {"legacy": True}}]},
                "decision": None,
                "original_inputs": {"claim_id": "claim-0"},
            }
        },
    }
    assert decode_revision_records(old) == old["revision_records"]
