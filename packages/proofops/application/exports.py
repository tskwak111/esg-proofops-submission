"""Capture revisions once, then render only immutable inputs; no grading/model calls."""

from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from itertools import chain
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from proofops.application.reporting import build_report_model, render_report
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json

MAX_EXPORT_BYTES = 32 * 1024 * 1024

# manifest.revision_records encodings. A manifest without the field is the original
# v1 layout that always inlined original_inputs; every already-frozen export keeps
# reading unchanged because decode_revision_record still accepts that layout.
REVISION_RECORDS_V1 = "inline_original_inputs_v1"
REVISION_RECORDS_V2 = "shared_original_inputs_v2"
SHARED_ORIGINAL_INPUTS = "tag.inputs"
SOURCE_RECEIPTS_V1 = "shared_source_receipts_sha256_v1"
SOURCE_RECEIPT_REF_PREFIX = "sha256:"
SOURCE_RECEIPT_IDENTITY_KEYS = (
    "tenant_id",
    "document_version_id",
    "parse_manifest_id",
    "source_sha256",
)
SEARCH_COVERAGE_V1 = "shared_search_coverage_sha256_v1"
SEARCH_COVERAGE_REF = "search_coverage_ref"
SEARCH_COVERAGE_ENVELOPE = "__shared_coverage__"
SEARCH_COVERAGE_FIELDS = ("source_quality", "quality_issues")


def encode_revision_record(tag, decision, inputs):
    """Keep every observed byte, storing the original input packet once when it is identical.

    The second copy is dropped only when the canonical bytes of ``tag["inputs"]`` and the
    revision-1 packet are equal, so unequal packets are always both preserved verbatim.
    """
    record = dict(tag=tag, decision=decision)
    stored = tag.get("inputs") if isinstance(tag, dict) else None
    if stored is not None and canonical_json(stored) == canonical_json(inputs):
        record["original_inputs_ref"] = SHARED_ORIGINAL_INPUTS
        return record
    record["original_inputs"] = inputs
    return record


def decode_revision_record(record, *, encoding=REVISION_RECORDS_V1):
    """Restore the byte-identical inline record, dispatching on the manifest encoding.

    Callers pass ``manifest.get("revision_records_encoding", REVISION_RECORDS_V1)`` so a
    legacy manifest stays v1 and an unknown encoding is refused instead of guessed.
    """
    if encoding not in (REVISION_RECORDS_V1, REVISION_RECORDS_V2):
        raise ValueError("unsupported revision_records encoding")
    if not isinstance(record, dict):
        raise ValueError("revision record must be an object")
    reference = record.get("original_inputs_ref")
    if reference is None:
        if "original_inputs_ref" in record or "original_inputs" not in record:
            raise ValueError("revision record is missing original_inputs")
        return dict(record)
    if encoding != REVISION_RECORDS_V2:
        raise ValueError("original_inputs reference is not valid for this encoding")
    if reference != SHARED_ORIGINAL_INPUTS or "original_inputs" in record:
        raise ValueError("unsupported original_inputs reference")
    tag = record.get("tag")
    if not isinstance(tag, dict) or not isinstance(tag.get("inputs"), dict):
        raise ValueError("original_inputs reference has no tag.inputs target")
    restored = dict(record)
    restored.pop("original_inputs_ref")
    restored["original_inputs"] = tag["inputs"]
    return restored


def _source_identity(value):
    if not isinstance(value, dict):
        raise ValueError("source receipt identity must be an object")
    identity = {key: value.get(key) for key in SOURCE_RECEIPT_IDENTITY_KEYS}
    if any(not isinstance(item, str) or not item for item in identity.values()):
        raise ValueError("source receipt identity is incomplete")
    return identity


