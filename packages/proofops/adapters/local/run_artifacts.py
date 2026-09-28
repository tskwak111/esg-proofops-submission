"""Tenant-scoped verified reads of parser inputs and fence-published rich graphs."""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from importlib.resources import files
from uuid import UUID, uuid5

from proofops.adapters.parsing.opendataloader import ParseFailure
from proofops.application.ingest.graph_fusion import ParserProfile, SourceArtifact
from proofops.application.ports.jobs import JobMessage
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_sha256, _require_uuid


def load_run_inputs(store, uploads, *, tenant_id: str, run_id: str):
    snapshot = store.snapshot(tenant_id, run_id)
    frozen = {key: value for key, value in snapshot.items() if key != "input_hash"}
    if (snapshot["tenant_id"], snapshot["run_id"]) != (tenant_id, run_id) or (
        canonical_hash(frozen) != snapshot["input_hash"]
    ):
        raise ParseFailure("RUN_INPUT_INTEGRITY_MISMATCH")
    document = snapshot["document"]
    if uploads.version_snapshot(tenant_id, document["version_id"]) != document:
        raise ParseFailure("DOCUMENT_VERSION_MISMATCH")
    content = uploads.read_original(tenant_id, document["version_id"])
    if sha256(content).hexdigest() != document["sha256"]:
        raise ParseFailure("SOURCE_INTEGRITY_MISMATCH")
    source = SourceArtifact(
        tenant_id,
        document["document_id"],
        document["version_id"],
        document["sha256"],
        document["object_version_id"],
        content,
    )
    pages = snapshot["selected_pages"]
    if not pages or any(type(p) is not int or not 1 <= p <= document["page_count"] for p in pages):
        raise ParseFailure("PAGE_SELECTION_INVALID")
    try:
        profile = ParserProfile(
            parse_manifest_id=str(uuid5(UUID(run_id), "parse:" + snapshot["input_hash"])),
            physical_pages=tuple(pages),
            **snapshot["parser_profile"],
        )
    except (ValueError, TypeError, KeyError):
        raise ParseFailure("PARSER_CONFIG_INVALID") from None
    if (
        profile.config_snapshot() != snapshot["parser_profile"]
        or profile.config_hash() != snapshot["parser_profile_hash"]
    ):
        raise ParseFailure("RUN_INPUT_INTEGRITY_MISMATCH")
    return snapshot, source, profile


def checkpoint_raster(envelope):
    fields = {"raster_ocr_policy_sha256", "raster_ocr_artifacts", "raster_ocr_coverage"}
    present = {key for key in envelope if key.startswith("raster_ocr_")}
    if envelope.get("schema") != "local_parser_checkpoint_v5":
        if present:
            raise ParseFailure("RASTER_OCR_CHECKPOINT_UNSUPPORTED")
        return None
    try:
        if present != fields:
            raise ValueError("incomplete raster checkpoint")
        _require_sha256("raster_ocr_policy_sha256", envelope["raster_ocr_policy_sha256"])
        refs, coverage = envelope["raster_ocr_artifacts"], envelope["raster_ocr_coverage"]
        names = {
            "eligible_source_ids",
            "requested_source_ids",
            "corroborated_source_ids",
            "unresolved_source_ids",
            "failed_source_ids",
        }
        if (
            not isinstance(refs, list)
            or len(refs) > 20
            or not isinstance(coverage, dict)
            or set(coverage) != names
        ):
            raise ValueError("invalid raster coverage")
        for values in coverage.values():
            if (
                not isinstance(values, list)
                or any(not isinstance(v, str) or not v for v in values)
                or values != sorted(set(values))
            ):
                raise ValueError("invalid source set")
        sets = {key: set(values) for key, values in coverage.items()}
        if (
            not sets["corroborated_source_ids"]
            <= sets["requested_source_ids"]
            <= sets["eligible_source_ids"]
            or sets["unresolved_source_ids"]
            != sets["eligible_source_ids"] - sets["corroborated_source_ids"]
            or sets["failed_source_ids"]
        ):
            raise ValueError("invalid raster coverage")
        identifiers = []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {
                "request_id",
                "request_sha256",
                "receipt_sha256",
            }:
                raise ValueError("invalid raster reference")
            _require_uuid("request_id", ref["request_id"])
            _require_sha256("request_sha256", ref["request_sha256"])
            _require_sha256("receipt_sha256", ref["receipt_sha256"])
            identifiers.append(ref["request_id"])
        if identifiers != sorted(set(identifiers)):
            raise ValueError("duplicate/unordered raster references")
    except (ValueError, KeyError, TypeError):
        raise ParseFailure("RASTER_OCR_CHECKPOINT_INVALID") from None
    return refs, coverage


