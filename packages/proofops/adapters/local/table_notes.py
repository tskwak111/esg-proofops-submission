"""Original-PDF note proposal validation; no approval or model transport."""

import re
from dataclasses import asdict

from proofops.adapters.local.table_layout_context import (
    group_words,
    table_layout_context,
    validate_word_geometry,
)
from proofops.domain.provenance import canonical_hash

CONTRACT = dict(version=3, context="same_page_native_pdf", ownership="model_proposed")
# Missing PDF word spacing must not turn a numbered qualification into unrestricted
# ownership. Bare decimals stay outside this grammar; native raised-marker proof is
# still required by marker_targets below.
COVERAGE_NOTE_START = re.compile(r"(?i)^(?:데이터\s*커버리지|data\s+coverage)\s*[:：]")
NUMBERED_NOTE_START = re.compile(r"^([1-9][0-9]?)[.)](?:\s+|(?=[^\W\d_])|(?=\d{4}년))")
KINDS = frozenset(
    {"scope", "aggregation", "methodology", "restatement", "unit", "other", "unknown"}
)


def _page_context(graph, source, page, tenant_id):
    import io
    from hashlib import sha256
    from importlib.metadata import version

    import pdfplumber

    from proofops.application.ingest.gri import _validate_graph

    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
    ):
        raise ValueError("layout source mismatch")
    with pdfplumber.open(io.BytesIO(source)) as document:
        if type(page) is not int or not 1 <= page <= len(document.pages):
            raise ValueError("invalid note page")
        native = document.pages[page - 1]
        if (
            native.rotation
            or tuple(native.bbox[:2]) != (0, 0)
            or tuple(native.cropbox) != tuple(native.mediabox)
        ):
            raise ValueError("layout geometry unsupported")
        words = native.extract_words(return_chars=True)
        validate_word_geometry(words, native.width, native.height)
        if len(words) > 1000:
            raise ValueError("layout word limit")
        return dict(
            reader="pdfplumber",
            reader_version=version("pdfplumber"),
            words=[
                dict(index=i, text=w["text"], bbox=[w["x0"], w["top"], w["x1"], w["bottom"]])
                for i, w in enumerate(words)
            ],
            unreadable_word_indices=[i for i, w in enumerate(words) if not w["upright"]],
            status="table_not_recognized",
            verified=False,
        )