def _validate_source_receipt(receipt, identity):
    if not isinstance(receipt, dict):
        raise ValueError("source receipt must be an object")
    expected = _source_identity(identity)
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("source receipt identity mismatch")
    artifact_hash = receipt.get("artifact_sha256")
    body = {key: value for key, value in receipt.items() if key != "artifact_sha256"}
    if not isinstance(artifact_hash, str) or canonical_hash(body) != artifact_hash:
        raise ValueError("source receipt artifact hash mismatch")


def _validate_review_source_receipt(review, receipt, tag):
    refs, records = review.get("refs"), receipt.get("records")
    elements = tag.get("elements")
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(ref, dict) for ref in refs)
        or not isinstance(records, list)
        or len(records) != len(refs)
        or any(
            not isinstance(item, dict) or item.get("ref") != ref for item, ref in zip(records, refs)
        )
        or not isinstance(elements, list)
    ):
        raise ValueError("source receipt review mismatch")
    element_id, policy, credited_from = (
        review.get("element_id"),
        review.get("policy"),
        review.get("credited_from"),
    )
    matches = [
        element
        for element in elements
        if isinstance(element, dict) and element.get("element_id") == element_id
    ]
    if (
        not isinstance(element_id, str)
        or not element_id
        or not isinstance(policy, str)
        or not policy
        or not isinstance(credited_from, str)
        or credited_from not in {ref.get("source_id") for ref in refs}
        or len(matches) != 1
    ):
        raise ValueError("source receipt review mismatch")
    element = matches[0]
    evidence_refs = element.get("evidence_refs")
    if (
        element.get("state") != "present"
        or element.get("reason_code") != policy
        or element.get("credited_from") != credited_from
        or not isinstance(evidence_refs, list)
        or len(evidence_refs) != len(refs)
        or any(
            not isinstance(item, dict)
            or {**item, "verification_state": "candidate"}
            != {**ref, "verification_state": "candidate"}
            for item, ref in zip(evidence_refs, refs)
        )
    ):
        raise ValueError("source receipt review mismatch")


def _validate_revision_source_identity(record, claim_id, identity):
    expected = _source_identity(identity)
    packets = [record.get("original_inputs")]
    tag = record.get("tag")
    if isinstance(tag, dict) and isinstance(tag.get("inputs"), dict):
        packets.append(tag["inputs"])
    for packet in packets:
        if not isinstance(packet, dict):
            raise ValueError("revision source packet is missing")
        claim, original = packet.get("claim"), packet.get("original")
        if not isinstance(claim, dict) or not isinstance(original, dict):
            raise ValueError("revision source packet identity is incomplete")
        actual = {
            "tenant_id": claim.get("tenant_id"),
            "document_version_id": claim.get("document_version_id"),
            "parse_manifest_id": original.get("parse_manifest_id"),
            "source_sha256": original.get("source_sha256"),
        }
        if (
            actual != expected
            or claim.get("claim_id") != claim_id
            or claim.get("parse_manifest_id") != expected["parse_manifest_id"]
            or claim.get("source_sha256") != expected["source_sha256"]
            or original.get("document_version_id") != expected["document_version_id"]
        ):
            raise ValueError("revision source identity mismatch")


