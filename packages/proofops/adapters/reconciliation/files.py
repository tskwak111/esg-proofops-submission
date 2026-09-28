"""Root-constrained immutable file reader for reconciliation provenance."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofops.application.reconciliation.sources import (
    MAX_SOURCE_BYTES,
    SourceVerificationError,
    validate_source_bytes,
)


class SourceReadError(ValueError):
    """A local artifact cannot be proven to satisfy its source reference."""


@dataclass(frozen=True, slots=True)
class _Artifact:
    path: Path
    format: str
    sha256: str


class FileSourceReader:
    """Read operator-manifested local files and recheck location/hash on every read."""

    def __init__(self, root: str | Path, artifacts: Mapping[str, Mapping[str, Any]]) -> None:
        self._root = Path(root).resolve(strict=True)
        if not self._root.is_dir():
            raise SourceReadError("artifact_root_not_directory")
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise SourceReadError("artifact_manifest_empty")
        self._artifacts: dict[str, _Artifact] = {}
        for document_id, raw in artifacts.items():
            if not isinstance(document_id, str) or not document_id or not isinstance(raw, Mapping):
                raise SourceReadError("artifact_manifest_invalid")
            relative, kind, digest = raw.get("path"), raw.get("format"), raw.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative
                or ":" in relative
                or Path(relative).is_absolute()
            ):
                raise SourceReadError("path_outside_root")
            path = self._contained(self._root / relative)
            if kind not in {"text", "xml", "html", "pdf"}:
                raise SourceReadError("unsupported_format")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise SourceReadError("artifact_manifest_hash_invalid")
            self._artifacts[document_id] = _Artifact(path, kind, digest)

    def _contained(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SourceReadError("artifact_not_found") from exc
        if not resolved.is_relative_to(self._root) or not resolved.is_file():
            raise SourceReadError("path_outside_root")
        return resolved

    def _artifact_for(self, ref: Mapping[str, Any]) -> _Artifact:
        document_id = ref.get("document_id") if isinstance(ref, Mapping) else None
        if not isinstance(document_id, str) or document_id not in self._artifacts:
            raise SourceReadError("document_not_manifested")
        return self._artifacts[document_id]

    @staticmethod
    def _verify(ref: Mapping[str, Any], artifact: _Artifact, payload: bytes) -> None:
        if (
            hashlib.sha256(payload).hexdigest() != artifact.sha256
            or ref.get("artifact_sha256") != artifact.sha256
        ):
            raise SourceReadError("artifact_hash_mismatch")

    def __call__(self, ref: Mapping[str, Any]) -> bytes:
        artifact = self._artifact_for(ref)
        path = self._contained(artifact.path)
        try:
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                    raise SourceReadError("source_size_invalid")
                payload = stream.read(MAX_SOURCE_BYTES + 1)
            if len(payload) > MAX_SOURCE_BYTES or self._contained(artifact.path) != path:
                raise SourceReadError("source_changed_or_too_large")
        except OSError as exc:
            raise SourceReadError("artifact_read_failed") from exc
        self._verify(ref, artifact, payload)
        return payload

    def validate(self, ref: Mapping[str, Any], payload: bytes | None = None) -> bool:
        artifact = self._artifact_for(ref)
        payload = self(ref) if payload is None else payload
        if not isinstance(payload, bytes):
            raise SourceReadError("artifact_payload_not_bytes")
        self._verify(ref, artifact, payload)
        try:
            validate_source_bytes(payload, ref, format_name=artifact.format)
        except SourceVerificationError as exc:
            raise SourceReadError(str(exc)) from exc
        return True
