"""Read actual fence-published parser evidence through the fixed source/quality API."""

from dataclasses import replace
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from proofops.application.authorization import MembershipRecord

from tests.acceptance.test_upload import FOREIGN, TENANT
from tests.integration.test_local_parser_runner import runner_setup
from tests.integration.test_run_lifecycle import client, validate


def test_published_sources_quality_pagination_and_tenant_isolation(tmp_path, monkeypatch):
    from proofops_api.routers.sources import build_sources_router

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    http.app.include_router(
        build_sources_router(
            service.store, service.uploads, runner.parser, auth, clock=lambda: now[0]
        )
    )
    url = f"/v1/runs/{run_id}"
    assert http.get(url + "/quality").status_code == 409
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    graph = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    block = next(block for block in graph.blocks if block.raw_text == "1234 tCO2e")
    source_url = url + f"/sources/{block.source_id}"
    source = http.get(source_url)
    assert source.status_code == 200
    validate("SourceRef", source.json())
    assert source.json()["quote"] == "1234 tCO2e"
    assert source.json()["verification_state"] == "candidate"
    first = http.get(url + "/quality", params={"limit": 1}).json()
    validate("QualityIssuePage", first)
    assert first["next_cursor"]
    service.store.jobs.cancel_run(
        TENANT,
        run_id,
        expected_revision=service.get(TENANT, run_id)["revision"],
        idempotency_key=str(uuid4()),
        reason="Test immutable source pagination",
        actor_sub="synthetic-reviewer",
        now=now[0],
    )
    second = http.get(url + "/quality", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["snapshot_epoch"] == first["snapshot_epoch"]
    assert second["items"] != first["items"]
    assert http.get(url + "/quality", params={"cursor": first["next_cursor"]}).status_code == 400
    assert http.get(url + f"/sources/{uuid4()}").status_code == 404
    now[0] += 900
    assert (
        http.get(url + "/quality", params={"limit": 1, "cursor": first["next_cursor"]}).status_code
        == 400
    )
    artifact = (
        runner.parser.artifact_root
        / TENANT
        / graph.document_version_id
        / graph.parse_manifest_id
        / "graph.json"
    )
    artifact.chmod(0o600)
    artifact.write_text("[]")
    corrupt = http.get(source_url)
    assert corrupt.status_code == 409
    assert "1234" not in corrupt.text
    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=FOREIGN))
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "admin", "active"))
    assert http.get(source_url).status_code == 404
    assert http.get(url + "/quality").status_code == 404
    http.cookies.clear()
    assert http.get(source_url).status_code == 401