def encode_report_level_source_receipts(revision_records, *, identity):
    """Deduplicate verified report-level receipts across captured revision records."""
    if not isinstance(revision_records, dict):
        raise ValueError("revision records must be an object")
    encoded, receipts = {}, {}
    for claim_id, record in revision_records.items():
        if not isinstance(record, dict):
            raise ValueError("revision record must be an object")
        encoded_record = dict(record)
        tag = record.get("tag")
        if isinstance(tag, dict) and "report_level_review" in tag:
            reviews = tag["report_level_review"]
            if not isinstance(reviews, list):
                raise ValueError("report-level reviews must be an array")
            encoded_tag = dict(tag)
            encoded_reviews = []
            for review in reviews:
                if not isinstance(review, dict):
                    raise ValueError("report-level review must be an object")
                if "source_receipt_ref" in review:
                    raise ValueError("source receipt is already referenced")
                encoded_review = dict(review)
                if "source_receipt" in review:
                    expected = _source_identity(identity)
                    restored = decode_revision_record(record, encoding=REVISION_RECORDS_V2)
                    _validate_revision_source_identity(restored, claim_id, expected)
                    receipt = review["source_receipt"]
                    _validate_source_receipt(receipt, expected)
                    _validate_review_source_receipt(review, receipt, tag)
                    digest = canonical_hash(receipt)
                    previous = receipts.get(digest)
                    if previous is not None and canonical_json(previous) != canonical_json(receipt):
                        raise ValueError("source receipt hash collision")
                    receipts[digest] = receipt
                    del encoded_review["source_receipt"]
                    encoded_review["source_receipt_ref"] = f"{SOURCE_RECEIPT_REF_PREFIX}{digest}"
                encoded_reviews.append(encoded_review)
            encoded_tag["report_level_review"] = encoded_reviews
            encoded_record["tag"] = encoded_tag
        encoded[claim_id] = encoded_record
    return encoded, receipts


def _scoped_entry(value, scope):
    return {"scope": scope, "value": value}


def _packet_scope(packet, scope):
    if not isinstance(packet, dict) or any(
        packet.get(key) != value for key, value in scope.items()
    ):
        raise ValueError("coverage packet scope mismatch")


def _coverage_packets(record):
    seen = set()
    tag = record.get("tag")
    if isinstance(tag, dict) and isinstance(tag.get("inputs"), dict):
        seen.add(id(tag["inputs"]))
        yield tag["inputs"]
    if (
        isinstance(record.get("original_inputs"), dict)
        and id(record["original_inputs"]) not in seen
    ):
        yield record["original_inputs"]


def encode_search_coverages(record, *, identity):
    """Compact one revision without changing the source record or stored revisions."""
    scope = _source_identity(identity)
    encoded = dict(record)
    if isinstance(record.get("tag"), dict) and isinstance(record["tag"].get("inputs"), dict):
        encoded["tag"] = dict(record["tag"])
        encoded["tag"]["inputs"] = dict(record["tag"]["inputs"])
    if isinstance(record.get("original_inputs"), dict):
        encoded["original_inputs"] = dict(record["original_inputs"])
    coverages, documents = {}, {}
    for inputs in _coverage_packets(encoded):
        for name in ("original_packet", "packet"):
            packet = inputs.get(name)
            if not isinstance(packet, dict) or "search_coverage" not in packet:
                continue
            _packet_scope(packet, scope)
            if SEARCH_COVERAGE_REF in packet:
                raise ValueError("coverage already referenced")
            coverage = packet["search_coverage"]
            if not isinstance(coverage, dict):
                raise ValueError("coverage must be an object")
            digest = canonical_hash(coverage)
            compact = dict(coverage)
            envelope = {"refs": {}}
            if SEARCH_COVERAGE_ENVELOPE in compact:
                envelope["original_value"] = compact.pop(SEARCH_COVERAGE_ENVELOPE)
            for field in SEARCH_COVERAGE_FIELDS:
                if field in compact:
                    value = compact.pop(field)
                    field_digest = canonical_hash(value)
                    documents[field_digest] = _scoped_entry(value, scope)
                    envelope["refs"][field] = f"sha256:{field_digest}"
            compact[SEARCH_COVERAGE_ENVELOPE] = envelope
            coverages[digest] = _scoped_entry(compact, scope)
            inputs[name] = {key: value for key, value in packet.items() if key != "search_coverage"}
            inputs[name][SEARCH_COVERAGE_REF] = f"sha256:{digest}"
    return encoded, coverages, documents


def _resolve_shared(reference, table, scope, used):
    if (
        not isinstance(reference, str)
        or len(reference) != 71
        or not reference.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in reference[7:])
    ):
        raise ValueError("malformed coverage reference")
    digest = reference[7:]
    entry = table.get(digest)
    if not isinstance(entry, dict) or set(entry) != {"scope", "value"}:
        raise ValueError("missing coverage table entry")
    if entry["scope"] != scope:
        raise ValueError("coverage table scope mismatch")
    used.add(digest)
    return digest, entry["value"]


