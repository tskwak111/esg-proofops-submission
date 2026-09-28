"""AT-032: local synthetic annotations; no customer data or model call."""

from __future__ import annotations

import pytest

TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "99999999-9999-4999-8999-999999999999"


def _manifest(*claim_ids: str, split: str = "holdout"):
    from evaluation.splits.company_split import DatasetItem, verify_split

    return verify_split(
        tuple(
            DatasetItem(TENANT, claim_id, f"company-{claim_id}", "2025", split, "gold")
            for claim_id in claim_ids
        ),
        tenant_id=TENANT,
    )


def test_company_split_isolated_across_reporting_years():
    from evaluation.splits.company_split import DatasetItem, verify_split

    items = (
        DatasetItem(TENANT, "acme-2024", "acme", "2024", "silver", "legacy_silver"),
        DatasetItem(TENANT, "acme-2025", "acme", "2025", "holdout", "independent_gold"),
    )

    with pytest.raises(ValueError, match="company.*multiple splits"):
        verify_split(items, tenant_id=TENANT)


def test_review_confirmed_data_can_only_enter_fewshot_not_holdout():
    from evaluation.splits.company_split import DatasetItem, verify_split

    reviewed = DatasetItem(
        TENANT,
        "review-1",
        "newco",
        "2025",
        "holdout",
        "review_confirmed",
    )

    with pytest.raises(ValueError, match="review-confirmed.*fewshot"):
        verify_split((reviewed,), tenant_id=TENANT)

    report = verify_split(
        (
            DatasetItem(
                TENANT,
                "review-1",
                "newco",
                "2025",
                "fewshot",
                "review_confirmed",
            ),
            DatasetItem(
                TENANT,
                "gold-1",
                "holdoutco",
                "2025",
                "holdout",
                "independent_gold",
            ),
        ),
        tenant_id=TENANT,
    )

    assert report.item_counts == (
        ("silver", 0),
        ("development", 0),
        ("fewshot", 1),
        ("validation", 0),
        ("holdout", 1),
    )
    assert report.company_counts == report.item_counts


def test_development_is_a_supported_company_split():
    from evaluation.splits.company_split import DatasetItem, verify_split

    report = verify_split(
        (DatasetItem(TENANT, "dev-1", "devco", "2025", "development", "curated"),),
        tenant_id=TENANT,
    )

    assert dict(report.item_counts)["development"] == 1


def test_split_rejects_duplicate_items_and_tenant_leaks():
    from evaluation.splits.company_split import DatasetItem, verify_split

    local = DatasetItem(TENANT, "same", "acme", "2025", "silver", "legacy_silver")
    foreign = DatasetItem(FOREIGN, "foreign", "other", "2025", "fewshot", "curated_fewshot")

    with pytest.raises(ValueError, match="duplicate item"):
        verify_split((local, local), tenant_id=TENANT)
    with pytest.raises(ValueError, match="tenant"):
        verify_split((local, foreign), tenant_id=TENANT)


def test_evaluation_reports_each_metric_with_its_real_denominator():
    from evaluation.metrics.elements import ElementFact
    from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset

    gold = GoldDataset(
        tenant_id=TENANT,
        dataset_id="synthetic-independent-gold-v1",
        split="holdout",
        elements=(
            ElementFact("c1", "P1", "10 tCO2e", True),
            ElementFact("c2", "G1", "2030", True),
        ),
        grades=(("c1", "E3"), ("c2", "E1"), ("c3", "E0")),
        labels=(
            ("c1", "SUBSTANTIATED"),
            ("c2", "INCOMPLETE"),
            ("c3", "UNSUBSTANTIATED"),
        ),
    )
    predictions = PredictionSet(
        tenant_id=TENANT,
        dataset_id=gold.dataset_id,
        split=gold.split,
        elements=(
            ElementFact("c1", "P1", "10 tCO2e", True),
            ElementFact("c2", "G1", "2050", True),
        ),
        grades=(("c1", "E3"), ("c2", "E2")),
        labels=(("c1", "SUBSTANTIATED"), ("c2", "INCOMPLETE")),
    )

    result = evaluate_dataset(predictions, gold, split_manifest=_manifest("c1", "c2", "c3"))
    metrics = {metric.name: metric for metric in result.metrics}

    assert (metrics["element_precision"].value, metrics["element_precision"].denominator) == (
        0.5,
        2,
    )
    assert (metrics["element_recall"].value, metrics["element_recall"].denominator) == (0.5, 2)
    assert metrics["grade_accuracy"].value == pytest.approx(1 / 3)
    assert metrics["grade_accuracy"].denominator == 3
    assert (
        metrics["grade_selective_accuracy"].value,
        metrics["grade_selective_accuracy"].denominator,
    ) == (
        0.5,
        2,
    )
    assert metrics["label_accuracy"].value == pytest.approx(2 / 3)
    assert metrics["label_accuracy"].denominator == 3
    assert (
        metrics["label_selective_accuracy"].value,
        metrics["label_selective_accuracy"].denominator,
    ) == (
        1.0,
        2,
    )
    assert metrics["automatic_coverage"].value == pytest.approx(2 / 3)
    assert metrics["automatic_coverage"].denominator == 3
    assert metrics["label_majority_baseline"].value == pytest.approx(1 / 3)
    assert result.tenant_id == TENANT
    assert result.dataset_id == gold.dataset_id
    assert result.split == "holdout"