@pytest.mark.parametrize("invalid_geometry", [False, True])
def test_source_view_is_short_lived_user_scoped_and_serves_verified_original(
    tmp_path, monkeypatch, invalid_geometry
):
    from proofops_api.routers.sources import build_sources_router

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    if invalid_geometry:
        import json

        execute = runner.parser._execute

        def outside_crop(command, work, env, profile):
            execute(command, work, env, profile)
            path = work / "source.json"
            raw = json.loads(path.read_bytes())
            raw["kids"][0]["bounding box"] = [-10, 10, 100, 30]
            path.write_text(json.dumps(raw))

        monkeypatch.setattr(runner.parser, "_execute", outside_crop)
    http, auth = client(service)
    http.app.include_router(
        build_sources_router(
            service.store,
            service.uploads,
            runner.parser,
            auth,
            allowed_origin="https://testserver",
            clock=lambda: now[0],
        )
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    graph = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    block = graph.blocks[0]
    if invalid_geometry:
        block = next(b for b in graph.blocks if b.candidates[0].has_invalid_geometry)
        assert http.get(f"/v1/runs/{run_id}/sources/{block.source_id}").status_code == 409
    endpoint = f"/v1/runs/{run_id}/sources/{block.source_id}/view"
    assert http.post(endpoint, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    ticket_response = http.post(endpoint)
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()
    validate("Download", ticket)
    assert ticket["sha256"] == graph.source_sha256
    assert urlsplit(ticket["url"]).fragment == f"page={block.page_num}"
    fetched = http.get(ticket["url"])
    assert fetched.status_code == 200
    assert fetched.headers["Content-Type"] == "application/pdf"
    assert fetched.headers["Cache-Control"] == "no-store, private"
    assert fetched.headers["X-Content-Type-Options"] == "nosniff"
    assert fetched.headers["Referrer-Policy"] == "no-referrer"
    assert sha256(fetched.content).hexdigest() == graph.source_sha256
    assert fetched.content == service.uploads.read_original(TENANT, graph.document_version_id)

    parsed = urlsplit(ticket["url"])
    signed = parse_qs(parsed.query)["ticket"][0]
    preview = http.get(parsed.path, params={"ticket": signed, "preview": "page"})
    assert preview.status_code == 200
    assert preview.headers["Content-Type"] == "image/png"
    assert preview.headers["Cache-Control"] == "no-store, private"
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    assert preview.headers["Referrer-Policy"] == "no-referrer"
    assert float(preview.headers["X-Page-Width-Pt"]) > 0
    assert float(preview.headers["X-Page-Height-Pt"]) > 0
    assert preview.headers["X-Source-Highlight"] == "unavailable"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert sha256(http.get(ticket["url"]).content).hexdigest() == graph.source_sha256

    from proofops_api.request_limits import RequestLimit

    for _ in range(59):
        assert (
            auth.request_limits.consume(
                (RequestLimit("user", "admin-user", "source_view", 60),), now=now[0]
            )
            is None
        )
    assert http.post(endpoint, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    limited = http.post(endpoint)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    now[0] += 60

    assert (
        http.get(parsed.path, params={"ticket": "x" + signed, "preview": "page"}).status_code == 403
    )
    alternate = f"/local/sources/{run_id}/{uuid4()}"
    assert http.get(alternate, params={"ticket": signed, "preview": "page"}).status_code == 403
    session = auth.sessions.get("admin-session")
    auth.sessions.put(replace(session, user_sub="another-user"))
    auth.memberships.put(MembershipRecord(TENANT, "another-user", "admin", "active"))
    assert http.get(parsed.path, params={"ticket": signed, "preview": "page"}).status_code == 403
    auth.sessions.put(replace(session, active_tenant_id=FOREIGN))
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "admin", "active"))
    assert http.get(parsed.path, params={"ticket": signed, "preview": "page"}).status_code == 403
    assert http.post(endpoint).status_code == 404
    auth.sessions.put(session)
    now[0] += 300
    assert http.get(parsed.path, params={"ticket": signed, "preview": "page"}).status_code == 403
    renewed = http.post(endpoint).json()
    original = service.uploads.root / "original" / TENANT / f"{graph.document_version_id}.pdf"
    original.chmod(0o600)
    original.write_bytes(b"corrupted original")
    renewed_parsed = urlsplit(renewed["url"])
    assert (
        http.get(
            renewed_parsed.path,
            params={"ticket": parse_qs(renewed_parsed.query)["ticket"][0], "preview": "page"},
        ).status_code
        == 409
    )
    http.cookies.clear()
    assert http.get(renewed["url"]).status_code == 401


def test_page_preview_renders_cropbox_and_all_rotations_at_canonical_coordinates():
    from io import BytesIO

    from PIL import Image
    from proofops.adapters.parsing.source_preview import render_page_preview
    from proofops.application.ingest.geometry import affine_apply
    from proofops.domain.documents import PageGeometry
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject, RectangleObject

    writer = PdfWriter()
    for rotation in (0, 90, 180, 270):
        page = writer.add_blank_page(width=600, height=800)
        page.cropbox = RectangleObject([50, 100, 550, 750])
        page.rotate(rotation)
        stream = DecodedStreamObject()
        # The blue rectangle is outside CropBox and must not reach the preview.
        stream.set_data(b"0 0 1 rg 10 10 30 30 re f\n1 0 0 rg 100 200 40 40 re f")
        page[NameObject("/Contents")] = writer._add_object(stream)
    source = BytesIO()
    writer.write(source)

    for page_number, rotation in enumerate((0, 90, 180, 270), start=1):
        geometry = PageGeometry(
            *((500, 650) if rotation in (0, 180) else (650, 500)),
            rotation,
            (50, 100, 550, 750),
        )
        png, width_pt, height_pt = render_page_preview(source.getvalue(), page_number, geometry)
        image = Image.open(BytesIO(png)).convert("RGB")
        assert (width_pt, height_pt) == (geometry.width_pt, geometry.height_pt)
        assert image.width <= 2000 and image.height <= 2000
        assert image.width * image.height <= 4_000_000
        x, y = affine_apply(geometry.to_canonical_affine(), 120, 220)
        pixel = image.getpixel(
            (round(x * image.width / width_pt), round(y * image.height / height_pt))
        )
        assert pixel[0] > 180 and pixel[1] < 80 and pixel[2] < 80


def test_page_preview_uses_fixed_process_limits_and_returns_sanitized_failure(monkeypatch):
    import pytest
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
    from proofops.adapters.parsing.source_preview import SourcePreviewFailure, render_page_preview
    from proofops.domain.documents import PageGeometry

    def fail_within_preview_limits(command, work, env, profile):
        assert command[1] == "-I"
        assert profile.timeout_seconds == 15
        assert profile.memory_bytes == 512 * 1024 * 1024
        assert profile.max_output_bytes == 16 * 1024 * 1024
        assert env == {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        raise ParseFailure("PARSER_MEMORY_LIMIT")

    monkeypatch.setattr(OpenDataLoaderParser, "_execute", fail_within_preview_limits)
    with pytest.raises(SourcePreviewFailure, match="SOURCE_PREVIEW_FAILED"):
        render_page_preview(b"%PDF-1.7", 1, PageGeometry(10, 10, 0, (0, 0, 10, 10)))


def test_decimal_page_size_accepts_pdfium_rounding_but_rejects_geometry_mismatch():
    from io import BytesIO

    import pytest
    from proofops.adapters.parsing.source_preview import SourcePreviewFailure, render_page_preview
    from proofops.domain.documents import PageGeometry
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=841.92, height=1191.12)
    source = BytesIO()
    writer.write(source)
    geometry = PageGeometry(841.92, 1191.12, 0, (0, 0, 841.92, 1191.12))
    assert render_page_preview(source.getvalue(), 1, geometry)[0].startswith(b"\x89PNG")
    for wrong in (
        PageGeometry(841.93, 1191.12, 0, (0, 0, 841.93, 1191.12)),
        PageGeometry(841.92, 1191.12, 180, (0, 0, 841.92, 1191.12)),
    ):
        with pytest.raises(SourcePreviewFailure):
            render_page_preview(source.getvalue(), 1, wrong)


def test_quality_surfaces_unread_image_even_when_page_has_header_text(tmp_path, monkeypatch):
    from proofops_api.routers import sources

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    original = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    header = next(b for b in original.blocks if b.raw_text)
    empty_candidates = tuple(
        replace(c, source=replace(c.source, raw_text="", char_start=0, char_end=0))
        for c in header.candidates
    )
    image = replace(header, source_id=str(uuid4()), kind="figure", candidates=empty_candidates)
    graph = replace(original, blocks=original.blocks + (image,))
    before = graph.to_dict()
    monkeypatch.setattr(sources, "load_run_graph", lambda *a, **kw: graph)
    http, auth = client(service)
    http.app.include_router(
        sources.build_sources_router(
            service.store, service.uploads, runner.parser, auth, clock=lambda: now[0]
        )
    )
    response = http.get(f"/v1/runs/{run_id}/quality", params={"limit": 100})
    assert response.status_code == 200
    validate("QualityIssuePage", response.json())
    warnings = [i for i in response.json()["items"] if i["kind"] == "image_text_not_extracted"]
    assert len(warnings) == 1
    assert warnings[0]["source_ids"] == [image.source_id]
    assert warnings[0]["page_num"] == header.page_num
    assert warnings[0]["state"] == "open"  # not proof of unreadability or missing evidence
    assert graph.to_dict() == before
    assert warnings == [
        i
        for i in http.get(f"/v1/runs/{run_id}/quality", params={"limit": 100}).json()["items"]
        if i["kind"] == "image_text_not_extracted"
    ]
    graph = replace(graph, blocks=original.blocks + (replace(image, candidates=header.candidates),))
    assert not any(
        i["kind"] == "image_text_not_extracted"
        for i in http.get(f"/v1/runs/{run_id}/quality", params={"limit": 100}).json()["items"]
    )


def test_quality_exposes_unprocessed_extraction_spans_without_approving_them(tmp_path, monkeypatch):
    from proofops_agent.extraction import SyntheticClaimExtractor
    from proofops_api.routers.sources import build_sources_router

    from tests.integration.test_local_extract_runner import extraction_setup

    class EmptyExtractor(SyntheticClaimExtractor):
        def extract(self, packet):
            return {"spans": []}

    service, run_id, runner, now, _ = extraction_setup(
        tmp_path, monkeypatch, extractor=EmptyExtractor()
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    discovery = runner.claims.load(TENANT, run_id)
    expected = {e.source_id for e in discovery.exclusions if e.reason == "unprocessed_span"}
    assert expected
    before = service.store.jobs.get_run(TENANT, run_id)["claim_snapshot_sha256"]
    http, auth = client(service)
    http.app.include_router(
        build_sources_router(
            service.store, service.uploads, runner.parser, auth, clock=lambda: now[0]
        )
    )
    response = http.get(f"/v1/runs/{run_id}/quality", params={"limit": 100})
    assert response.status_code == 200
    validate("QualityIssuePage", response.json())
    items = [i for i in response.json()["items"] if i["kind"] == "extraction_span_unprocessed"]
    assert {s for i in items for s in i["source_ids"]} == expected
    assert all(i["state"] == "open" and "주장 여부" in i["reason"] for i in items)
    assert service.store.jobs.get_run(TENANT, run_id)["claim_snapshot_sha256"] == before
    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=FOREIGN))
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "admin", "active"))
    assert http.get(f"/v1/runs/{run_id}/quality").status_code == 404