def _decode_coverage_record(
    record, encoding, coverages, documents, scope, used_coverages, used_documents
):
    if encoding is None:
        for inputs in _coverage_packets(record):
            for name in ("original_packet", "packet"):
                packet = inputs.get(name)
                if isinstance(packet, dict) and SEARCH_COVERAGE_REF in packet:
                    raise ValueError("coverage reference has no encoding")
        return
    tag = record.get("tag")
    if isinstance(tag, dict) and isinstance(tag.get("inputs"), dict):
        copied_tag = dict(tag)
        copied_tag["inputs"] = dict(tag["inputs"])
        record["tag"] = copied_tag
    original = record.get("original_inputs")
    if isinstance(original, dict):
        record["original_inputs"] = (
            record["tag"]["inputs"]
            if isinstance(tag, dict) and original is tag.get("inputs")
            else dict(original)
        )
    for inputs in _coverage_packets(record):
        for name in ("original_packet", "packet"):
            packet = inputs.get(name)
            if not isinstance(packet, dict):
                continue
            _packet_scope(packet, scope)
            if "search_coverage" in packet:
                raise ValueError("inline coverage under shared encoding")
            if SEARCH_COVERAGE_REF not in packet:
                continue
            digest, compact = _resolve_shared(
                packet[SEARCH_COVERAGE_REF], coverages, scope, used_coverages
            )
            if not isinstance(compact, dict):
                raise ValueError("coverage entry must be an object")
            coverage = dict(compact)
            envelope = coverage.pop(SEARCH_COVERAGE_ENVELOPE, None)
            if (
                not isinstance(envelope, dict)
                or set(envelope) not in ({"refs"}, {"refs", "original_value"})
                or not isinstance(envelope["refs"], dict)
                or set(envelope["refs"]) - set(SEARCH_COVERAGE_FIELDS)
            ):
                raise ValueError("malformed coverage envelope")
            if "original_value" in envelope:
                coverage[SEARCH_COVERAGE_ENVELOPE] = envelope["original_value"]
            for field in SEARCH_COVERAGE_FIELDS:
                if field in envelope["refs"]:
                    if field in coverage:
                        raise ValueError("inline and referenced coverage field")
                    field_digest, value = _resolve_shared(
                        envelope["refs"][field], documents, scope, used_documents
                    )
                    if canonical_hash(value) != field_digest:
                        raise ValueError("coverage document hash mismatch")
                    coverage[field] = value
            if canonical_hash(coverage) != digest:
                raise ValueError("search coverage hash mismatch")
            inputs[name] = {
                key: value for key, value in packet.items() if key != SEARCH_COVERAGE_REF
            }
            inputs[name]["search_coverage"] = coverage
    tag = record.get("tag")
    if isinstance(tag, dict) and isinstance(tag.get("inputs"), dict):
        expected = tag.get("input_snapshot_sha256")
        if expected is not None and canonical_hash(tag["inputs"]) != expected:
            raise ValueError("tag input snapshot hash mismatch")


