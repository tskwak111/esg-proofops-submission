"""Create inspectable synthetic originals and trusted test registries.

This does not certify external documents or approve a live policy. The original
handoff packets use placeholder hashes, so they are unsuitable for provenance
tests without constructing real, explicitly synthetic artifact bytes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

EXAMPLES = Path(__file__).resolve().parents[1] / "contracts/reconciliation/examples"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_case(name: str, root: Path) -> dict[str, Any]:
    if name not in {p.stem for p in EXAMPLES.glob("*.json")}:
        raise ValueError("unknown_fixture")
    case = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    packet, policy = copy.deepcopy(case["input"]), copy.deepcopy(case["policy"])
    root.mkdir(parents=True, exist_ok=True)
    artifacts, documents = {}, {}
    identity = packet["identity"]
    for document_id in sorted({s["document_id"] for s in packet["sources"]}):
        text = "SYNTHETIC FIXTURE ONLY\n"
        refs = [s for s in packet["sources"] if s["document_id"] == document_id]
        for ref in refs:
            start = len(text)
            text += ref["quote"]
            ref["locator"] = f"chars:{start}:{len(text)}"
            text += "\n"
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        artifact_path = f"{document_id}.txt"
        destination = root / artifact_path
        with destination.open("xb") as stream:
            stream.write(raw)
        for ref in refs:
            ref["artifact_sha256"] = digest
        artifacts[document_id] = {"path": artifact_path, "format": "text", "sha256": digest}
        financial = document_id == identity["financial_document_version"]
        source_bindings = {}
        for ref in refs:
            sid = ref["source_id"]
            roles = []
            if sid == packet["claim"]["source_id"]:
                roles.append("claim")
            for role in ("sustainability", "financial"):
                if sid == packet[role]["source_id"]:
                    roles.append(f"{role}_fact")
            c3 = packet.get("c3_context") or {}
            for field, role in (
                ("commitment_source_id", "c3_commitment"),
                ("funding_plan_source_id", "c3_funding"),
            ):
                if sid == c3.get(field):
                    roles.append(role)
            c4 = packet.get("c4_context") or {}
            for field, role in (
                ("definition_source_ids", "c4_definition"),
                ("calculation_source_ids", "c4_calculation"),
            ):
                if sid in c4.get(field, []):
                    roles.append(role)
            if sid == packet["explanation"]["source_id"]:
                roles.append("explanation")
            source_bindings[sid] = {
                "locator": ref["locator"],
                "quote": ref["quote"],
                "roles": roles,
            }
        documents[document_id] = {
            "tenant_id": identity["tenant_id"],
            "company_id": identity["company_id"],
            "package_id": identity["package_id"],
            "document_version_id": document_id,
            "document_role": "financial" if financial else "sustainability",
            "artifact_sha256": digest,
            "corp_code": identity["dart_corp_code"],
            "fiscal_year": identity["financial_fiscal_year"],
            "rcept_no": identity["rcept_no"] if financial else None,
            "consolidation": identity["consolidation"],
            "published_at": identity["financial_published_at" if financial else "sr_published_at"],
            "available_on": identity["financial_published_at" if financial else "sr_published_at"],
            "as_of_date": identity["as_of_date"],
            "period_start": identity["financial_period_start" if financial else "period_start"],
            "period_end": identity["financial_period_end" if financial else "period_end"],
            "relevant_items": [packet["item"]],
            "synthetic": True,
            "decision_binding": {
                **{
                    key: copy.deepcopy(packet[key])
                    for key in (
                        "item",
                        "comparability",
                        "claim",
                        "c3_context",
                        "c4_context",
                    )
                },
                "claim_id": identity["claim_id"],
            },
            "source_bindings": source_bindings,
            "fact_bindings": {
                fact["source_id"]: {k: fact[k] for k in ("raw", "normalized", "kind", "unit")}
                for fact in (packet["sustainability"], packet["financial"])
                if fact["source_id"] in {ref["source_id"] for ref in refs}
            },
        }
    policy["source_policy_sha256"] = hashlib.sha256(
        b"Synthetic testing policy only; no live approval."
    ).hexdigest()
    policies = {
        canonical_hash(policy): {
            key: copy.deepcopy(policy[key])
            for key in (
                "approved",
                "approved_by",
                "approved_on",
                "version",
                "source_policy_sha256",
                "synthetic_only",
            )
        }
    }
    coverage = {}
    search = packet["search"]
    if search["state"] == "complete":
        coverage[search["receipt_id"]] = {
            **copy.deepcopy(search),
            **{k: identity[k] for k in ("tenant_id", "company_id", "package_id")},
        }
    return {
        "packet": packet,
        "policy": policy,
        "documents": documents,
        "artifacts": artifacts,
        "policies": policies,
        "coverage": coverage,
    }


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", required=True, type=Path)
    args = cli.parse_args(argv)
    for example in sorted(EXAMPLES.glob("*.json")):
        destination = args.output / example.stem
        bundle = build_case(example.stem, destination / "artifacts")
        for key, value in bundle.items():
            with (destination / f"{key}.json").open("x", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
