"""Opt-in structural repair keeps original artifacts and immutable replay."""

import json
from dataclasses import replace

import pytest
from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
from proofops.application.ingest.graph_fusion import ParserProfile

from tests.acceptance.test_parsing import JAVA, MANIFEST, TENANT, pdf, source


def test_repair_is_versioned_and_preserves_original_and_replay(tmp_path):
    base = ParserProfile(MANIFEST, java_executable=JAVA, table_auxiliary=False)
    assert "table_structure_repair" not in base.config_snapshot()
    assert hasattr(base, "table_structure_repair"), "repair needs a versioned opt-in"
    profile = replace(base, table_structure_repair="odl_header_v1")
    assert profile.config_hash() != base.config_hash()
    with pytest.raises(ValueError):
        replace(base, table_structure_repair="unknown")
    item = source(pdf(table=True))
    parser = OpenDataLoaderParser(tmp_path)
    graph = parser.parse(item, profile, tenant_id=TENANT)
    directory = tmp_path / TENANT / item.document_version_id / MANIFEST
    assert json.loads((directory / "source.json").read_bytes())
    assert (directory / "source-repaired.json").exists()
    assert (directory / "table-repair.json").exists()
    assert parser.load_verified(item, profile, tenant_id=TENANT) == graph
    path = directory / "table-repair.json"
    path.chmod(0o600)
    path.write_text("{}")
    with pytest.raises(ParseFailure, match="PARSER_ARTIFACT_INTEGRITY_MISMATCH"):
        parser.load_verified(item, profile, tenant_id=TENANT)
