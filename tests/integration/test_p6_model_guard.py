"""A model's cross-facility conflict is a review candidate, not a computed P6."""

import json
from dataclasses import replace

from tests.acceptance.test_tagging import execute, setup


def test_model_p6_conflict_requires_deterministic_recheck(tmp_path):
    inputs = setup(tmp_path)
    original = inputs["invoke"]

    def conflicting(request):
        response = original(request)
        payload = json.loads(response.raw_response_json)
        next(e for e in payload["elements"] if e["element_id"] == "P6")["state"] = "conflict"
        return replace(response, raw_response_json=json.dumps(payload))

    inputs["invoke"] = conflicting
    runs = execute(inputs)
    for run in runs:
        p6 = next(e for e in run.guarded.elements if e.element_id == "P6")
        assert (
            next(
                e for e in json.loads(run.raw_response_json)["elements"] if e["element_id"] == "P6"
            )["state"]
            == "conflict"
        )
        assert p6.state == "unknown"
        assert "DETERMINISTIC_CHECK_REQUIRED:P6" in run.errors
