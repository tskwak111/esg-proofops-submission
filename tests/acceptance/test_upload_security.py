"""AT-038: real PDF bytes/processes and explicitly synthetic local object storage."""

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from proofops.application.uploads_security import (
    LocalUploadVault,
    PdfLimits,
    QuarantinedPdf,
    UploadRejected,
    verify_quarantined_pdf,
)
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)


def pdf(*, pages=1, action=None, password=None, bomb=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    if action and action[0] == "/URI":
        from pypdf.annotations import Link

        writer.add_annotation(0, Link(rect=(0, 0, 100, 100), url=action[2]))
    elif action:
        writer.root_object[NameObject("/OpenAction")] = DictionaryObject(
            {
                NameObject("/S"): NameObject(action[0]),
                NameObject(action[1]): TextStringObject(action[2]),
            }
        )
    if bomb:
        stream = DecodedStreamObject()
        stream.set_data(b" " * 2_000_000)
        writer.pages[0][NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    if password is not None:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def source(data):
    return QuarantinedPdf(
        tenant_id="00000000-0000-4000-8000-000000000001",
        document_version_id="00000000-0000-4000-8000-000000000003",
        object_version_id="object-1",
        content=data,
        expected_size=len(data),
        expected_sha256=sha256(data).hexdigest(),
    )


def test_verified_bytes_and_versions_are_preserved_and_promoted_immutably(tmp_path):
    item = source(pdf())
    vault = LocalUploadVault(tmp_path)
    result = vault.verify_and_promote(
        item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item
    assert result.page_count == 1
    path = vault.original_path(result)
    assert path.read_bytes() == item.content
    with ThreadPoolExecutor(max_workers=2) as pool:
        retries = list(
            pool.map(
                lambda _: vault.verify_and_promote(
                    item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
                ),
                range(2),
            )
        )
    assert retries == [result, result]
    different = replace(source(pdf(pages=2)), object_version_id="object-2")
    with pytest.raises(UploadRejected, match="VERSION_CONFLICT"):
        vault.verify_and_promote(
            different, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
        )
    assert path.read_bytes() == item.content


@pytest.mark.parametrize("kind,key", [("/URI", "/URI"), ("/JavaScript", "/JS"), ("/Launch", "/F")])
def test_active_actions_do_not_fetch_or_execute(kind, key, tmp_path):
    marker = tmp_path / "executed"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.05)
        payload = (
            f"http://127.0.0.1:{listener.getsockname()[1]}/private"
            if kind == "/URI"
            else f"touch {marker}"
        )
        item = source(pdf(action=(kind, key, payload)))
        if kind == "/URI":
            assert (
                LocalUploadVault(tmp_path / "vault")
                .verify_and_promote(
                    item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
                )
                .source
                == item
            )
        else:
            with pytest.raises(UploadRejected, match="PDF_INVALID"):
                LocalUploadVault(tmp_path / "vault").verify_and_promote(
                    item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
                )
        with pytest.raises(TimeoutError):
            listener.accept()
    assert not marker.exists()
    assert len(list((tmp_path / "vault").rglob("*.pdf"))) == (1 if kind == "/URI" else 0)


@pytest.mark.parametrize(
    "data,limit,code",
    [
        (b"not PDF", PdfLimits(), "PDF_INVALID"),
        (b"%PDF-1.7\ninvalid", PdfLimits(), "PDF_INVALID"),
        (pdf(password="secret"), PdfLimits(), "PDF_PASSWORD_REQUIRED"),
        (pdf(password=""), PdfLimits(), "PDF_PASSWORD_REQUIRED"),
        (pdf(pages=2), PdfLimits(max_pages=1), "UPLOAD_LIMIT_EXCEEDED"),
        pytest.param(
            pdf(pages=301), PdfLimits(), "UPLOAD_LIMIT_EXCEEDED", id="configured-page-limit"
        ),
        (pdf(), PdfLimits(max_bytes=100), "UPLOAD_LIMIT_EXCEEDED"),
        (pdf(bomb=True), PdfLimits(max_decoded_bytes=1024), "UPLOAD_LIMIT_EXCEEDED"),
        (pdf(), PdfLimits(timeout_seconds=0.000001), "UPLOAD_LIMIT_EXCEEDED"),
        (pdf(), PdfLimits(memory_bytes=1), "UPLOAD_LIMIT_EXCEEDED"),
    ],
)
def test_rejected_sources_never_reach_original_vault(data, limit, code, tmp_path):
    with pytest.raises(UploadRejected, match=code):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(data), limit, tenant_id="00000000-0000-4000-8000-000000000001"
        )
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize(
    "changes,tenant,code",
    [
        ({}, "00000000-0000-4000-8000-000000000002", "NOT_FOUND"),
        (
            {"expected_sha256": "0" * 64},
            "00000000-0000-4000-8000-000000000001",
            "UPLOAD_INTEGRITY_MISMATCH",
        ),
        ({"expected_size": 1}, "00000000-0000-4000-8000-000000000001", "UPLOAD_INTEGRITY_MISMATCH"),
        (
            {"document_version_id": "../escape"},
            "00000000-0000-4000-8000-000000000001",
            "PDF_INVALID",
        ),
    ],
)
def test_identity_and_integrity_are_checked(changes, tenant, code):
    with pytest.raises(UploadRejected, match=code):
        verify_quarantined_pdf(replace(source(pdf()), **changes), PdfLimits(), tenant_id=tenant)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_pages": 0},
        {"timeout_seconds": float("nan")},
        {"max_bytes": True},
        {"memory_bytes": -1},
    ],
)
def test_limits_cannot_disable_guards(changes):
    with pytest.raises(ValueError):
        PdfLimits(**changes)


