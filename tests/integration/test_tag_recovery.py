"""Tag-stage recovery: the never-sent proof, the durable bound and the carried stage.

No paid call and no network: the provider is the existing fake-HTTP composition and
every proof is recomputed from a local ledger and a local receipt tree.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from proofops_agent.upstage_tagging import TransportResume
from proofops_worker.live_tagging import LiveTaggingRuntime
from proofops_worker.tag_recovery import (
    RecoveryRejected,
    TagRecoveryPlan,
    _classify,
    issued_since_baseline,
    recovery_proof,
)

from tests.integration.test_live_tagging_worker import configured


def _row(request_id, *, status, code=None, latency=0, provider=None, tokens=None, cost=None):
    return {
        "reservation": {
            "call": {"request_id": request_id, "role": "tagger", "attempt": 1},
            "input_tokens": 100,
            "max_output_tokens": 10,
        },
        "state": "settled",
        "ledger": {
            "usage": {
                "status": status,
                "error_code": code,
                "latency_ms": latency,
                "provider_request_id": provider,
                "input_tokens": tokens,
                "output_tokens": tokens,
            },
            "cost_decimal": cost,
        },
        "release": None,
    }


def _receipts(root: Path, prefix: str, request_id: str, *, status="succeeded", code=None):
    directory = root / prefix / request_id
    directory.mkdir(parents=True)
    (directory / "request.json").write_text("{}")
    (directory / "response.json").write_text(
        json.dumps({"usage": {"status": status, "error_code": code}})
    )
    return directory


# --------------------------------------------------------------------------- proof


def test_never_sent_needs_no_receipt_an_explicit_code_and_exactly_zero_cost(tmp_path):
    """The suppression proof is an allow-list plus structural evidence, not timing.

    A real provider failure can round its latency to 0ms and can be missing a
    provider id, so neither may be sufficient. What separates the two is that a
    suppressed request never reached ``directory.mkdir()`` and therefore has no
    receipt, and that its settled code is an explicitly known suppression code.
    """
    suppressed = str(UUID(int=1))
    unknown_code = str(UUID(int=2))
    with_receipt = str(UUID(int=3))
    _receipts(tmp_path, "preliminary", with_receipt, status="failed", code="UPSTREAM_UNAVAILABLE")
    proof = recovery_proof(
        [
            _row(suppressed, status="failed", code="UPSTREAM_UNAVAILABLE"),
            # Same zero metadata, but not an acknowledged suppression code.
            _row(unknown_code, status="failed", code="UPSTAGE_HTTP_500"),
            # Zero metadata and the right code, but it has a receipt: it reached
            # the provider path, so its billing status is unknown.
            _row(with_receipt, status="failed", code="UPSTREAM_UNAVAILABLE"),
        ],
        tmp_path,
    )
    assert proof["classes"]["never_sent"] == [suppressed]
    assert sorted(proof["classes"]["unresolved_failure"]) == sorted([unknown_code, with_receipt])


def test_tiny_nonzero_cost_is_not_rounded_away_to_never_sent(tmp_path):
    """Decimal, not float: 1E-400 is a real charge that binary floats flatten to 0."""
    charged = str(UUID(int=4))
    row = _row(charged, status="failed", code="UPSTREAM_UNAVAILABLE", cost="1E-400")
    assert float(row["ledger"]["cost_decimal"]) == 0.0  # the unsafe comparison
    assert _classify(row, set()) == "unresolved_failure"
    free = _row(str(UUID(int=5)), status="failed", code="UPSTREAM_UNAVAILABLE", cost="0.00")
    assert _classify(free, set()) == "never_sent"


def test_paid_success_without_a_receipt_is_never_treated_as_recoverable(tmp_path):
    paid = str(UUID(int=6))
    proof = recovery_proof(
        [_row(paid, status="succeeded", latency=900, provider="p", tokens=5)], tmp_path
    )
    assert proof["classes"] == {"succeeded_without_receipt": [paid]}
    assert "never_sent" not in proof["classes"]


# ------------------------------------------------------------------- durable bound


def test_resume_refuses_a_root_holding_an_unacknowledged_failure(tmp_path):
    failed = str(UUID(int=10))
    root = tmp_path / "preliminary"
    _receipts(tmp_path, "preliminary", failed, status="failed", code="UPSTAGE_REQUEST_FAILED")
    (root / "transport-stop.json").write_text(json.dumps({"code": "X", "request_id": failed}))
    blind = TransportResume(acknowledged_failures=frozenset(), max_new_requests=5)
    assert blind.armed(root) is False and blind.allows(root) is False
    named = TransportResume(acknowledged_failures=frozenset({failed}), max_new_requests=5)
    assert named.armed(root) is True and named.allows(root) is True


def test_resume_refuses_an_incomplete_receipt_even_when_acknowledged(tmp_path):
    failed = str(UUID(int=11))
    root = tmp_path / "preliminary"
    (root / str(UUID(int=12))).mkdir(parents=True)  # no response.json
    gate = TransportResume(acknowledged_failures=frozenset({failed}), max_new_requests=5)
    assert gate.armed(root) is False


def test_resume_bound_is_job_wide_and_survives_a_restart(tmp_path):
    """The crash/restart regression: one authorization means one total allowance.

    A recovery that already issued requests must resume on the remainder. The count
    comes from durable state (receipt directories and reservation rows measured
    against the baseline pinned at authorization time), never from process memory.
    """
    baseline = [str(UUID(int=20)), str(UUID(int=21))]
    for request_id in baseline:
        _receipts(tmp_path, "preliminary", request_id)
    store = SimpleNamespace(
        usage=SimpleNamespace(
            cost_data=lambda tenant, run: [
                _row(request_id, status="succeeded") for request_id in baseline
            ]
        )
    )
    tenant, run = str(UUID(int=30)), str(UUID(int=31))
    assert issued_since_baseline(store, tenant, run, tmp_path, baseline) == []

    # First attempt spends 4 of 10 and then the process dies.
    first = TransportResume(acknowledged_failures=frozenset(), max_new_requests=10)
    issued_now = [str(uuid4()) for _ in range(4)]
    for request_id in issued_now:
        first.consume(tmp_path / "preliminary", request_id)
        _receipts(tmp_path, "preliminary", request_id)
    assert first.remaining == 6

    # The restart recounts durably and gets the remainder, not a second allowance.
    spent = issued_since_baseline(store, tenant, run, tmp_path, baseline)
    assert sorted(spent) == sorted(issued_now)
    second = TransportResume(
        acknowledged_failures=frozenset(), max_new_requests=10, already_dispatched=len(spent)
    )
    assert second.already_dispatched == 4 and second.remaining == 6

    # A fully spent authorization cannot be reconstructed at all.
    with pytest.raises(ValueError, match="ALLOWANCE_ALREADY_SPENT"):
        TransportResume(acknowledged_failures=frozenset(), max_new_requests=4, already_dispatched=4)


def test_resume_counts_a_reservation_written_before_the_process_died(tmp_path):
    """A reservation row exists before the receipt, so a crash between the two counts."""
    baseline = [str(UUID(int=40))]
    _receipts(tmp_path, "preliminary", baseline[0])
    orphan = str(UUID(int=41))
    store = SimpleNamespace(
        usage=SimpleNamespace(
            cost_data=lambda tenant, run: [
                _row(baseline[0], status="succeeded"),
                _row(orphan, status="failed", code="UPSTREAM_UNAVAILABLE"),
            ]
        )
    )
    spent = issued_since_baseline(store, str(UUID(int=42)), str(UUID(int=43)), tmp_path, baseline)
    assert spent == [orphan]


def test_exhausted_allowance_blocks_without_consuming_more(tmp_path):
    root = tmp_path / "preliminary"
    root.mkdir(parents=True)
    gate = TransportResume(acknowledged_failures=frozenset(), max_new_requests=1)
    gate.consume(root, str(UUID(int=50)))
    assert gate.remaining == 0 and gate.allows(root) is False
    # Re-counting the same request is idempotent, never a second charge.
    gate.consume(root, str(UUID(int=50)))
    assert gate.remaining == 0
    from proofops.application.preflight import PreflightBlocked

    with pytest.raises(PreflightBlocked, match="ALLOWANCE_EXHAUSTED"):
        gate.consume(root, str(UUID(int=51)))


# ------------------------------------------------------------------------- plan


def _plan_fields(**overrides):
    job = str(UUID(int=60))
    fields = dict(
        schema="tag_recovery_plan_v1",
        tenant_id=str(UUID(int=61)),
        run_id=str(UUID(int=62)),
        lineage_job_id=str(UUID(int=63)),
        lineage_checkpoint_sha256="a" * 64,
        recovery_job_id=job,
        shard="tag-recovery-" + job,
        receipts_root="/tmp/tagging-receipts/" + str(UUID(int=62)),
        claim_ids=(str(UUID(int=64)),),
        reissued_request_ids=(str(UUID(int=65)),),
        acknowledged_failures=(str(UUID(int=66)),),
        max_new_requests=30,
        proof_sha256="b" * 64,
        authorized_by="operator",
        authorized_at=1,
    )
    return fields | overrides


def test_plan_pins_its_own_job_shard_and_receipt_tree():
    plan = TagRecoveryPlan(**_plan_fields())
    assert plan.to_dict() == json.loads(json.dumps(plan.to_dict()))
    with pytest.raises(RecoveryRejected, match="JOB_MUST_BE_NEW"):
        TagRecoveryPlan(**_plan_fields(lineage_job_id=str(UUID(int=60))))
    with pytest.raises(RecoveryRejected, match="SHARD_MISMATCH"):
        TagRecoveryPlan(**_plan_fields(shard="tag-recovery-other"))
    with pytest.raises(RecoveryRejected, match="RECEIPTS_ROOT_MISMATCH"):
        TagRecoveryPlan(**_plan_fields(receipts_root="/tmp/tagging-receipts/elsewhere"))
    with pytest.raises(RecoveryRejected, match="BOUND_INVALID"):
        TagRecoveryPlan(**_plan_fields(max_new_requests=0))


# ------------------------------------------------------------ live runtime wiring


def _sized_budget(runtime, tmp_path, *, calls=24):
    """Give the fixture enough headroom to model a recovery inside existing limits.

    This only sizes the test's own fresh budget before anything is spent; it never
    changes a budget that already has reservations. The real Lotte run needs no
    widening either: 617 of its 2000 authorized tagger calls are used, so a bounded
    recovery fits inside the immutable limits it already has.
    """
    from proofops.adapters.aws.usage import LocalSQLiteUsageStore
    from proofops.application.budget import BudgetLimits, RoleLimit

    bound = runtime.snapshot["input_reservation_policy"]["reservation_input_tokens"]
    budget = LocalSQLiteUsageStore(tmp_path / "usage-sized.sqlite3")
    budget.create_budget(
        runtime.graph.tenant_id,
        runtime.snapshot["run_id"],
        runtime.graph.document_version_id,
        BudgetLimits(
            bound * calls, 1024 * calls, (RoleLimit("tagger", calls, bound, 1024, bound + 1024),)
        ),
    )
    runtime.runner.store.usage = budget
    return budget


def _echo_requested_claim(runtime, monkeypatch, calls):
    """Make the fake provider answer about whichever claim was actually asked.

    The shared harness always answers about its first claim, which is fine when a
    test only ever asks about that one. A recovery deliberately sends a claim that
    was never sent before, so the fake has to echo the claim_id on the wire or
    validation rejects the reply for the wrong reason.
    """
    settings = runtime.preliminary_settings

    def post(body):
        calls.append(body)
        user = json.loads(body["messages"][-1]["content"])
        return dict(
            id=f"provider-{len(calls)}",
            model=settings.model_id,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[
                dict(
                    finish_reason="stop",
                    message=dict(
                        content=json.dumps(
                            dict(
                                claim_id=user["claim_id"],
                                track="management",
                                safe_harbor_category=None,
                                track_confidence=0.8,
                                dimensions=dict(entity=None, metric=None, reporting_period=None),
                            )
                        )
                    ),
                )
            ],
        )

    monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", post)


def _rearmed(runtime, gate, *, job_id=None):
    """Re-create the runtime under an authorization, optionally as a new job.

    A real recovery is a *new* tag job, so its deterministic request_ids differ
    from the burned ones. Reusing the lineage job_id here is what makes the
    ``max_attempts=1`` guard visible: the same request_id cannot be re-reserved.
    """
    runtime.runner.resume = gate
    lease = (
        runtime.lease if job_id is None else SimpleNamespace(message=SimpleNamespace(job_id=job_id))
    )
    return LiveTaggingRuntime(
        runtime.runner,
        runtime.snapshot,
        runtime.graph,
        lease,
        runtime.usage,
        probe=runtime.preliminary_transport._probe,
        ledger=runtime.ledger,
        receipts=runtime.receipts,
    )


def test_acknowledged_resume_sends_again_and_leaves_the_old_stop_untouched(tmp_path, monkeypatch):
    """The actual recovery behavior: the acknowledged stop stays, new work proceeds."""
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    _sized_budget(runtime, tmp_path)
    _echo_requested_claim(runtime, monkeypatch, calls)
    assert runtime.preliminary(claim, graph) is not None
    paid = len(calls)

    # A real unresolved failure stops the root, exactly as the incident did.
    failure = str(UUID(int=70))
    root = runtime.preliminary_transport._receipts
    _receipts(
        runtime.receipts, "preliminary", failure, status="failed", code="UPSTAGE_REQUEST_FAILED"
    )
    stop = root / "transport-stop.json"
    stop.write_text(json.dumps({"code": "UPSTAGE_REQUEST_FAILED", "request_id": failure}))
    before = stop.read_bytes()

    from dataclasses import replace

    later = replace(claim, claim_id=str(UUID(int=71)))
    # Without an authorization the stop still blocks and nothing is sent.
    assert runtime.preliminary(later, graph) is None
    assert len(calls) == paid
    assert (
        runtime.preliminary_records[later.claim_id][0]["stable_reason"]["category"] == "never_sent"
    )

    # Reusing the lineage job would collide with the already-burned request_id:
    # max_attempts=1 is intact and is never relaxed to make progress.
    same_job = _rearmed(
        runtime,
        TransportResume(
            acknowledged_failures=frozenset({failure}),
            max_new_requests=3,
            expected_root=runtime.receipts,
        ),
    )
    assert same_job.preliminary(later, graph) is None
    assert len(calls) == paid
    assert (
        same_job.preliminary_records[later.claim_id][0]["stable_reason"]["error_code"]
        == "PRELIMINARY_PENDING_CALL"
    )

    # A recovery job is a new job, so its request_ids are new and reservable inside
    # the run's existing immutable limits. Nothing about the old rows changes.
    gate = TransportResume(
        acknowledged_failures=frozenset({failure}),
        max_new_requests=3,
        expected_root=runtime.receipts,
    )
    resumed = _rearmed(runtime, gate, job_id=str(UUID(int=72)))
    assert resumed.preliminary(later, graph) is not None
    assert len(calls) == paid + 3
    assert gate.remaining == 0
    # Immutable residue is byte-unchanged: the stop and the acknowledged receipt.
    assert stop.read_bytes() == before
    assert (root / failure / "response.json").exists()
    # The burned never-sent row is still there, still settled, still not retried.
    burned = same_job.preliminary_records[later.claim_id][0]["request_id"]
    assert burned not in gate.dispatched


def test_a_new_failure_while_resumed_disarms_and_chains_its_own_stop(tmp_path, monkeypatch):
    """Stop at the first new provider failure, durably, with no blanket retry."""
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    failure = str(UUID(int=80))
    root = runtime.receipts / "preliminary"
    root.mkdir(parents=True, exist_ok=True)
    _receipts(
        runtime.receipts, "preliminary", failure, status="failed", code="UPSTAGE_REQUEST_FAILED"
    )
    stop = root / "transport-stop.json"
    stop.write_text(json.dumps({"code": "UPSTAGE_REQUEST_FAILED", "request_id": failure}))
    before = stop.read_bytes()
    gate = TransportResume(
        acknowledged_failures=frozenset({failure}),
        max_new_requests=9,
        expected_root=runtime.receipts,
    )
    resumed = _rearmed(runtime, gate)

    def broken(system, user, *, request_id, max_tokens, json_mode):
        raise ValueError("UPSTAGE_HTTP_503")

    monkeypatch.setattr(resumed.preliminary_transport._probe, "complete", broken)
    assert resumed.preliminary(claim, graph) is None
    # One new attempt only: the failure withdraws the whole remaining allowance.
    assert gate.remaining == 0 and len(gate.dispatched) == 1
    new_request = next(iter(gate.dispatched))
    assert stop.read_bytes() == before  # the acknowledged stop is never rewritten
    chained = root / f"transport-stop.{new_request}.json"
    assert json.loads(chained.read_text())["request_id"] == new_request
    # A later process re-arming this root now finds an unacknowledged failure.
    fresh = TransportResume(acknowledged_failures=frozenset({failure}), max_new_requests=9)
    assert fresh.armed(root) is False


def test_exhausted_allowance_costs_no_reservation_and_no_ledger_row(tmp_path, monkeypatch):
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    root = runtime.receipts / "preliminary"
    root.mkdir(parents=True, exist_ok=True)
    gate = TransportResume(
        acknowledged_failures=frozenset(), max_new_requests=1, expected_root=runtime.receipts
    )
    gate.consume(root, str(UUID(int=90)))
    resumed = _rearmed(runtime, gate)
    ledger_before = len(
        resumed.runner.store.usage.ledger(graph.tenant_id, resumed.snapshot["run_id"])
    )
    assert resumed.preliminary(claim, graph) is None
    assert calls == []
    record = resumed.preliminary_records[claim.claim_id][0]
    assert record["stable_reason"]["category"] == "local_stop"
    assert record["stable_reason"]["error_code"] == "PRELIMINARY_RECOVERY_ALLOWANCE_EXHAUSTED"
    after = resumed.runner.store.usage.ledger(graph.tenant_id, resumed.snapshot["run_id"])
    assert len(after) == ledger_before  # no reservation, no settled row, no spend


def test_resume_cannot_be_applied_to_a_different_receipt_tree(tmp_path, monkeypatch):
    runtime, _, _, _, _, _ = configured(tmp_path, monkeypatch)
    gate = TransportResume(
        acknowledged_failures=frozenset(),
        max_new_requests=2,
        expected_root=tmp_path / "somewhere-else",
    )
    with pytest.raises(ValueError, match="RESUME_ROOT_MISMATCH"):
        _rearmed(runtime, gate)


def test_ordinary_operation_has_no_resume_and_is_unchanged(tmp_path, monkeypatch):
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    assert runtime.resume is None
    assert runtime.preliminary_transport.may_dispatch() is True
    assert runtime.preliminary(claim, graph) is not None
    assert len(calls) == 3


# ------------------------------------------------------- end to end over a real run


def test_bound_below_one_claim_is_refused():
    with pytest.raises(RecoveryRejected, match="BOUND_BELOW_ONE_CLAIM"):
        TagRecoveryPlan(**_plan_fields(max_new_requests=8))
    TagRecoveryPlan(**_plan_fields(max_new_requests=9))


def _stopped_live_run(tmp_path, monkeypatch):
    """A genuine upstage_local run whose tag stage stopped before any provider call."""
    from tests.integration.test_live_tagging_pipeline import _pipeline_setup

    ctx = _pipeline_setup(tmp_path, monkeypatch)
    runner, tenant, run_id = ctx["tag_runner"], ctx["tenant"], ctx["run_id"]
    receipts = tmp_path / "tag-receipts" / run_id
    # The incident shape: a prior unresolved failure has stopped the preliminary root.
    failure = str(UUID(int=4242))
    _receipts(receipts, "preliminary", failure, status="failed", code="UPSTAGE_REQUEST_FAILED")
    (receipts / "preliminary" / "transport-stop.json").write_text(
        json.dumps({"code": "UPSTAGE_REQUEST_FAILED", "request_id": failure})
    )
    return ctx, runner, tenant, run_id, receipts, failure


def test_stopped_tag_stage_commits_blocked_without_any_provider_call(tmp_path, monkeypatch):
    """The starting condition: the stage stops, spends nothing, and stays honest."""
    ctx, runner, tenant, run_id, receipts, failure = _stopped_live_run(tmp_path, monkeypatch)
    calls = ctx["calls"]
    before = len(calls)
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "blocked"
    assert len(calls) == before  # the stop suppressed every preliminary replica
    stage = runner.tags.load_snapshot(tenant, run_id)
    item = stage["claims"][0]
    assert item["status"] == "blocked" and item["reason"] == "PRELIMINARY_TAGS_UNRESOLVED"
    assert item["preliminary_records"][0]["stable_reason"]["category"] == "never_sent"
    # And the proof agrees that nothing was billed for the suppressed attempt.
    proof = recovery_proof(runner.store.usage.cost_data(tenant, run_id), receipts)
    assert proof["class_counts"].get("never_sent") == 1
    assert proof["classes"]["receipt_failure_without_ledger"] == [failure]
    assert proof["durable_failures"]["preliminary"] == [failure]


def test_authorized_recovery_carries_the_stage_and_reissues_only_never_sent(tmp_path, monkeypatch):
    """The whole recovery path end to end, over a real run, with no paid call.

    The stopped stage commits first, then one acknowledged bounded authorization
    re-issues exactly the suppressed claim while every other claim is carried
    forward verbatim. The acknowledged stop, its receipt and the old ledger rows are
    all left as they are.
    """
    from proofops_worker.tag_recovery import authorize_recovery, plan_recovery

    ctx, runner, tenant, run_id, receipts, failure = _stopped_live_run(tmp_path, monkeypatch)
    calls = ctx["calls"]
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "blocked"
    suppressed_calls = len(calls)
    lineage = runner.store.jobs.get_run(tenant, run_id)
    lineage_job = lineage["tag_job"]["job_id"]
    lineage_sha = lineage["tag_snapshot_sha256"]
    lineage_stage = runner.tags.load_snapshot(tenant, run_id)
    stop = receipts / "preliminary" / "transport-stop.json"
    stop_bytes = stop.read_bytes()
    ledger_before = runner.store.usage.cost_data(tenant, run_id)

    # An unacknowledged unresolved failure blocks authorization outright.
    with pytest.raises(RecoveryRejected, match="ACKNOWLEDGEMENT_REQUIRED"):
        plan_recovery(
            runner.store,
            runner.tags,
            tenant_id=tenant,
            run_id=run_id,
            receipts=receipts,
            claim_limit=1,
            max_new_requests=9,
            acknowledged_failures=frozenset(),
            authorized_by="operator",
            now=int(ctx["now"][0]),
        )
    # So does acknowledging something that is not actually unresolved.
    with pytest.raises(RecoveryRejected, match="ACKNOWLEDGEMENT_UNKNOWN"):
        plan_recovery(
            runner.store,
            runner.tags,
            tenant_id=tenant,
            run_id=run_id,
            receipts=receipts,
            claim_limit=1,
            max_new_requests=9,
            acknowledged_failures=frozenset({failure, str(UUID(int=7777))}),
            authorized_by="operator",
            now=int(ctx["now"][0]),
        )

    plan, proof, run = plan_recovery(
        runner.store,
        runner.tags,
        tenant_id=tenant,
        run_id=run_id,
        receipts=receipts,
        claim_limit=1,
        max_new_requests=9,
        acknowledged_failures=frozenset({failure}),
        authorized_by="operator",
        now=int(ctx["now"][0]),
    )
    assert plan.lineage_job_id == lineage_job
    assert plan.lineage_checkpoint_sha256 == lineage_sha
    assert plan.recovery_job_id != lineage_job
    assert len(plan.claim_ids) == 1
    assert plan.acknowledged_failures == (failure,)

    message = authorize_recovery(runner.store, plan, proof, run, now=int(ctx["now"][0]))
    assert message.stage == "tag" and message.shard == plan.shard

    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    # Real new work happened, bounded, and only for the claim that was never sent.
    assert len(calls) > suppressed_calls
    stage = runner.tags.load_snapshot(tenant, run_id)
    assert [item["claim_id"] for item in stage["claims"]] == [
        item["claim_id"] for item in lineage_stage["claims"]
    ]
    assert stage["recovery"]["lineage_job_id"] == lineage_job
    assert stage["recovery"]["attempted_claims"] == sorted(plan.claim_ids)
    recovered = next(i for i in stage["claims"] if i["claim_id"] == plan.claim_ids[0])
    assert recovered["review_inputs"] is not None
    assert len(recovered["preliminary_records"]) == 3
    assert all(r["status"] == "validated_candidate" for r in recovered["preliminary_records"])

    # Nothing immutable moved: the stop, its receipt, and every prior ledger row.
    assert stop.read_bytes() == stop_bytes
    assert (receipts / "preliminary" / failure / "response.json").exists()
    after = {
        row["reservation"]["call"]["request_id"]: row
        for row in runner.store.usage.cost_data(tenant, run_id)
    }
    for row in ledger_before:
        request_id = row["reservation"]["call"]["request_id"]
        assert after[request_id] == row  # byte-identical: no row rewritten or retried
    # The suppressed request_id was never re-reserved; the new ones are genuinely new.
    assert len(after) > len(ledger_before)
    for row in after.values():
        assert row["reservation"]["call"]["attempt"] == 1  # max_attempts=1 untouched


def test_recovery_refuses_a_superseded_lineage(tmp_path, monkeypatch):
    """An authorization is bound to the exact stage it was proven against."""
    from proofops_worker.tag_recovery import authorize_recovery, plan_recovery

    ctx, runner, tenant, run_id, receipts, failure = _stopped_live_run(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "blocked"
    plan, proof, run = plan_recovery(
        runner.store,
        runner.tags,
        tenant_id=tenant,
        run_id=run_id,
        receipts=receipts,
        claim_limit=1,
        max_new_requests=9,
        acknowledged_failures=frozenset({failure}),
        authorized_by="operator",
        now=int(ctx["now"][0]),
    )
    authorize_recovery(runner.store, plan, proof, run, now=int(ctx["now"][0]))
    # Rewrite the run's committed tag pointer to simulate a different later stage.
    with runner.store.jobs._transaction() as db:
        meta = runner.store.jobs._get(db, tenant, run_id, "run", "META")
        meta["tag_snapshot_sha256"] = "f" * 64
        runner.store.jobs._put(db, tenant, run_id, "run", "META", meta)
    with pytest.raises(RecoveryRejected, match="LINEAGE_SUPERSEDED"):
        runner.run_once(tenant_id=tenant, run_id=run_id)


def test_only_one_authorization_per_lineage_may_be_outstanding(tmp_path, monkeypatch):
    """A second authorization against the same committed stage is refused.

    Two authorizations for one lineage would enqueue two tag jobs, each carrying the
    same stage forward, each minting its own new requests and each arming its own
    allowance: the bound would silently double. The check runs inside the same
    transaction as the write, so two operators cannot both pass it.
    """
    from proofops_worker.tag_recovery import authorize_recovery, plan_recovery

    ctx, runner, tenant, run_id, receipts, failure = _stopped_live_run(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "blocked"

    def proposal():
        return plan_recovery(
            runner.store,
            runner.tags,
            tenant_id=tenant,
            run_id=run_id,
            receipts=receipts,
            claim_limit=1,
            max_new_requests=9,
            acknowledged_failures=frozenset({failure}),
            authorized_by="operator",
            now=int(ctx["now"][0]),
        )

    first_plan, first_proof, run = proposal()
    authorize_recovery(runner.store, first_plan, first_proof, run, now=int(ctx["now"][0]))

    # A distinct second authorization for the same lineage is refused while the
    # first job has not settled.
    second_plan, second_proof, run = proposal()
    assert second_plan.recovery_job_id != first_plan.recovery_job_id
    assert second_plan.lineage_checkpoint_sha256 == first_plan.lineage_checkpoint_sha256
    with pytest.raises(RecoveryRejected, match="ALREADY_PENDING"):
        authorize_recovery(runner.store, second_plan, second_proof, run, now=int(ctx["now"][0]))
    # Nothing partial was written for the refused authorization.
    with runner.store.jobs._transaction() as db:
        assert (
            runner.store.jobs._raw(db, tenant, run_id, "tag_recovery", second_plan.recovery_job_id)
            is None
        )
        assert (
            runner.store.jobs._raw(
                db, tenant, run_id, "tag_recovery_proof", second_plan.recovery_job_id
            )
            is None
        )
        assert (
            runner.store.jobs._raw(db, tenant, run_id, "job", second_plan.recovery_job_id) is None
        )

    # Re-recording the very same authorization is refused too, not silently merged.
    with pytest.raises(RecoveryRejected, match="ALREADY_AUTHORIZED"):
        authorize_recovery(runner.store, first_plan, first_proof, run, now=int(ctx["now"][0]))

    # Once the first job settles the claim is published, so there is nothing left to
    # recover: a further authorization is refused rather than looping on paid work.
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    with pytest.raises(RecoveryRejected, match="NO_ELIGIBLE_CLAIM"):
        proposal()


def test_a_claim_can_never_need_more_than_the_reserved_nine_requests():
    """The per-claim reserve is exact, so the bound cannot strand a claim half-tagged.

    Three transport roots exist and each is driven by exactly one three-replica loop
    per claim, with the budget attempt pinned to 1 and no in-stage repair retry.
    """
    import inspect

    from proofops.application.tagging import service
    from proofops_worker import live_tagging
    from proofops_worker.tag_recovery import CLAIM_REQUEST_BUDGET, PREFIXES

    assert PREFIXES == ("preliminary", "elements", "relation")
    assert CLAIM_REQUEST_BUDGET == 9
    runtime_source = inspect.getsource(live_tagging)
    # One replica loop in the runtime (preliminary and relation share it) and one in
    # the element service; a third loop would break the ceiling.
    assert runtime_source.count("for replica in (1, 2, 3)") == 1
    element_source = inspect.getsource(service.tag_replicates)
    assert element_source.count("for replica in (1, 2, 3)") == 1
    assert element_source.count("invoke(model_request)") == 1
    assert (
        inspect.getsource(live_tagging.LiveTaggingRuntime.preliminary).count("_source_replicas(")
        == 1
    )
    assert (
        inspect.getsource(live_tagging.LiveTaggingRuntime.relations).count("_source_replicas(") == 1
    )


def test_recovery_does_not_rebuy_a_paid_replica_in_a_mixed_claim():
    from proofops_worker.tag_recovery import eligible_claims

    items = [
        dict(
            claim_id="mixed",
            status="blocked",
            reason="PRELIMINARY_TAGS_UNRESOLVED",
            preliminary_records=[dict(request_id="paid"), dict(request_id="suppressed")],
        )
    ]
    assert eligible_claims(
        items, published=set(), never_sent={"suppressed"}, unresolved=set(), limit=1
    ) == ([], [])
