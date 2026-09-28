"""Native -> canonical source-ref geometry.

`canonicalize_source_ref :: NativeSource + PageGeometry -> SourceRef`
(see docs/27_PARSING_AND_PROVENANCE.md §3-4).

This module is application-layer, not domain: it is allowed to depend on the
domain values it assembles, but still takes no I/O, no environment, and no
adapter/network dependencies -- geometry math and text normalization are
pure functions of their inputs.

The richer internal
provenance fields fixed by docs/27 (native_bbox, native_coordinate_system,
affine, raw/normalized text distinction) live on `NativeSource`/
`PageGeometry` and are not re-exported here as new public API surface. The
function below projects them into the existing, unmodified
`proofops.domain.values.SourceRef` (the fixed v1 API/internal contract
object) rather than introducing a second, competing SourceRef type.
"""

from __future__ import annotations

import hashlib
import unicodedata
from uuid import uuid4

from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.errors import DomainValidationError
from proofops.domain.values import SourceRef

Affine = tuple[float, float, float, float, float, float]


def affine_apply(matrix: Affine, x: float, y: float) -> tuple[float, float]:
    """Apply affine `(a,b,c,d,e,f)` to point `(x, y)`: PDF/SVG convention."""
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def invert_affine(matrix: Affine) -> Affine:
    """Return the inverse of an invertible affine transform.

    Raises `DomainValidationError` if the matrix is singular (determinant
    ~0), which would indicate a degenerate page geometry rather than a valid
    rotation/translation.
    """
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise DomainValidationError("page geometry affine transform is singular")
    ia = d / det
    ib = -b / det
    ic = -c / det
    id_ = a / det
    ie = -(ia * e + ic * f)
    if_ = -(ib * e + id_ * f)
    return (ia, ib, ic, id_, ie, if_)


