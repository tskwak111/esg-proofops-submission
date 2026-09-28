"""Offline preparation of review candidates from pinned DART originals.

The adapter deliberately produces *candidates*, never reviewed facts, search
receipts, accounting mappings, policy approvals, or reconciliation outcomes.
Every usable source points either at original XML bytes by a unique element ID
or at an explicitly derived UTF-8 text projection with complete parent lineage.
"""

from __future__ import annotations

import codecs
import copy
import hashlib
import json
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from proofops.adapters.dart.artifacts import ArtifactStore, safe_parse_xml
from proofops.adapters.dart.normalization import normalize_financial_amount
from proofops.adapters.reconciliation import FileSourceReader

CATALOG_SCHEMA = "reconciliation-candidates-1"
MAX_CANDIDATES = 2_000
MAX_QUOTE_CHARS = 8_000
# Cell tags DART uses inside <TR>, and the largest ROWSPAN that may bind rows.
_ROW_CELL_TAGS = frozenset({"td", "te", "th", "tu"})
MAX_DECLARED_ROWSPAN = 8
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CORP = re.compile(r"^[0-9]{8}$")
_RECEIPT = re.compile(r"^[0-9]{14}$")
_XML_DECL_ENCODING = re.compile(rb"<\?xml[^>]+encoding=['\"]\s*([^'\"\s]+)", re.I)


class CandidatePreparationError(ValueError):
    """A collection cannot be safely converted into review candidates."""


