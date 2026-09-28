import json
from types import SimpleNamespace
from uuid import uuid4

from proofops_worker import tag_recovery_cli


def test_recovery_estimate_uses_maximum_nine_calls_per_claim(monkeypatch, capsys, tmp_path):
    plan = SimpleNamespace(
        claim_ids=("claim-a", "claim-b"),
        max_new_requests=30,
        to_dict=lambda: {"claim_ids": ["claim-a", "claim-b"]},
    )
    monkeypatch.setattr(
        tag_recovery_cli,
        "plan_recovery",
        lambda *args, **kwargs: (plan, {"class_counts": {}}, {}),
    )
    runner = SimpleNamespace(
        store=SimpleNamespace(
            path=str(tmp_path / "state.sqlite"), usage=SimpleNamespace(cost_data=lambda *args: [])
        ),
        tags=object(),
    )
    options = SimpleNamespace(
        tenant_id=uuid4(),
        run_id=uuid4(),
        claim_limit=2,
        max_new_requests=30,
        acknowledge_stop=[],
        authorized_by="operator",
        confirm=False,
        report=None,
    )

    assert tag_recovery_cli._authorize(runner, options) == 0
    assert json.loads(capsys.readouterr().out)["proposed_new_requests"] == 18
