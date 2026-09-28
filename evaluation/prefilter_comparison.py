"""Compare all text blocks with local preselection on an explicit E-page subset.

Default is offline planning. --live uses the existing cumulative USD 10 ledger.
No graph/table vision, tagging, grades, production API changes or gold claims.
"""

import argparse
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from time import monotonic
from uuid import uuid4

from proofops.adapters.local.upstage import UPSTAGE_TRANSPORT_STOP_CODES, UpstageProbe
from proofops.domain.provenance import canonical_hash

from evaluation.claim_prefilter import POLICY_HASH, SYSTEM, prepare, validate
from evaluation.upstage_live_probe import load_graph


def save(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def execute(plan, graph, client, output, *, stop_error=None):
    HARDSTOP = UPSTAGE_TRANSPORT_STOP_CODES
    if stop_error is not None and stop_error not in HARDSTOP:
        raise ValueError("invalid stop code")
    records, claims = [], []
    for packet in plan["packets"]:
        wire_packet = deepcopy(packet)
        sentence_ids = {}
        for block in wire_packet["untrusted_document_data"]["targets"]:
            for sentence in block["sentences"]:
                alias = f"s{len(sentence_ids)}"
                sentence_ids[alias] = sentence["sentence_id"]
                sentence["sentence_id"] = alias
        request_id = str(uuid4())
        folder = output / request_id
        folder.mkdir(mode=0o700, parents=True)
        record = dict(request_id=request_id, status="failed", claims=[])
        started = monotonic()
        if stop_error is not None:
            # Fail-closed: no further paid calls within execute after first hardstop.
            record["error"] = f"deferred_after_{stop_error}"
        else:
            save(
                folder / "request.json",
                dict(
                    packet=wire_packet,
                    packet_sha256=canonical_hash(wire_packet),
                    original_packet_sha256=canonical_hash(packet),
                    sentence_id_map=sentence_ids,
                    wire_version=2,
                    system_prompt=SYSTEM,
                    prompt_sha256=canonical_hash(SYSTEM),
                    policy_sha256=POLICY_HASH,
                    section_map_sha256=plan.get("section_map_sha256"),
                    replicate_id=1,
                    task="claim_extraction_comparison",
                    response_format={"type": "json_object"},
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
                record.update(
                    input_tokens=response["input_tokens"],
                    output_tokens=response["output_tokens"],
                    cost_with_vat_reserve_usd=response["cost_with_vat_reserve_usd"],
                    model_sha256=canonical_hash(response["provider_model"]),
                )
                payload = json.loads(response["content"])
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"sentence_ids"}
                    or not isinstance(payload["sentence_ids"], list)
                    or any(
                        not isinstance(sid, str) or sid not in sentence_ids
                        for sid in payload["sentence_ids"]
                    )
                ):
                    raise ValueError("invalid packet-local sentence IDs")
                found = validate(
                    {"sentence_ids": [sentence_ids[sid] for sid in payload["sentence_ids"]]},
                    packet,
                    graph,
                )
                record.update(status="passed", claims=found)
                claims.extend(found)
            except ValueError as exc:
                msg = str(exc)
                if msg in HARDSTOP:
                    stop_error = msg
                    record["error"] = msg
                else:
                    record["error"] = "REQUEST_OR_SOURCE_VALIDATION_FAILED"
            except (TimeoutError, ConnectionError):
                stop_error = "UPSTAGE_REQUEST_FAILED"
                record["error"] = "UPSTAGE_REQUEST_FAILED"
        record["duration_seconds"] = round(monotonic() - started, 3)
        save(folder / "validation.json", record)
        records.append(record)
        print(
            json.dumps({k: record[k] for k in ("request_id", "status", "duration_seconds")}),
            flush=True,
        )
    attempted = sum(1 for r in records if not str(r.get("error", "")).startswith("deferred_after_"))
    deferred = sum(1 for r in records if str(r.get("error", "")).startswith("deferred_after_"))
    return dict(
        status="passed" if records and all(r["status"] == "passed" for r in records) else "partial",
        claims=claims,
        packets=len(records),
        failed_packets=sum(r["status"] != "passed" for r in records),
        input_tokens=sum(r.get("input_tokens", 0) for r in records),
        output_tokens=sum(r.get("output_tokens", 0) for r in records),
        usage_complete=all("input_tokens" in r for r in records),
        settled_cost_usd=str(
            sum((Decimal(r.get("cost_with_vat_reserve_usd", "0")) for r in records), Decimal(0))
        ),
        duration_seconds=sum(r["duration_seconds"] for r in records),
        records=records,
        stop_error=stop_error,
        attempted_count=attempted,
        deferred_count=deferred,
    )


def compare(baseline, filtered, plan):
    def key(c):
        return c["source_id"], c["span"]["char_start"], c["span"]["char_end"]

    left, right = {key(c): c for c in baseline["claims"]}, {key(c): c for c in filtered["claims"]}
    sent = {
        b["source_id"] for p in plan["packets"] for b in p["untrusted_document_data"]["targets"]
    }
    return dict(
        status="passed" if baseline["status"] == filtered["status"] == "passed" else "partial",
        recall=None,
        gold_status="not_human_labeled",
        exact_span_matches=len(left.keys() & right.keys()),
        baseline_only_exact_spans=[c for k, c in left.items() if k not in right],
        filtered_only_exact_spans=[c for k, c in right.items() if k not in left],
        baseline_claims_in_deferred_sources=[
            c for c in baseline["claims"] if c["source_id"] not in sent
        ],
        decision=None,
    )


def run(args):
    graph = load_graph(args.pdf, args.manifest)
    root = Path(__file__).resolve().parents[1]
    output = root / ".local/prefilter" / str(uuid4())
    output.mkdir(mode=0o700, parents=True)
    plans = {
        mode: prepare(graph, tenant_id=graph.tenant_id, pages=tuple(args.pages), mode=mode)
        for mode in ("all_text", "filtered")
    }
    for mode, plan in plans.items():
        save(output / (mode + "-plan.json"), plan)
    summary = dict(
        synthetic=any(b.synthetic for b in graph.candidates),
        scope="E text subset; tables/figures unresolved",
        modes={
            mode: dict(
                packets=len(p["packets"]),
                eligible_blocks=p["eligible_blocks"],
                target_blocks=sum(
                    len(x["untrusted_document_data"]["targets"]) for x in p["packets"]
                ),
                deferred_blocks=len(p["deferred"]),
                packet_bytes=sum(len(json.dumps(x).encode()) for x in p["packets"]),
            )
            for mode, p in plans.items()
        },
        live="not_run",
    )
    if args.live:
        key = next(
            line.split("=", 1)[1].strip().strip('"').strip("'")
            for line in (root / ".env.upstage.local").read_text().splitlines()
            if line.startswith("UPSTAGE_API_KEY=")
        )
        client = UpstageProbe(key, root / ".local/upstage/budget.sqlite3")
        summary["budget_before"] = client.summary()
        results = {}
        stop_error = None
        for mode in ("all_text", "filtered"):
            plan = plans[mode]
            result = execute(plan, graph, client, output / mode, stop_error=stop_error)
            stop_error = result.get("stop_error")
            results[mode] = result
        for mode, result in results.items():
            save(output / (mode + "-results.json"), result)
            summary["modes"][mode].update(
                {k: v for k, v in result.items() if k not in ("claims", "records")},
                claim_candidates=len(result["claims"]),
            )
        differences = compare(results["all_text"], results["filtered"], plans["filtered"])
        save(output / "differences.json", differences)
        summary.update(
            live=differences["status"],
            budget_after=client.summary(),
            deferred_source_claims=len(differences["baseline_claims_in_deferred_sources"]),
            exact_span_matches=differences["exact_span_matches"],
            recall=None,
        )
    save(output / "summary.json", summary)
    print(json.dumps(dict(artifact=str(output), **summary), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--pdf", type=Path, required=True)
    cli.add_argument("--manifest", type=Path, required=True)
    cli.add_argument("--pages", type=int, nargs="+", required=True)
    cli.add_argument("--live", action="store_true")
    run(cli.parse_args())