@dataclass(frozen=True, slots=True)
class CandidateArtifact:
    """Bytes destined for the bounded review artifact directory."""

    document_id: str
    path: str
    format: str
    sha256: str
    payload: bytes
    lineage: dict[str, Any]

    def index_entry(self) -> dict[str, str]:
        return {"path": self.path, "format": self.format, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class PreparedCandidates:
    catalog: dict[str, Any]
    artifacts: tuple[CandidateArtifact, ...]

    @property
    def artifact_index(self) -> dict[str, dict[str, str]]:
        return {artifact.document_id: artifact.index_entry() for artifact in self.artifacts}


def add_operator_sr_sources(
    prepared: PreparedCandidates,
    manifest: Mapping[str, Any],
    root: str | Path,
    *,
    max_sources: int = 100,
) -> PreparedCandidates:
    """Add bounded, byte-verified sustainability-report source candidates.

    ``root`` is operator-controlled local input.  Every path remains constrained
    by ``FileSourceReader`` and every source locator/quote is validated before
    bytes are copied to the prepared output.
    """
    if (
        type(max_sources) is not int
        or not 1 <= max_sources <= MAX_CANDIDATES
        or not isinstance(manifest, Mapping)
        or set(manifest) != {"schema_version", "artifacts", "documents", "sources"}
        or manifest.get("schema_version") != "reconciliation-sr-sources-1"
        or not isinstance(manifest.get("artifacts"), Mapping)
        or not isinstance(manifest.get("documents"), Mapping)
        or not isinstance(manifest.get("sources"), list)
        or not 1 <= len(manifest["sources"]) <= max_sources
    ):
        raise CandidatePreparationError("sr_source_manifest_invalid")
    try:
        reader = FileSourceReader(root, manifest["artifacts"])
    except ValueError as exc:
        raise CandidatePreparationError("sr_artifact_index_invalid") from exc
    documents = copy.deepcopy(dict(manifest["documents"]))
    if set(documents) != set(manifest["artifacts"]):
        raise CandidatePreparationError("sr_document_index_mismatch")
    artifacts = {artifact.document_id: artifact for artifact in prepared.artifacts}
    candidates = list(prepared.catalog["candidates"])
    seen_sources: set[str] = set()
    for source in manifest["sources"]:
        if not isinstance(source, Mapping) or set(source) != {
            "source_id",
            "document_id",
            "artifact_sha256",
            "locator",
            "quote",
            "lineage",
        }:
            raise CandidatePreparationError("sr_source_invalid")
        source_id, document_id = source.get("source_id"), source.get("document_id")
        lineage = source.get("lineage")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in seen_sources
            or not isinstance(document_id, str)
            or document_id not in documents
            or not isinstance(lineage, Mapping)
            or lineage.get("representation") not in {"original", "derived"}
            or not isinstance(lineage.get("original_artifact_sha256"), str)
            or _SHA256.fullmatch(lineage["original_artifact_sha256"]) is None
            or not isinstance(lineage.get("transformation"), str)
            or not lineage["transformation"]
            or not isinstance(lineage.get("transformation_locator"), str)
            or not lineage["transformation_locator"]
        ):
            raise CandidatePreparationError("sr_source_or_lineage_invalid")
        if lineage["representation"] == "original" and (
            lineage["original_artifact_sha256"] != source.get("artifact_sha256")
            or lineage["transformation"] != "none"
        ):
            raise CandidatePreparationError("sr_original_lineage_invalid")
        if lineage["representation"] == "derived":
            original_path = lineage.get("original_path")
            if (
                not isinstance(original_path, str)
                or not original_path
                or ":" in original_path
                or Path(original_path).is_absolute()
            ):
                raise CandidatePreparationError("sr_derived_parent_invalid")
            root_path = Path(root).resolve(strict=True)
            try:
                parent = (root_path / original_path).resolve(strict=True)
                if not parent.is_relative_to(root_path) or not parent.is_file():
                    raise CandidatePreparationError("sr_derived_parent_invalid")
                if parent.stat().st_size > 50 * 1024 * 1024:
                    raise CandidatePreparationError("sr_derived_parent_invalid")
                parent_payload = parent.read_bytes()
            except OSError as exc:
                raise CandidatePreparationError("sr_derived_parent_invalid") from exc
            if _digest(parent_payload) != lineage["original_artifact_sha256"]:
                raise CandidatePreparationError("sr_derived_parent_hash_mismatch")
        try:
            payload = reader(source)
            reader.validate(source, payload)
        except ValueError as exc:
            raise CandidatePreparationError("sr_source_verification_failed") from exc
        artifact_spec = manifest["artifacts"][document_id]
        format_name = artifact_spec["format"]
        extension = {"text": ".txt", "xml": ".xml", "html": ".html", "pdf": ".pdf"}[format_name]
        artifact = CandidateArtifact(
            document_id=document_id,
            path=f"operator/{source['artifact_sha256']}{extension}",
            format=format_name,
            sha256=source["artifact_sha256"],
            payload=payload,
            lineage=dict(lineage),
        )
        previous = artifacts.get(document_id)
        if previous is not None and previous.sha256 != artifact.sha256:
            raise CandidatePreparationError("sr_document_artifact_conflict")
        artifacts[document_id] = artifact
        raw = {"quote": source["quote"], "document_role": "sustainability"}
        candidate_lineage = {
            **dict(lineage),
            "source_locator": source["locator"],
        }
        candidate = {
            "candidate_type": "sustainability_source",
            "verification_state": "candidate",
            "source": {
                key: source[key]
                for key in (
                    "source_id",
                    "document_id",
                    "artifact_sha256",
                    "locator",
                    "quote",
                )
            }
            | {"format": format_name},
            "lineage": candidate_lineage,
            "raw": raw,
            "normalization_suggestions": {},
        }
        candidate["candidate_id"] = _candidate_id(candidate_lineage, raw)
        candidates.append(candidate)
        seen_sources.add(source_id)
    catalog = copy.deepcopy(prepared.catalog)
    catalog["candidates"] = candidates
    # ``max_candidates`` bounds the DART catalogue only.  Operator sustainability
    # sources are added on top of it, so the separate bound and count are reported
    # instead of leaving ``len(candidates) > max_candidates`` unexplained.
    # ``max_operator_sources`` is the bound of *this* call, i.e. of the manifest
    # being added now, while ``operator_source_count`` is the cumulative number of
    # operator sources already in the catalogue.  Neither changes any semantics.
    operator_sources = sum(
        1 for item in candidates if item["candidate_type"] == "sustainability_source"
    )
    catalog["limits"] = {
        **catalog["limits"],
        "max_operator_sources": max_sources,
        "operator_source_count": operator_sources,
    }
    catalog["artifacts"] = [
        {"document_id": item.document_id, **item.index_entry(), "lineage": item.lineage}
        for item in sorted(artifacts.values(), key=lambda value: value.document_id)
    ]
    catalog["operator_documents"] = documents
    return PreparedCandidates(
        catalog=catalog,
        artifacts=tuple(sorted(artifacts.values(), key=lambda value: value.document_id)),
    )