def test_zero_denominators_are_null_not_zero_percent():
    from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset

    gold = GoldDataset(TENANT, "empty-synthetic-gold", "validation", (), (), ())
    predictions = PredictionSet(TENANT, gold.dataset_id, gold.split, (), (), ())

    result = evaluate_dataset(predictions, gold, split_manifest=_manifest(split="validation"))

    assert all(metric.value is None and metric.denominator == 0 for metric in result.metrics)


def test_evaluation_rejects_identity_mismatch_duplicates_and_unverified_gold():
    from evaluation.metrics.elements import ElementFact
    from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset

    gold = GoldDataset(
        TENANT,
        "synthetic-gold",
        "validation",
        (ElementFact("c1", "P1", "1 tCO2e", True),),
        (("c1", "E1"),),
        (("c1", "INCOMPLETE"),),
    )

    with pytest.raises(ValueError, match="identity"):
        evaluate_dataset(
            PredictionSet(FOREIGN, gold.dataset_id, gold.split, (), (), ()),
            gold,
            split_manifest=_manifest("c1", split="validation"),
        )
    with pytest.raises(ValueError, match="duplicate.*grade"):
        PredictionSet(
            TENANT,
            gold.dataset_id,
            gold.split,
            (),
            (("c1", "E1"), ("c1", "E2")),
            (),
        )
    prediction = ElementFact("c1", "P1", "1 tCO2e", False)
    assert prediction.valid_source_binding is False
    with pytest.raises(ValueError, match="gold.*verified source binding"):
        GoldDataset(
            TENANT,
            "synthetic-gold",
            "validation",
            (prediction,),
            (("c1", "E1"),),
            (("c1", "INCOMPLETE"),),
        )
    with pytest.raises(ValueError, match="strict boolean"):
        ElementFact("c1", "P1", "1 tCO2e", 0)  # type: ignore[arg-type]


def test_conflicting_values_for_one_element_are_rejected():
    from evaluation.metrics.elements import ElementFact
    from evaluation.metrics.pipeline import GoldDataset

    with pytest.raises(ValueError, match="duplicate element assignment"):
        GoldDataset(
            TENANT,
            "synthetic-gold",
            "validation",
            (
                ElementFact("c1", "P1", "1 tCO2e", True),
                ElementFact("c1", "P1", "2 tCO2e", True),
            ),
            (("c1", "E1"),),
            (("c1", "INCOMPLETE"),),
        )


def test_incorrect_prediction_binding_is_scored_as_false_positive_and_false_negative():
    from evaluation.metrics.elements import ElementFact
    from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset

    gold = GoldDataset(
        TENANT,
        "binding-gold",
        "holdout",
        (ElementFact("c1", "P1", "1 tCO2e", True),),
        (("c1", "E1"),),
        (("c1", "INCOMPLETE"),),
    )
    predictions = PredictionSet(
        TENANT,
        gold.dataset_id,
        gold.split,
        (ElementFact("c1", "P1", "1 tCO2e", False),),
        (("c1", "E1"),),
        (("c1", "INCOMPLETE"),),
    )

    metrics = {
        metric.name: metric
        for metric in evaluate_dataset(predictions, gold, split_manifest=_manifest("c1")).metrics
    }

    assert (metrics["element_precision"].value, metrics["element_precision"].denominator) == (
        0.0,
        1,
    )
    assert (metrics["element_recall"].value, metrics["element_recall"].denominator) == (
        0.0,
        1,
    )


