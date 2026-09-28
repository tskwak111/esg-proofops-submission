"""Bounded, opt-in cross-report development probes; no source approval or grading.

Consumes existing selection.json + source-verified local parse manifests. Every run
writes new receipts, never overwrites or retries a paid call. Shared cumulative
budget ledger; extensions require explicit authorization.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from proofops.adapters.local.upstage import UPSTAGE_TRANSPORT_STOP_CODES, UpstageProbe
from proofops.adapters.local.upstage_parse import UpstageParseProbe
from proofops.application.claims import ClaimScope
from proofops_agent.upstage_extraction import UpstageClaimExtractor
from proofops_worker.extract_runner import select_stable_paragraph_sources

from evaluation.upstage_live_probe import load_graph

ROOT = Path(__file__).resolve().parents[1]


def save(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def run(folder: Path, action: str, *, invoke=False, max_calls=20, key_file: Path | None = None):
    if (
        type(max_calls) is not int
        or not 1 <= max_calls <= 20
        or action not in {"extract", "standard", "enhanced"}
    ):
        raise ValueError("invalid bounded probe")
    selection = json.loads((folder / "selection.json").read_text())
    pdf = Path(selection["source_path"])
    if sha256(pdf.read_bytes()).hexdigest() != selection["source_sha256"]:
        raise ValueError("original source changed")
    graph = load_graph(pdf, Path(selection["manifest"]))
    scope = ClaimScope(
        graph.tenant_id,
        graph.document_version_id,
        graph.parse_manifest_id,
        "declared_subset",
        (selection["pages"][0],),
    )
    selected = select_stable_paragraph_sources(graph, scope, max_calls)
    blocks = sorted(
        (b for b in graph.blocks if b.source_id in selected),
        key=lambda b: (b.page_num, tuple(b.bbox or ()), b.normalized_text),
    )
    sample = (folder / "sample.pdf").read_bytes()
    if sha256(sample).hexdigest() != selection["subset_sha256"]:
        raise ValueError("sample source changed")
    plan = dict(
        action=action,
        source_sha256=graph.source_sha256,
        subset_sha256=selection["subset_sha256"],
        pages=selection["pages"],
        selected_sources=[b.source_id for b in blocks],
        max_calls=max_calls,
        source_quality="unverified",
        benchmark="development_only",
    )
    if not invoke:
        return plan
    ledger = ROOT / ".local/upstage/budget.sqlite3"
    if not ledger.is_file():
        raise ValueError("shared ledger required")
    key = next(
        line.split("=", 1)[1].strip().strip('"').strip("'")
        for line in (key_file or ROOT / ".env.upstage.local").read_text().splitlines()
        if line.startswith("UPSTAGE_API_KEY=")
    )
    out = folder / f"{action}-{uuid4()}"
    out.mkdir(mode=0o700)
    save(out / "plan.json", plan)
    start = time.monotonic()
    if action != "extract":
        probe = UpstageParseProbe(key, ledger)
        request_id = str(uuid4())
        save(out / "request.json", dict(request_id=request_id, **plan))
        try:
            result = probe.parse(sample, request_id=request_id, mode=action)
            save(out / "response.json", result)
            summary = dict(
                status="parsed", request_id=request_id, cost_usd=result["cost_with_vat_reserve_usd"]
            )
        except ValueError as error:
            summary = dict(status="failed", request_id=request_id, error=str(error))
    else:
        extractor = UpstageClaimExtractor(UpstageProbe(key, ledger), out / "receipts")
        outcomes = []
        for block in blocks:
            packet = dict(
                tenant_id=graph.tenant_id,
                document_version_id=graph.document_version_id,
                parse_manifest_id=graph.parse_manifest_id,
                source_sha256=graph.source_sha256,
                extraction_profile=asdict(extractor.profile),
                untrusted_document_data=dict(
                    source_id=block.source_id,
                    page_num=block.page_num,
                    kind=block.kind,
                    text=block.normalized_text,
                ),
            )
            before = time.monotonic()
            try:
                response = extractor.extract(packet)
                outcome = dict(status="processed", response=response)
            except (TimeoutError, ConnectionError):
                outcome = dict(status="unknown", error="UPSTAGE_REQUEST_FAILED")
            except ValueError as error:
                outcome = dict(status="unknown", error=str(error))
            outcomes.append(
                dict(
                    source_id=block.source_id,
                    original_page=block.page_num,
                    text_sha256=sha256(block.normalized_text.encode()).hexdigest(),
                    seconds=time.monotonic() - before,
                    **outcome,
                )
            )
            save(out / f"outcome-{len(outcomes):03}.json", outcomes[-1])
            if outcome.get("error") in UPSTAGE_TRANSPORT_STOP_CODES:
                break
        stop_error = next(
            (o["error"] for o in outcomes if o.get("error") in UPSTAGE_TRANSPORT_STOP_CODES),
            None,
        )
        summary = dict(
            status="stopped" if stop_error else "evaluated",
            selected_count=len(blocks),
            attempted_count=len(outcomes),
            processed_count=sum(o["status"] == "processed" for o in outcomes),
            unprocessed_count=len(blocks) - len(outcomes),
            unknown_count=len(blocks) - sum(o["status"] == "processed" for o in outcomes),
            outcomes=outcomes,
            usage=extractor.usage,
        )
        if stop_error:
            summary["error"] = stop_error
    summary.update(seconds=time.monotonic() - start, output=str(out), source_quality="unverified")
    save(out / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--action", choices=["extract", "standard", "enhanced"], required=True)
    parser.add_argument("--invoke", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--max-calls", type=int, default=20)
    args = parser.parse_args()
    result = run(
        args.folder,
        args.action,
        invoke=args.invoke,
        max_calls=args.max_calls,
        key_file=args.key_file,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "outcomes"}, ensure_ascii=False))
