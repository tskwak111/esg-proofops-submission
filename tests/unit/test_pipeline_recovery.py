"""Focused checks: manifest reuses existing receipts, never fabricates.

Verifies the behaviors this task cares about:
1. building the manifest does not silently invent claim/tag data for a
   company that only has a parse-level receipt (kepco).
2. every company's source hash matches the live PDF bytes on disk.
3. idempotent versioned writes never overwrite changed prior content.
"""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from evaluation import pipeline_recovery as recovery
from evaluation.pipeline_recovery import build_manifest, write_idempotent_versioned
from tests.acceptance.test_upload import pdf


@pytest.mark.skipif(
    not all(
        path.is_file()
        for path in (
            recovery.FIVE_REPORT_COMPARISON,
            recovery.HEADER_ROLE_FROZEN_INPUTS,
            recovery.HEADER_ROLE_EVAL,
            recovery.SCOPE_FLOW_BEFORE,
            recovery.SCOPE_FLOW_AFTER,
        )
    ),
    reason="historical five-company corpus and run receipts are local-only",
)
def test_manifest_marks_kepco_parse_only_and_verifies_present_hashes():
    manifest = build_manifest()
    by_slug = {c["slug"]: c for c in manifest["companies"]}

    assert set(manifest["requested_companies"]) == {"lotte", "kia", "kb", "naver", "kepco"}

    # kepco only has a parse-level candidate snapshot, no claim/tag receipt;
    # it must be reported parse_only with EXTRACT/TAG explicitly not_run,
    # never defaulted to 0 (which would misleadingly read as "ran, found none").
    assert by_slug["kepco"]["status"] == "parse_only"
    assert by_slug["kepco"]["reason"]
    assert by_slug["kepco"]["stage_counts"]["EXTRACT"] == "not_run"
    assert by_slug["kepco"]["stage_counts"]["TAG"] == "not_run"
    assert by_slug["kepco"]["stage_counts"]["PARSE"]["table_count"] == 3

    # every company reported "ok" must carry a real, non-empty source hash
    # that this build recomputed from the actual file, plus at least one
    # stage-progression signal recovered from an existing receipt (not
    # invented precision/recall), and explicit per-page prior-exposure
    # tracking rather than a blanket "same heldout pages" claim.
    for slug in ("lotte", "kia", "kb", "naver"):
        company = by_slug[slug]
        assert company["status"] == "ok"
        assert len(company["source_sha256"]) == 64
        assert company["pages"], f"{slug} must keep its frozen page selection"
        assert company["stage_counts"], f"{slug} must carry real stage counts"
        assert company["receipt_source"]
        assert company["prior_exposure"], f"{slug} must track prior exposure explicitly"

    # kb's two runs really do share physical page 30; kia/naver's don't.
    # This is the specific overlap claim the manifest must get right instead
    # of asserting all three share "the same" patch-heldout pages.
    kb_other = by_slug["kb"]["prior_exposure"][1]
    kia_other = by_slug["kia"]["prior_exposure"][1]
    naver_other = by_slug["naver"]["prior_exposure"][1]
    assert kb_other["overlaps_this_baseline_pages"] is True
    assert kia_other["overlaps_this_baseline_pages"] is False
    assert naver_other["overlaps_this_baseline_pages"] is False

    # lotte and kia must carry the real same-page external-parser comparison
    # with concrete failure rows, not just an element-count match rate.
    lotte_api = by_slug["lotte"]["stage_counts"].get("api_comparison")
    assert lotte_api is not None
    assert lotte_api["known_failure_rows"]

    kia_api = by_slug["kia"]["stage_counts"].get("api_comparison")
    assert kia_api is not None
    assert kia_api["known_failure_rows"]
    assert kia_api["visual_adjudication"] != "completed"  # coordinator marked pending