def checkpoint_note_reviews(envelope):
    """Versioned checkpoint shape; legacy readers must never silently discard notes."""
    schema = envelope.get("schema")
    checkpoint_raster(envelope)
    if any(key.startswith("native_paragraph_") for key in envelope):
        if schema not in {
            "local_parser_checkpoint_v4",
            "local_parser_checkpoint_v5",
            "local_parser_checkpoint_v6",
        }:
            raise ParseFailure("NATIVE_PARAGRAPH_CHECKPOINT_INVALID")
    if schema == "local_parser_checkpoint_v1":
        if {"runtime_note_review_artifacts", "graph_sha256"} & envelope.keys():
            raise ParseFailure("NOTE_REVIEW_CHECKPOINT_INVALID")
        return ()
    if schema not in {
        "local_parser_checkpoint_v2",
        "local_parser_checkpoint_v3",
        "local_parser_checkpoint_v4",
        "local_parser_checkpoint_v5",
        "local_parser_checkpoint_v6",
    }:
        raise ParseFailure("PARSER_CHECKPOINT_SCHEMA_UNSUPPORTED")
    artifacts = envelope.get("runtime_note_review_artifacts")
    digest = envelope.get("graph_sha256")
    if (
        schema
        in {
            "local_parser_checkpoint_v4",
            "local_parser_checkpoint_v5",
            "local_parser_checkpoint_v6",
        }
        and artifacts is None
    ):
        artifacts = []
    if (
        not isinstance(artifacts, list)
        or (
            not artifacts
            and schema
            not in {
                "local_parser_checkpoint_v3",
                "local_parser_checkpoint_v4",
                "local_parser_checkpoint_v5",
                "local_parser_checkpoint_v6",
            }
        )
        or any(not isinstance(item, str) for item in artifacts)
        or len(set(artifacts)) != len(artifacts)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ParseFailure("NOTE_REVIEW_CHECKPOINT_INVALID")
    return tuple(artifacts)


def native_paragraph_policy():
    local = files("proofops.adapters.local")
    return dict(
        mode="paragraph_native_glyph_v2",
        glyph_verifier_sha256=sha256(
            local.joinpath("native_glyph_geometry.py").read_bytes()
        ).hexdigest(),
        verifier_sha256=sha256(local.joinpath("source_verification.py").read_bytes()).hexdigest(),
        rendered_reader_sha256=sha256(local.joinpath("native_ocr.swift").read_bytes()).hexdigest(),
        normalization_sha256=sha256(
            files("proofops.application.evidence").joinpath("citations.py").read_bytes()
        ).hexdigest(),
    )


def accepted_native_policy_digests():
    """Policy hashes a stored checkpoint's `native_paragraph_policy_sha256`
    may carry: the live verifier, or any pinned historical verifier whose
    vendored source still reproduces it.

    This field is always the base verifier's own hash (never this module's
    typography wrapper hash -- that lives in the separate, optional
    `native_paragraph_typography_policy_sha256` field checked directly in
    `checkpoint_native_attestation`), because `job_store.commit_job` requires
    it to equal the hash of whatever was bound via `bind_parser_native_policy`,
    which only ever binds the plain base policy.

    A historical digest is admitted only when its frozen verifier bytes are
    present and hash-verified, so this never allowlists a bare hash.
    """
    digests = {canonical_hash(native_paragraph_policy())}
    from proofops.adapters.local import frozen_native_replay

    for bundle in frozen_native_replay._PINNED_VERIFIERS.values():
        try:
            digest = canonical_hash(frozen_native_replay.frozen_policy(bundle))
        except frozen_native_replay.FrozenVerifierUnavailable:
            continue
        if digest == bundle["policy_hash"]:
            digests.add(digest)
    return digests


def raster_policy_matches_accepted(stored_policy, live_policy_factory):
    """Compare a stored `raster_ocr_policy` dict against the live composition,
    tolerating only the embedded `native_policy_sha256` field.

    `raster_ocr_policy()` embeds `canonical_hash(native_paragraph_policy())`
    verbatim. When a checkpoint was published under a pinned historical
    native verifier (see `accepted_native_policy_digests`), that one field
    legitimately differs from the live recomputation while every other
    component (raster/composition/checkpoint helper hashes, reader versions,
    mode/max_pages/max_calls) must still match exactly. This never widens
    acceptance beyond hashes already admitted by
    `accepted_native_policy_digests`, and every other field is still compared
    for byte-exact equality against the live policy.
    """
    if not isinstance(stored_policy, dict) or "native_policy_sha256" not in stored_policy:
        return False
    live = live_policy_factory(
        mode=stored_policy.get("mode"),
        max_pages=stored_policy.get("max_pages"),
        max_calls=stored_policy.get("max_calls"),
    )
    if stored_policy.get("native_policy_sha256") not in accepted_native_policy_digests():
        return False
    stripped_stored = {k: v for k, v in stored_policy.items() if k != "native_policy_sha256"}
    stripped_live = {k: v for k, v in live.items() if k != "native_policy_sha256"}
    return stripped_stored == stripped_live


def checkpoint_native_attestation(envelope):
    """Versioned native receipt shape; v1-v3 carry no native attestation.

    v6 additionally allows one optional key,
    `native_paragraph_typography_policy_sha256`: an opt-in, separately hashed
    pointer to this module's quote-typography wrapper policy. It never
    replaces `native_paragraph_policy_sha256` (still always the base
    verifier's own hash, still what `commit_job`'s lease-bound policy check
    requires) and never changes `graph_sha256`'s meaning -- `graph` in the
    checkpoint stays the unmodified base-replayed graph. A reader that wants
    the wider, quote-typography-tolerant verified set calls
    `native_paragraph_typography.replay_native_with_typography_policy`
    against the published receipt itself; see `load_run_evidence`.
    """
    schema = envelope.get("schema")
    if schema not in {
        "local_parser_checkpoint_v4",
        "local_parser_checkpoint_v5",
        "local_parser_checkpoint_v6",
    }:
        if any(key.startswith("native_paragraph_") for key in envelope):
            raise ParseFailure("NATIVE_PARAGRAPH_CHECKPOINT_INVALID")
        return None
    receipt = envelope.get("native_paragraph_attestation")
    policy_digest = envelope.get("native_paragraph_policy_sha256")
    typography_digest = envelope.get("native_paragraph_typography_policy_sha256")
    typography_proof = envelope.get("native_paragraph_typography_proof")
    if typography_digest is not None or typography_proof is not None:
        if typography_digest is None or typography_proof is None:
            raise ParseFailure("NATIVE_PARAGRAPH_CHECKPOINT_INVALID")
        from proofops.adapters.local.native_paragraph_typography import (
            native_paragraph_typography_policy,
        )

        expected_policy = native_paragraph_typography_policy()
        expected_policy_hash = canonical_hash(expected_policy)
        if (
            not isinstance(typography_digest, str)
            or len(typography_digest) != 64
            or any(c not in "0123456789abcdef" for c in typography_digest)
            or typography_digest != expected_policy_hash
        ):
            raise ParseFailure("NATIVE_PARAGRAPH_CHECKPOINT_INVALID")
        if (
            not isinstance(typography_proof, dict)
            or typography_proof.get("schema") != "native_paragraph_typography_proof_v1"
            or typography_proof.get("policy") != expected_policy
            or typography_proof.get("policy_sha256") != expected_policy_hash
            or not isinstance(typography_proof.get("artifact_sha256"), str)
            or len(typography_proof["artifact_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in typography_proof["artifact_sha256"])
            or canonical_hash({k: v for k, v in typography_proof.items() if k != "artifact_sha256"})
            != typography_proof["artifact_sha256"]
            or typography_proof.get("tenant_id") != envelope.get("tenant_id")
            or typography_proof.get("document_version_id") != envelope.get("document_version_id")
            or typography_proof.get("parse_manifest_id") != envelope.get("parse_manifest_id")
            or typography_proof.get("source_sha256") != envelope.get("source_sha256")
            or typography_proof.get("output_graph_sha256") != envelope.get("graph_sha256")
            or not isinstance(typography_proof.get("promoted_source_ids"), list)
            or not isinstance(typography_proof.get("base_verified_source_ids"), list)
        ):
            raise ParseFailure("NATIVE_PARAGRAPH_CHECKPOINT_INVALID")
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "native_paragraph_attestation_v2"
        or not isinstance(receipt.get("records"), list)
        or not isinstance(receipt.get("artifact_sha256"), str)
        or len(receipt["artifact_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in receipt["artifact_sha256"])
        or not isinstance(receipt.get("tenant_id"), str)
        or not isinstance(receipt.get("document_version_id"), str)
        or not isinstance(receipt.get("parse_manifest_id"), str)
        or not isinstance(receipt.get("source_sha256"), str)
        or not isinstance(receipt.get("input_graph_sha256"), str)
        or policy_digest not in accepted_native_policy_digests()
    ):
        raise ParseFailure("NATIVE_PARAGRAPH_CHECKPOINT_INVALID")
    return receipt


def validate_automatic_note_reviews(graph, artifacts, policy, *, selected_pages=None):
    if (
        not isinstance(policy, dict)
        or set(policy) != {"mode", "model", "extractor_sha256", "batch_sha256"}
        or policy["mode"] not in {"automatic_v1", "automatic_pages_v2"}
    ):
        raise ParseFailure("NOTE_REVIEW_POLICY_MISMATCH")
    tables = {b.source_id for b in graph.blocks if b.kind == "table"}
    pages = {b.page_num for b in graph.blocks if b.kind == "table"}
    if policy["mode"] == "automatic_pages_v2":
        if (
            not selected_pages
            or any(type(p) is not int or p < 1 for p in selected_pages)
            or len(set(selected_pages)) != len(selected_pages)
            or not pages <= set(selected_pages)
        ):
            raise ParseFailure("NOTE_REVIEW_PAGE_COVERAGE_MISMATCH")
        pages = set(selected_pages)
    covered = set()
    covered_pages = set()
    for artifact in artifacts:
        raw = json.loads(artifact)
        page = raw["packet"]["untrusted_document_data"]["page"]
        if type(page) is not int or page not in pages or page in covered_pages:
            raise ParseFailure("NOTE_REVIEW_PAGE_COVERAGE_MISMATCH")
        covered_pages.add(page)
        covered.update(raw["packet"]["table_ids"])
        for request in raw["extracted"].get("requests", []):
            if (
                request.get("model") != policy["model"]
                or request.get("helper_sha256") != policy["extractor_sha256"]
            ):
                raise ParseFailure("NOTE_REVIEW_POLICY_MISMATCH")
    if covered != tables or covered_pages != pages or len(artifacts) != len(pages):
        raise ParseFailure("NOTE_REVIEW_TABLE_COVERAGE_MISMATCH")


def load_run_graph(store, uploads, parser, *, tenant_id: str, run_id: str):
    return load_run_evidence(store, uploads, parser, tenant_id=tenant_id, run_id=run_id)["graph"]


def load_run_evidence(store, uploads, parser, *, tenant_id: str, run_id: str):
    """Only committed pointers publish evidence; private prepared files are insufficient."""
    snapshot, source, profile = load_run_inputs(store, uploads, tenant_id=tenant_id, run_id=run_id)
    run = store.jobs.get_run(tenant_id, run_id)
    if "parse_job" not in run:
        raise ParseFailure("PARSE_NOT_PUBLISHED")
    message = JobMessage(**run["parse_job"])
    if (
        message.tenant_id,
        message.run_id,
        message.document_version_id,
        message.stage,
        message.input_hash,
    ) != (tenant_id, run_id, source.document_version_id, "parse", snapshot["input_hash"]):
        raise ParseFailure("RUN_INPUT_INTEGRITY_MISMATCH")
    payload = store.jobs.read_checkpoint(message)
    if payload is None:
        raise ParseFailure("PARSE_NOT_PUBLISHED")
    envelope = json.loads(payload)
    note_reviews = checkpoint_note_reviews(envelope)
    native_receipt = checkpoint_native_attestation(envelope)
    from proofops.adapters.local.raster_job_store import validate_raster_checkpoint_bindings

    with store.jobs._transaction() as db:
        validate_raster_checkpoint_bindings(db, store.jobs, message, snapshot, envelope)
    policy = store.jobs.parser_note_policy(message)
    native_policy = store.jobs.parser_native_policy(message)
    if native_policy is not None:
        if (
            envelope.get("schema")
            not in {
                "local_parser_checkpoint_v4",
                "local_parser_checkpoint_v5",
                "local_parser_checkpoint_v6",
            }
            or envelope.get("native_paragraph_policy_sha256") != canonical_hash(native_policy)
            or native_receipt is None
        ):
            raise ParseFailure("NATIVE_PARAGRAPH_POLICY_MISMATCH")
    elif envelope.get("schema") in {
        "local_parser_checkpoint_v4",
        "local_parser_checkpoint_v5",
        "local_parser_checkpoint_v6",
    } or (native_receipt is not None):
        raise ParseFailure("NATIVE_PARAGRAPH_POLICY_MISMATCH")
    if policy is not None:
        if envelope.get("schema") not in {
            "local_parser_checkpoint_v3",
            "local_parser_checkpoint_v4",
            "local_parser_checkpoint_v5",
            "local_parser_checkpoint_v6",
        } or envelope.get("note_review_policy_sha256") != canonical_hash(policy):
            raise ParseFailure("NOTE_REVIEW_POLICY_MISMATCH")
    elif (
        envelope.get("schema") == "local_parser_checkpoint_v3"
        or "note_review_policy_sha256" in envelope
    ):
        raise ParseFailure("NOTE_REVIEW_POLICY_MISMATCH")
    pinned = store.jobs.parser_note_reviews(message)
    if ((note_reviews or policy is not None) and pinned is None) or (
        pinned is not None and note_reviews != pinned
    ):
        raise ParseFailure("NOTE_REVIEW_CHECKPOINT_INPUT_MISMATCH")
    if (
        envelope["tenant_id"],
        envelope["run_id"],
        envelope["document_version_id"],
        envelope["input_hash"],
        envelope["parse_manifest_id"],
        envelope["source_sha256"],
        envelope["object_version_id"],
    ) != (
        tenant_id,
        run_id,
        source.document_version_id,
        snapshot["input_hash"],
        profile.parse_manifest_id,
        source.sha256,
        source.object_version_id,
    ):
        raise ParseFailure("RUN_INPUT_INTEGRITY_MISMATCH")
    graph = parser.load_verified(
        source, profile, tenant_id=tenant_id, manifest_sha256=envelope["manifest_sha256"]
    )
    base_graph = graph
    if note_reviews:
        from proofops.adapters.local.table_notes import replay_note_reviews

        try:
            graph = replay_note_reviews(note_reviews, graph, source.content, tenant_id=tenant_id)
        except (ValueError, TypeError, KeyError):
            raise ParseFailure("NOTE_REVIEW_REPLAY_INVALID") from None
    # The native receipt pins `input_graph_sha256`: every native replay below
    # must start from this pre-native graph, never from an already-replayed one.
    pre_native_graph = graph
    if envelope.get("schema") == "local_parser_checkpoint_v5":
        from proofops.adapters.local.native_replay_cache import replay_raster_cached
        from proofops.adapters.local.raster_job_store import raster_receipt, raster_requests

        registrations = raster_requests(store.jobs, message)
        receipts = {
            row["request"]["request_id"]: raster_receipt(
                store.jobs, message, row["request"]["request_id"]
            )
            for row in registrations
        }
        try:
            graph, raster_coverage, raster_refs = replay_raster_cached(
                snapshot,
                message,
                native_receipt,
                graph,
                source.content,
                registrations,
                receipts,
                native_policy=native_policy,
            )
            if (
                raster_coverage != envelope["raster_ocr_coverage"]
                or raster_refs != envelope["raster_ocr_artifacts"]
            ):
                raise ValueError("raster replay differs")
        except (ValueError, TypeError, KeyError):
            raise ParseFailure("RASTER_OCR_REPLAY_INVALID") from None
    elif native_receipt is not None:
        from proofops.adapters.local.frozen_native_replay import replay_native_with_policy

        try:
            graph = replay_native_with_policy(
                native_policy, native_receipt, graph, source.content, tenant_id=tenant_id
            )
        except (ValueError, TypeError, KeyError):
            raise ParseFailure("NATIVE_PARAGRAPH_REPLAY_INVALID") from None
    typography_digest = envelope.get("native_paragraph_typography_policy_sha256")
    typography_proof = envelope.get("native_paragraph_typography_proof")
    recomputed_typography_proof = None
    if typography_digest is not None:
        from proofops.adapters.local.native_paragraph_typography import (
            apply_typography_tolerance,
            native_paragraph_typography_policy,
        )

        expected_policy = native_paragraph_typography_policy()
        if typography_digest != canonical_hash(expected_policy):
            raise ParseFailure("NATIVE_PARAGRAPH_TYPOGRAPHY_POLICY_MISMATCH")
        try:
            graph, recomputed_typography_proof = apply_typography_tolerance(
                native_receipt, pre_native_graph, source.content, tenant_id=tenant_id
            )
        except (ValueError, TypeError, KeyError):
            raise ParseFailure("NATIVE_PARAGRAPH_TYPOGRAPHY_REPLAY_INVALID") from None
        if typography_proof is not None and (
            recomputed_typography_proof.get("artifact_sha256")
            != typography_proof.get("artifact_sha256")
            or recomputed_typography_proof.get("promoted_source_ids")
            != typography_proof.get("promoted_source_ids")
        ):
            raise ParseFailure("NATIVE_PARAGRAPH_TYPOGRAPHY_PROOF_MISMATCH")
    if note_reviews or policy is not None or native_receipt is not None:
        if canonical_hash(asdict(graph)) != envelope["graph_sha256"]:
            raise ParseFailure("NOTE_REVIEW_GRAPH_MISMATCH")
    if policy is not None:
        validate_automatic_note_reviews(
            graph, note_reviews, policy, selected_pages=profile.physical_pages
        )
    return dict(
        graph=graph,
        base_graph=base_graph,
        source=source,
        note_reviews=note_reviews,
        native_attestation=native_receipt,
        typography_proof=typography_proof or recomputed_typography_proof,
        input_hash=snapshot["input_hash"],
        parse_checkpoint_sha256=sha256(payload).hexdigest(),
    )
