"""Bounded local byte-level verification for the A-side linkage exchange (R08a).

Two verification paths, both refusing anything that isn't a real, already-
opened local artifact, and both requiring MORE than a bare hash match:

1. Sustainability side: reuses the existing tenant-scoped
   `UploadService.read_original` (never a new upload/object-store path) to
   confirm a `Claim.source_sha256` matches the actual bytes already on disk
   for this tenant/document version, THEN re-opens that same PDF with
   pdfplumber (already an installed dependency; no new one added) and
   confirms `quote` is literally present on the locator's declared
   `physical_page`. A hash match alone is not accepted as source
   verification -- the quote must actually be found at the claimed page in
   the same real bytes.
2. Financial side: `verify_financial_source` opens an EXPLICIT local
   filesystem path the caller supplies (never a URL, never a path read out
   of an untrusted returned packet/result), confirms its SHA-256 matches
   `FinancialSource.artifact_sha256`, and then -- ONLY for the one locator
   format this reader understands (`physical_page=<int>[;...]` against a
   real PDF) -- confirms the quote is literally present on that page. Any
   other locator format (XBRL fact/context, HTML section/element, or a PDF
   locator this reader cannot parse) is explicitly rejected as
   `unsupported_locator_format` rather than silently accepted on hash alone.

Neither path fetches anything over the network. Both raise
`LinkageVerificationError` (never silently return True) on any mismatch,
missing file, unsupported locator, or oversized read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from proofops.application.linkage_exchange import FinancialSource
from proofops.domain.errors import DomainValidationError

# Refuse to hash-check anything larger than this; a legitimate financial
# statement PDF/XBRL/HTML artifact is not expected to exceed it, and this
# bounds memory use for a bounded local reader (mirrors
# UploadService.limits.max_bytes's role for the sustainability side).
MAX_FINANCIAL_ARTIFACT_BYTES = 64 * 1024 * 1024

_PHYSICAL_PAGE_LOCATOR = re.compile(r"^physical_page=(\d+)(?:;.*)?$")


class LinkageVerificationError(DomainValidationError):
    """A cited artifact's real bytes/locator/quote do not match the packet."""


@dataclass(frozen=True, slots=True)
class VerifiedFinancialArtifact:
    """A local financial artifact whose real bytes/locator/quote were checked here."""

    path: str
    artifact_sha256: str
    size_bytes: int
    physical_page: int


def _parse_physical_page_locator(locator: str) -> int:
    """Parse the ONLY locator format this bounded reader supports.

    Any other format (XBRL `fact=...;context=...`, HTML `section=...`, or a
    malformed/unrecognized PDF locator) raises rather than being guessed at
    or silently accepted because the hash already matched.
    """
    match = _PHYSICAL_PAGE_LOCATOR.match(locator.strip())
    if match is None:
        raise LinkageVerificationError(
            f"unsupported_locator_format: {locator!r} is not a recognized "
            "physical_page=<int> PDF locator"
        )
    page = int(match.group(1))
    if page < 1:
        raise LinkageVerificationError(f"unsupported_locator_format: invalid page number {page}")
    return page


def _quote_present_on_pdf_page(pdf_bytes: bytes, *, physical_page: int, quote: str) -> bool:
    """Open real PDF bytes with pdfplumber and check the quote on one page.

    `physical_page` is 1-based, matching the locator convention this module
    and `application.linkage_exchange.build_packet` both use. Returns False
    (never raises) for "the quote is not there"; a genuinely unreadable/
    corrupt PDF raises `LinkageVerificationError` instead, since that is a
    verification-infrastructure failure, not a normal negative result.
    """
    import pdfplumber

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            if not 1 <= physical_page <= len(pdf.pages):
                raise LinkageVerificationError(
                    f"physical_page {physical_page} out of range "
                    f"(document has {len(pdf.pages)} pages)"
                )
            text = pdf.pages[physical_page - 1].extract_text() or ""
    except LinkageVerificationError:
        raise
    except Exception as exc:  # pragma: no cover - defensive: corrupt/unreadable PDF
        raise LinkageVerificationError(f"financial artifact PDF could not be read: {exc}") from exc
    return quote.strip() in text


def verify_financial_source(
    source: FinancialSource,
    *,
    local_path: str | Path,
    max_bytes: int = MAX_FINANCIAL_ARTIFACT_BYTES,
) -> VerifiedFinancialArtifact:
    """Open `local_path`, check its hash, AND confirm locator+quote against real bytes.

    `local_path` must already be a real filesystem path the caller controls
    (e.g. a path from developer-B's own collection directory, or an
    operator-provided path) -- never a URL, never a path taken from a
    returned/untrusted packet or result. This function performs no path
    resolution beyond what `Path` does locally and makes no network request
    of any kind. A hash match alone is never treated as sufficient: the
    locator must be the one supported format (`physical_page=<int>`) and the
    quote must be literally found on that page of the real opened bytes, or
    this raises rather than accepting on hash alone.
    """
    if not isinstance(source, FinancialSource):
        raise LinkageVerificationError("verify_financial_source requires a FinancialSource")
    path_str = str(local_path)
    if "://" in path_str:
        raise LinkageVerificationError(
            f"refusing arbitrary/network path as financial artifact: {local_path}"
        )
    physical_page = _parse_physical_page_locator(source.locator)
    path = Path(local_path)
    if path.is_symlink():
        # A symlink could point outside the caller's intended local artifact
        # (including, in principle, a network filesystem mount); refuse
        # rather than silently follow it.
        raise LinkageVerificationError(
            f"refusing to read a symlink as a financial artifact: {path}"
        )
    try:
        stat = path.stat()
    except OSError as exc:
        raise LinkageVerificationError(f"financial artifact not readable: {path}") from exc
    if not path.is_file():
        raise LinkageVerificationError(f"financial artifact is not a regular file: {path}")
    if stat.st_size > max_bytes:
        raise LinkageVerificationError(
            f"financial artifact exceeds {max_bytes} bytes; refusing bounded read"
        )
    digest = sha256()
    size = 0
    chunks = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise LinkageVerificationError(
                    f"financial artifact exceeds {max_bytes} bytes; refusing bounded read"
                )
            digest.update(chunk)
            chunks.append(chunk)
    actual = digest.hexdigest()
    if actual != source.artifact_sha256:
        raise LinkageVerificationError(
            f"financial artifact byte hash mismatch for source_id={source.source_id}: "
            f"declared {source.artifact_sha256}, actual {actual}"
        )
    content = b"".join(chunks)
    if not _quote_present_on_pdf_page(content, physical_page=physical_page, quote=source.quote):
        raise LinkageVerificationError(
            f"financial source_id={source.source_id}: quote not found on physical_page "
            f"{physical_page} of the real opened artifact (hash matched, but locator/quote did not)"
        )
    return VerifiedFinancialArtifact(
        path=str(path), artifact_sha256=actual, size_bytes=size, physical_page=physical_page
    )