def prepare(graph, source, table_ids, *, tenant_id, page=None, contract_version=3):
    if type(contract_version) is not int or contract_version not in (2, 3):
        raise ValueError("unsupported note contract")
    if page is not None and type(page) is not int:
        raise ValueError("invalid note page")
    if table_ids:
        layout = table_layout_context(
            graph, source, table_ids, tenant_id=tenant_id, allow_unresolved=True
        )
        if len({t["page"] for t in layout}) != 1 or (
            page is not None and page != layout[0]["page"]
        ):
            raise ValueError("one physical page per note packet")
        page = layout[0]["page"]
        first = layout[0]
    else:
        context = _page_context(graph, source, page, tenant_id)
        layout = []
        first = dict(words=[], page_context=context)
    blocks = {b.source_id: b for b in graph.blocks}
    # Include other tables on this page so repeated markers do not disappear from context.
    same_page = sorted(
        b.source_id for b in graph.blocks if b.kind == "table" and b.page_num == page
    )
    if same_page != sorted(table_ids):
        raise ValueError("all same-page tables required for note ownership")
    parents = {}
    for e in graph.edges:
        if e.relation == "table_parent" and e.target_id in table_ids:
            parents.setdefault(e.source_id, set()).add(e.target_id)
    sources, targets = {}, []
    unresolved = {t["table_id"] for t in layout if t["bbox"] is None}
    if contract_version == 3:
        links = {}
        for edge in graph.edges:
            if edge.relation == "table_parent":
                links.setdefault(edge.source_id, set()).add(edge.target_id)
        candidates = set(parents) | {
            b.source_id
            for b in graph.blocks
            if b.page_num == page and b.kind in ("table_row", "table_cell")
        }
        for sid in candidates:
            owners, seen = set(), set()
            current = sid
            while True:
                block = blocks.get(current)
                if (
                    current in seen
                    or block is None
                    or block.page_num != page
                    or block.kind not in ("table_cell", "table_row", "heading")
                    or type(block.winner) is not int
                    or not 0 <= block.winner < len(block.candidates)
                    or block.quality == "conflicted"
                    or (current != sid and block.quality in ("unreadable", "unlocated"))
                ):
                    unresolved.add(sid)
                    break
                seen.add(current)
                direct = links.get(current, set())
                owners.update(direct & set(table_ids))
                intermediates = direct - set(table_ids)
                if not intermediates:
                    if not owners:
                        unresolved.add(sid)
                    break
                # One explicit row chain; competing rows must not collapse to one root.
                if len(intermediates) != 1:
                    unresolved.add(sid)
                    break
                current = next(iter(intermediates))
                if current not in blocks or blocks[current].kind != "table_row":
                    unresolved.add(sid)
                    break
            parents[sid] = owners
    for sid in sorted(set(table_ids) | set(parents)):
        b = blocks[sid]
        if (
            b.winner is None
            or sid in unresolved
            or (sid in parents and (len(parents[sid]) != 1 or parents[sid] & unresolved))
        ):
            continue  # Conflicts remain explicit in unresolved_source_ids below.
        c = b.candidates[b.winner]
        identifier = f"c{len(sources)}"
        sources[identifier] = asdict(b.source_ref())
        targets.append(
            dict(
                id=identifier,
                source_id=sid,
                kind=b.kind,
                text=b.raw_text,
                table_id=sid if b.kind == "table" else next(iter(parents[sid])),
                row=c.row_number,
                column=c.column_number,
                bbox=b.bbox,
            )
        )
    unreadable = set(first["page_context"]["unreadable_word_indices"])
    words = {w["index"]: w for w in first["words"] + first["page_context"]["words"]}
    fragments = [
        dict(id=f"f{i}", **f)
        for i, f in enumerate(group_words([w for i, w in words.items() if i not in unreadable]))
    ]
    packet = dict(
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        table_ids=sorted(table_ids),
        sources=sources,
        layout_sources=layout,
        contract={**CONTRACT, "version": contract_version},
        untrusted_document_data=dict(
            page=page,
            coordinate_system="pdf_top_left_points",
            targets=targets,
            fragments=fragments,
            styled_words=[w for t in layout for w in t["styled_words"]],
            unreadable_word_indices=sorted(unreadable),
            unresolved_source_ids=sorted(
                (set(table_ids) | set(parents)) - {t["source_id"] for t in targets}
            ),
        ),
    )
    if not layout:
        packet["native_page_context"] = context
    return packet


def _aligned_tables(fragment_ids, packet):
    fragments = {f["id"]: f for f in packet["untrusted_document_data"]["fragments"]}
    boxes = [fragments[i]["bbox"] for i in fragment_ids]
    return {
        t["table_id"]
        for t in packet["layout_sources"]
        if t["bbox"] is not None
        and all(
            t["bbox"][0] - 2 * (b[3] - b[1]) <= b[0] < b[2] <= t["bbox"][2] + 2 * (b[3] - b[1])
            and (b[3] <= t["bbox"][1] or b[1] >= t["bbox"][3])
            for b in boxes
        )
    }


def _near_tables(fragment_ids, packet):
    fragments = {f["id"]: f for f in packet["untrusted_document_data"]["fragments"]}
    boxes = [fragments[i]["bbox"] for i in fragment_ids]
    # ponytail: two text heights cover ink/grid offsets; distant notes stay unresolved.
    return {
        t["table_id"]
        for t in packet["layout_sources"]
        if t["bbox"] is not None
        and all(
            t["bbox"][0] - 2 * (b[3] - b[1]) <= b[0] < b[2] <= t["bbox"][2] + 2 * (b[3] - b[1])
            and max(t["bbox"][1] - b[3], b[1] - t["bbox"][3], 0) <= 2 * (b[3] - b[1])
            for b in boxes
        )
    }


