"""Repeatable, bounded preliminary diagnostics; never publish service decisions."""

import json
from dataclasses import replace

import pytest

from tests.acceptance.test_binding import corpus
from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary import payload


class Probe:
    model = "solar-pro4"

    def __init__(self, response):
        self.response, self.calls = response, []

    def complete(self, system, user, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return {
            "content": json.dumps(self.response),
            "provider_request_id": kwargs["request_id"],
            "input_tokens": 100,
            "output_tokens": 30,
        }


def evaluate(tmp_path, probe, *, claims=None):
    from evaluation.preliminary_probe import evaluate_preliminary

    graph, claim, _ = corpus()
    return evaluate_preliminary(
        probe,
        graph,
        (claim,) if claims is None else claims,
        tenant_id=TENANT,
        output=tmp_path / "receipts",
        max_claims=1,
        authorization="explicit local evaluation authorization",
    )


def test_three_independent_receipts_and_no_published_decision(tmp_path):
    _, claim, _ = corpus()
    probe = Probe(payload(claim))
    result = evaluate(tmp_path, probe)
    assert len(probe.calls) == 3
    assert len({c["request_id"] for c in probe.calls}) == 3
    assert result["published"] is False and result["gold_accuracy"] is None
    assert result["claims"][0]["status"] == "consistent_candidate"
    assert result["claims"][0]["replicas_valid"] == 3
    for call in probe.calls:
        folder = tmp_path / "receipts" / call["request_id"]
        assert all(
            (folder / name).is_file()
            for name in ("request.json", "response.json", "validation.json")
        )
    with pytest.raises(FileExistsError):
        evaluate(tmp_path, probe)
    assert len(probe.calls) == 3


def test_invalid_model_content_is_retained_and_not_confirmed(tmp_path):
    probe = Probe({"grade": "E3"})
    result = evaluate(tmp_path, probe)
    assert result["claims"][0]["status"] == "needs_review"
    assert result["claims"][0]["replicas_valid"] == 0
    assert len(probe.calls) == 3


def test_transport_failure_stops_remaining_replicas_and_survives_restart(tmp_path):
    probe = Probe(TimeoutError("must not leak secret exception text"))
    result = evaluate(tmp_path, probe)
    assert result["stopped"] is True and len(probe.calls) == 1
    assert "must not leak" not in (tmp_path / "receipts" / "summary.json").read_text()
    with pytest.raises(FileExistsError):
        evaluate(tmp_path, probe)
    assert len(probe.calls) == 1


def test_unverified_claims_are_unresolved_without_calls(tmp_path):
    _, claim, _ = corpus()
    probe = Probe(payload(claim))
    result = evaluate(tmp_path, probe, claims=(replace(claim, source_quality="unverified"),))
    assert probe.calls == []
    assert result["unverified_claims"] == 1


def test_consistent_null_track_still_needs_review(tmp_path):
    _, claim, _ = corpus()
    probe = Probe(payload(claim) | dict(track=None, track_confidence=None))
    assert evaluate(tmp_path, probe)["claims"][0]["status"] == "needs_review"


def test_provider_replay_cannot_count_as_three_votes(tmp_path):
    _, claim, _ = corpus()

    class Replayed(Probe):
        def complete(self, *args, **kwargs):
            return super().complete(*args, **kwargs) | {"provider_request_id": "same-id"}

    assert evaluate(tmp_path, Replayed(payload(claim)))["claims"][0]["status"] == "needs_review"


def test_source_mismatch_blocks_before_receipts_or_spend(tmp_path):
    _, claim, _ = corpus()
    probe = Probe(payload(claim))
    with pytest.raises(ValueError, match="SOURCE_MISMATCH"):
        evaluate(tmp_path, probe, claims=(replace(claim, source_sha256="0" * 64),))
    assert not probe.calls and not (tmp_path / "receipts").exists()


def test_track_disagreement_is_not_majority_confirmation(tmp_path):
    _, claim, _ = corpus()

    class Disagreeing(Probe):
        def complete(self, *args, **kwargs):
            if len(self.calls) == 2:
                self.response = self.response | dict(track="management")
            return super().complete(*args, **kwargs)

    result = evaluate(tmp_path, Disagreeing(payload(claim)))
    assert result["claims"][0]["replicas_valid"] == 3
    assert result["claims"][0]["status"] == "needs_review"


def test_cli_missing_key_fails_before_loading_source(tmp_path, monkeypatch, capsys):
    from evaluation.preliminary_probe import main

    key_file = tmp_path / "empty.env"
    key_file.write_text("UNRELATED=private-value\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "probe",
            "--state",
            str(tmp_path / "absent"),
            "--output",
            str(tmp_path / "out"),
            "--key-file",
            str(key_file),
            "--invoke",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "UPSTAGE_API_KEY is missing" in output and "private-value" not in output