class _DartTextProjectionParser(HTMLParser):
    """Tolerant text reader for DART's non-well-formed proprietary markup.

    ``rows`` is the projected text and is byte-identical to earlier releases.
    ``row_groups`` is a parallel, non-projected record of the disclosed table row
    each text node belongs to, so the cells of one row can be recognised later
    without ever joining two different rows.

    A row is one ``<tr>`` instance, except where the document itself binds
    consecutive ``<tr>`` siblings together with ``ROWSPAN``: the Samsung FY2024
    cover states its reporting period as ``<TD ROWSPAN="2">사업연도</TD>`` over one
    ``<TR>`` holding the start date and a second ``<TR>`` holding the end date.
    Those rows are grouped because the original markup says they are one logical
    row, never because two rows happen to be adjacent.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.rows: list[tuple[str, str]] = []
        self.row_groups: list[int | None] = []
        self._instances: list[int] = []
        self._opened = 0
        self._logical_rows = 0
        self._current: dict[int, int] = {}
        # Named to stay clear of HTMLParser's own private state: CPython 3.12.14
        # introduced ``HTMLParser._pending`` and ``close()`` joins it as strings.
        self._rowspan_pending: dict[int, int] = {}
        self._row_of: dict[int, int] = {}
        self._promised: dict[int, int] = {}
        self._attached: dict[int, int] = {}

    @property
    def incomplete_rows(self) -> frozenset[int]:
        """Logical rows that promised more ``<tr>`` siblings than the markup carried.

        A ``ROWSPAN="3"`` followed by only one further row -- because ``</TBODY>``
        or the end of the member arrived first -- is a malformed declaration. What
        did arrive is not a complete row and must not be published as one, so the
        caller drops these groups entirely.
        """
        return frozenset(
            row for row, promised in self._promised.items() if self._attached.get(row, 0) < promised
        )

    def _note_rowspan(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record a well-formed ``ROWSPAN`` declared by a cell of the open row.

        Only a cell tag directly inside the open ``<tr>`` may bind rows, the value
        must be a plain integer, and an out-of-range declaration is ignored so the
        rows simply stay separate instead of producing an oversized span.
        """
        if tag not in _ROW_CELL_TAGS or not self.stack or self.stack[-1] != "tr":
            return
        if len(self._instances) < 2:
            return
        for key, value in attrs:
            if key.lower() != "rowspan":
                continue
            try:
                declared = int((value or "").strip())
            except ValueError:
                return
            if 2 <= declared <= MAX_DECLARED_ROWSPAN:
                parent = self._instances[-2]
                self._rowspan_pending[parent] = max(
                    self._rowspan_pending.get(parent, 0), declared - 1
                )
                row = self._row_of.get(self._instances[-1])
                if row is not None:
                    # The declaration starts at the row holding this cell, which is
                    # the ``_attached[row]``-th row of the group, so it reaches row
                    # ``_attached[row] + declared - 1``. Using ``declared`` alone
                    # would call a group complete while a promised row is missing.
                    reaches = self._attached.get(row, 1) + declared - 1
                    self._promised[row] = max(self._promised.get(row, 1), reaches)
            return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._opened += 1
        parent = self._instances[-1] if self._instances else 0
        if tag == "tr":
            if self._rowspan_pending.get(parent, 0) > 0:
                self._rowspan_pending[parent] -= 1
            else:
                self._logical_rows += 1
                self._current[parent] = self._logical_rows
            row = self._current[parent]
            self._row_of[self._opened] = row
            self._attached[row] = self._attached.get(row, 0) + 1
            self._promised.setdefault(row, 1)
        else:
            self._note_rowspan(tag, attrs)
        self.stack.append(tag)
        self._instances.append(self._opened)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._note_rowspan(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]
            del self._instances[index:]

    def _enclosing_row(self) -> int | None:
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position] == "tr":
                return self._row_of.get(self._instances[position])
        return None

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.rows.append(("/" + "/".join(self.stack or ["document"]), text))
            self.row_groups.append(self._enclosing_row())


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidatePreparationError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise CandidatePreparationError("non_finite_json_number")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CandidatePreparationError("non_canonical_value") from exc


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _strict_xml_decode(payload: bytes) -> str:
    """Prove that XML is losslessly decodable before ElementTree sees it."""
    if payload.startswith(b"\x00\x00\xfe\xff"):
        encoding = "utf-32-be"
    elif payload.startswith(b"\xff\xfe\x00\x00"):
        encoding = "utf-32-le"
    elif payload.startswith(b"\xfe\xff"):
        encoding = "utf-16-be"
    elif payload.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
    else:
        match = _XML_DECL_ENCODING.search(payload[:512])
        if match:
            try:
                encoding = match.group(1).decode("ascii")
                codecs.lookup(encoding)
            except (UnicodeDecodeError, LookupError) as exc:
                raise CandidatePreparationError("xml_encoding_unknown") from exc
        elif b"\x00" in payload[:8]:
            raise CandidatePreparationError("xml_encoding_unknown")
        else:
            encoding = "utf-8"
    try:
        return payload.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise CandidatePreparationError("xml_encoding_unreadable") from exc


def _artifact_kind(source_id: str, locator: str) -> str:
    suffix = Path(locator).suffix.lower()
    if source_id.endswith(":statements") and suffix == ".json":
        return "statements"
    if source_id.endswith(":document") and suffix == ".zip":
        return "document"
    if source_id.endswith(":xbrl") and suffix == ".zip":
        return "xbrl"
    raise CandidatePreparationError("artifact_kind_or_extension_invalid")


