"""Explicit local preliminary diagnostics, never production tagging or grading.

Run with --state EXISTING_PILOT --output NEW_DIRECTORY; no call without --invoke.
All paid calls use the already-authorized shared ledger; interrupted operations
cannot resume or silently retry. Three independent replies measure consistency,
not semantic correctness or gold accuracy.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from proofops.application.tagging.preliminary import (
    SYSTEM_PROMPT,
    preliminary_request,
    validate_preliminary,
)
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import _require_uuid
from proofops_agent.upstage_extraction import UpstageClaimExtractor


def evaluate_preliminary(
    probe, graph, claims, *, tenant_id, output: Path, max_claims: int, authorization: str
):
    """Caller-authorized evaluation only; authorization text grants no service rights."""
    _require_uuid("tenant_id", tenant_id)
    if (
        type(max_claims) is not int
        or not 1 <= max_claims <= 20
        or not isinstance(authorization, str)
        or not authorization.strip()
        or probe.model not in ("solar-pro3", "solar-pro4")
        or graph.tenant_id != tenant_id
    ):
        raise ValueError("PRELIMINARY_EVALUATION_CONFIG_INVALID")
    claims = tuple(claims)
    if len({c.claim_id for c in claims}) != len(claims) or any(
        (c.tenant_id, c.document_version_id, c.parse_manifest_id, c.source_sha256)
        != (tenant_id, graph.document_version_id, graph.parse_manifest_id, graph.source_sha256)
        for c in claims
    ):
        raise ValueError("PRELIMINARY_EVALUATION_SOURCE_MISMATCH")
    verified = [c for c in claims if c.source_quality == "verified"]
    selected = verified[:max_claims]
    # Validate the entire bounded selection before any external transmission.
    packets = [preliminary_request(c, graph, tenant_id=tenant_id) for c in selected]
    output.mkdir(mode=0o700)  # exclusive ownership fences concurrent/restarted operations
    write = UpstageClaimExtractor._write
    manifest = dict(
        schema="preliminary-evaluation-v1",
        authorization=authorization,
        tenant_id=tenant_id,
        source_sha256=graph.source_sha256,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        graph_sha256=canonical_hash(asdict(graph)),
        model=probe.model,
        prompt_sha256=canonical_hash(SYSTEM_PROMPT),
        model_sha256=canonical_hash(
            dict(model=probe.model, provider="upstage", transport="UpstageProbe")
        ),
        selected_claim_ids=[c.claim_id for c in selected],
        max_claims=max_claims,
        max_calls=len(selected) * 3,
        max_output_tokens=1024,
        replicas=[1, 2, 3],
        published=False,
        gold_accuracy=None,
    )
    write(output / "manifest.json", canonical_json(manifest))
    summary = dict(
        manifest_sha256=canonical_hash(manifest),
        published=False,
        gold_accuracy=None,
        total_claims=len(claims),
        unverified_claims=len(claims) - len(verified),
        deferred_verified_claims=max(0, len(verified) - len(selected)),
        stopped=False,
        claims=[],
        request_ids=[],
    )
    for claim, packet in zip(selected, packets, strict=True):
        records, signatures, provider_ids = [], [], []
        for replica in (1, 2, 3):
            request_id = str(uuid4())
            directory = output / request_id
            directory.mkdir(mode=0o700)
            request = dict(
                request_id=request_id,
                replicate_id=replica,
                packet=packet,
                manifest_sha256=canonical_hash(manifest),
                rule_sha256=claim.receipt.profile.rule_sha256,
                system=SYSTEM_PROMPT,
                max_tokens=1024,
            )
            write(directory / "request.json", canonical_json(request))
            summary["request_ids"].append(request_id)
            record: dict[str, Any] = dict(
                request_id=request_id, replicate_id=replica, status="rejected"
            )
            try:
                response = probe.complete(
                    SYSTEM_PROMPT,
                    json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    request_id=request_id,
                    max_tokens=1024,
                    json_mode=True,
                )
            except Exception:
                # Unknown usage remains reserved by UpstageProbe; no automatic retries.
                record.update(status="transport_failed", reason="UPSTREAM_UNAVAILABLE")
                summary["stopped"] = True
            else:
                write(directory / "response.json", canonical_json(response))
                try:
                    result = validate_preliminary(
                        claim, graph, json.loads(response["content"]), tenant_id=tenant_id
                    )
                    provider_id = response["provider_request_id"]
                    if not isinstance(provider_id, str) or not provider_id.strip():
                        raise ValueError("provider identity required")
                except (ValueError, KeyError, TypeError):
                    record["reason"] = "PRELIMINARY_RESPONSE_INVALID"
                else:
                    values = dict(
                        track=asdict(result.track) if result.track else None,
                        safe_harbor_category=result.safe_harbor_category,
                        dimensions={
                            k: asdict(v) if v else None
                            for k, v in result.context.dimensions.items()
                        },
                    )
                    record.update(
                        status="validated_candidate",
                        values=values,
                        track_confidence=result.track_confidence,
                    )
                    signatures.append(canonical_hash(values))
                    provider_ids.append(provider_id)
            write(directory / "validation.json", canonical_json(record))
            records.append(record)
            if summary["stopped"]:
                break
        consistent = (
            len(signatures) == 3
            and len(set(signatures)) == 1
            and len(set(provider_ids)) == 3
            and all(r.get("values", {}).get("track") is not None for r in records)
        )
        summary["claims"].append(
            dict(
                claim_id=claim.claim_id,
                replicas_valid=len(signatures),
                status="consistent_candidate" if consistent else "needs_review",
                records=records,
            )
        )
        if summary["stopped"]:
            break
    write(output / "summary.json", canonical_json(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=("solar-pro3", "solar-pro4"), default="solar-pro4")
    parser.add_argument("--max-claims", type=int, default=2)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--invoke", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_claims <= 20:
        parser.error("--max-claims must be 1..20")
    if not args.invoke:
        print(
            json.dumps(
                dict(
                    invoke=False,
                    maximum_calls=args.max_claims * 3,
                    production_ready=False,
                    output=str(args.output),
                )
            )
        )
        return
    if args.key_file is None:
        parser.error("--key-file is required with --invoke")
    if args.output.exists():
        parser.error("output already exists; no automatic retry or overwrite")
    key = next(
        (
            line.split("=", 1)[1].strip().strip('"').strip("'")
            for line in args.key_file.read_text().splitlines()
            if line.startswith("UPSTAGE_API_KEY=")
        ),
        None,
    )
    if not key:
        parser.error("UPSTAGE_API_KEY is missing")
    state = args.state.resolve()
    pilot = json.loads((state / "pilot.json").read_text())
    if sha256(Path(pilot["source_path"]).read_bytes()).hexdigest() != pilot["source_sha256"]:
        raise ValueError("PILOT_SOURCE_HASH_MISMATCH")
    # Explicit evaluation composition; never activate paid worker modes from ambient env.
    os.environ.update(
        APP_ENV="local",
        MODEL_ADAPTER="synthetic",
        LOCAL_EXTRACTION_MODE="",
        LOCAL_TAGGING_MODE="",
        LOCAL_RUN_SETTINGS_PATH=str(state / "settings.json"),
        LOCAL_DATABASE_PATH=str(state / "state.sqlite3"),
        LOCAL_PARSER_PROFILE_PATH=str(state / "parser.json"),
    )
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.upstage import UpstageProbe, request_usage
    from proofops_worker.composition import build_composition

    runner = build_composition()
    _, discovery, graph = LocalClaimStore(
        runner.store, runner.uploads, runner.parser
    ).load_evidence(pilot["tenant_id"], pilot["run_id"])
    if (graph.source_sha256, graph.document_version_id) != (
        pilot["source_sha256"],
        pilot["document_version_id"],
    ):
        raise ValueError("PILOT_GRAPH_MISMATCH")
    ledger = Path(__file__).resolve().parents[1] / ".local/upstage/budget.sqlite3"
    if not ledger.is_file():
        raise ValueError("EXISTING_SHARED_LEDGER_REQUIRED")
    probe = UpstageProbe(key, ledger, model=args.model)
    result = evaluate_preliminary(
        probe,
        graph,
        discovery.claims,
        tenant_id=pilot["tenant_id"],
        output=args.output,
        max_claims=args.max_claims,
        authorization=(
            "explicit --invoke local evaluation; existing user cumulative USD20 authorization"
        ),
    )
    usage = request_usage(ledger, result["request_ids"])
    UpstageClaimExtractor._write(args.output / "usage.json", canonical_json(usage))
    print(
        json.dumps(
            dict(
                output=str(args.output),
                usage=usage,
                stopped=result["stopped"],
                statuses=[r["status"] for r in result["claims"]],
            )
        )
    )


if __name__ == "__main__":
    main()