def marker_targets(fragment_ids, packet):
    """A numbered note needs a unique same-table, native raised-digit candidate.

    # ponytail: Arabic suffix/separate markers only; other typography stays unresolved.
    This constrains model proposals, never proves semantic ownership.
    """
    data = packet["untrusted_document_data"]
    if "native_page_context" in packet:
        return []
    fragments = {f["id"]: f for f in data["fragments"]}
    if not any(c.isalpha() for i in fragment_ids for c in fragments[i]["text"]):
        return []  # A bare value/marker is not an interpretable qualification.
    if any(t["bbox"] is None for t in packet.get("layout_sources", [])):
        return []  # Discover source notes, but unresolved table structure cannot own them.
    if any(COVERAGE_NOTE_START.match(fragments[i]["text"]) for i in fragment_ids):
        aligned = _aligned_tables(fragment_ids, packet)
        return (
            [t["id"] for t in data["targets"] if t["kind"] == "table" and t["source_id"] in aligned]
            if len(aligned) == 1 and aligned <= _near_tables(fragment_ids, packet)
            else []
        )
    markers = {
        match[1] for i in fragment_ids if (match := NUMBERED_NOTE_START.match(fragments[i]["text"]))
    }
    if not markers:
        return None
    if len(markers) != 1:
        return []
    marker, targets = next(iter(markers)), set()
    aligned = _near_tables(fragment_ids, packet)
    if len(aligned) != 1:
        return []
    for table in packet["layout_sources"]:
        if table["table_id"] not in aligned:
            continue
        for pair in table.get("separate_markers", []):
            suffix = pair["marker"]["text"]
            base = re.escape(pair["base"]["text"].strip()).replace(r"\ ", r"\s*")
            if suffix != marker + ")":
                continue
            marker_pattern = rf"(?<!\d){re.escape(suffix)}"
            for target in data["targets"]:
                text = target["text"].strip()
                if (
                    target["kind"] == "table_cell"
                    and target["table_id"] == table["table_id"]
                    and len(re.findall(marker_pattern, text)) == 1
                    and (
                        re.search(rf"(?<!\w){base}\s*{re.escape(suffix)}$", text)
                        or re.search(rf"{marker_pattern}\s*{base}$", text)
                        or (re.match(marker_pattern, text) and re.search(rf"(?<!\w){base}$", text))
                    )
                ):
                    targets.add(target["id"])
        for word in table["styled_words"]:
            chars = word["characters"]
            suffix = marker + ")" if word["text"].endswith(marker + ")") else marker
            if (
                word["index"] in table["clipped_word_indices"]
                or not word["text"].endswith(suffix)
                or "".join(c["text"] for c in chars) != word["text"]
                or len(chars) <= len(suffix)
                or "".join(c["text"] for c in chars[-len(suffix) :]) != suffix
            ):
                continue
            base = chars[-len(suffix) - 1]
            if base["text"].isdigit() or not all(
                c["size"] < base["size"]
                and c["bbox"][1] < base["bbox"][1]
                and c["bbox"][3] < base["bbox"][3]
                for c in chars[-len(suffix) :]
            ):
                continue
            targets.update(
                t["id"]
                for t in data["targets"]
                if t["kind"] == "table_cell"
                and t["table_id"] == table["table_id"]
                and t["text"].endswith(word["text"])
            )
    return sorted(targets) if len(targets) == 1 else []


