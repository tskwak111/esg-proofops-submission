"""Opt-in local section scope → claim batches and bounded evidence search.

Run with --pdf PDF --manifest MANIFEST --section-map MAP --output NEW_JSON.
Maps remain candidates, not approved scope or verified evidence. No network calls.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from proofops.adapters.local.evidence_search import LocalEvidenceSearch
from proofops.adapters.local.evidence_search import search_terms as search_terms
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.provenance import canonical_hash

from evaluation.claim_prefilter import prepare
from evaluation.report_sections import POLICY_HASH, build_map
from evaluation.upstage_live_probe import load_graph


def validated_map(graph, mapped, tenant_id):
    _validate_graph(graph, tenant_id)
    content = {k: v for k, v in mapped.items() if k != "map_sha256"}
    if (
        canonical_hash(content) != mapped.get("map_sha256")
        or mapped.get("source_sha256") != graph.source_sha256
        or mapped.get("policy_sha256") != POLICY_HASH
        or mapped.get("status") != "candidate_only"
    ):
        raise ValueError("section map identity/policy mismatch; inspect this source again")
    expected = build_map(
        mapped["page_count"], [a for section in mapped["sections"] for a in section["anchors"]]
    )
    if any(mapped.get(key) != value for key, value in expected.items()):
        raise ValueError("section map ranges do not match their anchors")
    if any(b.page_num > mapped["page_count"] for b in graph.blocks):
        raise ValueError("graph page outside document")
    return expected


def plan_for_graph(graph, mapped, *, tenant_id):
    scope = validated_map(graph, mapped, tenant_id)
    pages = tuple(scope["claim_candidate_pages"])
    if not pages:
        raise ValueError(
            "no E candidate scope; unresolved map must not become full/empty discovery"
        )
    plan = prepare(graph, tenant_id=tenant_id, pages=pages, mode="all_text")
    available = {b.page_num for b in graph.blocks}
    plan.update(
        section_map_sha256=mapped["map_sha256"],
        scope_status="candidate_only",
        scope_unknown_pages=scope["unknown_pages"],
        scope_conflict_pages=scope["conflict_pages"],
        other_candidate_pages=scope["other_candidate_pages"],
        evidence_candidate_pages=scope["evidence_candidate_pages"],
        missing_evidence_pages=sorted(set(scope["evidence_candidate_pages"]) - available),
    )
    return plan


def table_row_contexts(graph, source_id, *, tenant_id):
    """Same-page literal row candidates, never inferred table edges or accepted bindings."""
    _validate_graph(graph, tenant_id)
    blocks = {b.source_id: b for b in graph.blocks}
    source = blocks[source_id]
    rows = {}
    tables = {}
    for block in graph.blocks:
        if block.page_num != source.page_num or block.winner is None:
            continue
        candidate = block.candidates[block.winner]
        if block.kind == "table":
            key = (candidate.source.parser_run_id, candidate.source.source_native_id)
            tables[key] = block
            continue
        if not candidate.table_native_id:
            continue
        key = (candidate.source.parser_run_id, candidate.table_native_id)
        if type(candidate.row_number) is int and type(candidate.column_number) is int:
            rows.setdefault((key, candidate.row_number), []).append(
                (candidate.column_number, block)
            )
    matches = []
    text = " ".join(source.normalized_text.split())
    # ponytail: literal row search proposes context only; semantic/geometry review remains separate.
    for (key, row), cells in sorted(rows.items()):
        if source.kind == "table_cell" and not any(b.source_id == source_id for _, b in cells):
            continue
        cells = sorted(cells, key=lambda item: (item[0], item[1].source_id))
        row_text = " ".join(" ".join(b.normalized_text.split()) for _, b in cells)
        if key not in tables or not text or text not in row_text:
            continue

        def refs(items):
            return [
                dict(
                    source_id=b.source_id,
                    column=col,
                    row_span=b.candidates[b.winner].row_span or 1,
                    column_span=b.candidates[b.winner].column_span or 1,
                    quality=b.quality,
                    source_ref=asdict(b.source_ref()),
                )
                for col, b in items
            ]

        first_number = min(number for table_key, number in rows if table_key == key)
        first_cells = sorted(rows[(key, first_number)], key=lambda x: x[0])
        # Structural clue only: merged leading cells can span multiple header rows.
        header_end = first_number + max(
            b.candidates[b.winner].row_span or 1 for _, b in first_cells
        )
        matches.append(
            dict(
                source_id=source_id,
                table_id=tables[key].source_id,
                row=row,
                parser_run_id=key[0],
                table_native_id=key[1],
                cells=refs(cells),
                first_row_context=refs(first_cells),
                additional_header_context=refs(
                    [
                        item
                        for (table_key, number), prior in sorted(rows.items())
                        if table_key == key and first_number < number < min(row, header_end)
                        for item in sorted(prior, key=lambda x: x[0])
                    ]
                ),
                binding_status="undetermined",
                relation="literal_row_context_candidate",
            )
        )
    return matches


class SectionSearch(LocalEvidenceSearch):
    """Existing EvidenceSearchPort over one immutable same-document graph.

    Lexical hits are candidate routes only; retrieve_evidence still checks sources,
    quality, citation and binding. Unknown pages remain explicit in coverage.
    """

    def __init__(self, graph, mapped, *, tenant_id):
        self.coverage = validated_map(graph, mapped, tenant_id)
        available = {block.page_num for block in graph.blocks}
        declared = frozenset(self.coverage["evidence_candidate_pages"])
        self.missing_pages = tuple(sorted(declared - available))
        super().__init__(
            graph,
            tenant_id=tenant_id,
            pages=tuple(sorted(declared & available)),
            index_generation=canonical_hash(
                dict(map=mapped["map_sha256"], search="korean-bigram-bm25-v2")
            ),
        )

    def search(self, scope, query, *, vector=None):
        # Preserve this evaluation API's existing sanitized scope error.
        if scope != self.scope:
            raise ValueError("section search scope mismatch")
        return super().search(scope, query, vector=vector)

    def search_with_parent(self, scope, claim, parent):
        """Two bounded review routes; parent text never becomes a claim dimension."""
        from copy import deepcopy

        from evaluation.atomic_pilot import selected_targets

        if scope != self.scope:
            raise ValueError("section search scope mismatch")
        selected_targets(self.graph, dict(claims=[claim, parent]))
        a, b = claim["span"], parent["span"]
        if claim["source_id"] != parent["source_id"] or not (
            b["char_start"] <= a["char_start"] < a["char_end"] <= b["char_end"]
        ):
            raise ValueError("claim must be contained in its source-exact parent")
        return dict(
            claim=deepcopy(claim),
            parent=deepcopy(parent),
            status="candidate_only",
            binding_status="undetermined",
            routes=[
                dict(
                    role=role,
                    query=c["span"]["quote"],
                    **asdict(self.search(scope, c["span"]["quote"])),
                )
                for role, c in (("claim_only", claim), ("parent_context_only", parent))
            ],
        )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--pdf", type=Path, required=True)
    cli.add_argument("--manifest", type=Path, required=True)
    cli.add_argument("--section-map", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--query", default="")
    args = cli.parse_args()
    graph = load_graph(args.pdf, args.manifest)
    mapped = json.loads(args.section_map.read_text())
    plan = plan_for_graph(graph, mapped, tenant_id=graph.tenant_id)
    if args.query:
        search = SectionSearch(graph, mapped, tenant_id=graph.tenant_id)
        plan["search_preview"] = dict(
            query=args.query, **asdict(search.search(search.scope, args.query))
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            dict(
                packets=len(plan["packets"]),
                deferred=len(plan["deferred"]),
                missing_claim_pages=plan["missing_pages"],
                missing_evidence_pages=plan["missing_evidence_pages"],
            )
        )
    )
