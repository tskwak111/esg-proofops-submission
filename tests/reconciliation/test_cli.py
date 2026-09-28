"""Standalone B CLI boundary tests; no network or model calls."""

import json

import pytest

from evaluation.reconciliation_cli import InputRejected, load_json, main, write_json


def test_projection_does_not_invent_explanation_or_confidence():
    from evaluation.reconciliation_cli import project_result

    result = {
        "item": "C1",
        "claim_id": "claim",
        "execution_state": "completed",
        "status": "matched",
        "explanation_source_id": None,
        "source_ids": [],
        "synthetic": True,
    }
    projection = project_result(result, {"sources": []}, {})
    assert projection["confidence"] is None
    assert projection["explanation_present"] is False
    assert projection["explanation_location"] is None
    assert projection["allowed_difference_type"] is None
    assert "evidence_grade" not in projection


def test_projection_includes_only_selected_verified_candidate():
    from evaluation.reconciliation_cli import project_result

    source = {"source_id": "e", "document_id": "doc", "quote": "explanation"}
    result = {
        "execution_state": "completed",
        "status": "matched",
        "source_ids": ["e"],
        "explanation_source_id": "e",
    }
    view = project_result(result, {"sources": []}, {"doc": {}}, [source])
    assert view["explanation_location"] == source
    assert view["source_refs"][0]["verification_state"] == "verified"
    result["execution_state"] = "blocked"
    view = project_result(result, {"sources": []}, {"doc": {}}, [source])
    assert view["explanation_location"] is None
    assert view["source_refs"][0]["verification_state"] == "not_verified"


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e400}', "[]", "null"])
def test_rejects_ambiguous_json(tmp_path, payload):
    source = tmp_path / "input.json"
    source.write_text(payload, encoding="utf-8")
    with pytest.raises(InputRejected):
        load_json(source)


def test_utf8_and_exclusive_output(tmp_path):
    output = tmp_path / "result.json"
    write_json(output, {"text": "합성 원문"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"text": "합성 원문"}
    with pytest.raises(FileExistsError):
        write_json(output, {"text": "overwrite"})
    assert load_json(output)["text"] == "합성 원문"


def test_missing_input_is_exit2_without_traceback(tmp_path, capsys):
    code = main(
        [
            "--packet",
            str(tmp_path / "missing.json"),
            "--policy",
            str(tmp_path / "policy.json"),
            "--output",
            str(tmp_path / "output.json"),
        ]
    )
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err
    assert not (tmp_path / "output.json").exists()


def test_does_not_overwrite_input(tmp_path, capsys):
    source = tmp_path / "packet.json"
    source.write_text("{}", encoding="utf-8")
    code = main(["--packet", str(source), "--policy", str(source), "--output", str(source)])
    assert code == 2
    assert source.read_text(encoding="utf-8") == "{}"


def test_help():
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