def _validated_originals(
    manifest: Mapping[str, Any],
    store: ArtifactStore,
    *,
    corp_code: str,
    fy: int,
    rcept_no: str,
    consolidation: str,
) -> list[tuple[str, Mapping[str, Any], bytes]]:
    if (
        not isinstance(manifest, Mapping)
        or set(manifest)
        != {
            "schema_version",
            "manifest_id",
            "package_id",
            "synthetic",
            "fetched_at",
            "artifacts",
        }
        or manifest.get("schema_version") != "collection-1"
        or not isinstance(manifest.get("manifest_id"), str)
        or not manifest.get("manifest_id")
        or not isinstance(manifest.get("package_id"), str)
        or not manifest.get("package_id")
        or type(manifest.get("synthetic")) is not bool
        or not isinstance(manifest.get("artifacts"), list)
        or not manifest["artifacts"]
    ):
        raise CandidatePreparationError("collection_schema_invalid")
    if (
        _CORP.fullmatch(corp_code) is None
        or type(fy) is not int
        or not 1900 <= fy <= 2200
        or _RECEIPT.fullmatch(rcept_no) is None
        or consolidation not in {"consolidated", "separate"}
    ):
        raise CandidatePreparationError("expected_identity_invalid")

    required = {
        "source_id",
        "document_version_id",
        "artifact_sha256",
        "corp_code",
        "fy",
        "rcept_no",
        "consolidation",
        "source_system",
        "locator",
        "status",
        "error_code",
        "fetched_at",
    }
    seen: set[str] = set()
    originals: list[tuple[str, Mapping[str, Any], bytes]] = []
    for entry in manifest["artifacts"]:
        if not isinstance(entry, Mapping) or set(entry) != required:
            raise CandidatePreparationError("collection_artifact_schema_invalid")
        source_id = entry.get("source_id")
        digest = entry.get("artifact_sha256")
        locator = entry.get("locator")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in seen
            or entry.get("status") != "retrieved"
            or entry.get("error_code") is not None
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(locator, str)
            or not locator
            or entry.get("source_system") != "DART"
            or entry.get("corp_code") != corp_code
            or entry.get("fy") != fy
            or entry.get("rcept_no") != rcept_no
            or entry.get("consolidation") != consolidation
        ):
            raise CandidatePreparationError("collection_identity_or_status_mismatch")
        kind = _artifact_kind(source_id, locator)
        extension = Path(locator).suffix
        expected_path = store.path_for(digest, ext=extension).relative_to(store.root).as_posix()
        if locator != expected_path:
            raise CandidatePreparationError("artifact_locator_mismatch")
        try:
            payload = store.get(digest, ext=extension)
        except (OSError, ValueError) as exc:
            raise CandidatePreparationError("artifact_hash_or_read_failure") from exc
        seen.add(source_id)
        originals.append((kind, entry, payload))
    if set(kind for kind, _entry, _payload in originals) != {"statements", "document", "xbrl"}:
        raise CandidatePreparationError("required_artifact_set_missing")
    return originals


def _candidate_id(lineage: Mapping[str, Any], raw: Mapping[str, Any] | str) -> str:
    return "cand-" + _digest(_canonical({"lineage": lineage, "raw": raw}))[:24]


def _projection_artifact(
    *,
    document_id: str,
    payload: bytes,
    parent_entry: Mapping[str, Any],
    zip_member: str | None,
    member_sha256: str | None,
    transformation: str,
) -> CandidateArtifact:
    digest = _digest(payload)
    return CandidateArtifact(
        document_id=document_id,
        path=f"derived/{digest}.txt",
        format="text",
        sha256=digest,
        payload=payload,
        lineage={
            "representation": "derived",
            "original_source_id": parent_entry["source_id"],
            "original_artifact_sha256": parent_entry["artifact_sha256"],
            "zip_member": zip_member,
            "member_sha256": member_sha256,
            "transformation": transformation,
        },
    )


def _source(
    artifact: CandidateArtifact, source_id: str, locator: str, quote: str
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "document_id": artifact.document_id,
        "artifact_sha256": artifact.sha256,
        "format": artifact.format,
        "locator": locator,
        "quote": quote,
    }