def test_pending_gold_and_predictions_are_excluded_from_selective_scores():
    from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset

    gold = GoldDataset(
        TENANT,
        "rule-gap-gold",
        "holdout",
        (),
        (("c1", "E2"), ("c2", None), ("c3", "E0")),
        (("c1", "INCOMPLETE"), ("c2", None), ("c3", "UNSUBSTANTIATED")),
    )
    predictions = PredictionSet(
        TENANT,
        gold.dataset_id,
        gold.split,
        (),
        (("c1", "E1"), ("c2", "E0"), ("c3", None)),
        (("c1", "INCOMPLETE"), ("c2", "UNSUBSTANTIATED"), ("c3", None)),
    )

    result = evaluate_dataset(predictions, gold, split_manifest=_manifest("c1", "c2", "c3"))
    metrics = {metric.name: metric for metric in result.metrics}

    assert gold.grades[1] == ("c2", None)
    assert (
        metrics["grade_selective_accuracy"].value,
        metrics["grade_selective_accuracy"].denominator,
    ) == (0.0, 1)
    assert (
        metrics["label_selective_accuracy"].value,
        metrics["label_selective_accuracy"].denominator,
    ) == (1.0, 1)
    assert (metrics["grade_accuracy"].value, metrics["grade_accuracy"].denominator) == (0.0, 2)
    assert (metrics["label_accuracy"].value, metrics["label_accuracy"].denominator) == (0.5, 2)
    assert (metrics["automatic_coverage"].value, metrics["automatic_coverage"].denominator) == (
        pytest.approx(2 / 3),
        3,
    )
    assert result.ordinal_confusion.denominator == 1
    assert result.ordinal_confusion.matrix[2][1] == 1


def test_evaluation_requires_manifest_membership_and_rejects_unknown_element_claims():
    from evaluation.metrics.elements import ElementFact
    from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset

    gold = GoldDataset(
        TENANT,
        "manifest-gold",
        "holdout",
        (ElementFact("c1", "P1", "1 tCO2e", True),),
        (("c1", "E1"),),
        (("c1", "INCOMPLETE"),),
    )
    predictions = PredictionSet(
        TENANT,
        gold.dataset_id,
        gold.split,
        (ElementFact("outside", "P1", "1 tCO2e", True),),
        (("c1", "E1"),),
        (("c1", "INCOMPLETE"),),
    )

    with pytest.raises(ValueError, match="element claim.*fixed gold dataset"):
        evaluate_dataset(predictions, gold, split_manifest=_manifest("c1"))
    with pytest.raises(ValueError, match="manifest.*split"):
        evaluate_dataset(
            PredictionSet(
                TENANT, gold.dataset_id, gold.split, (), (("c1", "E1"),), (("c1", "INCOMPLETE"),)
            ),
            gold,
            split_manifest=_manifest("c1", split="validation"),
        )


def test_unprovided_upstream_stage_metrics_are_explicitly_not_run():
    from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset

    gold = GoldDataset(TENANT, "not-run-gold", "development", (), (), ())
    result = evaluate_dataset(
        PredictionSet(TENANT, gold.dataset_id, gold.split, (), (), ()),
        gold,
        split_manifest=_manifest(split="development"),
    )
    metrics = {metric.name: metric for metric in result.metrics}

    for name in (
        "claim_recall",
        "parsing_table_tuple_exact",
        "retrieval_recall_at_12",
        "assurance_covered_precision",
    ):
        assert (metrics[name].status, metrics[name].value, metrics[name].denominator) == (
            "not_run",
            None,
            0,
        )


def test_gold_snapshots_freeze_nested_labels_and_require_full_claim_denominator():
    from evaluation.metrics.elements import ElementFact
    from evaluation.metrics.pipeline import GoldDataset

    grades, labels = [["c1", "E1"]], [["c1", "INCOMPLETE"]]
    gold = GoldDataset(TENANT, "fixed", "validation", (), grades, labels)
    grades[0][1] = "E3"
    labels[0][1] = "SUBSTANTIATED"
    assert gold.grades == (("c1", "E1"),)
    assert gold.labels == (("c1", "INCOMPLETE"),)
    with pytest.raises(ValueError, match="claim.*grade"):
        GoldDataset(TENANT, "fixed", "validation", (ElementFact("c2", "P1", "1", True),), (), ())