def validate(payload, packet, graph, source, *, tenant_id):
    expected = prepare(
        graph,
        source,
        packet["table_ids"],
        tenant_id=tenant_id,
        page=packet["untrusted_document_data"]["page"],
        contract_version=packet["contract"]["version"],
    )
    if canonical_hash(packet) != canonical_hash(expected):
        raise ValueError("note packet identity mismatch")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"notes"}
        or not isinstance(payload["notes"], list)
    ):
        raise ValueError("notes-only response required")
    data = packet["untrusted_document_data"]
    fragments = {f["id"]: f for f in data["fragments"]}
    seen, result = set(), []
    for note in payload["notes"]:
        if not isinstance(note, dict) or set(note) != {"fragment_ids", "target_ids", "kind"}:
            raise ValueError("invalid note fields")
        fs, ts, kind = note["fragment_ids"], note["target_ids"], note["kind"]
        if (
            not isinstance(fs, list)
            or not fs
            or not isinstance(ts, list)
            or any(not isinstance(i, str) or i not in fragments or i in seen for i in fs)
            or any(not isinstance(i, str) or i not in packet["sources"] for i in ts)
            or len(set(fs)) != len(fs)
            or len(set(ts)) != len(ts)
            or not isinstance(kind, str)
            or kind not in KINDS
        ):
            raise ValueError("invalid note source/target/kind")
        allowed = marker_targets(fs, packet)
        if allowed is not None and not set(ts) <= set(allowed):
            raise ValueError("numbered note target lacks unique native marker")
        seen.update(fs)
        result.append(
            dict(
                **note,
                source_fragments=[fragments[i] for i in fs],
                target_source_refs=[packet["sources"][i] for i in ts],
                association_status="model_proposed" if ts else "unknown",
                source_sha256=graph.source_sha256,
                page=data["page"],
                eligible_for_scoring=False,
            )
        )
    return dict(
        notes=result,
        unassigned_fragment_ids=[i for i in fragments if i not in seen],
        unreadable_word_indices=data["unreadable_word_indices"],
        unresolved_source_ids=data["unresolved_source_ids"],
        coverage_status="unknown",
        decision=None,
        validation_scope="source_identity_and_fragment_membership_only",
        packet_sha256=canonical_hash(packet),
    )


def freeze_note_review(graph, source, packet, extracted, *, tenant_id):
    """Serialize a source-replayed proposal, never an ownership or coverage approval."""
    from hashlib import sha256
    from importlib.resources import files

    from proofops.domain.rulepacks import canonical_json

    if not isinstance(extracted, dict) or not isinstance(extracted.get("notes"), list):
        raise ValueError("invalid note result")
    if any(
        not isinstance(n, dict) or not {"fragment_ids", "target_ids", "kind"} <= n.keys()
        for n in extracted["notes"]
    ):
        raise ValueError("invalid note result fields")
    if extracted.get("packet_sha256") != canonical_hash(packet):
        raise ValueError("note result/packet mismatch")
    checked = validate(
        {
            "notes": [
                {k: n[k] for k in ("fragment_ids", "target_ids", "kind")}
                for n in extracted["notes"]
            ]
        },
        packet,
        graph,
        source,
        tenant_id=tenant_id,
    )
    if any(canonical_hash(extracted.get(k)) != canonical_hash(v) for k, v in checked.items()):
        raise ValueError("note result differs from source replay")
    body = dict(
        schema="runtime_note_review_v1",
        validator_sha256=sha256(
            files(__package__).joinpath("table_notes.py").read_bytes()
        ).hexdigest(),
        layout_sha256=sha256(
            files(__package__).joinpath("table_layout_context.py").read_bytes()
        ).hexdigest(),
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(graph)),
        packet=packet,
        extracted=extracted,
        checked=checked,
    )
    artifact = canonical_json(dict(body, artifact_sha256=canonical_hash(body)))
    if len(artifact.encode()) > 16 * 1024 * 1024:
        raise ValueError("note artifact exceeds limit")
    return artifact


