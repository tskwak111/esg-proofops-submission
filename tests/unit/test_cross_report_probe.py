"""Offline safety checks for the bounded paid evaluation runner."""

import json
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from evaluation import cross_report_probe as probe


@pytest.mark.parametrize(
    "error",
    [
        "BUDGET_EXHAUSTED",
        "DUPLICATE_PROBE_REQUEST",
        "PRICE_RECHECK_REQUIRED",
        "UPSTAGE_HTTP_429",
        "UPSTAGE_HTTP_503",
        "UPSTAGE_REQUEST_FAILED",
        "UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED",
    ],
)
def test_probe_stops_after_first_shared_budget_or_provider_stop(tmp_path, monkeypatch, error):
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"source")
    (tmp_path / "sample.pdf").write_bytes(b"subset")
    (tmp_path / "selection.json").write_text(
        json.dumps(
            dict(
                source_path=str(pdf),
                source_sha256=sha256(b"source").hexdigest(),
                subset_sha256=sha256(b"subset").hexdigest(),
                manifest="unused",
                pages=[1],
            )
        )
    )
    ledger = tmp_path / ".local/upstage/budget.sqlite3"
    ledger.parent.mkdir(parents=True)
    ledger.touch()
    (tmp_path / ".env.upstage.local").write_text("UPSTAGE_API_KEY=offline-test\n")
    blocks = [
        SimpleNamespace(
            source_id=str(uuid4()), page_num=1, bbox=None, kind="paragraph", normalized_text=str(n)
        )
        for n in range(2)
    ]
    graph = SimpleNamespace(
        tenant_id=str(uuid4()),
        document_version_id=str(uuid4()),
        parse_manifest_id=str(uuid4()),
        source_sha256=sha256(b"source").hexdigest(),
        blocks=blocks,
    )
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "load_graph", lambda *_: graph)
    monkeypatch.setattr(
        probe, "select_stable_paragraph_sources", lambda *_: {b.source_id for b in blocks}
    )
    monkeypatch.setattr(probe, "UpstageProbe", lambda *_: None)

    @dataclass
    class Profile:
        version: str = "offline"

    calls = []

    class Extractor:
        profile = Profile()
        usage = {}

        def __init__(self, *_):
            pass

        def extract(self, packet):
            calls.append(packet)
            raise ValueError(error)

    monkeypatch.setattr(probe, "UpstageClaimExtractor", Extractor)
    result = probe.run(tmp_path, "extract", invoke=True)
    assert len(calls) == 1
    assert result["status"] == "stopped" and result["error"] == error
    assert result["selected_count"] == 2 and result["processed_count"] == 0
    assert result["attempted_count"] == 1
    assert result["unprocessed_count"] == 1
    assert result["unknown_count"] == 2
    assert result["processed_count"] + result["unknown_count"] == result["selected_count"]
    assert len(list(tmp_path.glob("extract-*/outcome-*.json"))) == 1
    pdf.write_bytes(b"changed")
    with pytest.raises(ValueError, match="original source changed"):
        probe.run(tmp_path, "extract", invoke=True)
    assert len(calls) == 1