def _statement_candidates(
    entry: Mapping[str, Any], payload: bytes, *, corp_code: str, fy: int, rcept_no: str
) -> tuple[list[dict[str, Any]], CandidateArtifact]:
    try:
        parsed = json.loads(
            payload.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CandidatePreparationError("statements_json_invalid") from exc
    if not isinstance(parsed, dict) or parsed.get("status") != "000":
        raise CandidatePreparationError("statements_status_invalid")
    rows = parsed.get("list")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise CandidatePreparationError("statements_rows_invalid")

    lines: list[str] = []
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for row in rows:
        if (
            row.get("corp_code") != corp_code
            or row.get("bsns_year") != str(fy)
            or row.get("rcept_no") != rcept_no
        ):
            raise CandidatePreparationError("statement_row_identity_mismatch")
        line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        lines.append(line)
        ranges.append((cursor, cursor + len(line)))
        cursor += len(line) + 1
    projection = ("\n".join(lines) + "\n").encode("utf-8")
    artifact = _projection_artifact(
        document_id=f"{entry['document_version_id']}:statements-projection",
        payload=projection,
        parent_entry=entry,
        zip_member=None,
        member_sha256=None,
        transformation="dart-statements-json/list-row -> canonical-key-order UTF-8 JSONL",
    )
    candidates: list[dict[str, Any]] = []
    for index, (row, line, (start, end)) in enumerate(zip(rows, lines, ranges, strict=True)):
        lineage = {
            **artifact.lineage,
            "transformation_locator": f"/list/{index}",
            "derived_locator": f"chars:{start}:{end}",
        }
        suggestions: dict[str, Any] = {}
        for prefix in ("thstrm", "frmtrm", "bfefrmtrm"):
            amount = row.get(f"{prefix}_amount")
            if isinstance(amount, str | int) and not isinstance(amount, bool):
                try:
                    suggestions[prefix] = normalize_financial_amount(
                        amount, currency=str(row.get("currency") or "KRW"), unit_raw=""
                    )
                except ValueError:
                    pass
        candidate: dict[str, Any] = {
            "candidate_type": "statement_row",
            "verification_state": "candidate",
            "source": _source(
                artifact,
                f"{entry['source_id']}:row:{index}",
                lineage["derived_locator"],
                line,
            ),
            "lineage": lineage,
            "raw": {
                key: copy.deepcopy(row.get(key))
                for key in (
                    "rcept_no",
                    "reprt_code",
                    "bsns_year",
                    "corp_code",
                    "sj_div",
                    "sj_nm",
                    "account_id",
                    "account_nm",
                    "account_detail",
                    "thstrm_nm",
                    "thstrm_amount",
                    "frmtrm_nm",
                    "frmtrm_amount",
                    "bfefrmtrm_nm",
                    "bfefrmtrm_amount",
                    "ord",
                    "currency",
                )
            },
            "normalization_suggestions": suggestions,
        }
        candidate["candidate_id"] = _candidate_id(lineage, candidate["raw"])
        candidates.append(candidate)
    return candidates, artifact


def _element_path(root: Any, target: Any) -> str:
    found: list[str] | None = None

    def walk(node: Any, parts: list[str]) -> None:
        nonlocal found
        if found is not None:
            return
        if node is target:
            found = parts
            return
        counts: Counter[str] = Counter()
        for child in list(node):
            name = _local_name(child.tag)
            counts[name] += 1
            walk(child, [*parts, f"{name}[{counts[name]}]"])

    walk(root, [f"{_local_name(root.tag)}[1]"])
    return "/" + "/".join(found or ["unknown[1]"])


def _row_element_path(path: str) -> str:
    """The path of the enclosing ``<tr>``: the first path cut after its last ``tr``."""
    parts = path.split("/")
    last = len(parts) - 1 - parts[::-1].index("tr")
    return "/".join(parts[: last + 1])


def _row_span_candidates(
    entry: Mapping[str, Any],
    artifact: CandidateArtifact,
    member_hash: str,
    rows: list[tuple[str, str]],
    lines: list[str],
    ranges: list[tuple[int, int]],
    kept_groups: list[int | None],
    disclosed: Mapping[int, int],
    *,
    row_span_limit: int,
) -> list[dict[str, Any]]:
    """Exact contiguous spans over the derived bytes, one per disclosed table row.

    A DART cover row such as ``사업연도 | 2024년 01월 01일 | 부터 | 2024년 12월 31일 |
    까지`` projects as separate cells, so no single cell candidate carries a whole
    reporting period. Each span quotes one disclosed row verbatim from the
    already-written projection, so the artifact bytes, hash, existing candidates
    and every quote/locator guard are untouched. Rows are never joined by
    adjacency: a span is emitted only when the cells of one row (one ``<tr>``, or
    the consecutive ``<tr>`` siblings the markup binds with ``ROWSPAN``) are
    consecutive in the projection and every disclosed cell of that row is present
    in that one run. A row interrupted by a nested table, shortened by the
    quote-length filter, cut by the candidate bound or longer than a reviewable
    quote is skipped entirely rather than emitted as a partial "complete" row.
    Nothing here is a reviewed fact or a normalization.
    """
    spans: list[dict[str, Any]] = []
    index = 0
    while index < len(rows) and len(spans) < row_span_limit:
        group = kept_groups[index]
        if group is None:
            index += 1
            continue
        last = index
        while last + 1 < len(rows) and kept_groups[last + 1] == group:
            last += 1
        quote = "\n".join(lines[index : last + 1])
        covered = last - index + 1
        if last > index and covered == disclosed.get(group) and len(quote) <= MAX_QUOTE_CHARS:
            locator = f"chars:{ranges[index][0]}:{ranges[last][1]}"
            row_path = _row_element_path(rows[index][0])
            lineage = {
                **artifact.lineage,
                "transformation_locator": (
                    f"proprietary-markup-row-span:{index}:{last}:{row_path}"
                ),
                "derived_locator": locator,
            }
            raw = {
                "element_path": row_path,
                "cell_paths": [path for path, _ in rows[index : last + 1]],
                "cell_texts": [text for _, text in rows[index : last + 1]],
            }
            candidate = {
                "candidate_type": "document_row_span",
                "verification_state": "candidate",
                "source": _source(
                    artifact,
                    f"{entry['source_id']}:{member_hash[:12]}:row:{index}-{last}",
                    locator,
                    quote,
                ),
                "lineage": lineage,
                "raw": raw,
                "normalization_suggestions": {},
            }
            candidate["candidate_id"] = _candidate_id(lineage, raw)
            spans.append(candidate)
        index = last + 1
    return spans


def _proprietary_member_candidates(
    entry: Mapping[str, Any],
    member_name: str,
    member: bytes,
    decoded: str,
    *,
    candidate_limit: int,
    row_span_limit: int = 0,
) -> tuple[list[dict[str, Any]], list[CandidateArtifact]]:
    lowered = decoded.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise CandidatePreparationError("xml_parse_rejected")
    parser = _DartTextProjectionParser()
    try:
        parser.feed(decoded)
        parser.close()
    except (ValueError, RecursionError) as exc:
        raise CandidatePreparationError("xml_parse_rejected") from exc
    usable = [
        (path, text, group)
        for (path, text), group in zip(parser.rows, parser.row_groups, strict=True)
        if len(text) <= MAX_QUOTE_CHARS
    ][:candidate_limit]
    rows = [(path, text) for path, text, _ in usable]
    kept_groups = [group for _, _, group in usable]
    disclosed = Counter(group for group in parser.row_groups if group is not None)
    for group in parser.incomplete_rows:
        # An unsatisfied ROWSPAN never yields a span: dropping the group here makes
        # the ``covered == disclosed.get(group)`` test below fail closed.
        disclosed.pop(group, None)
    if not rows:
        return [], []
    lines: list[str] = []
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for path, text in rows:
        line = f"{path}\t{text}"
        lines.append(line)
        ranges.append((cursor, cursor + len(line)))
        cursor += len(line) + 1
    member_hash = _digest(member)
    artifact = _projection_artifact(
        document_id=f"{entry['document_version_id']}:document:{member_hash[:16]}:projection",
        payload=("\n".join(lines) + "\n").encode("utf-8"),
        parent_entry=entry,
        zip_member=member_name,
        member_sha256=member_hash,
        transformation=(
            "DART proprietary markup HTMLParser text-node extraction, "
            "character-reference decode and whitespace-collapse -> path-tab-text UTF-8 projection"
        ),
    )
    candidates: list[dict[str, Any]] = []
    for index, ((path, text), line, (start, end)) in enumerate(
        zip(rows, lines, ranges, strict=True)
    ):
        locator = f"chars:{start}:{end}"
        lineage = {
            **artifact.lineage,
            "transformation_locator": f"proprietary-markup-text-node:{index}:{path}",
            "derived_locator": locator,
        }
        raw = {"element_path": path, "text": text}
        candidate = {
            "candidate_type": "document_element",
            "verification_state": "candidate",
            "source": _source(
                artifact,
                f"{entry['source_id']}:{member_hash[:12]}:{index}",
                locator,
                line,
            ),
            "lineage": lineage,
            "raw": raw,
            "normalization_suggestions": {},
        }
        candidate["candidate_id"] = _candidate_id(lineage, raw)
        candidates.append(candidate)
    candidates.extend(
        _row_span_candidates(
            entry,
            artifact,
            member_hash,
            rows,
            lines,
            ranges,
            kept_groups,
            disclosed,
            row_span_limit=row_span_limit,
        )
    )
    return candidates, [artifact]


def _xml_member_candidates(
    entry: Mapping[str, Any],
    member_name: str,
    member: bytes,
    *,
    kind: str,
    candidate_limit: int,
    row_span_limit: int = 0,
) -> tuple[list[dict[str, Any]], list[CandidateArtifact]]:
    decoded = _strict_xml_decode(member)
    try:
        root = safe_parse_xml(member)
    except (ET.ParseError, LookupError, ValueError) as exc:
        if kind == "document":
            return _proprietary_member_candidates(
                entry,
                member_name,
                member,
                decoded,
                candidate_limit=candidate_limit,
                row_span_limit=row_span_limit,
            )
        raise CandidatePreparationError("xml_parse_rejected") from exc
    member_hash = _digest(member)
    id_values = [
        value
        for element in root.iter()
        for key, value in element.attrib.items()
        if _local_name(key).lower() == "id" and isinstance(value, str) and value
    ]
    id_counts = Counter(id_values)
    usable: list[tuple[Any, str, str, str | None]] = []
    for element in root.iter():
        literal_text = "".join(element.itertext()).strip()
        text = " ".join(literal_text.split())
        if not text or len(text) > MAX_QUOTE_CHARS:
            continue
        element_id = next(
            (
                value
                for key, value in element.attrib.items()
                if _local_name(key).lower() == "id" and id_counts[value] == 1
            ),
            None,
        )
        # Narrative XML: leaf-like text blocks. XBRL: facts with contextRef.
        if kind == "xbrl":
            if not any(_local_name(k).lower() == "contextref" for k in element.attrib):
                continue
        elif list(element) and len(text) > 2_000:
            continue
        usable.append((element, text, literal_text, element_id))
        if len(usable) == candidate_limit:
            break

    artifacts: list[CandidateArtifact] = []
    original_artifact: CandidateArtifact | None = None
    derived_rows: list[tuple[Any, str, str, int, int]] = []
    cursor = 0
    for element, text, _literal_text, element_id in usable:
        if element_id is None:
            path = _element_path(root, element)
            line = f"{path}\t{text}"
            derived_rows.append((element, text, path, cursor, cursor + len(line)))
            cursor += len(line) + 1
    derived_artifact: CandidateArtifact | None = None
    if derived_rows:
        projection = (
            "\n".join(f"{path}\t{text}" for _, text, path, _, _ in derived_rows) + "\n"
        ).encode("utf-8")
        derived_artifact = _projection_artifact(
            document_id=f"{entry['document_version_id']}:{kind}:{member_hash[:16]}:projection",
            payload=projection,
            parent_entry=entry,
            zip_member=member_name,
            member_sha256=member_hash,
            transformation="XML itertext whitespace-collapse -> path-tab-text UTF-8 projection",
        )
        artifacts.append(derived_artifact)
    if any(element_id is not None for _, _, _, element_id in usable):
        original_artifact = CandidateArtifact(
            document_id=f"{entry['document_version_id']}:{kind}:{member_hash[:16]}",
            path=f"members/{member_hash}{Path(member_name).suffix.lower() or '.xml'}",
            format="xml",
            sha256=member_hash,
            payload=member,
            lineage={
                "representation": "original_member",
                "original_source_id": entry["source_id"],
                "original_artifact_sha256": entry["artifact_sha256"],
                "zip_member": member_name,
                "member_sha256": member_hash,
                "transformation": "safe ZIP extraction only; member bytes unchanged",
            },
        )
        artifacts.append(original_artifact)

    derived_lookup = {
        id(element): (path, start, end) for element, _, path, start, end in derived_rows
    }
    candidates: list[dict[str, Any]] = []
    for element, text, literal_text, element_id in usable:
        raw = {
            "element_name": _local_name(element.tag),
            "attributes": {_local_name(k): v for k, v in sorted(element.attrib.items())},
            "text": text,
        }
        if element_id is not None and original_artifact is not None:
            artifact = original_artifact
            locator = f"id:{element_id}"
            lineage = {**artifact.lineage, "original_locator": locator}
        elif derived_artifact is not None:
            artifact = derived_artifact
            path, start, end = derived_lookup[id(element)]
            locator = f"chars:{start}:{end}"
            lineage = {
                **artifact.lineage,
                "transformation_locator": f"xml-path:{path}",
                "derived_locator": locator,
            }
        else:
            continue
        candidate = {
            "candidate_type": "xbrl_fact" if kind == "xbrl" else "document_element",
            "verification_state": "candidate",
            "source": _source(
                artifact,
                f"{entry['source_id']}:{member_hash[:12]}:{len(candidates)}",
                locator,
                literal_text
                if element_id is not None
                else f"{lineage['transformation_locator'][9:]}\t{text}",
            ),
            "lineage": lineage,
            "raw": raw,
            "normalization_suggestions": {},
        }
        candidate["candidate_id"] = _candidate_id(lineage, raw)
        candidates.append(candidate)
    return candidates, artifacts


def build_candidate_catalog(
    manifest: Mapping[str, Any],
    store: ArtifactStore,
    *,
    corp_code: str,
    fy: int,
    rcept_no: str,
    consolidation: str,
    max_candidates: int = 500,
    max_row_spans: int = 200,
    max_zip_files: int = 1_000,
    max_zip_bytes: int = 50 * 1024 * 1024,
) -> PreparedCandidates:
    """Validate originals and return a deterministic, bounded candidate snapshot."""
    if type(max_candidates) is not int or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise CandidatePreparationError("candidate_limit_invalid")
    if type(max_row_spans) is not int or not 0 <= max_row_spans <= MAX_CANDIDATES:
        raise CandidatePreparationError("row_span_limit_invalid")
    originals = _validated_originals(
        manifest,
        store,
        corp_code=corp_code,
        fy=fy,
        rcept_no=rcept_no,
        consolidation=consolidation,
    )
    groups: dict[str, list[dict[str, Any]]] = {
        "statements": [],
        "document": [],
        "xbrl": [],
    }
    artifacts: dict[str, CandidateArtifact] = {}
    row_spans: list[dict[str, Any]] = []
    truncated = False
    for kind, entry, payload in sorted(originals, key=lambda item: item[0]):
        if kind == "statements":
            found, artifact = _statement_candidates(
                entry, payload, corp_code=corp_code, fy=fy, rcept_no=rcept_no
            )
            artifacts[artifact.document_id] = artifact
        else:
            with tempfile.TemporaryDirectory(prefix="proofops-dart-") as temp:
                try:
                    members = store.extract_zip_safe(
                        payload,
                        temp,
                        max_files=max_zip_files,
                        max_total_size=max_zip_bytes,
                    )
                except (OSError, ValueError) as exc:
                    raise CandidatePreparationError("zip_rejected") from exc
                found = []
                for relative in sorted(members, key=lambda value: value.as_posix().casefold()):
                    remaining = max_candidates - len(found)
                    if remaining <= 0:
                        truncated = True
                        break
                    if relative.suffix.lower() not in {".xml", ".xbrl"}:
                        continue
                    member = (Path(temp) / relative).read_bytes()
                    extracted, member_artifacts = _xml_member_candidates(
                        entry,
                        relative.as_posix(),
                        member,
                        kind=kind,
                        candidate_limit=remaining,
                        row_span_limit=max(0, max_row_spans - len(row_spans)),
                    )
                    # Row spans carry their own bound so they never displace an
                    # existing element candidate inside ``max_candidates``.
                    for candidate in extracted:
                        if candidate["candidate_type"] == "document_row_span":
                            row_spans.append(candidate)
                        else:
                            found.append(candidate)
                    for artifact in member_artifacts:
                        artifacts[artifact.document_id] = artifact
        remaining = max_candidates - len(groups[kind])
        groups[kind].extend(found[: max(0, remaining)])
        if len(found) > remaining:
            truncated = True
    candidates: list[dict[str, Any]] = []
    for index in range(max_candidates):
        added = False
        for kind in ("statements", "document", "xbrl"):
            if index < len(groups[kind]):
                candidates.append(groups[kind][index])
                added = True
                if len(candidates) == max_candidates:
                    break
        if len(candidates) == max_candidates or not added:
            break
    if sum(len(group) for group in groups.values()) > len(candidates):
        truncated = True
    candidates.extend(row_spans)
    referenced = {candidate["source"]["document_id"] for candidate in candidates}
    bounded_artifacts = tuple(artifacts[key] for key in sorted(artifacts) if key in referenced)
    catalog = {
        "schema_version": CATALOG_SCHEMA,
        "status": "candidate_only",
        "synthetic": manifest["synthetic"],
        "manifest_id": manifest["manifest_id"],
        "package_id": manifest["package_id"],
        "identity": {
            "corp_code": corp_code,
            "fy": fy,
            "rcept_no": rcept_no,
            "consolidation": consolidation,
        },
        "limits": {
            "max_candidates": max_candidates,
            "truncated": truncated,
            "max_row_span_candidates": max_row_spans,
            "row_span_candidate_count": len(row_spans),
        },
        "trust": {
            "reviewed": False,
            "policy_approved": False,
            "search_complete": False,
            "notice": "Candidates and normalization suggestions require operator review.",
        },
        "artifacts": [
            {"document_id": item.document_id, **item.index_entry(), "lineage": item.lineage}
            for item in bounded_artifacts
        ],
        "candidates": candidates,
    }
    return PreparedCandidates(catalog=catalog, artifacts=bounded_artifacts)
