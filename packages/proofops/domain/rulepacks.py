"""Immutable pure-domain RulePack snapshot.

Stdlib only. This module never imports proofops.application (dependency
direction is application -> domain). It retains the verified configuration
content itself as canonical JSON payloads -- never a mutable external
file pointer -- so the pure rules engine can evaluate pinned
explicit ladders deterministically.

Hashing is deterministic and NaN-hostile: canonical JSON (sorted keys,
compact separators, ASCII, allow_nan=False) over a semantic envelope that
binds manifest identity (version, ontology, source hash, mode,
effective date, file set, gaps) together with every file payload.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from proofops.domain.errors import DomainValidationError

MANIFEST_HASH_FIELDS = (
    "version",
    "ontology_version",
    "source_document_sha256",
    "mode",
    "effective_date",
)


def canonical_json(value: Any) -> str:
    """Deterministic encoding; rejects NaN/Infinity and unserializable values."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(
            f"rulepack payload is not deterministically encodable: {exc}"
        ) from exc


def pack_content_hash(
    pack: Mapping[str, Any],
    files_content: Mapping[str, Any],
) -> str:
    """Semantic content hash: manifest identity + every file payload."""
    manifest = {field: pack.get(field) for field in MANIFEST_HASH_FIELDS}
    manifest["files"] = sorted(pack.get("files", []))
    manifest["unresolved_gap_ids"] = sorted(pack.get("unresolved_gap_ids", []))
    envelope = {
        "manifest": json.loads(canonical_json(manifest)),
        "files": {
            path: json.loads(canonical_json(files_content[path]))
            for path in sorted(files_content.keys())
        },
    }
    return hashlib.sha256(canonical_json(envelope).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class RulePackSnapshot:
    """Frozen, self-verifying rule configuration retained for the rules engine.

    `content` holds (path, canonical-JSON payload) pairs sorted by path.
    `file_content` returns a fresh dict per call, so callers never share
    mutable state with the snapshot.
    """

    rule_pack_id: str
    tenant_id: str
    version: str
    effective_date: str
    mode: str
    status: str
    ontology_version: str
    source_document_sha256: str
    sha256: str
    files: tuple[str, ...]
    unresolved_gap_ids: tuple[str, ...]
    approved_by: str | None
    approved_at: str | None
    content: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "unresolved_gap_ids", tuple(self.unresolved_gap_ids))
        content = tuple((path, payload) for path, payload in self.content)
        if any(
            not isinstance(path, str) or not isinstance(payload, str) for path, payload in content
        ):
            raise DomainValidationError("rulepack content must contain string path/payload pairs")
        object.__setattr__(self, "content", content)
        paths = [path for path, _ in self.content]
        if sorted(paths) != sorted(self.files):
            raise DomainValidationError(
                "rulepack snapshot content paths differ from manifest file set"
            )
        rebuilt = {path: json.loads(payload) for path, payload in self.content}
        manifest = {
            "rule_pack_id": self.rule_pack_id,
            "tenant_id": self.tenant_id,
            "version": self.version,
            "effective_date": self.effective_date,
            "mode": self.mode,
            "status": self.status,
            "ontology_version": self.ontology_version,
            "source_document_sha256": self.source_document_sha256,
            "files": list(self.files),
            "unresolved_gap_ids": list(self.unresolved_gap_ids),
        }
        if pack_content_hash(manifest, rebuilt) != self.sha256:
            raise DomainValidationError(
                "rulepack snapshot sha256 does not match retained content "
                "(refusing a tampered snapshot)"
            )

    def file_content(self, path: str) -> dict[str, Any]:
        """Return a fresh copy of one retained file payload."""
        for stored_path, payload in self.content:
            if stored_path == path:
                parsed = json.loads(payload)
                if not isinstance(parsed, dict):
                    raise DomainValidationError(f"rulepack file is not a mapping: {path}")
                return parsed
        raise KeyError(f"unknown rule file: {path}")


def snapshot_from_validated(
    pack: Mapping[str, Any],
    files_content: Mapping[str, Any],
) -> RulePackSnapshot:
    """Freeze validated pack metadata + content into a self-verifying snapshot.

    Call only after `validate_rulepack` passes; construction re-verifies the
    content hash and deep-freezes every payload through canonical JSON, so
    later mutation of the inputs cannot leak into the snapshot.
    """
    content = tuple(
        (path, canonical_json(files_content[path])) for path in sorted(files_content.keys())
    )
    return RulePackSnapshot(
        rule_pack_id=str(pack["rule_pack_id"]),
        tenant_id=str(pack["tenant_id"]),
        version=str(pack.get("version")),
        effective_date=str(pack.get("effective_date")),
        mode=str(pack.get("mode")),
        status=str(pack.get("status")),
        ontology_version=str(pack.get("ontology_version")),
        source_document_sha256=str(pack.get("source_document_sha256")),
        sha256=str(pack.get("sha256")),
        files=tuple(pack.get("files", [])),
        unresolved_gap_ids=tuple(pack.get("unresolved_gap_ids", [])),
        approved_by=pack.get("approved_by"),
        approved_at=pack.get("approved_at"),
        content=content,
    )