def _iter_decoded_revision_records(manifest):
    """Validate and restore one revision at a time, including both shared tables."""
    if not isinstance(manifest, dict):
        raise ValueError("export manifest must be an object")
    raw_records = manifest.get("revision_records", {})
    if not isinstance(raw_records, dict):
        raise ValueError("revision records must be an object")
    encoding = manifest.get("revision_records_encoding", REVISION_RECORDS_V1)
    if encoding not in (REVISION_RECORDS_V1, REVISION_RECORDS_V2):
        raise ValueError("unsupported revision_records encoding")
    receipt_encoding = manifest.get("source_receipts_encoding")
    if receipt_encoding not in (None, SOURCE_RECEIPTS_V1):
        raise ValueError("unsupported source receipt encoding")
    receipts = manifest.get("source_receipts")
    if receipt_encoding is None:
        if "source_receipts" in manifest:
            raise ValueError("source receipt reference has no encoding")
    elif not isinstance(receipts, dict):
        raise ValueError("source receipt table is missing")
    coverage_encoding = manifest.get("search_coverage_encoding")
    if coverage_encoding not in (None, SEARCH_COVERAGE_V1):
        raise ValueError("unsupported coverage encoding")
    coverages, documents = manifest.get("search_coverages"), manifest.get("coverage_documents")
    if coverage_encoding is None:
        if "search_coverages" in manifest or "coverage_documents" in manifest:
            raise ValueError("coverage table has no encoding")
    elif not isinstance(coverages, dict) or not isinstance(documents, dict):
        raise ValueError("coverage tables are missing")
    scope = _source_identity(manifest) if receipt_encoding or coverage_encoding else None
    used_receipts, used_coverages, used_documents = set(), set(), set()
    for claim_id, raw in raw_records.items():
        record = decode_revision_record(raw, encoding=encoding)
        tag = record.get("tag")
        if isinstance(tag, dict) and "report_level_review" in tag:
            reviews = tag["report_level_review"]
            if not isinstance(reviews, list):
                raise ValueError("report-level reviews must be an array")
            decoded_reviews = []
            for review in reviews:
                if not isinstance(review, dict):
                    raise ValueError("report-level review must be an object")
                decoded_review = dict(review)
                if receipt_encoding is None:
                    if "source_receipt_ref" in review:
                        raise ValueError("source receipt reference has no encoding")
                elif "source_receipt_ref" not in review:
                    if "source_receipt" in review:
                        raise ValueError("inline source receipt under shared encoding")
                else:
                    if "source_receipt" in review:
                        raise ValueError("inline and referenced source receipts conflict")
                    reference = review["source_receipt_ref"]
                    if (
                        not isinstance(reference, str)
                        or not reference.startswith(SOURCE_RECEIPT_REF_PREFIX)
                        or len(reference) != len(SOURCE_RECEIPT_REF_PREFIX) + 64
                    ):
                        raise ValueError("malformed source receipt reference")
                    digest = reference[len(SOURCE_RECEIPT_REF_PREFIX) :]
                    if any(char not in "0123456789abcdef" for char in digest):
                        raise ValueError("malformed source receipt hash")
                    _validate_revision_source_identity(record, claim_id, scope)
                    receipt = receipts.get(digest)
                    if receipt is None:
                        raise ValueError("referenced source receipt is missing")
                    _validate_source_receipt(receipt, scope)
                    if canonical_hash(receipt) != digest:
                        raise ValueError("referenced source receipt hash mismatch")
                    _validate_review_source_receipt(review, receipt, tag)
                    decoded_review["source_receipt"] = receipt
                    del decoded_review["source_receipt_ref"]
                    used_receipts.add(digest)
                decoded_reviews.append(decoded_review)
            if receipt_encoding is not None:
                decoded_tag = dict(tag)
                decoded_tag["report_level_review"] = decoded_reviews
                record["tag"] = decoded_tag
        _decode_coverage_record(
            record,
            coverage_encoding,
            coverages,
            documents,
            scope,
            used_coverages,
            used_documents,
        )
        yield claim_id, record
    if receipt_encoding is not None and used_receipts != set(receipts):
        raise ValueError("source receipt table has unreferenced entries")
    if coverage_encoding is not None and (
        used_coverages != set(coverages) or used_documents != set(documents)
    ):
        raise ValueError("unreferenced coverage table entries")


def decode_revision_records(manifest):
    """Restore records; callers needing validation alone can consume the iterator."""
    return dict(_iter_decoded_revision_records(manifest))