def _bbox_corners(bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = bbox
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def project_native_bbox(
    native_bbox: tuple[float, float, float, float] | None,
    geometry: PageGeometry,
) -> tuple[float, float, float, float] | None:
    """Raw affine projection for provenance checks; not an approved source location."""
    if native_bbox is None:
        return None
    corners = [
        affine_apply(geometry.to_canonical_affine(), x, y) for x, y in _bbox_corners(native_bbox)
    ]
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    return min(xs), min(ys), max(xs), max(ys)


def canonical_bbox_from_native(
    native_bbox: tuple[float, float, float, float] | None,
    geometry: PageGeometry,
) -> tuple[float, float, float, float] | None:
    """Project a native (pre-rotation, bottom-left) bbox to canonical space.

    docs/27 §3: rotation 90/180/270 or a shifted CropBox origin require an
    affine transform on all four corners followed by min/max -- a straight
    coordinate swap is not sufficient once rotation and translation combine.
    Missing input stays `None` (never a zero bbox, FX-LOC-001).

    docs/27 §3: "경계 밖·NaN·역전 좌표는 parse issue다" -- a canonical bbox
    that falls (beyond floating-point tolerance) outside the page's
    displayed extents `[0, width_pt] x [0, height_pt]` indicates a
    corrupted native bbox or mismatched geometry, and is rejected rather
    than silently clipped or accepted.
    """
    projected = project_native_bbox(native_bbox, geometry)
    if projected is None:
        return None
    x0, y0, x1, y1 = projected
    if not (x0 < x1 and y0 < y1):
        raise DomainValidationError("canonical bbox degenerated to zero/inverted area")
    tolerance = 1e-6
    if (
        x0 < -tolerance
        or y0 < -tolerance
        or x1 > geometry.width_pt + tolerance
        or y1 > geometry.height_pt + tolerance
    ):
        raise DomainValidationError(
            "canonical bbox falls outside the page's displayed extents "
            f"(got [{x0}, {y0}, {x1}, {y1}], page is "
            f"[0, {geometry.width_pt}] x [0, {geometry.height_pt}])"
        )
    return (x0, y0, x1, y1)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_raw_boundary(raw_text: str, normalized: str, normalized_offset: int) -> int:
    """Find raw index `i` such that splitting at `i` is a *stable* boundary
    for normalized offset `normalized_offset`.

    docs/27 §4: normalized and raw positions must not be conflated
    (FX-LOC-003). Per-code-point combining-class checks are not sufficient:
    Hangul jamo compose via the Hangul Syllable Composition algorithm and
    carry combining class 0, so they give no boundary signal, and the same
    is true in general for scripts using non-combining-mark composition.

    The only condition that is actually sufficient: raw index `i` is a valid
    split point for normalized offset `n` iff normalizing *both halves
    independently* reproduces the normalized string split at `n`:
    `NFC(raw[:i]) == normalized[:n]` and `NFC(raw[i:]) == normalized[n:]`.
    If composing across the split point would change either half, `i` is
    not a real boundary for `n` and is rejected.

    Raises `DomainValidationError` if no raw index satisfies this (fail
    closed rather than mis-cite a source location) -- this happens when the
    normalized offset itself falls inside a grapheme cluster with no raw
    representative split.

    O(n) candidate scan x O(n) normalize-and-compare per candidate = O(n^2)
    per block; acceptable for per-claim provenance text, not intended for
    whole-document scans.
    """
    prefix_target = normalized[:normalized_offset]
    suffix_target = normalized[normalized_offset:]
    for i in range(len(raw_text) + 1):
        if unicodedata.normalize("NFC", raw_text[:i]) != prefix_target:
            continue
        if unicodedata.normalize("NFC", raw_text[i:]) != suffix_target:
            continue
        return i
    raise DomainValidationError("normalized offset does not land on a raw-text cluster boundary")


def _map_normalized_offsets_to_raw(
    raw_text: str,
    normalized_char_start: int,
    normalized_char_end: int,
) -> tuple[int, int]:
    """Map NFC-normalized-string offsets back to raw-text code point offsets.

    After mapping, the caller-visible contract is verified again by
    `canonicalize_source_ref`: `NFC(raw_text[raw_start:raw_end])` must equal
    the requested normalized substring, or canonicalization fails closed
    rather than citing the wrong span.
    """
    normalized = unicodedata.normalize("NFC", raw_text)
    total_normalized_len = len(normalized)
    if normalized_char_start < 0 or normalized_char_end > total_normalized_len:
        raise DomainValidationError("normalized offsets are out of range for raw_text")
    if normalized_char_end < normalized_char_start:
        raise DomainValidationError("normalized_char_end must be >= normalized_char_start")

    raw_start = _find_raw_boundary(raw_text, normalized, normalized_char_start)
    raw_end = _find_raw_boundary(raw_text, normalized, normalized_char_end)
    return raw_start, raw_end


def canonicalize_source_ref(
    source: NativeSource,
    geometry: PageGeometry,
    *,
    source_id: str | None = None,
    normalized_char_start: int | None = None,
    normalized_char_end: int | None = None,
    verification_state: str = "candidate",
) -> SourceRef:
    """Canonicalize one native parser candidate into a `SourceRef`.

    - Coordinates: `native_bbox` (pre-rotation, bottom-left PDF points) is
      transformed to canonical (rotation/CropBox-applied, top-left, PDF
      points) via `PageGeometry.to_canonical_affine`. Missing bbox stays
      `None` and `location_quality` becomes `unlocated` (never a fabricated
      zero bbox, FX-LOC-001).
    - Pages: `page_num` is always `source.physical_page` (1-based); the
      independent `printed_page_label` is passed through unchanged
      (docs/27 §3 -- the three page numbers are never conflated).
    - Text: if `normalized_char_start`/`normalized_char_end` are given (as a
      UI/LLM would report them against NFC-normalized text), they are mapped
      back to raw-text code point offsets before being stored, so the raw
      `char_start`/`char_end` and `quote` always describe the actual raw
      text (FX-LOC-003). Otherwise `source.char_start`/`char_end` are used
      directly (already raw offsets).
    - Hashing: `raw_text_sha256` is always computed over the raw (not
      NFC-normalized) text, so provenance is never silently rewritten by
      normalization.
    """
    if normalized_char_start is None and normalized_char_end is None:
        char_start, char_end = source.char_start, source.char_end
    elif normalized_char_start is not None and normalized_char_end is not None:
        char_start, char_end = _map_normalized_offsets_to_raw(
            source.raw_text, normalized_char_start, normalized_char_end
        )
        # Fail closed rather than mis-cite: the raw slice picked out by the
        # mapped offsets must NFC-normalize to exactly the requested
        # normalized substring (docs/27 §4, FX-LOC-003).
        requested = unicodedata.normalize("NFC", source.raw_text)[
            normalized_char_start:normalized_char_end
        ]
        actual = unicodedata.normalize("NFC", source.raw_text[char_start:char_end])
        if actual != requested:
            raise DomainValidationError(
                "normalized-to-raw offset mapping did not round-trip to the "
                "requested substring; refusing to cite the wrong span"
            )
    else:
        raise DomainValidationError(
            "normalized_char_start and normalized_char_end must be provided together"
        )

    canonical_bbox = canonical_bbox_from_native(source.native_bbox, geometry)
    location_quality = "located" if canonical_bbox is not None else "unlocated"

    quote = source.raw_text[char_start:char_end]
    raw_text_sha256 = _sha256_hex(source.raw_text)

    return SourceRef(
        source_id=source_id if source_id is not None else str(uuid4()),
        document_version_id=source.document_version_id,
        parse_manifest_id=source.parse_manifest_id,
        page_num=source.physical_page,
        printed_page_label=source.printed_page_label,
        bbox=canonical_bbox,
        raw_text_sha256=raw_text_sha256,
        quote=quote,
        char_start=char_start,
        char_end=char_end,
        location_quality=location_quality,  # type: ignore[arg-type]
        verification_state=verification_state,  # type: ignore[arg-type]
    )