def test_fargate_template_has_enforced_resource_and_network_boundaries():
    import json
    import subprocess

    root = Path(__file__).resolve().parents[2]
    script = """
      import { buildQuarantineCompute } from './infra/cdk/lib/compute-stack.ts';
      const props = {vpcId:'vpc-1234abcd', endpointSecurityGroupIds:['sg-1234abcd'],
        s3PrefixListId:'pl-1234abcd', image:'example.invalid/verifier@sha256:'+'a'.repeat(64),
        executionRoleArn:'arn:aws:iam::123456789012:role/synthetic-execution'};
      const result = buildQuarantineCompute(props);
      for (const patch of [{image:'latest'}, {endpointSecurityGroupIds:[]},
                           {s3PrefixListId:'0.0.0.0/0'}]) {
        let rejected = false;
        try {buildQuarantineCompute({...props,...patch})} catch {rejected=true}
        if (!rejected) throw Error('Unsafe deployment config accepted');
      }
      console.log(JSON.stringify(result));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    resources = json.loads(result.stdout)["Resources"]
    task = resources["QuarantineTask"]["Properties"]
    container = task["ContainerDefinitions"][0]
    assert task["RequiresCompatibilities"] == ["FARGATE"]
    assert int(task["Cpu"]) == 2048 and int(task["Memory"]) == 8192
    assert container["User"] == "10001:10001"
    assert container["ReadonlyRootFilesystem"] is True
    assert container["LinuxParameters"]["Capabilities"]["Drop"] == ["ALL"]
    scratch = container["LinuxParameters"]["Tmpfs"][0]
    assert scratch["Size"] == 256
    assert {"noexec", "nosuid", "nodev"} <= set(scratch["MountOptions"])
    assert "TaskRoleArn" not in task
    egress = resources["QuarantineSecurityGroup"]["Properties"]["SecurityGroupEgress"]
    assert len(egress) == 2
    assert all(rule["FromPort"] == rule["ToPort"] == 443 for rule in egress)
    assert all("CidrIp" not in rule and "CidrIpv6" not in rule for rule in egress)


def test_indirect_action_subtype_in_unreachable_object_is_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    subtype = writer._add_object(NameObject("/Launch"))
    writer._add_object(
        DictionaryObject(
            {NameObject("/S"): subtype, NameObject("/F"): TextStringObject("never-execute")}
        )
    )
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        verify_quarantined_pdf(
            source(output.getvalue()), PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
        )


def test_opaque_storage_version_is_preserved_without_becoming_a_path():
    item = replace(source(pdf()), object_version_id="S3.version+/=opaque")
    assert (
        verify_quarantined_pdf(
            item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
        ).source
        == item
    )


def test_object_budget_is_enforced():
    with pytest.raises(UploadRejected, match="UPLOAD_LIMIT_EXCEEDED"):
        verify_quarantined_pdf(
            source(pdf()),
            PdfLimits(max_objects=2),
            tenant_id="00000000-0000-4000-8000-000000000001",
        )


# --- CORPUS-INGEST repair regression tests ---------------------------------
# These reproduce two real corpus findings (evidence/corpus-first-pass.md):
#  1. Samsung: an /AA additional-actions table whose only entry was an inert
#     /D -> /Named (in-viewer destination navigation) was rejected solely
#     because the /AA *key* was present, not because the action was dangerous.
#  2. KT: a document with legitimately shared/repeated object references
#     (e.g. a resource dict reachable from many pages) inflated the pop-based
#     `visited` counter past max_objects even though the number of distinct
#     nodes was well under budget.


def _writer_with_named_aa(trigger="/D", dest_name="NextPage"):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.root_object[NameObject("/AA")] = DictionaryObject(
        {
            NameObject(trigger): DictionaryObject(
                {
                    NameObject("/S"): NameObject("/Named"),
                    NameObject("/N"): NameObject(f"/{dest_name}"),
                }
            )
        }
    )
    return writer


def test_aa_with_inert_named_navigation_is_accepted(tmp_path):
    writer = _writer_with_named_aa()
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    result = LocalUploadVault(tmp_path).verify_and_promote(
        item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item


def test_aa_with_internal_goto_destination_is_accepted(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    page0_ref = writer.pages[0].indirect_reference
    writer.root_object[NameObject("/OpenAction")] = DictionaryObject(
        {
            NameObject("/S"): NameObject("/GoTo"),
            NameObject("/D"): ArrayObject([page0_ref, NameObject("/Fit")]),
        }
    )
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    result = LocalUploadVault(tmp_path).verify_and_promote(
        item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item


@pytest.mark.parametrize(
    "action_dict",
    [
        {"/S": "/JavaScript", "/JS": "app.alert(1)"},
        {"/S": "/Launch", "/F": "calc.exe"},
        {"/S": "/GoToR", "/F": "other.pdf", "/D": "/Page1"},
        {"/S": "/GoToE", "/D": "/Page1"},
        {"/S": "/SubmitForm", "/F": "https://example.invalid/submit"},
        {"/S": "/ImportData", "/F": "data.fdf"},
        {"/S": "/Rendition"},
        {"/S": "/Movie"},
        {"/S": "/SetOCGState"},
        {"/S": "/Named", "/N": "/SaveAs"},  # not on the inert allowlist
        {"/N": "/NextPage"},  # missing /S entirely
    ],
)
def test_aa_with_dangerous_or_unknown_action_is_rejected(action_dict, tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    entries = {
        NameObject(key): TextStringObject(value) if isinstance(value, str) else value
        for key, value in action_dict.items()
    }
    for key in list(entries):
        if key == "/S":
            entries[key] = NameObject(action_dict[key])
        elif key == "/N" and action_dict[key].startswith("/"):
            entries[key] = NameObject(action_dict[key])
    writer.root_object[NameObject("/AA")] = DictionaryObject(
        {NameObject("/D"): DictionaryObject(entries)}
    )
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()), PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
        )


def test_aa_next_chain_with_hidden_dangerous_action_is_rejected(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    dangerous_next = DictionaryObject(
        {NameObject("/S"): NameObject("/JavaScript"), NameObject("/JS"): TextStringObject("evil()")}
    )
    chained = DictionaryObject(
        {
            NameObject("/S"): NameObject("/Named"),
            NameObject("/N"): NameObject("/NextPage"),
            NameObject("/Next"): dangerous_next,
        }
    )
    writer.root_object[NameObject("/AA")] = DictionaryObject({NameObject("/D"): chained})
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()), PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
        )


def test_shared_object_fan_in_does_not_inflate_object_budget(tmp_path):
    # Many pages sharing one resource subtree must count once against
    # max_objects, not once per inbound reference (KT: visited > max_objects
    # on a legitimately-structured document with heavy sharing).
    writer = PdfWriter()
    shared = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/SharedRes")}))
    for _ in range(40):
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/SharedResource")] = shared
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    # Budget large enough for unique nodes (pages + shared dict + xref scaffolding)
    # but far smaller than pop-count would be if every page->shared reference
    # were counted as a distinct visit.
    result = LocalUploadVault(tmp_path).verify_and_promote(
        item, PdfLimits(max_objects=400), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item


def test_tagged_pdf_structure_element_attributes_are_not_treated_as_actions(tmp_path):
    # /A on a /StructElem names attribute objects (PDF 32000-1 14.7.6), not an
    # action. Accessible/tagged ESG reports commonly carry this and must not
    # be rejected as if /A were always an action trigger.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    attribute_dict = DictionaryObject(
        {NameObject("/O"): NameObject("/Layout"), NameObject("/Placement"): NameObject("/Block")}
    )
    struct_elem = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/StructElem"),
            NameObject("/S"): NameObject("/P"),
            NameObject("/A"): ArrayObject([attribute_dict]),
        }
    )
    writer._add_object(struct_elem)
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    result = LocalUploadVault(tmp_path).verify_and_promote(
        item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item


def test_shared_uri_action_via_a_and_openaction_still_rejects_on_openaction(tmp_path):
    # A /URI action reachable via annotation /A is user-gesture-gated and
    # inert data; the SAME shared action object also reachable via
    # /OpenAction auto-fires and must still be rejected -- regardless of
    # which object the xref scan happens to validate first.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    shared_uri_action = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Action"),
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("http://127.0.0.1:1/private"),
        }
    )
    shared_ref = writer._add_object(shared_uri_action)
    from pypdf.annotations import Link

    writer.add_annotation(0, Link(rect=(0, 0, 100, 100), url="http://127.0.0.1:1/private"))
    annots = writer.pages[0]["/Annots"]
    annot = annots[-1].get_object()
    annot[NameObject("/A")] = shared_ref
    writer.root_object[NameObject("/OpenAction")] = shared_ref
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()), PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
        )


@pytest.mark.parametrize("action_first", [True, False])
def test_indirect_uri_action_accepted_regardless_of_xref_scan_order(tmp_path, action_first):
    # The xref scan visits every indexed object independently of parent
    # order. A /URI action stored as an indirect object (discovered via the
    # generic /Type /Action fallback, possibly before or after its owning
    # annotation's /A is scanned) must be accepted either way: it is inert
    # stored data in every context.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    action = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Action"),
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("http://127.0.0.1:1/private"),
        }
    )
    if action_first:
        action = writer._add_object(action)
    from pypdf.annotations import Link

    writer.add_annotation(0, Link(rect=(0, 0, 100, 100), url="http://127.0.0.1:1/private"))
    if not action_first:
        action = writer._add_object(action)
    writer.pages[0]["/Annots"][-1].get_object()[NameObject("/A")] = action
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    result = LocalUploadVault(tmp_path).verify_and_promote(
        item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item


@pytest.mark.parametrize("action_first", [True, False])
@pytest.mark.parametrize("action_kind", ["print", "uri", "find"])
@pytest.mark.parametrize(
    "trigger",
    ["gesture", "open", "additional", "javascript_next", "D", "U", "Fo", "PO", "shared_widget"],
)
def test_gesture_actions_require_click_and_safe_chain(tmp_path, action_first, action_kind, trigger):
    """Report buttons are usable; automatic or executable chains stay rejected."""
    from pypdf.annotations import Link

    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    action = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Action"),
            NameObject("/S"): NameObject("/Named"),
            NameObject("/N"): NameObject("/Print"),
        }
    )
    if action_kind == "uri":
        action[NameObject("/S")] = NameObject("/URI")
        del action[NameObject("/N")]
        action[NameObject("/URI")] = TextStringObject("https://example.invalid")
    elif action_kind == "find":
        action[NameObject("/N")] = NameObject("/Find")
    if trigger == "javascript_next":
        action[NameObject("/Next")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/JavaScript"),
                NameObject("/JS"): TextStringObject("evil()"),
            }
        )
    shared = writer._add_object(action) if action_first else None
    writer.add_annotation(0, Link(rect=(0, 0, 100, 100), url="https://example.invalid"))
    if shared is None:
        shared = writer._add_object(action)
    annotation = writer.pages[0]["/Annots"][-1].get_object()
    annotation[NameObject("/A")] = shared
    if trigger == "open":
        writer.root_object[NameObject("/OpenAction")] = shared
    elif trigger == "additional":
        writer.root_object[NameObject("/AA")] = DictionaryObject({NameObject("/D"): shared})
    elif trigger in {"D", "U", "Fo", "PO", "shared_widget"}:
        del annotation[NameObject("/A")]
        annotation[NameObject("/Subtype")] = NameObject("/Widget")
        event = "/D" if trigger == "shared_widget" else "/" + trigger
        actions = writer._add_object(DictionaryObject({NameObject(event): shared}))
        annotation[NameObject("/AA")] = actions
        if trigger == "shared_widget":
            writer.root_object[NameObject("/AA")] = actions
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    vault = LocalUploadVault(tmp_path)
    if trigger in {"gesture", "D", "U"}:
        assert vault.verify_and_promote(item, PdfLimits(), tenant_id=item.tenant_id).source == item
    else:
        with pytest.raises(UploadRejected, match="PDF_INVALID"):
            vault.verify_and_promote(item, PdfLimits(), tenant_id=item.tenant_id)


# --- NEW-REPORT-FLOW repair regression tests -------------------------------
# Real corpus finding (LOTTE CHEMICAL 2025 ESG Report, non-encrypted, native
# text, 149 pages): the file carries /Hide actions whose bearers are all
# /Type /Annot /Subtype /Widget annotations triggered by a mouse-down /AA /D
# gesture (8 Hide->Hide /Next chains, 2 single /Hide). /Hide toggles visibility
# of this document's own form fields/annotations only -- no code, file, or
# network access. The free-floating /Type /Action xref scan reached these and
# rejected the whole document as PDF_INVALID solely because /Hide was on no
# allowlist, blocking UploadService.complete before any model call.
#
# Fix: /Hide is permitted ONLY for explicit user gestures (annotation/outline
# /A and Widget mouse down/up /AA D//U) and for the structural /Type /Action
# scan; it is NOT in inert_action_subtypes, so automatic /OpenAction and
# automatic /AA events carrying /Hide stay rejected. /T must name an annotation
# dict / text field-name string / array of those; optional /H is boolean; a
# /Next chain is walked exactly as before. Fixtures are generated (no real PDF
# committed).


def _hide_action(target="field.a", *, hidden=False, next_action=None, target_override=None):
    entries = {
        NameObject("/Type"): NameObject("/Action"),
        NameObject("/S"): NameObject("/Hide"),
        NameObject("/H"): BooleanObject(hidden),
    }
    if target_override is not None:
        entries[NameObject("/T")] = target_override
    elif target is not None:
        entries[NameObject("/T")] = TextStringObject(target)
    if next_action is not None:
        entries[NameObject("/Next")] = next_action
    return DictionaryObject(entries)


def _widget_with_down_action(writer, action):
    # Reproduce the real report's bearer: a /Widget annotation firing /Hide on
    # its mouse-down (/AA /D) user gesture.
    from pypdf.annotations import Link

    writer.add_annotation(0, Link(rect=(0, 0, 100, 100), url="https://example.invalid"))
    annotation = writer.pages[0]["/Annots"][-1].get_object()
    del annotation[NameObject("/A")]
    annotation[NameObject("/Subtype")] = NameObject("/Widget")
    annotation[NameObject("/AA")] = DictionaryObject({NameObject("/D"): action})
    return annotation


def test_hide_on_widget_mouse_down_gesture_is_accepted(tmp_path):
    # Positive regression: the exact real-report shape -- a /Widget /AA /D
    # gesture firing a Hide->Hide /Next chain over internal field names.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    chained = _hide_action(
        target="field.a",
        hidden=False,
        next_action=_hide_action(target="field.b", hidden=True),
    )
    _widget_with_down_action(writer, chained)
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    result = LocalUploadVault(tmp_path).verify_and_promote(
        item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item


def test_hide_targeting_array_of_own_fields_on_gesture_is_accepted(tmp_path):
    # /Hide /T may be an array of field-name strings; still fully internal and
    # still only under a user gesture.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    action = _hide_action(
        target=None,
        target_override=ArrayObject([TextStringObject("field.a"), TextStringObject("field.b")]),
    )
    _widget_with_down_action(writer, action)
    output = BytesIO()
    writer.write(output)
    item = source(output.getvalue())
    result = LocalUploadVault(tmp_path).verify_and_promote(
        item, PdfLimits(), tenant_id="00000000-0000-4000-8000-000000000001"
    )
    assert result.source == item


def test_automatic_openaction_hide_is_rejected(tmp_path):
    # Negative regression (core guard): /Hide fired automatically on document
    # open must stay rejected -- it is not inert-for-automatic-triggers.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.root_object[NameObject("/OpenAction")] = _hide_action(target="field.a")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()),
            PdfLimits(),
            tenant_id="00000000-0000-4000-8000-000000000001",
        )


def test_automatic_catalog_aa_hide_is_rejected(tmp_path):
    # Negative regression: /Hide in an automatic document-level /AA event
    # (not a Widget mouse gesture) must stay rejected.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.root_object[NameObject("/AA")] = DictionaryObject(
        {NameObject("/WC"): _hide_action(target="field.a")}
    )
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()),
            PdfLimits(),
            tenant_id="00000000-0000-4000-8000-000000000001",
        )


def test_hide_without_target_on_gesture_is_rejected(tmp_path):
    # Negative regression: a /Hide with no /T is malformed and stays rejected
    # even under a valid gesture trigger.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    _widget_with_down_action(writer, _hide_action(target=None))
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()),
            PdfLimits(),
            tenant_id="00000000-0000-4000-8000-000000000001",
        )


def test_hide_with_filespec_shaped_target_on_gesture_is_rejected(tmp_path):
    # Negative regression: a /Hide whose /T is a file-specification-shaped dict
    # (a /Filespec carrying /F) is not a legitimate in-document hide target and
    # must not be laundered through the /Hide allowance.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    filespec = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Filespec"),
            NameObject("/F"): TextStringObject("../../etc/passwd"),
        }
    )
    _widget_with_down_action(writer, _hide_action(target=None, target_override=filespec))
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()),
            PdfLimits(),
            tenant_id="00000000-0000-4000-8000-000000000001",
        )


def test_hide_chaining_to_dangerous_next_on_gesture_is_rejected(tmp_path):
    # Negative regression: /Hide is inert, but a dangerous action hidden behind
    # its /Next chain must still be rejected (fail-closed traversal unchanged).
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    dangerous_next = DictionaryObject(
        {
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject("evil()"),
        }
    )
    _widget_with_down_action(writer, _hide_action(target="field.a", next_action=dangerous_next))
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()),
            PdfLimits(),
            tenant_id="00000000-0000-4000-8000-000000000001",
        )


def test_hide_with_nonboolean_H_on_gesture_is_rejected(tmp_path):
    # Negative regression: /H must be a boolean when present.
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    action = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Action"),
            NameObject("/S"): NameObject("/Hide"),
            NameObject("/T"): TextStringObject("field.a"),
            NameObject("/H"): TextStringObject("true"),  # wrong type
        }
    )
    _widget_with_down_action(writer, action)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(UploadRejected, match="PDF_INVALID"):
        LocalUploadVault(tmp_path).verify_and_promote(
            source(output.getvalue()),
            PdfLimits(),
            tenant_id="00000000-0000-4000-8000-000000000001",
        )
