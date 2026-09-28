"""Precompute reviewer scenarios with the pinned Python rule engine."""

from __future__ import annotations

import argparse
import json
import sys
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from proofops.domain.rulepacks import RulePackSnapshot  # noqa: E402
from proofops.domain.rules.engine import (  # noqa: E402
    MAPPINGS,
    ConfirmedFact,
    ConfirmedTags,
    RuleContext,
    evaluate,
)
from proofops.domain.values import SourceRef  # noqa: E402

TENANT = "00000000-0000-0000-0000-000000000000"
DOCUMENT = "00000000-0000-0000-0000-000000000001"
CLAIM = "00000000-0000-0000-0000-000000000002"
SOURCE = "00000000-0000-0000-0000-000000000003"
MANIFEST = "00000000-0000-0000-0000-000000000004"
HASH = "0" * 64
STATES = "puan"  # present, unknown, absent, conflict
STATE_NAMES = dict(zip(STATES, ("present", "unknown", "absent", "conflict"), strict=True))
LADDER = {
    "management": ("M1", "M2", "M3"),
    "performance": ("P1", "P2", "P3", "P4"),
    "goal": ("G1", "G2", "G3", "G4", "G5", "G6"),
}


def build(pack: RulePackSnapshot) -> dict:
    source = SourceRef(
        source_id=SOURCE,
        document_version_id=DOCUMENT,
        parse_manifest_id=MANIFEST,
        page_num=1,
        printed_page_label=None,
        bbox=None,
        raw_text_sha256=HASH,
        quote="시뮬레이션 입력",
        char_start=0,
        char_end=9,
        location_quality="located",
        verification_state="verified",
    )

    def fact(name: str, state: str) -> ConfirmedFact:
        return ConfirmedFact(
            name=name,
            state=state,
            evidence_refs=(source,) if state == "present" else (),
            source_tenant_id=TENANT if state == "present" else None,
            citation_verified=state == "present",
            binding_accepted=state == "present",
            search_coverage_verified=state == "absent",
            source_scope="global_bound" if name == "assurance_covered" else "local_claim",
            normalized_value="covered"
            if name == "assurance_covered" and state == "present"
            else None,
        )

    tracks: dict[str, dict] = {}
    for track, ladder in LADDER.items():
        others = tuple(name for name in MAPPINGS[track] if name not in ladder)
        rows = {}
        for vector in map("".join, product(STATES, repeat=len(ladder))):
            for other_unresolved in (0, 1):
                for willingness_only in (0, 1) if track == "management" else (0,):
                    state_by_element = dict(
                        zip(ladder, map(STATE_NAMES.__getitem__, vector), strict=True)
                    )
                    state_by_element.update(
                        {name: "unknown" if other_unresolved else "absent" for name in others}
                    )
                    facts = [
                        fact(name, state_by_element[element])
                        for element, names in MAPPINGS[track].items()
                        for name in names
                    ]
                    if track == "management":
                        facts.append(
                            fact("willingness_only", "present" if willingness_only else "absent")
                        )
                    tags = ConfirmedTags(
                        tenant_id=TENANT,
                        document_version_id=DOCUMENT,
                        claim_id=CLAIM,
                        track=track,
                        facts=tuple(facts),
                        tag_revision=1,
                        packet_sha256=HASH,
                        model_sha256=HASH,
                        prompt_sha256=HASH,
                        replicate_hashes=(HASH,) * 3,
                        ontology_version=pack.ontology_version,
                    )
                    decision = evaluate(
                        tags,
                        RuleContext(
                            tenant_id=TENANT,
                            document_version_id=DOCUMENT,
                            claim_id=CLAIM,
                            packet_sha256=HASH,
                            local_synthetic=True,
                        ),
                        pack,
                    )
                    key = (
                        f"{vector}{other_unresolved}{willingness_only}"
                        if track == "management"
                        else f"{vector}{other_unresolved}"
                    )
                    rows[key] = [
                        decision.decision_status,
                        decision.evidence_grade,
                        decision.label,
                        [
                            decision.grade_floor,
                            decision.grade_ceiling,
                            list(decision.grade_open_elements),
                        ]
                        if decision.grade_floor
                        else None,
                        list(decision.rule_ids),
                        list(decision.gap_ids),
                        list(decision.missing_elements),
                        list(decision.unresolved_elements),
                    ]
        tracks[track] = {"ladder": ladder, "other": others, "rows": rows}
    return {"rule_pack_sha256": pack.sha256, "state_codes": STATE_NAMES, "tracks": tracks}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "apps/web/public/demo/engine-table.json"
    )
    args = parser.parse_args()
    pack = RulePackSnapshot(**json.loads((ROOT / "api/rulepack.json").read_text()))
    assert pack.version == "proofops-domain-v2.0-impl2"
    table = build(pack)
    management = table["tracks"]["management"]["rows"]
    assert management["pua00"][3] == ["E1", "E2", ["M2"]]
    assert management["ppa00"][1:3] == ["E2", "INCOMPLETE"]
    assert management["aaa01"][1:3] == ["E0", "UNSUBSTANTIATED"]
    payload = (json.dumps(table, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    assert len(payload) < 1_500_000, f"lookup exceeds 1.5 MB: {len(payload)} bytes"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    scenarios = sum(len(track["rows"]) for track in table["tracks"].values())
    print(f"{args.output}: {len(payload)} bytes, {scenarios} scenarios")


if __name__ == "__main__":
    main()
