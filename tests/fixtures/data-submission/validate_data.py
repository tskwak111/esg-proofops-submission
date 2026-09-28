"""CSV field/reference/review gates; never certifies source truth or policy approval."""

import argparse
import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).parent
NUMBER = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")


def _require(condition, location, message):
    if not condition:
        raise ValueError(f"{location}: {message}")


def _finite(value, location):
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{location}: invalid decimal") from exc
    _require(result.is_finite(), location, "non-finite decimal")
    return result


def _fields(tables, final):
    """Reject malformed meaning-bearing fields without resolving domain questions."""
    enums = {
        "corpus": {
            "rights_status": {"pending", "approved", "restricted"},
            "split": {"train", "dev", "test", "challenge"},
        },
        "claims": {
            "annotation_status": {"draft", "reviewed", "adjudicated", "unresolved"},
            "track": {"goal", "performance", "management", "unknown"},
        },
        "elements": {"annotation_status": {"draft", "reviewed", "adjudicated", "unresolved"}},
        "numeric": {
            "expected_check": {
                "consistent",
                "inconsistent",
                "not_comparable",
                "not_computable",
                "unresolved",
            },
            "scope2_basis": {"", "location_based", "market_based", "unknown"},
        },
        "assurance": {
            "expected_status": {"covered", "not_covered", "undetermined", ""},
            "level": {"limited", "reasonable", "unknown", ""},
        },
        "rules": {"decision_status": {"unresolved", "proposed", "approved", "rejected"}},
        "acceptance": {"approval_status": {"proposed", "approved", "unresolved"}},
    }
    for name, rows in tables.items():
        seen = set()
        for i, row in enumerate(rows, 2):
            identifier = next(
                (
                    row[k]
                    for k in ("annotation_id", "case_id", "rule_id", "document_id", "metric")
                    if k in row
                ),
                "",
            )
            loc = f"{name}.csv:{i} ({identifier})"
            _require(identifier and identifier not in seen, loc, "missing/duplicate row identity")
            seen.add(identifier)
            for field, allowed in enums.get(name, {}).items():
                _require(row[field] in allowed, loc, f"invalid {field}")
            if name in ("claims", "elements"):
                if final:
                    _require(
                        row["annotation_status"] == "adjudicated",
                        loc,
                        "final annotations must be adjudicated",
                    )
                if row["annotation_status"] == "adjudicated":
                    _require(row["adjudicator"].strip(), loc, "adjudication requires actor")
            if final and name in ("numeric", "assurance", "reconciliation"):
                _require(row["adjudicator"].strip(), loc, "final case requires adjudicator")
            if name == "numeric":
                raw, decimal = row["value_raw"].strip(), row["value_decimal"].strip()
                if decimal:
                    parentheses = raw.startswith("(") and raw.endswith(")")
                    literal = raw[1:-1] if parentheses else raw
                    _require(
                        NUMBER.fullmatch(literal) and not (parentheses and literal[0] in "+-"),
                        loc,
                        "one literal raw number required",
                    )
                    exact = Decimal(literal.replace(",", ""))
                    if parentheses:
                        exact = -exact
                    _require(
                        _finite(decimal, loc) == exact,
                        loc,
                        "value_decimal must match raw value before multiplier",
                    )
                else:
                    _require(
                        not NUMBER.fullmatch(raw)
                        and row["expected_check"] in ("unresolved", "not_computable"),
                        loc,
                        "unreadable value must remain unresolved/not_computable; "
                        "numeric literal requires value_decimal",
                    )
                if row["multiplier"]:
                    _require(_finite(row["multiplier"], loc) > 0, loc, "invalid multiplier")
            if name == "assurance" and row["expected_status"] in ("covered", "not_covered"):
                _require(
                    row["quote"] and row["physical_page"] and row["reason"],
                    loc,
                    "decided assurance requires source and reason",
                )
            if name == "rules" and row["decision_status"] == "approved":
                _require(
                    all(
                        row[k].strip()
                        for k in (
                            "domain_reviewer",
                            "product_approver",
                            "approved_on",
                            "official_source_url",
                            "clause",
                            "verified_on",
                            "rulepack_before",
                            "rulepack_after",
                        )
                    ),
                    loc,
                    "incomplete rule approval",
                )
            if name == "acceptance":
                count, observed = row["sample_count"], row["observed_value"]
                if count or observed:
                    _require(
                        count.isdigit() and int(count) > 0 and observed,
                        loc,
                        "measurement requires positive sample_count and observed_value",
                    )
                    _finite(observed, loc)
                if row["approval_status"] == "approved":
                    _require(row["domain_reviewer"].strip(), loc, "approval requires reviewer")


