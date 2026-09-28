"""Bounded atomic-claim pilot using existing extraction and source-span contracts.

Offline unless --live. Selected source IDs come from the earlier sentence-candidate
artifact; other text remains unknown. No source approval, bindings or grades.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from proofops.adapters.local.upstage import MODEL, UPSTAGE_TRANSPORT_STOP_CODES, UpstageProbe
from proofops.application.claims import ClaimScope, ExtractionProfile, discover_atomic_claims
from proofops.domain.provenance import canonical_hash
from proofops_agent.extraction import StructuredClaimExtractor

from evaluation.prefilter_comparison import save
from evaluation.section_pipeline import validated_map
from evaluation.upstage_live_probe import SYSTEM as BASE_SYSTEM
from evaluation.upstage_live_probe import load_graph, locate_quotes

SYSTEM = BASE_SYSTEM + (
    " Split separate independently asserted goals, results or management actions into "
    "minimal exact clauses when possible. Keep each year, value, unit, Scope, entity and "
    "qualifier attached to its assertion. Do not split a list of measures or Scope 1 and 2 "
    "into invented assertions. The original paragraph remains available as context; never "
    "insert a missing subject. If exact splitting loses essential qualifiers, keep the "
    "compound quote for later review. Isolated headings, chart labels and generic background "
    "are not assertions. No sentences may be rewritten. Extract only from targets; context "
    "is for interpretation and must not create additional claims. Cover company assertions "
    "in targets, excluding introductory phrases such as 'examples are as follows'."
)


def selected_targets(graph, artifact):
    """Rebind serialized candidates to the original snapshot before any model call."""
    blocks = {b.source_id: b for b in graph.blocks}
    selected: dict[str, list[tuple[int, int]]] = {}
    for claim in artifact["claims"]:
        block = blocks.get(claim["source_id"])
        start, end = claim["span"]["char_start"], claim["span"]["char_end"]
        if (
            block is None
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(block.normalized_text)
        ):
            raise ValueError("candidate range/source mismatch")
        ref = block.source_ref(normalized_char_start=start, normalized_char_end=end)
        if (
            claim["span"]["quote"] != ref.quote
            or canonical_hash(claim["source_ref"]) != canonical_hash(asdict(ref))
            or claim["source_quality"] != block.quality
        ):
            raise ValueError("candidate provenance mismatch")
        selected.setdefault(block.source_id, []).append((start, end))
    return {sid: sorted(intervals) for sid, intervals in selected.items()}


class ReplayClient:
    """Exact local request replay; never a new model observation or network fallback."""

    def __init__(self, folder):
        self.responses = {}
        for path in sorted(folder.glob("*/request.json")):
            request = json.loads(path.read_text())
            key = (canonical_hash(request["system_prompt"]), canonical_hash(request["packet"]))
            if key != (request["prompt_sha256"], request["packet_sha256"]) or key in self.responses:
                raise ValueError("invalid or ambiguous replay request")
            response = json.loads(path.with_name("response.json").read_text())
            self.responses[key] = {**response, "replay_of_request_id": request["request_id"]}
        if not self.responses:
            raise ValueError("no archived requests")

    def complete(self, system, user_json, **kwargs):
        key = (canonical_hash(system), canonical_hash(json.loads(user_json)))
        if key not in self.responses:
            raise ValueError("no exact archived request; replay cannot call a model")
        return self.responses[key]


def extract_selected(
    graph, mapped, selected_source_ids, client, output, *, tenant_id, include_context=True
):
    mapped_scope = validated_map(graph, mapped, tenant_id)
    selected = frozenset(selected_source_ids)
    blocks = {b.source_id: b for b in graph.blocks}
    if (
        not selected
        or not selected <= blocks.keys()
        or any(
            blocks[sid].page_num not in mapped_scope["claim_candidate_pages"] for sid in selected
        )
    ):
        raise ValueError("selected sources must belong to this graph's E candidate scope")
    ranges = {
        sid: selected_source_ids[sid]
        if isinstance(selected_source_ids, dict)
        else [(0, len(blocks[sid].normalized_text))]
        for sid in sorted(selected)
    }
    for sid, intervals in ranges.items():
        previous_end = 0
        if not intervals:
            raise ValueError("empty target ranges")
        for start, end in intervals:
            if (
                type(start) is not int
                or type(end) is not int
                or not previous_end <= start < end <= len(blocks[sid].normalized_text)
            ):
                raise ValueError("invalid or overlapping target ranges")
            previous_end = end
    output.mkdir(parents=True, exist_ok=False)
    rule = dict(version=2, targets=ranges, map=mapped["map_sha256"])
    if not include_context:
        rule.update(version=3, context_mode="target_only")
    profile = ExtractionProfile(
        canonical_hash(dict(provider="upstage", configured_model=MODEL)),
        canonical_hash(SYSTEM),
        canonical_hash(rule),
        any(batch.synthetic for batch in graph.candidates),
    )
    records = []
    HARDSTOP = UPSTAGE_TRANSPORT_STOP_CODES
    stop_error: str | None = None

    def unknown(text, reason):
        return dict(
            spans=[
                dict(
                    char_start=0,
                    char_end=len(text),
                    quote=text,
                    kind="unknown",
                    reason=reason,
                    topic_ids=[],
                )
            ]
        )

    def respond(packet):
        nonlocal stop_error
        data = packet["untrusted_document_data"]
        if data["source_id"] not in selected:
            return unknown(data["text"], "not_selected_for_atomic_pilot")
        intervals = ranges[data["source_id"]]
        wire_packet = {
            **packet,
            "untrusted_document_data": {
                **{key: value for key, value in data.items() if key != "text"},
                "context": data["text"] if include_context else "",
                "targets": [
                    dict(char_start=start, char_end=end, quote=data["text"][start:end])
                    for start, end in intervals
                ],
            },
        }
        request_id = str(uuid4())
        folder = output / request_id
        folder.mkdir()
        record = dict(
            source_id=data["source_id"],
            request_id=request_id,
            packet_sha256=canonical_hash(wire_packet),
            application_packet_sha256=canonical_hash(packet),
            status="failed",
        )
        # Shared fail-closed: no extra paid call after first hardstop, through same respond.
        if stop_error is not None:
            payload = unknown(data["text"], "atomic_response_invalid")
            record["error"] = f"deferred_after_{stop_error}"
        else:
            save(
                folder / "request.json",
                dict(
                    packet=wire_packet,
                    **record,
                    system_prompt=SYSTEM,
                    prompt_sha256=profile.prompt_sha256,
                    section_map_sha256=mapped["map_sha256"],
                    model_sha256=profile.model_sha256,
                ),
            )
            try:
                response = client.complete(
                    SYSTEM,
                    json.dumps(wire_packet, ensure_ascii=False),
                    request_id=request_id,
                    max_tokens=4096,
                    json_mode=True,
                )
                save(folder / "response.json", response)
                if "replay_of_request_id" in response:
                    record["replay_of_request_id"] = response["replay_of_request_id"]
                record["provider_model_sha256"] = canonical_hash(response["provider_model"])
                spans = locate_quotes(json.loads(response["content"]), data["text"])
                if any(
                    not any(
                        start <= span.char_start < span.char_end <= end for start, end in intervals
                    )
                    for span in spans
                ):
                    raise ValueError("claim outside selected target ranges")
                payload = dict(spans=[asdict(span) for span in spans])
                # The application wire schema uses arrays, not the internal tuple representation.
                for span in payload["spans"]:
                    span["topic_ids"] = list(span["topic_ids"])
                record.update(status="passed", spans=len(spans))
            except ValueError as exc:
                msg = str(exc)
                if msg in HARDSTOP:
                    stop_error = msg
                    record["error"] = msg
                    payload = unknown(data["text"], "atomic_response_invalid")
                else:
                    payload = unknown(data["text"], "atomic_response_invalid")
                    # Use stable code, not arbitrary exception string leaking upstream data.
                    record["error"] = "atomic_response_invalid"
            except (TimeoutError, ConnectionError):
                stop_error = "UPSTAGE_REQUEST_FAILED"
                record["error"] = "UPSTAGE_REQUEST_FAILED"
                payload = unknown(data["text"], "atomic_response_invalid")
        targets = []
        for start, end in intervals:
            returned = [
                s
                for s in payload["spans"]
                if s["kind"] == "claim" and start <= s["char_start"] < s["char_end"] <= end
            ]
            gaps = []
            cursor = start
            for span in returned + [dict(char_start=end, char_end=end)]:
                stop = span["char_start"]
                if data["text"][cursor:stop].strip():
                    gaps.append(
                        dict(
                            char_start=cursor,
                            char_end=stop,
                            quote=data["text"][cursor:stop],
                            state="unknown",
                        )
                    )
                cursor = span["char_end"]
            targets.append(
                dict(
                    char_start=start,
                    char_end=end,
                    quote=data["text"][start:end],
                    coverage="not_returned"
                    if not returned
                    else "partial"
                    if gaps
                    else "full_text_returned",
                    returned_spans=returned,
                    unreturned_spans=gaps,
                )
            )
        record["targets"] = targets
        save(folder / "validation.json", dict(**record, normalized_response=payload))
        records.append(record)
        return payload

    discovery = discover_atomic_claims(
        graph,
        ClaimScope(
            tenant_id,
            graph.document_version_id,
            graph.parse_manifest_id,
            "declared_subset",
            tuple(sorted({blocks[s].page_num for s in selected})),
        ),
        extractor=StructuredClaimExtractor(profile, respond),
    )
    save(output / "discovery.json", asdict(discovery))
    attempted = sum(1 for r in records if not str(r.get("error", "")).startswith("deferred_after_"))
    deferred = sum(1 for r in records if str(r.get("error", "")).startswith("deferred_after_"))
    save(
        output / "summary.json",
        dict(
            selected_sources=sorted(selected),
            requests=records,
            claims=len(discovery.claims),
            atomicity="model_proposed_not_independently_reviewed",
            coverage_note="Text coverage is not semantic completeness or atomicity",
            context_mode="paragraph" if include_context else "target_only",
            execution_mode="archived_response_replay"
            if isinstance(client, ReplayClient)
            else "live_or_test",
            decision=None,
            local_deferral_receipts="processed means handled locally, not a model call",
            section_map_sha256=mapped["map_sha256"],
            stop_error=stop_error,
            attempted_count=attempted,
            deferred_count=deferred,
        ),
    )
    return discovery, records


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("pdf", "manifest", "section-map", "candidates", "output"):
        cli.add_argument("--" + name, type=Path, required=True)
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    cli.add_argument(
        "--target-only",
        action="store_true",
        help="Bounded ablation: omit paragraph context; do not infer missing subjects",
    )
    args = cli.parse_args()
    graph = load_graph(args.pdf, args.manifest)
    mapped = json.loads(args.section_map.read_text())
    selected = selected_targets(graph, json.loads(args.candidates.read_text()))
    if args.replay:
        discovery, records = extract_selected(
            graph,
            mapped,
            selected,
            ReplayClient(args.replay),
            args.output,
            tenant_id=graph.tenant_id,
            include_context=not args.target_only,
        )
        print(
            json.dumps(
                dict(
                    claims=len(discovery.claims),
                    live="not_run",
                    failed=sum(r["status"] != "passed" for r in records),
                )
            )
        )
    elif not args.live:
        validated_map(graph, mapped, graph.tenant_id)
        print(json.dumps(dict(selected_sources=selected, live="not_run")))
    else:
        root = Path(__file__).resolve().parents[1]
        key = next(
            line.split("=", 1)[1].strip().strip('"').strip("'")
            for line in (root / ".env.upstage.local").read_text().splitlines()
            if line.startswith("UPSTAGE_API_KEY=")
        )
        client = UpstageProbe(key, root / ".local/upstage/budget.sqlite3")
        before = client.summary()
        discovery, records = extract_selected(
            graph,
            mapped,
            selected,
            client,
            args.output,
            tenant_id=graph.tenant_id,
            include_context=not args.target_only,
        )
        budget = dict(before=before, after=client.summary())
        save(args.output / "budget.json", budget)
        print(
            json.dumps(
                dict(
                    claims=len(discovery.claims),
                    requests=len(records),
                    failed=sum(r["status"] != "passed" for r in records),
                    budget=budget,
                )
            )
        )
