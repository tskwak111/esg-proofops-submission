import pytest

from evaluation.review_benchmark import freeze_cases, score_review


def packet():
    return freeze_cases(
        [
            dict(
                case_id="a",
                company_id="one",
                task="table",
                source_sha256="a" * 64,
                page=1,
                source={"text": "2024 목표 95.0%"},
            ),
            dict(
                case_id="b",
                company_id="two",
                task="link",
                source_sha256="b" * 64,
                page=2,
                source={"text": "2024 실적 93.5%"},
            ),
        ]
    )


def test_missing_predictions_and_unreviewed_cases_are_visible():
    p = packet()
    review = dict(
        packet_sha256=p["packet_sha256"],
        reviewer_kind="agent",
        reviewer_id="test-agent",
        labels={"a": "target", "b": "actual"},
    )
    result = score_review(p, {"a": "target"}, review)
    assert result["exact_matches"] == 1 and result["reviewed_cases"] == 2
    assert result["agreement"] == 0.5 and result["missing_predictions"] == 1
    assert result["human_accuracy"] is None
    assert result["by_company"]["two"]["agreement"] == 0
    review["labels"]["b"] = None
    result = score_review(p, {"a": "target"}, review)
    assert result["reviewed_cases"] == 1 and result["unreviewed_cases"] == 1
    assert result["review_coverage"] == 0.5


def test_changed_source_or_foreign_case_is_rejected():
    p = packet()
    review = dict(
        packet_sha256=p["packet_sha256"],
        reviewer_kind="human",
        reviewer_id="fixture-only",
        labels={"a": "target", "b": None},
    )
    with pytest.raises(ValueError):
        score_review(p, {"foreign": "target"}, review)
    p["cases"][0]["source"]["text"] = "different source"
    with pytest.raises(ValueError):
        score_review(p, {}, review)


def test_review_html_cannot_execute_source_markup():
    from evaluation.review_benchmark import review_html

    cases = packet()["cases"]
    cases[0]["source"]["text"] = '</script><img src=x onerror="alert(1)">'
    html = review_html(freeze_cases(cases))
    assert "</script><img" not in html
    assert "\\u003c/script>" in html
