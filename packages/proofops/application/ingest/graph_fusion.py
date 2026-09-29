"""Immutable parser candidates and conservative graph fusion.

The fixed v1 graph projection cannot carry conflict state. Consumers must retain
this internal graph/quality envelope; unresolved blocks have no selected text.
No legacy implementation or grading policy is imported.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from uuid import UUID, uuid5

from proofops.application.ingest.geometry import (
    affine_apply,
    canonical_bbox_from_native,
    canonicalize_source_ref,
    invert_affine,
    project_native_bbox,
)
from proofops.domain.documents import NativeSource, PageGeometry, _require_finite_bbox
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef, _require_uuid

KINDS = frozenset(
    (
        "paragraph",
        "heading",
        "table",
        "table_row",
        "table_cell",
        "caption",
        "footnote",
        "figure",
        "unknown",
    )
)
RELATIONS = frozenset(
    (
        "derived_from",
        "table_parent",
        "caption_of",
        "footnote_of",
        "same_statement",
        "section_parent",
        "alias_of",
    )
)


def _hash(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise ValueError("invalid SHA-256")


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    tenant_id: str
    document_id: str
    document_version_id: str
    sha256: str
    object_version_id: str
    content: bytes = field(repr=False)
    synthetic: bool = False

    def __post_init__(self):
        for name in ("tenant_id", "document_id", "document_version_id"):
            _require_uuid(name, getattr(self, name))
        _hash(self.sha256)
        if (
            not isinstance(self.content, bytes)
            or not isinstance(self.object_version_id, str)
            or not 1 <= len(self.object_version_id) <= 1024
            or type(self.synthetic) is not bool
        ):
            raise ValueError("invalid immutable source artifact")


@dataclass(frozen=True, slots=True)
class ParserProfile:
    parse_manifest_id: str
    physical_pages: tuple[int, ...] = ()
    java_executable: str = "java"
    timeout_seconds: float = 45
    max_bytes: int = 104_857_600
    max_output_bytes: int = 20_000_000
    memory_bytes: int = 768 * 1024 * 1024
    table_auxiliary: bool = True
    table_text_y_tolerance: float | None = None
    table_source_policy_sha256: str | None = None
    table_structure_repair: str | None = None
    vision_parse: str = "off"
    parser_mode: str = "local"

    def __post_init__(self):
        _require_uuid("parse_manifest_id", self.parse_manifest_id)
        pages = tuple(self.physical_pages)
        if (
            any(type(page) is not int or page < 1 for page in pages)
            or tuple(sorted(set(pages))) != pages
        ):
            raise ValueError("physical_pages must be sorted unique 1-based integers")
        object.__setattr__(self, "physical_pages", pages)
        for name in ("timeout_seconds", "max_bytes", "max_output_bytes", "memory_bytes"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
                or (name != "timeout_seconds" and type(value) is not int)
            ):
                raise ValueError("positive finite parser limit required")
        if not isinstance(self.java_executable, str) or not self.java_executable:
            raise ValueError("Java executable is required")
        if type(self.table_auxiliary) is not bool:
            raise ValueError("table_auxiliary must be boolean")

        if self.table_source_policy_sha256 is not None:
            _hash(self.table_source_policy_sha256)
        if self.table_structure_repair not in (None, "odl_header_v1", "odl_header_v2"):
            raise ValueError("unsupported table structure repair")
        if self.vision_parse not in ("auto", "all", "off"):
            raise ValueError("unsupported vision parse mode")
        if self.parser_mode not in ("upstage", "local"):
            raise ValueError("unsupported parser mode")
        tolerance = self.table_text_y_tolerance
        if tolerance is not None and (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, int | float)
            or not math.isfinite(tolerance)
            or tolerance < 0
        ):
            raise ValueError("table text tolerance must be finite and nonnegative")

    def invocation_snapshot(self) -> dict:
        """Keep historical invocation hashes when optional tuning is absent."""
        values = asdict(self)
        if self.table_text_y_tolerance is None:
            values.pop("table_text_y_tolerance")
        if self.table_source_policy_sha256 is None:
            values.pop("table_source_policy_sha256")
        if self.table_structure_repair is None:
            values.pop("table_structure_repair")
        if self.vision_parse == "off":
            values.pop("vision_parse")
        if self.parser_mode == "local":
            values.pop("parser_mode")
        return values

    def config_snapshot(self) -> dict:
        """Executable configuration independent of invocation identity/page selection."""
        return {
            key: value
            for key, value in self.invocation_snapshot().items()
            if key not in {"parse_manifest_id", "physical_pages"}
        }

    def config_hash(self) -> str:
        return canonical_hash(self.config_snapshot())


@dataclass(frozen=True, slots=True)
class CandidateBlock:
    kind: str
    source: NativeSource
    geometry: PageGeometry
    context: tuple[str, ...] = ()
    parser_bbox: tuple[float, float, float, float] | None = None
    parser_coordinate_system: str = "pdf_bottom_left_points"
    parser_to_canonical: tuple[float, float, float, float, float, float] | None = None
    table_native_id: str | None = None
    row_number: int | None = None
    column_number: int | None = None
    row_span: int | None = None
    column_span: int | None = None

    def __post_init__(self):
        if self.kind not in KINDS or any(not isinstance(item, str) for item in self.context):
            raise ValueError("invalid candidate kind/context")
        object.__setattr__(self, "context", tuple(self.context))
        expected = project_native_bbox(self.source.native_bbox, self.geometry)
        box = _require_finite_bbox("parser_bbox", self.parser_bbox)
        object.__setattr__(self, "parser_bbox", box)
        matrix = self.parser_to_canonical
        if matrix is not None:
            if len(matrix) != 6 or any(
                isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x)
                for x in matrix
            ):
                raise ValueError("invalid parser affine")
            matrix = tuple(matrix)
            invert_affine(matrix)
            object.__setattr__(self, "parser_to_canonical", matrix)
        if not isinstance(self.parser_coordinate_system, str) or not self.parser_coordinate_system:
            raise ValueError("parser coordinate system required")
        if box is not None:
            if matrix is None or expected is None:
                raise ValueError("parser geometry requires a canonical mapping")
            corners = [
                affine_apply(matrix, x, y) for x in (box[0], box[2]) for y in (box[1], box[3])
            ]
            actual = (
                min(p[0] for p in corners),
                min(p[1] for p in corners),
                max(p[0] for p in corners),
                max(p[1] for p in corners),
            )
            if any(
                not math.isclose(a, b, abs_tol=1e-6) for a, b in zip(actual, expected, strict=True)
            ):
                raise ValueError("parser geometry disagrees with canonical geometry")

    @property
    def bbox(self):
        try:
            return canonical_bbox_from_native(self.source.native_bbox, self.geometry)
        except DomainValidationError:
            return None

    @property
    def has_invalid_geometry(self) -> bool:
        return self.source.native_bbox is not None and self.bbox is None


@dataclass(frozen=True, slots=True)
class CandidateEdge:
    source_native_id: str
    target_native_id: str
    relation: str

    def __post_init__(self):
        if self.relation not in RELATIONS:
            raise ValueError("invalid edge relation")


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    parser_run_id: str
    parser_name: str
    parser_version: str
    parser_family: str
    config_hash: str
    blocks: tuple[CandidateBlock, ...]
    edges: tuple[CandidateEdge, ...] = ()
    synthetic: bool = False

    def __post_init__(self):
        for name in ("tenant_id", "document_version_id", "parse_manifest_id", "parser_run_id"):
            _require_uuid(name, getattr(self, name))
        _hash(self.source_sha256)
        _hash(self.config_hash)
        if not all(
            isinstance(value, str) and value
            for value in (self.parser_name, self.parser_version, self.parser_family)
        ):
            raise ValueError("parser identity required")
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "edges", tuple(self.edges))
        identifiers = set()
        for block in self.blocks:
            source = block.source
            if (
                source.document_version_id != self.document_version_id
                or source.parse_manifest_id != self.parse_manifest_id
                or source.parser_run_id != self.parser_run_id
            ):
                raise ValueError("candidate provenance identity mismatch")
            if source.source_native_id in identifiers:
                raise ValueError("duplicate native source identifier")
            identifiers.add(source.source_native_id)


@dataclass(frozen=True, slots=True)
class CanonicalEdge:
    source_id: str
    target_id: str
    relation: str


@dataclass(frozen=True, slots=True)
class QualityIssue:
    issue_id: str
    kind: str
    page_num: int
    source_ids: tuple[str, ...]
    state: str
    reason: str

    def to_dict(self) -> dict:
        return dict(asdict(self), source_ids=list(self.source_ids))


@dataclass(frozen=True, slots=True)
class CanonicalBlock:
    source_id: str
    kind: str
    candidates: tuple[CandidateBlock, ...]
    independent_families: tuple[str, ...]
    quality: str
    winner: int | None

    @property
    def sources(self) -> tuple[NativeSource, ...]:
        return tuple(block.source for block in self.candidates)

    @property
    def page_num(self) -> int:
        return self.sources[0].physical_page

    @property
    def bbox(self):
        return self.candidates[0].bbox

    @property
    def raw_text(self) -> str:
        return self.sources[self.winner].raw_text if self.winner is not None else ""

    @property
    def normalized_text(self) -> str:
        return unicodedata.normalize("NFC", self.raw_text)

    def source_ref(
        self, *, normalized_char_start: int | None = None, normalized_char_end: int | None = None
    ) -> SourceRef:
        if self.winner is None:
            raise ValueError("conflicted candidate has no selected source")
        block = self.candidates[self.winner]
        return canonicalize_source_ref(
            block.source,
            block.geometry,
            source_id=self.source_id,
            normalized_char_start=normalized_char_start,
            normalized_char_end=normalized_char_end,
        )

    def to_dict(self) -> dict:
        return dict(
            source_id=self.source_id,
            page_num=self.page_num,
            kind=self.kind,
            raw_text=self.raw_text,
            raw_text_sha256=sha256(self.raw_text.encode()).hexdigest(),
            normalized_text=self.normalized_text,
            bbox=list(self.bbox) if self.bbox is not None else None,
            provenance=[
                dict(
                    parser_run_id=item.source.parser_run_id,
                    source_native_id=item.source.source_native_id,
                    native_bbox=list(item.parser_bbox)
                    if item.parser_bbox is not None
                    else list(item.source.native_bbox)
                    if item.source.native_bbox
                    else None,
                    native_coordinate_system=item.parser_coordinate_system,
                    to_canonical_affine=list(
                        item.parser_to_canonical or item.geometry.to_canonical_affine()
                    ),
                )
                for item in self.candidates
            ],
        )


@dataclass(frozen=True, slots=True)
class CanonicalDocumentGraph:
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    blocks: tuple[CanonicalBlock, ...]
    edges: tuple[CanonicalEdge, ...]
    issues: tuple[QualityIssue, ...]
    candidates: tuple[CandidateBatch, ...]
    validation_profile: str = "fast_preview"

    def to_dict(self) -> dict:
        """Fixed v1 projection; rich provenance/conflicts remain in the manifest envelope."""
        return dict(
            schema_version="1",
            document_version_id=self.document_version_id,
            parse_manifest_id=self.parse_manifest_id,
            source_sha256=self.source_sha256,
            blocks=[block.to_dict() for block in self.blocks],
            edges=[asdict(edge) for edge in self.edges],
        )


def _matches(left: CandidateBlock, right: CandidateBlock, *, fusion_version: int) -> bool:
    if (
        left.kind != right.kind
        or left.source.physical_page != right.source.physical_page
        # bbox is a recomputing property: bind each operand's value once here and reuse it
        # for the IoU below. Short-circuiting is unchanged -- a kind/page mismatch still
        # projects neither bbox, and an unlocated/invalid left never evaluates right.
        or (a := left.bbox) is None
        or (b := right.bbox) is None
    ):
        return False
    left_context, right_context = left.context, right.context
    if fusion_version >= 2 and left.kind == "table_cell":
        # v2: only parser-assigned grid indices may differ. A grid line dropped
        # by one parser shifts its row/column numbers (KOGAS physical 68: the
        # auxiliary grid loses the first column, so the same visual cell
        # carries different column numbers per parser). Every other context
        # entry (table/title scope, year, unit, ...) still has to agree, so
        # cells with different explicit scope never merge on bbox alone.
        # Tall multi-row bundles and disjoint tables can never reach the IoU
        # threshold below, so they stay separate and no values are inferred.
        grid_prefixes = ("row number=", "column number=")
        left_context = tuple(entry for entry in left_context if not entry.startswith(grid_prefixes))
        right_context = tuple(
            entry for entry in right_context if not entry.startswith(grid_prefixes)
        )
    if left_context != right_context:
        # v1 legacy: parser-assigned row/column numbers must agree for every kind.
        return False
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return (
        intersection / union >= 0.8
    )  # docs/27 candidate alignment default, not proof of correctness


def fuse_candidates(
    candidates: tuple[CandidateBatch, ...], *, tenant_id: str, fusion_version: int = 3
) -> CanonicalDocumentGraph:
    if not candidates:
        raise ValueError("no parser candidates; parser failure is not an empty successful graph")
    if type(fusion_version) is not int or fusion_version not in (1, 2, 3, 4):
        raise ValueError("unsupported fusion version")
    if fusion_version < 3 and any(
        block.has_invalid_geometry for batch in candidates for block in batch.blocks
    ):
        raise ValueError("legacy fusion does not admit invalid geometry")
    first = candidates[0]
    identity = (tenant_id, first.document_version_id, first.parse_manifest_id, first.source_sha256)
    seen_runs = set()
    for batch in candidates:
        if (
            batch.tenant_id,
            batch.document_version_id,
            batch.parse_manifest_id,
            batch.source_sha256,
        ) != identity:
            raise ValueError("tenant/version/source/manifest mismatch")
        if batch.parser_run_id in seen_runs:
            raise ValueError("duplicate parser run")
        seen_runs.add(batch.parser_run_id)
    ordered = sorted(candidates, key=lambda batch: (batch.parser_name, batch.parser_run_id))
    table_lookup = {
        (batch.parser_run_id, block.source.source_native_id): block
        for batch in candidates
        for block in batch.blocks
        if block.kind == "table"
    }

    def compatible_tables(left_batch, left, right_batch, right):
        if fusion_version < 4 or left.kind == "table":
            return True
        if left.table_native_id is None and right.table_native_id is None:
            return True
        a = table_lookup.get((left_batch.parser_run_id, left.table_native_id))
        b = table_lookup.get((right_batch.parser_run_id, right.table_native_id))
        return a is not None and b is not None and _matches(a, b, fusion_version=fusion_version)

    groups: list[list[tuple[CandidateBatch, CandidateBlock]]] = []
    # ponytail: pairwise local region alignment, scanned per (physical page, kind) bucket because
    # _matches rejects that pair first and every group is homogeneous in it; global group creation
    # order and the first-matching-group choice are unchanged. Same-page cost is still quadratic.
    buckets: dict[tuple[int, str], list[list[tuple[CandidateBatch, CandidateBlock]]]] = {}
    aliases: dict[tuple[str, str], str] = {}
    for batch in ordered:
        for block in sorted(
            batch.blocks,
            key=lambda block: (block.source.physical_page, block.source.source_native_id),
        ):
            bucket = buckets.setdefault((block.source.physical_page, block.kind), [])
            group = next(
                (
                    group
                    for group in bucket
                    if all(
                        _matches(item, block, fusion_version=fusion_version)
                        and compatible_tables(item_batch, item, batch, block)
                        for item_batch, item in group
                    )
                ),
                None,
            )
            if group is None:
                group = [(batch, block)]
                groups.append(group)
                bucket.append(group)
            else:
                group.append((batch, block))
    blocks, issues = [], []
    for group in groups:
        sources = tuple(block for _, block in group)
        keys = tuple((batch.parser_run_id, block.source.source_native_id) for batch, block in group)
        identifier = str(uuid5(UUID(first.parse_manifest_id), f"{tenant_id}:{keys}"))
        conflict = len({unicodedata.normalize("NFC", item.source.raw_text) for item in sources}) > 1
        quality = (
            "conflicted" if conflict else "unlocated" if sources[0].bbox is None else "unverified"
        )
        canonical = CanonicalBlock(
            identifier,
            sources[0].kind,
            sources,
            tuple(sorted({batch.parser_family for batch, _ in group})),
            quality,
            None if conflict else 0,
        )
        blocks.append(canonical)
        aliases.update((key, identifier) for key in keys)
        if quality in ("conflicted", "unlocated"):
            invalid_geometry = any(item.has_invalid_geometry for item in sources)
            issues.append(
                QualityIssue(
                    str(uuid5(UUID(identifier), quality)),
                    "source_geometry_invalid"
                    if invalid_geometry
                    else "parse_conflict"
                    if conflict
                    else "source_unlocated",
                    canonical.page_num,
                    (identifier,),
                    "unreadable" if invalid_geometry else "open",
                    "Parser geometry is outside page bounds; original coordinates retained."
                    if invalid_geometry
                    else "Candidate content disagrees; no winner selected."
                    if conflict
                    else "Parser did not provide source geometry.",
                )
            )
    edges = set()
    for batch in ordered:
        for edge in batch.edges:
            try:
                origin = aliases[(batch.parser_run_id, edge.source_native_id)]
                target = aliases[(batch.parser_run_id, edge.target_native_id)]
            except KeyError:
                raise ValueError("dangling native edge") from None
            edges.add((origin, target, edge.relation))
    return CanonicalDocumentGraph(
        tenant_id,
        first.document_version_id,
        first.parse_manifest_id,
        first.source_sha256,
        tuple(sorted(blocks, key=lambda block: (block.page_num, block.source_id))),
        tuple(CanonicalEdge(*edge) for edge in sorted(edges)),
        tuple(sorted(issues, key=lambda issue: issue.issue_id)),
        tuple(ordered),
    )


def candidates_from_snapshot(values: list[dict]) -> tuple[CandidateBatch, ...]:
    """Restore validated native candidates; never promote the bare v1 projection."""
    return tuple(
        CandidateBatch(
            **{
                **batch,
                "blocks": tuple(
                    CandidateBlock(
                        **{
                            **block,
                            "source": NativeSource(**block["source"]),
                            "geometry": PageGeometry(**block["geometry"]),
                        }
                    )
                    for block in batch["blocks"]
                ),
                "edges": tuple(CandidateEdge(**edge) for edge in batch["edges"]),
            }
        )
        for batch in values
    )
