"""Validate handoff fixtures, not product decisions or real source verification.

Two modes:
  * default (no args)  -> fixture self-check of the built-in synthetic examples.
  * ``return`` sub-cmd -> structural + linkage validation of an ACTUAL developer-B
    return (an explicit --input packet, --policy, and --output result triple).

The ``return`` mode is a structural gate only. It never runs submitted code, never
trusts client-asserted "verified" flags, never approves/activates a rulepack, and
never claims semantic acceptance of the reconciliation. It fails closed and emits
actionable JSON on stderr describing every problem found.
"""

import argparse
import copy
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parent

# Reconciliation returns must not include evidence grades or labels.
PROHIBITED_RESULT_FIELDS = (
    "evidence_grade",
    "grade",
    "label",
    "evidence_label",
    "decision_grade",
)


def read(name):
    return json.loads((ROOT / name).read_text())


def digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def check_packet(packet):
    sources = packet["sources"]
    ids = [source["source_id"] for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source_id")
    refs = [packet["claim"]["source_id"], packet["explanation"]["source_id"]]
    refs += [packet[side]["source_id"] for side in ("sustainability", "financial")]
    refs += packet["search"]["reviewed_source_ids"]
    if packet["c4_context"]:
        refs += packet["c4_context"]["definition_source_ids"]
        refs += packet["c4_context"]["calculation_source_ids"]
    if packet["c3_context"]:
        refs += [
            packet["c3_context"][k] for k in ("commitment_source_id", "funding_plan_source_id")
        ]
    if any(ref is not None and ref not in ids for ref in refs):
        raise ValueError("unknown source reference")
    if packet["explanation"]["search_complete"] != (packet["search"]["state"] == "complete"):
        raise ValueError("search states disagree")
    search = packet["search"]
    if search["state"] == "complete" and (
        search["failed_document_ids"]
        or not search["coverage_policy_id"]
        or not search["receipt_id"]
        or not search["required_document_ids"]
    ):
        raise ValueError("invalid complete search receipt")
    identity = packet["identity"]
    for start, end in (
        ("period_start", "period_end"),
        ("financial_period_start", "financial_period_end"),
    ):
        if (
            identity[start]
            and identity[end]
            and date.fromisoformat(identity[start]) > date.fromisoformat(identity[end])
        ):
            raise ValueError("reversed period")


def load_schema_validators():
    validators = {}
    for kind in ("input", "output", "policy"):
        schema = read(f"{kind}.schema.json")
        Draft202012Validator.check_schema(schema)
        validators[kind] = Draft202012Validator(schema, format_checker=FormatChecker())
    return validators


def _read_json_file(path, errors):
    p = Path(path)
    if not p.is_file():
        errors.append({"where": path, "error": "file_not_found"})
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        errors.append({"where": path, "error": "invalid_json", "detail": str(exc)})
        return None
    except OSError as exc:  # pragma: no cover - defensive
        errors.append({"where": path, "error": "read_failed", "detail": str(exc)})
        return None


def _schema_errors(validator, document, where):
    problems = []
    for err in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        loc = "/".join(str(part) for part in err.path)
        problems.append(
            {"where": f"{where}:{loc}" if loc else where, "error": "schema", "detail": err.message}
        )
    return problems


def validate_return(input_path, policy_path, output_path):
    """Structurally validate a real developer-B return triple.

    Returns a list of actionable error dicts; an empty list means the structural
    gate passed. This is NOT semantic acceptance of the reconciliation.
    """
    errors = []
    packet = _read_json_file(input_path, errors)
    policy = _read_json_file(policy_path, errors)
    result = _read_json_file(output_path, errors)
    if errors:
        return errors

    validators = load_schema_validators()

    # 1. Prohibited fields / C5 first: do not trust submitted content, fail closed.
    for field in PROHIBITED_RESULT_FIELDS:
        if isinstance(result, dict) and field in result:
            errors.append(
                {
                    "where": f"output:{field}",
                    "error": "prohibited_field",
                    "detail": "grade/label fields are not allowed in a reconciliation return",
                }
            )
    for doc_name, doc in (("input", packet), ("output", result)):
        if isinstance(doc, dict) and doc.get("item") == "C5":
            errors.append(
                {
                    "where": f"{doc_name}:item",
                    "error": "prohibited_item",
                    "detail": "C5 is not an accepted reconciliation item",
                }
            )

    # 2. Schema validation of each document.
    errors += _schema_errors(validators["input"], packet, "input")
    errors += _schema_errors(validators["policy"], policy, "policy")
    errors += _schema_errors(validators["output"], result, "output")
    if errors:
        return errors

    # 3. Packet internal integrity (source id uniqueness, reference existence, ...).
    try:
        check_packet(packet)
    except (ValueError, KeyError) as exc:
        errors.append({"where": "input", "error": "packet_integrity", "detail": str(exc)})
        return errors

    # 4. Claim / item linkage between packet and result.
    if result["claim_id"] != packet["identity"]["claim_id"]:
        errors.append(
            {
                "where": "output:claim_id",
                "error": "linkage",
                "detail": "result claim_id does not match packet identity.claim_id",
            }
        )
    if result["item"] != packet["item"]:
        errors.append(
            {
                "where": "output:item",
                "error": "linkage",
                "detail": "result item does not match packet item",
            }
        )

    # 5. Packet / policy hash re-computation (do not trust submitted hashes).
    expected_packet = digest(packet)
    expected_policy = digest(policy)
    if result["packet_sha256"] != expected_packet:
        errors.append(
            {
                "where": "output:packet_sha256",
                "error": "hash_mismatch",
                "detail": f"expected {expected_packet}",
            }
        )
    if result["policy_sha256"] != expected_policy:
        errors.append(
            {
                "where": "output:policy_sha256",
                "error": "hash_mismatch",
                "detail": f"expected {expected_policy}",
            }
        )

    # 6. Output source IDs must be a subset of the packet's declared sources.
    packet_source_ids = {s["source_id"] for s in packet["sources"]}
    unknown = [sid for sid in result["source_ids"] if sid not in packet_source_ids]
    if unknown:
        errors.append(
            {
                "where": "output:source_ids",
                "error": "unknown_source",
                "detail": f"source ids not present in packet: {sorted(unknown)}",
            }
        )
    exp_src = result.get("explanation_source_id")
    if exp_src is not None and exp_src not in packet_source_ids:
        errors.append(
            {
                "where": "output:explanation_source_id",
                "error": "unknown_source",
                "detail": "explanation source id not present in packet",
            }
        )

    return errors


def run_return_cli(input_path, policy_path, output_path):
    errors = validate_return(input_path, policy_path, output_path)
    if errors:
        report = {
            "status": "rejected",
            "note": "structural gate only; not semantic acceptance, not rulepack approval",
            "errors": errors,
        }
        json.dump(report, sys.stderr, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stderr.write("\n")
        return 1
    report = {
        "status": "structurally_valid",
        "note": (
            "schema+linkage+hash+source checks passed; NOT semantic acceptance, "
            "NOT source-verification, NOT rulepack approval/activation"
        ),
    }
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def check_fixtures():
    validators = load_schema_validators()
    for kind in ("input", "output", "policy"):
        validators[kind].validate(read(f"example-{kind}.json"))
    cases = [read(str(p.relative_to(ROOT))) for p in sorted((ROOT / "examples").glob("*.json"))]
    for case in cases:
        for key, kind in (("input", "input"), ("policy", "policy"), ("expected", "output")):
            validators[kind].validate(case[key])
        check_packet(case["input"])
        result = case["expected"]
        assert result["packet_sha256"] == digest(case["input"])
        assert result["policy_sha256"] == digest(case["policy"])
        assert result["claim_id"] == case["input"]["identity"]["claim_id"]
        assert result["item"] == case["input"]["item"]
        assert set(result["source_ids"]) <= {s["source_id"] for s in case["input"]["sources"]}
    result = read("example-output.json")
    for key, value in (("status", "mismatch"), ("evidence_grade", "E3")):
        invalid = copy.deepcopy(result)
        invalid[key] = value
        assert not validators["output"].is_valid(invalid)
    assert not validators["output"].is_valid(result | {"execution_state": "blocked"})
    validators["output"].validate(result | {"execution_state": "blocked", "status": None})
    packet = read("example-input.json")
    assert not validators["input"].is_valid(packet | {"item": "C5"})
    for mutation in ("duplicate", "dangling", "coverage", "date"):
        broken = copy.deepcopy(packet)
        if mutation == "duplicate":
            broken["sources"].append(broken["sources"][0])
        elif mutation == "dangling":
            broken["claim"]["source_id"] = "missing"
        elif mutation == "coverage":
            broken["search"]["state"] = "complete"
            broken["explanation"]["search_complete"] = True
        else:
            broken["identity"]["period_start"] = "2025-01-01"
        try:
            check_packet(broken)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid packet: {mutation}")
    print(f"PASS: 3 schemas, {len(cases)} synthetic cases, hash/ref/status/stage guards")


def main(argv=None):
    parser = argparse.ArgumentParser(description="ESG ProofOps handoff contract validator")
    sub = parser.add_subparsers(dest="command")
    ret = sub.add_parser(
        "return",
        help="structurally validate an actual developer-B return (schema+linkage+hash+sources)",
    )
    ret.add_argument("--input", required=True, help="path to the returned packet JSON")
    ret.add_argument("--policy", required=True, help="path to the policy JSON")
    ret.add_argument("--output", required=True, help="path to the returned result JSON")
    args = parser.parse_args(argv)
    if args.command == "return":
        return run_return_cli(args.input, args.policy, args.output)
    check_fixtures()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