def load(folder):
    tables = {}
    for template in sorted((ROOT / "templates").glob("*.csv")):
        with template.open(encoding="utf-8-sig", newline="") as handle:
            expected = next(csv.reader(handle))
        with (folder / template.name).open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != expected:
                raise ValueError(f"{template.name}: header mismatch")
            rows = list(reader)
            if any(None in row or any(v is None for v in row.values()) for row in rows):
                raise ValueError(f"{template.name}: invalid column count")
            tables[template.stem] = rows
    return tables


def validate(tables, final=False):
    _fields(tables, final)
    docs = {row["document_id"]: row for row in tables["corpus"]}
    claims = {row["claim_id"] for row in tables["claims"]}
    if len(docs) != len(tables["corpus"]) or len(claims) != len(tables["claims"]):
        raise ValueError("duplicate document_id/claim_id")
    splits = {}
    for row in tables["corpus"]:
        if not row["document_id"] or not row["company_id"]:
            raise ValueError("missing document/company identity")
        company = row["company_id"]
        if company in splits and splits[company] != row["split"]:
            raise ValueError("company split leakage")
        splits[company] = row["split"]
        if final and row["rights_status"] != "approved":
            raise ValueError("final corpus rights not approved")
    for row in tables["claims"]:
        if row["document_id"] not in docs or not row["claim_id"] or not row["quote"]:
            raise ValueError("invalid claim reference/quote")
    for name in ("elements", "numeric", "assurance", "reconciliation"):
        for row in tables[name]:
            if row["claim_id"] not in claims:
                raise ValueError(f"{name}: dangling claim_id")
            if name in ("numeric", "assurance") and row["document_id"] not in docs:
                raise ValueError(f"{name}: missing source document")
    for row in tables["elements"]:
        if row["state"] not in {"present", "absent", "unknown", "conflict", "not_applicable"}:
            raise ValueError("invalid element state")
        if row["state"] == "present" and not (
            row["quote"]
            and row["physical_page"]
            and row["binding_reason"]
            and row["evidence_document_id"] in docs
        ):
            raise ValueError("present lacks source or binding")
        if row["state"] == "absent" and not row["searched_scope"]:
            raise ValueError("absent lacks search scope")
    for row in tables["reconciliation"]:
        if row["financial_document_id"] not in docs:
            raise ValueError("missing financial document")
        if row["expected_execution_state"] == "completed":
            if row["expected_status"] not in {"matched", "needs_explanation", "not_applicable"}:
                raise ValueError("invalid reconciliation status")
        elif (
            row["expected_execution_state"] not in {"blocked", "not_run"} or row["expected_status"]
        ):
            raise ValueError("blocked/not_run must not have status")
    return {name: len(rows) for name, rows in tables.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=ROOT / "examples")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    tables = load(args.folder)
    counts = validate(tables, args.final)
    if args.self_test:
        import copy

        for mutation in ("source", "split", "status", "claim"):
            broken = copy.deepcopy(tables)
            if mutation == "source":
                broken["elements"][0]["quote"] = ""
            elif mutation == "split":
                broken["corpus"][1]["split"] = "test"
            elif mutation == "status":
                broken["reconciliation"][0]["expected_status"] = "matched"
            else:
                broken["numeric"][0]["claim_id"] = "missing"
            try:
                validate(broken)
            except ValueError:
                pass
            else:
                raise AssertionError(f"failed to reject {mutation}")
    print(f"PASS: fields, references and review gates; rows={counts}; source review still required")


if __name__ == "__main__":
    main()
