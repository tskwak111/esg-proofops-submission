"""Immutable local publication/replay of source-validated assurance statements.

The trusted producer is `application.assurance.extract_assurance`, which reads
every field literally from the tenant-scoped canonical graph and rejects forged
or out-of-statement citations. This store never trusts a client-supplied
"verified" flag or a stored semantic hash on its own: both ``publish`` and
``load`` re-run ``extract_assurance`` against the run's *actual* trusted graph
(re-loaded from committed parser artifacts) using the stored tagged source refs,
and only accept the statement when the freshly recomputed ``semantic_hash`` and
the manifest/source/graph identities match. A forged statement carrying a
self-consistent hash but citations that do not exist in the real graph is
rejected because re-extraction raises before any hash comparison.

No public API/DB schema changes: statements live in the existing job_records
CAS (kind="assurance_statement") beside the run, using the same INSERT-once
immutability the tag/claim heads rely on, and publication increments the run's
mutation_epoch through the existing run-store fence. Absence of a published
statement is preserved as ``None`` so consumers keep returning "undetermined".
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.application.assurance import AssuranceStatement, extract_assurance
from proofops.application.ports.models import ModelBinding
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef, _require_uuid, _source_ref_from_dict

_KIND = "assurance_statement"
_KEY = "STATEMENT"


class AssurancePublicationConflict(ValueError):
    """A different immutable statement is already published for this run."""


def _statement_to_payload(statement: AssuranceStatement) -> dict:
    """Serialize the full frozen envelope; no field is dropped or summarized."""
    data = asdict(statement)
    data["semantic_hash"] = statement.semantic_hash
    return data


def _refs(items) -> tuple[SourceRef, ...]:
    return tuple(_source_ref_from_dict(item) for item in items)


def _tagged_fields_from_payload(data: dict) -> dict[str, tuple[SourceRef, ...]]:
    """Rebuild the name->SourceRefs tagging map used to re-extract the statement."""
    fields: dict[str, tuple[SourceRef, ...]] = {}
    for name, refs in data["tagged_fields"]:
        if refs:
            fields[name] = _refs(refs)
    return fields


class LocalAssuranceStore:
    """Publish/replay source-validated assurance statements in the run's CAS.

    ``uploads`` and ``parser`` let the store re-load the run's committed graph so
    every publish and replay re-verifies citations against original sources; they
    are optional only for narrow store-level tests that pass a graph directly.
    """

    def __init__(self, runs, uploads=None, parser=None):
        self.runs, self.uploads, self.parser = runs, uploads, parser

    def _trusted_graph(self, tenant_id, run_id):
        if self.uploads is None or self.parser is None:
            raise DomainValidationError("assurance replay requires uploads and parser")
        return load_run_graph(
            self.runs, self.uploads, self.parser, tenant_id=tenant_id, run_id=run_id
        )

    @staticmethod
    def _reextract(statement: AssuranceStatement, graph) -> AssuranceStatement:
        """Re-run the trusted extractor with the statement's own tagged refs.

        This is the original-source verification: `extract_assurance` re-checks
        each SourceRef against `graph` (page/bbox/quote/hash/offsets) and rejects
        any citation absent from the real document, so a fabricated statement
        cannot be revived by recomputing its hash.
        """
        payload = _statement_to_payload(statement)
        return extract_assurance(
            graph,
            statement.source_refs,
            statement.binding,
            tagged_fields=_tagged_fields_from_payload(payload),
            tenant_id=statement.tenant_id,
            statement_id=statement.statement_id,
            model_sha256=statement.model_sha256,
            prompt_sha256=statement.prompt_sha256,
            replicate_id=statement.replicate_id,
        )

    @staticmethod
    def _assert_graph_identity(statement: AssuranceStatement, graph) -> None:
        if (
            statement.document_version_id != graph.document_version_id
            or statement.parse_manifest_id != graph.parse_manifest_id
            or statement.source_sha256 != graph.source_sha256
            or statement.graph_sha256 != canonical_hash(asdict(graph))
        ):
            raise DomainValidationError("assurance statement graph identity mismatch")

    def publish(self, tenant_id: str, run_id: str, statement: AssuranceStatement) -> str:
        """Persist a statement immutably after re-verifying it against the graph.

        We re-extract from the run's actual committed graph and require the
        recomputed statement to be byte-identical (same semantic_hash) and to
        carry matching manifest/source/graph identities, then publish through
        the run-store fence so the run's mutation_epoch advances.
        """
        if not isinstance(statement, AssuranceStatement):
            raise DomainValidationError("publish requires an AssuranceStatement")
        _require_uuid("tenant_id", tenant_id)
        _require_uuid("run_id", run_id)
        run = self.runs.jobs.get_run(tenant_id, run_id)
        captured_epoch = run["mutation_epoch"]
        if statement.tenant_id != tenant_id:
            raise DomainValidationError("assurance statement tenant mismatch")
        if statement.document_version_id != run["document_version_id"]:
            raise DomainValidationError("assurance statement document version mismatch")
        graph = self._trusted_graph(tenant_id, run_id)
        if graph.tenant_id != tenant_id:
            raise DomainValidationError("assurance statement tenant mismatch")
        self._assert_graph_identity(statement, graph)
        recomputed = self._reextract(statement, graph)
        if recomputed.semantic_hash != statement.semantic_hash:
            raise DomainValidationError("assurance statement failed graph re-extraction")
        payload = _statement_to_payload(statement)
        with self.runs.jobs._transaction() as db:
            # CAS fence: re-read the run inside the writer transaction and reject
            # if it was deleted or re-parsed (mutation_epoch advanced) between the
            # graph load and here. _bump_run alone does not guard that race.
            fenced = self.runs.jobs._get(db, tenant_id, run_id, "run", "META")
            if fenced["document_version_id"] != statement.document_version_id:
                raise DomainValidationError("assurance statement document version mismatch")
            if fenced["mutation_epoch"] != captured_epoch:
                raise AssurancePublicationConflict(
                    "run changed while assurance statement was being verified"
                )
            existing = self.runs.jobs._raw(db, tenant_id, run_id, _KIND, _KEY)
            if existing is not None:
                stored = json.loads(existing)
                if stored.get("semantic_hash") != statement.semantic_hash:
                    raise AssurancePublicationConflict(
                        "a different assurance statement is already published"
                    )
                return statement.semantic_hash
            try:
                self.runs.jobs._put(db, tenant_id, run_id, _KIND, _KEY, payload, immutable=True)
            except sqlite3.IntegrityError as exc:  # pragma: no cover - race guard
                raise AssurancePublicationConflict(
                    "a different assurance statement is already published"
                ) from exc
            # Advance the run's mutation epoch so cached analysis snapshots that
            # predate this publication are invalidated (same fence tags use).
            self.runs.jobs._bump_run(db, fenced)
        return statement.semantic_hash

    def load(self, tenant_id: str, run_id: str) -> AssuranceStatement | None:
        """Replay the published statement, re-verified against the graph, or None.

        Returning None (never a fabricated statement) keeps consumers at
        "undetermined" until a source-backed opinion has actually been published.
        A stored statement is only returned after re-extraction against the run's
        current trusted graph reproduces its exact semantic hash and identity.
        """
        _require_uuid("tenant_id", tenant_id)
        _require_uuid("run_id", run_id)
        with self.runs.jobs._transaction() as db:
            # Re-read META in the same transaction as the raw read so a run
            # deleted concurrently is rejected (KeyError) rather than replayed.
            run = self.runs.jobs._get(db, tenant_id, run_id, "run", "META")
            raw = self.runs.jobs._raw(db, tenant_id, run_id, _KIND, _KEY)
        if raw is None:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or "semantic_hash" not in data:
            raise DomainValidationError("assurance statement payload is malformed")
        binding = data["binding"]
        if not isinstance(binding, dict):
            raise DomainValidationError("assurance binding payload is malformed")
        stored = AssuranceStatement(
            statement_id=data["statement_id"],
            tenant_id=data["tenant_id"],
            document_version_id=data["document_version_id"],
            parse_manifest_id=data["parse_manifest_id"],
            source_sha256=data["source_sha256"],
            graph_sha256=data["graph_sha256"],
            binding=ModelBinding(binding["binding_id"], binding["role"], binding["synthetic"]),
            model_sha256=data["model_sha256"],
            prompt_sha256=data["prompt_sha256"],
            replicate_id=data["replicate_id"],
            synthetic=data["synthetic"],
            provider=data["provider"],
            standard_raw=data["standard_raw"],
            level=data["level"],
            reporting_period=data["reporting_period"],
            entities=tuple(data["entities"]),
            facilities=tuple(data["facilities"]),
            covered_metrics=tuple(data["covered_metrics"]),
            excluded_entities=tuple(data["excluded_entities"]),
            excluded_facilities=tuple(data["excluded_facilities"]),
            excluded_metrics=tuple(data["excluded_metrics"]),
            excluded_periods=tuple(data["excluded_periods"]),
            explicit_exclusions=tuple((n, v) for n, v in data["explicit_exclusions"]),
            source_refs=_refs(data["source_refs"]),
            tagged_fields=tuple((n, _refs(refs)) for n, refs in data["tagged_fields"]),
            unresolved_fields=tuple(data["unresolved_fields"]),
        )
        if stored.semantic_hash != data["semantic_hash"]:
            raise DomainValidationError("assurance statement failed integrity replay")
        if (
            stored.tenant_id != tenant_id
            or stored.document_version_id != run["document_version_id"]
        ):
            raise DomainValidationError("published assurance statement identity mismatch")
        graph = self._trusted_graph(tenant_id, run_id)
        if graph.tenant_id != tenant_id:
            raise DomainValidationError("published assurance statement identity mismatch")
        self._assert_graph_identity(stored, graph)
        recomputed = self._reextract(stored, graph)
        if recomputed.semantic_hash != stored.semantic_hash:
            raise DomainValidationError("assurance statement failed graph re-extraction")
        return stored
