"""Local source review and bounded ownership; no numeric or grade approval."""

import json
import re
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

from proofops.adapters.local import native_note_ownership
from proofops.adapters.local.review_store import LocalSQLiteReviewStore
from proofops.adapters.local.run_artifacts import load_run_evidence
from proofops.adapters.local.source_condition_view import render_run_fragment
from proofops.application.authorization import AuthContext
from proofops.application.evidence import (
    citations,
    source_condition_interpretation,
    source_condition_ownership,
)
from proofops.application.evidence.source_condition_interpretation import (
    validate_source_condition_interpretation,
)
from proofops.application.evidence.source_condition_ownership import (
    validate_source_condition_ownership,
)
from proofops.application.ingest import normalize
from proofops.application.reviews import ReviewRejected
from proofops.domain import numeric
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash


class LocalSourceConditionReview:
    kind = "local-synthetic-only"

    def __init__(self, runs, uploads, parser):
        self.runs, self.uploads, self.parser = runs, uploads, parser
        self.store = LocalSQLiteReviewStore(runs.jobs)

    def _inputs(self, tenant, run_id):
        epoch = self.runs.get(tenant, run_id)["mutation_epoch"]
        evidence = load_run_evidence(
            self.runs, self.uploads, self.parser, tenant_id=tenant, run_id=run_id
        )
        graph = evidence["base_graph"]
        fragments = {}

        def add(fragment, text, bbox, page):
            identifier = canonical_hash(fragment)
            fragments[identifier] = dict(
                id=identifier,
                fragment=fragment,
                text=text,
                bbox=list(bbox) if bbox is not None else None,
                physical_page=page,
            )
            # ponytail: bounded complete inventory; paginate before supporting larger runs.
            if len(fragments) > 5000:
                raise ReviewRejected("SOURCE_REVIEW_INVENTORY_LIMIT", 413)

        for block in graph.blocks:
            for c in block.candidates:
                s = c.source
                if not s.raw_text:
                    continue
                add(
                    dict(
                        source_id=block.source_id,
                        parser_run_id=s.parser_run_id,
                        source_native_id=s.source_native_id,
                        char_start=0,
                        char_end=len(s.raw_text),
                        raw_text_sha256=sha256(s.raw_text.encode()).hexdigest(),
                    ),
                    s.raw_text,
                    c.bbox,
                    s.physical_page,
                )
        note_hashes = []
        for encoded in evidence["note_reviews"]:
            artifact = json.loads(encoded)
            note_hashes.append(artifact["artifact_sha256"])
            packet = artifact["packet"]
            data = packet["untrusted_document_data"]
            groups = [n["fragment_ids"] for n in artifact["checked"]["notes"]]
            groups += [[f["id"]] for f in data["fragments"]]
            for ids in groups:
                parts = [f for f in data["fragments"] if f["id"] in ids]
                add(
                    dict(
                        note_artifact_sha256=artifact["artifact_sha256"],
                        packet_sha256=canonical_hash(packet),
                        physical_page=data["page"],
                        fragment_ids=[f["id"] for f in parts],
                        native_word_indices=sorted({i for f in parts for i in f["word_indices"]}),
                    ),
                    "\n".join(f["text"] for f in parts),
                    (
                        min(f["bbox"][0] for f in parts),
                        min(f["bbox"][1] for f in parts),
                        max(f["bbox"][2] for f in parts),
                        max(f["bbox"][3] for f in parts),
                    ),
                    data["page"],
                )
        return dict(
            schema="source_condition_inputs_v1",
            tenant_id=tenant,
            run_id=run_id,
            document_version_id=graph.document_version_id,
            parse_manifest_id=graph.parse_manifest_id,
            source_sha256=graph.source_sha256,
            object_version_id=evidence["source"].object_version_id,
            base_graph_sha256=canonical_hash(asdict(graph)),
            graph_sha256=canonical_hash(asdict(evidence["graph"])),
            run_input_hash=evidence["input_hash"],
            parse_checkpoint_sha256=evidence["parse_checkpoint_sha256"],
            note_artifact_sha256=note_hashes,
            fragments=[fragments[k] for k in sorted(fragments)],
            issues=[i.to_dict() for i in evidence["graph"].issues],
        ), epoch

    @staticmethod
    def _authorize(actor):
        if not isinstance(actor, AuthContext) or not actor.has_capability("reviewer"):
            raise ReviewRejected("FORBIDDEN", 403)

    @staticmethod
    def _response(review, inputs):
        if canonical_hash(inputs) != review["source_snapshot_sha256"]:
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
        return dict(
            review=review,
            fragments=inputs["fragments"],
            issues=inputs["issues"],
            coverage_status="unknown",
        )

    def publish(self, actor, run_id):
        self._authorize(actor)
        inputs, epoch = self._inputs(actor.tenant_id, run_id)
        review = self.store.publish_source_conditions(inputs, expected_epoch=epoch)
        return self._response(review, inputs)

    def get(self, actor, run_id, *, revision=None):
        self._authorize(actor)
        review = self.store.source_conditions(actor.tenant_id, run_id, revision=revision)
        inputs, _epoch = self._inputs(actor.tenant_id, run_id)
        return self._response(review, inputs)

    def preview_observations(self, actor, run_id, body):
        self._authorize(actor)
        if (
            not isinstance(body, dict)
            or set(body) != {"revision", "table_id", "bindings"}
            or type(body["revision"]) is not int
            or body["revision"] < 1
            or not isinstance(body["table_id"], str)
            or not isinstance(body["bindings"], list)
            or not 1 <= len(body["bindings"]) <= 16
        ):
            raise ReviewRejected("SOURCE_OR_BINDING_REJECTED", 422)
        review = self.store.source_conditions(actor.tenant_id, run_id, revision=body["revision"])
        inputs, epoch = self._inputs(actor.tenant_id, run_id)
        self._response(review, inputs)
        evidence = load_run_evidence(
            self.runs, self.uploads, self.parser, tenant_id=actor.tenant_id, run_id=run_id
        )
        graph = evidence["base_graph"]
        if canonical_hash(asdict(graph)) != inputs["base_graph_sha256"]:
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
        graph = self._reviewed_graph(graph, review["state"], review["source_snapshot_sha256"])
        try:
            result = normalize.normalize_table_bindings(
                graph,
                table_id=body["table_id"],
                bindings=tuple(body["bindings"]),
                tenant_id=actor.tenant_id,
            )
        except (ValueError, DomainValidationError) as error:
            raise ReviewRejected("SOURCE_OR_BINDING_REJECTED", 422) from error
        issues = list(inputs["issues"])
        original_ids = {issue["issue_id"] for issue in issues}
        issues.extend(i.to_dict() for i in result.conflicts if i.issue_id not in original_ids)
        if self.runs.get(actor.tenant_id, run_id)["mutation_epoch"] != epoch:
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 412)
        response = dict(
            review_revision=review["revision"],
            review_sha256=review["revision_sha256"],
            source_snapshot_sha256=review["source_snapshot_sha256"],
            normalization_sha256=canonical_hash(
                {
                    name: sha256(Path(path).read_bytes()).hexdigest()
                    for name, path in (
                        ("normalizer", normalize.__file__),
                        ("numeric", numeric.__file__),
                        ("review", __file__),
                    )
                }
            ),
            request=json.loads(json.dumps(body)),
            observations=[o.to_dict() for o in result.observations],
            source_holds=[
                numeric.observation_source_holds(o, replace(graph, issues=evidence["graph"].issues))
                for o in result.observations
            ],
            issues=issues,
            status="proposal_only",
            coverage_status="unknown",
        )
        return {**response, "preview_sha256": canonical_hash(response)}

    def view(self, actor, run_id, *, revision, fragment_id):
        response = self.get(actor, run_id, revision=revision)
        item = next((f for f in response["fragments"] if f["id"] == fragment_id), None)
        if item is None:
            raise ReviewRejected("RESOURCE_NOT_FOUND", 404)
        display, png = render_run_fragment(
            self.runs,
            self.uploads,
            self.parser,
            item["fragment"],
            tenant_id=actor.tenant_id,
            run_id=run_id,
        )
        with self.runs.jobs._transaction() as db:
            inputs = self.runs.jobs._get(
                db, actor.tenant_id, run_id, "source_condition_inputs", "INPUTS"
            )
        if (
            display["published_graph_sha256"],
            display["graph_sha256"],
            display["run_input_hash"],
        ) != (inputs["graph_sha256"], inputs["base_graph_sha256"], inputs["run_input_hash"]):
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
        review = response["review"]
        return dict(
            review_revision=review["revision"],
            review_sha256=review["revision_sha256"],
            source_snapshot_sha256=review["source_snapshot_sha256"],
            fragment_id=fragment_id,
            display=display,
            image_url=f"/v1/runs/{run_id}/source-condition-review/source-view?revision={revision}&fragment_id={fragment_id}&format=image",
        ), png

    def resolve(self, actor, run_id, body, if_match, key):
        self._authorize(actor)
        if (
            not isinstance(if_match, str)
            or len(if_match) > 22
            or not re.fullmatch(r'"[1-9][0-9]*"', if_match)
        ):
            raise ReviewRejected("IF_MATCH_REQUIRED", 400)
        expected = int(if_match[1:-1])
        categories = ("classifications", "citations", "ownership", "conditions", "claim_bindings")
        if (
            not isinstance(body, dict)
            or set(body)
            != {
                "schema_version",
                "base_source_revision",
                "source_snapshot_sha256",
                "reason",
                *categories,
            }
            or type(body["schema_version"]) is not int
            or body["schema_version"] != 1
            or any(not isinstance(body[k], list) for k in categories)
            or body["claim_bindings"]
            or not 1 <= sum(len(body[k]) for k in categories) <= 16
        ):
            raise ReviewRejected("VALIDATION_ERROR", 422)
        cached = self.store.source_condition_retry(actor, run_id, body, expected, key)
        if cached is not None:
            return dict(review=cached, numeric_receipts=[], coverage_status="unknown")
        current = self.store.source_conditions(actor.tenant_id, run_id)
        if current["revision"] != expected or body["base_source_revision"] != expected:
            raise ReviewRejected("STALE_REVIEW_REVISION", 412)
        inputs, epoch = self._inputs(actor.tenant_id, run_id)
        self._response(current, inputs)
        if body["source_snapshot_sha256"] != current["source_snapshot_sha256"]:
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
        inventory = {f["id"]: f["fragment"] for f in inputs["fragments"]}
        displays = {}
        for category in ("classifications", "citations"):
            seen = set()
            for item in body[category]:
                fields = {"id", "fragment", "state", "source_view_receipt"}
                if category == "classifications":
                    fields.add("reason")
                if (
                    not isinstance(item, dict)
                    or set(item) != fields
                    or not isinstance(item["id"], str)
                ):
                    raise ReviewRejected("VALIDATION_ERROR", 422)
                identifier = item["id"]
                if (
                    identifier in seen
                    or identifier not in inventory
                    or canonical_hash(item["fragment"]) != canonical_hash(inventory[identifier])
                ):
                    raise ReviewRejected("SOURCE_OR_BINDING_REJECTED", 422)
                seen.add(identifier)
                states = (
                    {"note", "not_note", "unknown", "conflict"}
                    if category == "classifications"
                    else {"confirmed", "unknown", "conflict", "unreadable"}
                )
                if not isinstance(item["state"], str) or item["state"] not in states:
                    raise ReviewRejected("VALIDATION_ERROR", 422)
                if category == "classifications" and (
                    not isinstance(item["reason"], str)
                    or not 5 <= len(item["reason"].strip())
                    or len(item["reason"]) > 1000
                ):
                    raise ReviewRejected("VALIDATION_ERROR", 422)
                needs_view = item["state"] in {"confirmed", "note", "not_note"}
                if needs_view:
                    if identifier not in displays:
                        displays[identifier], _png = self.view(
                            actor, run_id, revision=expected, fragment_id=identifier
                        )
                    if canonical_hash(item["source_view_receipt"]) != canonical_hash(
                        displays[identifier]
                    ):
                        raise ReviewRejected("SOURCE_VIEW_RECEIPT_REJECTED", 422)
                elif item["source_view_receipt"] is not None:
                    raise ReviewRejected("VALIDATION_ERROR", 422)

        effective = json.loads(json.dumps(current["state"]))
        for category in ("classifications", "citations"):
            for item in body[category]:
                effective[category][item["id"]] = item
        proposals = {}
        for category in ("ownership", "conditions"):
            entries = {key: value["proposal"] for key, value in effective[category].items()}
            seen = set()
            for item in body[category]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or item["id"] in seen
                ):
                    raise ReviewRejected("VALIDATION_ERROR", 422)
                seen.add(item["id"])
                entries[item["id"]] = item
            # ponytail: bounded whole snapshots; paginate before larger source reviews.
            if len(entries) > 256:
                code = (
                    "SOURCE_REVIEW_OWNERSHIP_LIMIT"
                    if category == "ownership"
                    else "SOURCE_REVIEW_CONDITION_LIMIT"
                )
                raise ReviewRejected(code, 413)
            proposals[category] = entries
        for item in body["conditions"]:
            owner_id = item.get("ownership_id")
            if not isinstance(owner_id, str):
                raise ReviewRejected("VALIDATION_ERROR", 422)
            owner = proposals["ownership"].get(owner_id, {})
            if not owner or owner.get("fragment_id") != item.get("fragment_id"):
                raise ReviewRejected("SOURCE_OR_BINDING_REJECTED", 422)
        if any(proposals.values()):
            policy_hash = canonical_hash(
                {
                    "ownership": sha256(
                        Path(source_condition_ownership.__file__).read_bytes()
                    ).hexdigest(),
                    "interpretation": sha256(
                        Path(source_condition_interpretation.__file__).read_bytes()
                    ).hexdigest(),
                    "numeric_literals": sha256(Path(numeric.__file__).read_bytes()).hexdigest(),
                    "reviewed_view": sha256(Path(__file__).read_bytes()).hexdigest(),
                    "citations": sha256(Path(citations.__file__).read_bytes()).hexdigest(),
                    "native_note_ownership": sha256(
                        Path(native_note_ownership.__file__).read_bytes()
                    ).hexdigest(),
                }
            )
            evidence = load_run_evidence(
                self.runs, self.uploads, self.parser, tenant_id=actor.tenant_id, run_id=run_id
            )
            graph = evidence["base_graph"]
            if canonical_hash(asdict(graph)) != inputs["base_graph_sha256"]:
                raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
            graph = self._reviewed_graph(graph, effective, current["source_snapshot_sha256"])

            def assess_ownership(proposal):
                state = effective | {"ownership": proposals["ownership"]}
                assessment = validate_source_condition_ownership(
                    graph, inventory, state, proposal, tenant_id=actor.tenant_id
                )
                fragment = inventory[proposal["fragment_id"]]
                if "source_id" not in fragment and proposal["state"] == "linked":
                    proof = native_note_ownership.prove_native_note_marker(
                        evidence["base_graph"],
                        evidence["source"].content,
                        fragment,
                        proposal["targets"],
                        evidence["note_reviews"],
                        tenant_id=actor.tenant_id,
                    )
                    if (
                        proof is not None
                        and proof.get("graph_sha256") != inputs["base_graph_sha256"]
                    ):
                        raise DomainValidationError("native proof original graph mismatch")
                    assessment = validate_source_condition_ownership(
                        graph,
                        inventory,
                        state,
                        proposal,
                        tenant_id=actor.tenant_id,
                        native_proof=proof,
                    )
                return assessment | {"policy_sha256": policy_hash}

            try:
                effective["ownership"] = {
                    key: dict(proposal=proposal, assessment=assess_ownership(proposal))
                    for key, proposal in proposals["ownership"].items()
                }
                effective["conditions"] = {
                    key: dict(
                        proposal=proposal,
                        assessment=validate_source_condition_interpretation(
                            graph,
                            inventory,
                            effective | {"conditions": proposals["conditions"]},
                            proposal,
                            tenant_id=actor.tenant_id,
                        )
                        | {"policy_sha256": policy_hash},
                    )
                    for key, proposal in proposals["conditions"].items()
                }
            except DomainValidationError as exc:
                raise ReviewRejected("SOURCE_OR_BINDING_REJECTED", 422) from exc

        def build(state):
            return effective

        revised = self.store.resolve_source_conditions(
            actor, run_id, body, expected, key, build, expected_epoch=epoch
        )
        return dict(review=revised, numeric_receipts=[], coverage_status="unknown")

    @staticmethod
    def _reviewed_graph(graph, state, snapshot):
        """Internal confirmed-citation view; preserve source candidates and every issue."""
        confirmed = {}
        graph_hash = canonical_hash(asdict(graph))
        for identifier, citation in state["citations"].items():
            if citation["state"] != "confirmed":
                continue
            fragment = citation["fragment"]
            if "source_id" not in fragment:
                continue
            receipt = citation["source_view_receipt"]
            display = receipt["display"]
            if (
                identifier != canonical_hash(fragment)
                or receipt["fragment_id"] != identifier
                or receipt["source_snapshot_sha256"] != snapshot
                or display["fragment"] != fragment
                or display["graph_sha256"] != graph_hash
                or display["source_sha256"] != graph.source_sha256
                or display["tenant_id"] != graph.tenant_id
            ):
                raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
            confirmed.setdefault(fragment["source_id"], []).append(fragment)
        blocks = []
        for block in graph.blocks:
            verified = False
            if block.quality == "unverified" and block.winner is not None:
                source = block.candidates[block.winner].source
                verified = any(
                    fragment
                    == dict(
                        source_id=block.source_id,
                        parser_run_id=source.parser_run_id,
                        source_native_id=source.source_native_id,
                        char_start=0,
                        char_end=len(source.raw_text),
                        raw_text_sha256=sha256(source.raw_text.encode()).hexdigest(),
                    )
                    for fragment in confirmed.get(block.source_id, [])
                )
            blocks.append(replace(block, quality="verified") if verified else block)
        return replace(graph, blocks=tuple(blocks))