class ExportRejected(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def timestamp(now):
    return datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")


def create_snapshot(store, tenant_id, export_id):
    for _ in range(4):  # First attempt, then at most three snapshot-only retries.
        frozen = store.frozen(tenant_id, export_id)
        if frozen is not None:
            return frozen
        captured = store.capture(tenant_id, export_id)
        if store.freeze(tenant_id, export_id, captured):
            return captured
    raise ExportRejected("EXPORT_SNAPSHOT_BUSY")


def build_export(snapshot, formats):
    try:
        for _ in _iter_decoded_revision_records(snapshot["manifest"]):
            pass
    except (KeyError, TypeError, ValueError) as exc:
        raise ExportRejected("EXPORT_INTEGRITY_FAILED") from exc
    model = build_report_model(snapshot["manifest"], snapshot["decisions"])
    manifest = canonical_json(snapshot["manifest"]).encode()
    stream = BytesIO()
    # Deflate keeps the preserved provenance whole while the artifact stays inside the
    # unchanged 32 MiB cap; every ZIP reader handles it and frozen artifacts are untouched.
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as bundle:
        members = [("manifest.json", manifest)]
        if snapshot["manifest"].get("submitted_reviews"):
            members.append(
                (
                    "submitted-reviews.json",
                    canonical_json(snapshot["manifest"]["submitted_reviews"]).encode(),
                )
            )
        for name, content in chain(
            members, ((f"report.{fmt}", render_report(model, fmt)) for fmt in formats)
        ):
            # Pre-write guard: every raw member must fit the cap next to the bytes already
            # archived. Deflate can add overhead on incompressible input, so this is not a
            # proof about the archive; the final check below measures the actual ZIP.
            if stream.tell() + len(content) > MAX_EXPORT_BYTES:
                raise ExportRejected("EXPORT_SIZE_LIMIT")
            # ZipInfo() defaults to ZIP_STORED and overrides the ZipFile compression.
            info = ZipInfo(name)
            info.compress_type = ZIP_DEFLATED
            bundle.writestr(info, content)
    content = stream.getvalue()
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportRejected("EXPORT_SIZE_LIMIT")
    return content, sha256(manifest).hexdigest(), model["partial"]


class ExportService:
    def __init__(self, store):
        self.store = store

    def create(self, actor, run_id, body, key, *, now):
        if not isinstance(body, dict) or set(body) != {"formats", "allow_partial"}:
            raise ExportRejected("VALIDATION_ERROR", 422)
        formats = body["formats"]
        if (
            not isinstance(formats, list)
            or not 1 <= len(formats) <= 3
            or any(not isinstance(f, str) or f not in ("json", "csv", "html") for f in formats)
            or len(set(formats)) != len(formats)
            or type(body["allow_partial"]) is not bool
        ):
            raise ExportRejected("VALIDATION_ERROR", 422)
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise ExportRejected("IDEMPOTENCY_KEY_INVALID", 400)
        export_id = self.store.reserve(actor, run_id, body, key, now=now)
        result = self.get(actor, export_id, now=now)
        if result["state"] == "failed":
            raise ExportRejected(self.store.state(actor.tenant_id, export_id)["errors"][0])
        return result

    def get(self, actor, export_id, *, now):
        state = self.store.state(actor.tenant_id, export_id)
        if state["response"]["state"] in ("ready", "failed") or now < state["retry_at"]:
            return state["response"]
        try:
            snapshot = create_snapshot(self.store, actor.tenant_id, export_id)
            content, manifest_hash, partial = build_export(snapshot, state["body"]["formats"])
            if partial and not state["body"]["allow_partial"]:
                raise ExportRejected("REPORT_NOT_FINALIZABLE")
            return self.store.complete(actor.tenant_id, export_id, content, manifest_hash, partial)
        except ExportRejected as exc:
            self.store.defer(actor.tenant_id, export_id, exc.code, now=now)
            return self.store.get(actor.tenant_id, export_id)

    def authorize_download(self, actor, export_id, *, now):
        return self.store.authorize_download(actor, export_id, now=now)