def verify_sustainability_source(
    *,
    uploads,
    tenant_id: str,
    document_version_id: str,
    expected_sha256: str,
    locator: str,
    quote: str,
) -> bytes:
    """Confirm a claim's source against the real, already-uploaded bytes.

    Delegates the byte fetch entirely to the existing tenant-scoped
    `UploadService.read_original` (packages/proofops/application/uploads.py)
    -- this function adds no new storage path, no new object-store access,
    and no filesystem access outside that existing, already-hardened reader.
    Beyond the hash check, it also re-opens the same bytes with pdfplumber
    and confirms `quote` is literally present at the locator's
    `physical_page`; a hash match alone is not treated as verification.
    """
    try:
        content = uploads.read_original(tenant_id, document_version_id)
    except Exception as exc:
        raise LinkageVerificationError(
            f"sustainability source read failed for tenant {tenant_id} version "
            f"{document_version_id}: {exc}"
        ) from exc
    actual = sha256(content).hexdigest()
    if actual != expected_sha256:
        raise LinkageVerificationError(
            f"sustainability artifact byte hash mismatch: "
            f"declared {expected_sha256}, actual {actual}"
        )
    physical_page = _parse_physical_page_locator(locator)
    if not _quote_present_on_pdf_page(content, physical_page=physical_page, quote=quote):
        raise LinkageVerificationError(
            f"sustainability source: quote not found on physical_page {physical_page} "
            "of the real opened document (hash matched, but locator/quote did not)"
        )
    return content


def verify_packet_sources(
    packet: dict,
    *,
    uploads,
    tenant_id: str,
    financial_local_paths: dict[str, str | Path],
) -> list[dict]:
    """Verify every `sources[]` entry in a built packet against real bytes.

    `financial_local_paths` maps each non-sustainability `source_id` to the
    real local path the caller already has for it (explicit, never taken
    from the packet/result itself -- this map is a separate operator
    allowlist, never sourced from the returned output). The sustainability-
    side source (its `source_id` always starts with "sr-", see
    `linkage_exchange.build_packet`) is instead verified against the
    tenant's actual uploaded document via `verify_sustainability_source`.
    Returns a list of per-source result dicts; raises on the first mismatch
    or unsupported locator rather than silently skipping it.
    """
    packet_tenant = packet.get("identity", {}).get("tenant_id")
    if packet_tenant != tenant_id:
        raise LinkageVerificationError(
            f"tenant_mismatch: packet tenant {packet_tenant!r} does not match expected "
            f"{tenant_id!r}"
        )
    sustainability_version = packet.get("identity", {}).get("sustainability_document_version")
    if not sustainability_version:
        raise LinkageVerificationError("missing sustainability_document_version in packet identity")

    seen_ids: set[str] = set()
    results = []
    for entry in packet.get("sources", []):
        source_id = entry.get("source_id")
        if not source_id or source_id in seen_ids:
            raise LinkageVerificationError(
                f"duplicate or missing source_id in packet sources: {source_id!r}"
            )
        seen_ids.add(source_id)
        if source_id.startswith("sr-"):
            doc_version = entry.get("document_id")
            if doc_version != sustainability_version:
                raise LinkageVerificationError(
                    f"version_mismatch: sustainability source {source_id} document_id "
                    f"({doc_version!r}) does not match packet sustainability_document_version "
                    f"({sustainability_version!r})"
                )
            verify_sustainability_source(
                uploads=uploads,
                tenant_id=tenant_id,
                document_version_id=doc_version,
                expected_sha256=entry["artifact_sha256"],
                locator=entry["locator"],
                quote=entry["quote"],
            )
            results.append({"source_id": source_id, "verified": "sustainability_upload"})
            continue
        local_path = financial_local_paths.get(source_id)
        if local_path is None:
            raise LinkageVerificationError(
                f"no local_path supplied to verify financial source_id={source_id}"
            )
        source = FinancialSource(
            source_id=source_id,
            document_id=entry["document_id"],
            artifact_sha256=entry["artifact_sha256"],
            locator=entry["locator"],
            quote=entry["quote"],
        )
        verified = verify_financial_source(source, local_path=local_path)
        results.append(
            {"source_id": source_id, "verified": "financial_local_file", "path": verified.path}
        )
    return results
