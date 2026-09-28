"""The offline diagnostic must not collapse omitted axes into explicit unknowns."""

import runpy
from pathlib import Path


def test_relation_diagnostic_preserves_presence_null_and_literal_disagreement():
    audit = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/audit_live_tagging_blockers.py")
    )["relation_diagnostic"]
    records = [
        dict(replicate_id=i, status="validated_candidate", values={"source": roles})
        for i, roles in enumerate(
            [
                {"entity": None, "metric": {"quote": "same", "char_start": 0}},
                {"entity": None, "metric": {"quote": "same", "char_start": 5}, "scope": None},
                {"entity": None, "metric": {"quote": "same", "char_start": 0}},
            ],
            1,
        )
    ]
    result = audit(records)
    assert result["value_consensus"] is False
    assert result["all_three_validated"] is True
    differences = {d["dimension"]: d for d in result["differences"]}
    assert set(differences) == {"metric", "scope"}
    assert [v["present"] for v in differences["scope"]["replicas"]] == [False, True, False]
    assert differences["scope"]["replicas"][1]["value"] is None
    assert differences["metric"]["replicas"][1]["value"]["char_start"] == 5
    assert audit(records[:1])["value_consensus"] is False
    assert audit([])["all_three_validated"] is False