def test_manifest_reads_temporary_receipts_and_rejects_source_drift(tmp_path, monkeypatch):
    # Use the real disk reader and digest checks without publishing the local corpus.
    root = recovery.ROOT
    for name, path in list(vars(recovery).items()):
        if isinstance(path, Path) and path.is_relative_to(root):
            monkeypatch.setattr(recovery, name, tmp_path / path.relative_to(root))

    empty = build_manifest()
    assert set(empty["companies_missing"]) == {"lotte", "kia", "kb", "naver", "kepco"}
    assert empty["companies_ok"] == empty["companies_parse_only"] == []

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    source = tmp_path / "synthetic.pdf"
    source.write_bytes(pdf())
    digest = sha256(source.read_bytes()).hexdigest()
    write(
        recovery.HEADER_ROLE_FROZEN_INPUTS,
        {
            "companies": [
                {
                    "slug": "kepco",
                    "source_path": str(source),
                    "source_sha256": digest,
                    "selected_physical_pages": [1],
                }
            ]
        },
    )
    write(recovery.HEADER_ROLE_EVAL, {"companies": {"kepco": {"table_count": 3}}})
    selections = {"kia": [24, 106, 128], "kb": [30, 46, 109], "naver": [90, 224, 236]}
    for slug, pages in selections.items():
        write(
            recovery.FIVE_REPORT_DIR / slug / "pilot.json",
            {
                "source_path": str(source),
                "source_sha256": digest,
                "selected_pages": pages,
            },
        )
    write(
        recovery.FIVE_REPORT_COMPARISON,
        {
            "results": [
                {"company": slug, "claims": 2, "source_verified": 1} for slug in selections
            ],
        },
    )
    write(
        recovery.GENERALIZATION_MANIFEST,
        {
            "companies": [
                {"slug": slug, "original_page": page}
                for slug, page in (("kia", 45), ("kb", 30), ("naver", 84))
            ]
        },
    )
    result = build_manifest()
    companies = {c["slug"]: c for c in result["companies"]}
    assert result["companies_missing"] == ["lotte"]
    assert result["companies_parse_only"] == ["kepco"]
    assert result["companies_ok"] == ["kia", "kb", "naver"]
    assert companies["kepco"]["stage_counts"]["PARSE"]["table_count"] == 3
    assert companies["kepco"]["stage_counts"]["EXTRACT"] == "not_run"
    assert companies["kepco"]["stage_counts"]["TAG"] == "not_run"
    for slug in (*selections, "kepco"):
        assert companies[slug]["source_sha256"] == digest
    for slug, pages in selections.items():
        assert companies[slug]["pages"] == pages
        assert companies[slug]["stage_counts"]["claims"] == 2
        assert companies[slug]["stage_counts"]["source_verified"] == 1
        assert companies[slug]["prior_exposure"][1]["overlaps_this_baseline_pages"] == (
            slug == "kb"
        )
    source.write_bytes(pdf(pages=2))
    with pytest.raises(ValueError, match="source hash changed"):
        build_manifest()
    source.unlink()
    with pytest.raises(FileNotFoundError, match="source PDF missing"):
        build_manifest()


def test_idempotent_versioned_write_never_overwrites_changed_content(tmp_path):
    target = tmp_path / "manifest.json"

    first = write_idempotent_versioned(target, "v1-content")
    assert first == target
    assert target.read_text() == "v1-content"

    # identical content re-write is a true no-op on the same path
    again = write_idempotent_versioned(target, "v1-content")
    assert again == target
    assert target.read_text() == "v1-content"

    # changed content must go to a new sibling, leaving v1 untouched
    second = write_idempotent_versioned(target, "v2-content")
    assert second == tmp_path / "manifest.v2.json"
    assert target.read_text() == "v1-content"
    assert second.read_text() == "v2-content"

    # a third distinct change goes to v3, both prior files stay intact
    third = write_idempotent_versioned(target, "v3-content")
    assert third == tmp_path / "manifest.v3.json"
    assert target.read_text() == "v1-content"
    assert second.read_text() == "v2-content"
    assert third.read_text() == "v3-content"
