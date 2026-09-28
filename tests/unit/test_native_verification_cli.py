"""Opt-in native verification is limited to parsing, without model calls."""

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.parametrize("stage", ["parse", "extract", "tag"])
def test_native_verification_cli_rejects_nonparse_before_composition(monkeypatch, stage):
    from proofops_worker import main

    calls = []
    runner = SimpleNamespace(
        run_once=lambda **kwargs: "committed",
        uploads=SimpleNamespace(close=lambda: None, registry=SimpleNamespace(close=lambda: None)),
    )
    monkeypatch.setattr(main, "build_composition", lambda **kw: calls.append(kw) or runner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "worker",
            "--once",
            "--tenant-id",
            str(uuid4()),
            "--run-id",
            str(uuid4()),
            "--stage",
            stage,
            "--verify-paragraphs",
        ],
    )
    if stage == "parse":
        main.main()
        assert calls == [
            dict(
                stage="parse",
                review_table_notes=False,
                verify_paragraphs=True,
                native_typography_tolerance=False,
                raster_ocr=False,
            )
        ]
    else:
        with pytest.raises(SystemExit) as error:
            main.main()
        assert error.value.code == 1
        assert not calls


@pytest.mark.parametrize("stage,flag", [("extract", True), ("tag", True), ("parse", "true")])
def test_native_verification_composition_requires_explicit_boolean_parse(stage, flag):
    from proofops_worker.composition import build_composition

    with pytest.raises(ValueError, match="NATIVE_PARAGRAPHS_REQUIRE_PARSE_STAGE"):
        build_composition(stage=stage, verify_paragraphs=flag)


def test_raster_ocr_cli_forwards_explicit_native_parse_opt_in(monkeypatch):
    from proofops_worker import main

    calls = []
    runner = SimpleNamespace(
        run_once=lambda **kwargs: "committed",
        uploads=SimpleNamespace(close=lambda: None, registry=SimpleNamespace(close=lambda: None)),
    )
    monkeypatch.setattr(main, "build_composition", lambda **kw: calls.append(kw) or runner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "worker",
            "--once",
            "--tenant-id",
            str(uuid4()),
            "--run-id",
            str(uuid4()),
            "--verify-paragraphs",
            "--raster-ocr",
        ],
    )

    main.main()

    assert calls == [
        dict(
            stage="parse",
            review_table_notes=False,
            verify_paragraphs=True,
            native_typography_tolerance=False,
            raster_ocr=True,
        )
    ]


def test_raster_ocr_cli_rejects_nonparse_before_composition(monkeypatch):
    from proofops_worker import main

    called = False

    def composition(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(main, "build_composition", composition)
    monkeypatch.setattr(
        "sys.argv",
        [
            "worker",
            "--once",
            "--tenant-id",
            str(uuid4()),
            "--run-id",
            str(uuid4()),
            "--stage",
            "extract",
            "--verify-paragraphs",
            "--raster-ocr",
        ],
    )

    with pytest.raises(SystemExit) as error:
        main.main()

    assert error.value.code == 1
    assert not called
