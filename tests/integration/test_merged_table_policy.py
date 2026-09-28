"""Both immutable table-verification policies remain addressable."""

import pytest
from proofops.adapters.local import table_source_verification as legacy
from proofops.adapters.parsing import opendataloader as parser


def test_verifier_selects_frozen_policy_and_rejects_unknown_hash():
    assert hasattr(parser, "_table_verifier")
    assert parser._table_verifier(legacy.policy_sha256()) is legacy
    with pytest.raises(parser.ParseFailure, match="TABLE_SOURCE_POLICY_MISMATCH"):
        parser._table_verifier("0" * 64)


def test_new_policy_roundtrips_in_parser_manifest(tmp_path, monkeypatch):
    import json

    from proofops.adapters.local import merged_table_verification as merged
    from proofops.application.ingest.graph_fusion import ParserProfile

    from tests.acceptance.test_parsing import JAVA, MANIFEST, TENANT, pdf, source
    from tests.integration.test_native_table_admission import ocr

    merged._attest_json.cache_clear()
    monkeypatch.setattr(merged, "_rendered_text", ocr)
    item = source(pdf(table=True))
    profile = ParserProfile(
        MANIFEST,
        java_executable=JAVA,
        table_structure_repair="odl_header_v2",
        table_source_policy_sha256=merged.policy_sha256(),
    )
    engine = parser.OpenDataLoaderParser(tmp_path)
    graph = engine.parse(item, profile, tenant_id=TENANT)
    assert engine.load_verified(item, profile, tenant_id=TENANT) == graph
    folder = tmp_path / TENANT / item.document_version_id / MANIFEST
    manifest = json.loads((folder / "manifest.json").read_bytes())
    assert manifest["fusion_version"] == 4
    assert (
        json.loads((folder / "table-source.json").read_bytes())["schema"]
        == "native_merged_table_source_v2"
    )
    assert parser._table_verifier(legacy.policy_sha256()) is legacy
