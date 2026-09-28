"""Experimental composed raster visibility proof; not wired to parser checkpoints."""

from dataclasses import asdict, replace
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from proofops.adapters.local import raster_ocr
from proofops.adapters.local.run_artifacts import native_paragraph_policy
from proofops.adapters.local.source_verification import replay_native_sources
from proofops.domain.provenance import canonical_hash


def raster_ocr_policy(*, mode="standard", max_pages=10, max_calls=1):
    if mode not in {"standard", "enhanced"}:
        raise ValueError("RASTER_MODE_INVALID")
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError("RASTER_MAX_PAGES_INVALID")
    if type(max_calls) is not int or not 1 <= max_calls <= 20:
        raise ValueError("RASTER_MAX_CALLS_INVALID")
    return {
        "schema": "local_raster_ocr_policy_v1",
        "mode": mode,
        "max_pages": max_pages,
        "max_calls": max_calls,
        "native_policy_sha256": canonical_hash(native_paragraph_policy()),
        "raster_helper_sha256": sha256(Path(raster_ocr.__file__).read_bytes()).hexdigest(),
        "composition_helper_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "checkpoint_helper_sha256": sha256(
            Path(__file__).with_name("raster_checkpoint.py").read_bytes()
        ).hexdigest(),
        "reader_versions": {
            "pypdfium2": version("pypdfium2"),
            "pdfplumber": version("pdfplumber"),
            "pypdf": version("pypdf"),
            "Pillow": version("Pillow"),
        },
    }


def eligible_raster_sources(native):
    """Use only after replay_native_sources verifies the original receipt."""
    return {
        row["source_id"]
        for row in native["records"]
        if row["status"] == "unresolved"
        and row["reason"] == "rendered_text_unresolved"
        and row.get("rendered", {}).get("status") == "read"
        and row["rendered"].get("text", "").strip()
    }


def corroborate_native_visibility(
    native, request, receipt, graph, source, *, request_sha256, receipt_sha256, tenant_id
):
    if not isinstance(native, dict) or native.get("schema") != "native_paragraph_attestation_v2":
        raise ValueError("NATIVE_GLYPH_ATTESTATION_REQUIRED")
    baseline = replay_native_sources(native, graph, source, tenant_id=tenant_id)
    eligible = eligible_raster_sources(native)
    readings = raster_ocr.replay_raster_ocr(
        request,
        receipt,
        graph,
        source,
        request_sha256=request_sha256,
        receipt_sha256=receipt_sha256,
        tenant_id=tenant_id,
    )
    if any(row["source_id"] not in eligible for row in readings):
        raise ValueError("NATIVE_OCR_FALLBACK_INELIGIBLE")
    corroborated = {row["source_id"] for row in readings if row["exact_normalized_match"]}
    result = replace(
        baseline,
        blocks=tuple(
            replace(block, quality="verified") if block.source_id in corroborated else block
            for block in baseline.blocks
        ),
    )
    proof = dict(
        schema="experimental_native_raster_visibility_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(graph)),
        output_graph_sha256=canonical_hash(asdict(result)),
        policy_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        native_attestation_sha256=canonical_hash(native),
        request_sha256=request_sha256,
        receipt_sha256=receipt_sha256,
        corroborated_source_ids=sorted(corroborated),
    )
    proof["artifact_sha256"] = canonical_hash(proof)
    return result, proof
