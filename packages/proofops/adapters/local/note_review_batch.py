"""All-table note preparation; no source/coverage approval and no transport retry."""

import json
from dataclasses import asdict
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from proofops.adapters.local import note_extraction
from proofops.adapters.local.table_notes import (
    freeze_note_review,
    prepare,
    replay_note_reviews,
    validate,
)
from proofops.adapters.local.upstage import UPSTAGE_TRANSPORT_STOP_CODES
from proofops.domain.provenance import canonical_hash


def review_tables(graph, source, client, output, *, tenant_id, selected_pages=None):
    """Return immutable artifacts for every table, including unprocessed pages.

    The caller owns the authorized transport/ledger. An incomplete directory is
    deliberately not retried: an interrupted call may already have been charged.
    """
    if graph.tenant_id != tenant_id or sha256(source).hexdigest() != graph.source_sha256:
        raise ValueError("NOTE_BATCH_SOURCE_MISMATCH")
    if any(issue.kind == "table_note_review" for issue in graph.issues):
        raise ValueError("NOTE_BATCH_REQUIRES_BASE_GRAPH")
    pages = {}
    for block in graph.blocks:
        if block.kind == "table":
            pages.setdefault(block.page_num, []).append(block.source_id)
    if selected_pages is not None:
        if (
            not selected_pages
            or any(type(p) is not int or p < 1 for p in selected_pages)
            or len(set(selected_pages)) != len(selected_pages)
            or not set(pages) <= set(selected_pages)
        ):
            raise ValueError("NOTE_BATCH_PAGE_SELECTION_INVALID")
        for page in selected_pages:
            pages.setdefault(page, [])
    plan = dict(
        schema="table_note_batch_v1",
        tenant_id=tenant_id,
        graph_sha256=canonical_hash(asdict(graph)),
        model=client.model,
        helper_sha256=sha256(
            files(__package__).joinpath("note_extraction.py").read_bytes()
        ).hexdigest(),
        batch_sha256=sha256(
            files(__package__).joinpath("note_review_batch.py").read_bytes()
        ).hexdigest(),
        pages=[dict(page=page, table_ids=sorted(ids)) for page, ids in sorted(pages.items())],
    )
    output = Path(output)
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        try:
            body = json.loads((output / "review.json").read_bytes())
        except (OSError, ValueError):
            raise ValueError("NOTE_BATCH_INCOMPLETE") from None
    else:
        note_extraction._save_immutable_json(output / "plan.json", plan)
        artifacts, stop = [], None
        for item in plan["pages"]:
            packet = prepare(
                graph, source, item["table_ids"], tenant_id=tenant_id, page=item["page"]
            )
            if stop is None:
                result = note_extraction.run(
                    graph,
                    source,
                    item["table_ids"],
                    client,
                    output / str(item["page"]),
                    tenant_id=tenant_id,
                    page=item["page"],
                )
                if result.get("error") in UPSTAGE_TRANSPORT_STOP_CODES:
                    stop = result["error"]
            else:
                result = validate({"notes": []}, packet, graph, source, tenant_id=tenant_id)
                result.update(status="not_run", error=stop, requests=[])
            artifacts.append(freeze_note_review(graph, source, packet, result, tenant_id=tenant_id))
        body = dict(plan=plan, artifacts=artifacts)
        body["sha256"] = canonical_hash(body)
        note_extraction._save_immutable_json(output / "review.json", body)
    if (
        not isinstance(body, dict)
        or set(body) != {"plan", "artifacts", "sha256"}
        or body["plan"] != plan
        or not isinstance(body["artifacts"], list)
        or len(body["artifacts"]) != len(plan["pages"])
        or body["sha256"] != canonical_hash({k: v for k, v in body.items() if k != "sha256"})
    ):
        raise ValueError("NOTE_BATCH_REPLAY_MISMATCH")
    artifacts = tuple(body["artifacts"])
    if artifacts:
        replay_note_reviews(artifacts, graph, source, tenant_id=tenant_id)
        for artifact, item in zip(artifacts, plan["pages"], strict=True):
            packet = json.loads(artifact)["packet"]
            if (
                packet["table_ids"] != item["table_ids"]
                or packet["untrusted_document_data"]["page"] != item["page"]
            ):
                raise ValueError("NOTE_BATCH_TABLE_COVERAGE_MISMATCH")
    return artifacts