def replay_note_reviews(artifacts, graph, source, *, tenant_id):
    """Return a new issue-bearing graph; every receipt binds the same base graph."""
    import json
    from dataclasses import replace
    from uuid import UUID, uuid5

    from proofops.application.ingest.graph_fusion import QualityIssue
    from proofops.domain.rulepacks import canonical_json

    if not isinstance(artifacts, tuple) or not artifacts:
        raise ValueError("nonempty immutable note artifacts required")
    issues, seen = list(graph.issues), set()
    for artifact in artifacts:
        if not isinstance(artifact, str) or len(artifact.encode()) > 16 * 1024 * 1024:
            raise ValueError("invalid note artifact")
        raw = json.loads(artifact)
        if (
            not isinstance(raw, dict)
            or not {"packet", "extracted", "artifact_sha256"} <= raw.keys()
        ):
            raise ValueError("invalid note artifact fields")
        expected = freeze_note_review(
            graph, source, raw["packet"], raw["extracted"], tenant_id=tenant_id
        )
        # The pre-unresolved-context v1 profile: revalidate source and preserve old bytes/IDs.
        legacy = (
            "9d38885dc178d90e5a1fec071f9282262d51b17b019fbc948f87222ac93101b1",
            "d7bc170774fe9019ef2b2573d7e25a0a14163317ed222fc8558008a43622d15e",
        )
        previous = (
            "efb9ea76553dcc87efd58d004f5accecf37928f48cbc6525299962bf003a1077",
            "dc7b274dd68abeb9aeade05007f6040fab15dfdc5c234b56871aefcb6f0e8ac8",
        )
        stored = (raw.get("validator_sha256"), raw.get("layout_sha256"))
        page_preview = (
            "659638039a412095d8e1647dae6df2071b09db0cfe5713c9fdc549b79cc8c435",
            "dc7b274dd68abeb9aeade05007f6040fab15dfdc5c234b56871aefcb6f0e8ac8",
        )
        direct_children = (
            "1b705722e36c0860ef19728743ef8167c7c2ee78cd8f4d59bdae6d6c24d27994",
            "15c7631eadc03d440673b6bd4fc506a2acb385d87b610d45b91d6d3428fc6cc5",
        )
        previous_spacing = raw["packet"]["contract"] == CONTRACT and stored == (
            "e89854b5e54c5af4450a053fc3939249be3642dc1a84f663845782bf3ca95b6c",
            "15c7631eadc03d440673b6bd4fc506a2acb385d87b610d45b91d6d3428fc6cc5",
        )
        # Old metadata is retained only AFTER the current source/marker validator
        # above succeeds. Previously unrestricted wrong targets therefore reject.
        if previous_spacing or (
            raw["packet"]["contract"] == {**CONTRACT, "version": 2}
            and (
                stored in (direct_children, page_preview)
                or (
                    "native_page_context" not in raw["packet"]
                    and (
                        stored == previous
                        or (
                            stored == legacy
                            and all(t["bbox"] is not None for t in raw["packet"]["layout_sources"])
                        )
                    )
                )
            )
        ):
            body = json.loads(expected)
            del body["artifact_sha256"]
            body.update(validator_sha256=stored[0], layout_sha256=stored[1])
            expected = canonical_json(dict(body, artifact_sha256=canonical_hash(body)))
        if artifact != expected or raw["artifact_sha256"] in seen:
            raise ValueError("note artifact mismatch or duplicate")
        digest = raw["artifact_sha256"]
        seen.add(digest)
        page = raw["packet"]["untrusted_document_data"]["page"]
        for table_id in raw["packet"]["table_ids"] or [None]:
            related, pending = (
                ({table_id}, [table_id])
                if table_id is not None
                else ({b.source_id for b in graph.blocks if b.page_num == page}, [])
            )
            while pending:
                parent = pending.pop()
                for edge in graph.edges:
                    if (
                        edge.relation == "table_parent"
                        and edge.target_id == parent
                        and edge.source_id not in related
                    ):
                        related.add(edge.source_id)
                        pending.append(edge.source_id)
            issue_id = str(
                uuid5(UUID(graph.parse_manifest_id), digest + ":" + (table_id or f"page:{page}"))
            )
            if any(issue.issue_id == issue_id for issue in issues):
                raise ValueError("note review already registered")
            issues.append(
                QualityIssue(
                    issue_id,
                    "table_note_review",
                    raw["packet"]["untrusted_document_data"]["page"],
                    tuple(sorted(related)),
                    "open",
                    "Note conditions/coverage unresolved; artifact=" + digest,
                )
            )
    return replace(graph, issues=tuple(sorted(issues, key=lambda issue: issue.issue_id)))
