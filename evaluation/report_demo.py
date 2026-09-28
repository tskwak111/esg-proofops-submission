"""Replay a real extraction archive into source-linked, explicitly gated review.

No model calls, synthetic fallback, source approval or invented decisions.
Run as a module; the CLI needs a source selection, section map, extraction receipts,
and optional Upstage table receipt with its original physical page.
"""

import argparse
import json
import shutil
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from html import escape
from pathlib import Path
from uuid import UUID, uuid5

import yaml  # type: ignore[import-untyped]
from proofops.application.claims import ClaimScope, ExtractionProfile, discover_atomic_claims
from proofops.application.evidence.retrieval import retrieve_evidence
from proofops.application.ingest.graph_fusion import (
    SourceArtifact,
    candidates_from_snapshot,
    fuse_candidates,
)
from proofops.application.rulepacks import validate_rulepack
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import pack_content_hash, snapshot_from_validated

from evaluation.html_table_cells import parse_table_cells
from evaluation.section_pipeline import SectionSearch, plan_for_graph
from evaluation.table_numeric_candidates import (
    discover_candidates_from_cells,
    discover_table_candidates,
)
from evaluation.upstage_live_probe import load_graph
from evaluation.upstage_table_candidates import build_table_batch


def build_demo(graph, mapped, records, rulepack):
    """Source-checked extraction replay → scoped search → actual retrieval guard."""
    plan = plan_for_graph(graph, mapped, tenant_id=graph.tenant_id)
    blocks = {b.source_id: b for b in graph.blocks}
    saved = {}
    profiles = []
    for packet, result in records:
        data = packet["untrusted_document_data"]
        block = blocks.get(data["source_id"])
        if (
            block is None
            or any(
                packet[k] != getattr(graph, k)
                for k in ("tenant_id", "document_version_id", "parse_manifest_id", "source_sha256")
            )
            or data
            != dict(
                source_id=block.source_id,
                page_num=block.page_num,
                kind=block.kind,
                text=block.normalized_text,
            )
            or result["packet_sha256"] != canonical_hash(packet)
            or result["profile"] != packet["extraction_profile"]
            or result["packet_sha256"] in saved
        ):
            raise ValueError("extraction archive/source mismatch")
        profiles.append(ExtractionProfile(**packet["extraction_profile"]))
        saved[result["packet_sha256"]] = dict(spans=result["spans"])
    if not profiles or any(p != profiles[0] for p in profiles):
        raise ValueError("one explicit extraction profile required")

    class Replay:
        profile = profiles[0]

        def extract(self, packet):
            if canonical_hash(packet) in saved:
                return saved[canonical_hash(packet)]
            text = packet["untrusted_document_data"]["text"]
            return dict(
                spans=[
                    dict(
                        char_start=0,
                        char_end=len(text),
                        quote=text,
                        kind="unknown",
                        reason="not_in_extraction_archive",
                        topic_ids=[],
                    )
                ]
            )

    discovery = discover_atomic_claims(graph, ClaimScope(**plan["scope"]), extractor=Replay())
    search = SectionSearch(graph, mapped, tenant_id=graph.tenant_id)
    run_id = str(uuid5(UUID(graph.parse_manifest_id), "source-linked-review-v1"))

    def no_model_counter(_):
        # This preview has no approved model counter; never fake token counts.
        raise ValueError("MODEL_TOKEN_COUNTER_REQUIRED")

    rows = []
    for claim in discovery.claims:
        hits = search.search(search.scope, claim.quote)
        candidates = []
        for hit in hits.hits:
            block = blocks[hit.source_id]
            if (
                hit.scope != search.scope
                or hit.raw_text_sha256 != sha256(block.raw_text.encode()).hexdigest()
            ):
                raise ValueError("search source mismatch")
            candidates.append(
                dict(
                    source_id=block.source_id,
                    page=block.page_num,
                    text=block.normalized_text,
                    quality=block.quality,
                    bbox=block.bbox,
                    binding_status="undetermined",
                )
            )
        try:
            packet = retrieve_evidence(
                claim,
                graph,
                search,
                tenant_id=graph.tenant_id,
                run_id=run_id,
                index_generation=search.scope.index_generation,
                rulepack=rulepack,
                document_context={},
                token_counter=no_model_counter,
            ).to_dict()
            reason = (
                "SOURCE_VALIDATION_REQUIRED"
                if packet["status"] != "candidate"
                else "TAGGING_RUNTIME_REQUIRED"
            )
        except ValueError as error:
            if str(error) != "MODEL_TOKEN_COUNTER_REQUIRED":
                raise
            packet = None
            reason = str(error)
        rows.append(
            dict(
                claim_id=claim.claim_id,
                quote=claim.quote,
                source_refs=[asdict(r) for r in claim.source_refs],
                source_quality=claim.source_quality,
                search_candidates=candidates,
                retrieval=packet,
                decision=None,
                decision_status="not_run",
                reason=reason,
            )
        )
    demo = dict(
        schema="source-linked-review-v1",
        tenant_id=graph.tenant_id,
        document_version_id=graph.document_version_id,
        source_sha256=graph.source_sha256,
        parse_manifest_id=graph.parse_manifest_id,
        graph_sha256=canonical_hash(asdict(graph)),
        section_map_sha256=mapped["map_sha256"],
        rule_pack_sha256=rulepack.sha256,
        extraction_profile=asdict(profiles[0]),
        extraction_receipts=[
            {
                key: result.get(key)
                for key in ("request_id", "packet_sha256", "content_sha256", "response_sha256")
            }
            for _, result in records
        ],
        claims=rows,
        tables=[],
        coverage=dict(
            parsed_pages=sorted({b.page_num for b in graph.blocks}),
            missing_claim_pages=plan["missing_pages"],
            missing_evidence_pages=plan["missing_evidence_pages"],
            extraction_receipts=len(records),
            claims=len(rows),
            unresolved_spans=len(discovery.exclusions),
            decided=0,
            complete=False,
        ),
        model_calls_this_run=0,
        tagging_status="not_run",
        rule_evaluation_status="not_run",
    )
    return demo


REVIEW_LABELS = {
    "partial_support": "일부 내용 뒷받침",
    "context": "문맥 참고",
    "self_quote": "주장 원문 반복",
    "duplicate": "중복 후보",
    "insufficient": "단독 근거로 부족",
    "unrelated": "해당 주장 뒷받침 못함",
}


def attach_agent_review(demo, review):
    """Immutable display annotation; never changes source quality, bindings or grades."""
    body = {k: v for k, v in demo.items() if k != "artifact_sha256"}
    if (
        "agent_review" in demo
        or canonical_hash(body) != demo.get("artifact_sha256")
        or review.get("input_sha256") != demo["artifact_sha256"]
        or review.get("reviewer_kind") != "agent"
        or set(review.get("claims", {})) != {c["claim_id"] for c in demo["claims"]}
        or set(review.get("tables", {})) != {str(t["id"]) for t in demo["tables"]}
    ):
        raise ValueError("agent review/source mismatch")
    for claim in demo["claims"]:
        annotation = review["claims"][claim["claim_id"]]
        candidates = annotation.get("candidates", {})
        if not isinstance(annotation.get("note"), str) or set(candidates) != {
            c["source_id"] for c in claim["search_candidates"]
        }:
            raise ValueError("incomplete candidate review")
        for candidate in candidates.values():
            if candidate.get("status") not in REVIEW_LABELS or not isinstance(
                candidate.get("reason"), str
            ):
                raise ValueError("invalid candidate review")
    for table in review["tables"].values():
        if table.get("role") not in ("data", "navigation") or not isinstance(
            table.get("note"), str
        ):
            raise ValueError("invalid table review")
    result = deepcopy(body)
    result["agent_review"] = deepcopy(review)
    result["artifact_sha256"] = canonical_hash(result)
    return result


def attach_table_notes(demo, graph, source, packet, extracted):
    """Source-replayed, immutable review context; never approve a numeric check."""
    from evaluation.table_layout_context import cell_source_matches
    from evaluation.table_notes import validate

    body = {k: v for k, v in demo.items() if k != "artifact_sha256"}
    if (
        canonical_hash(body) != demo.get("artifact_sha256")
        or any(
            demo[k] != getattr(graph, k)
            for k in ("tenant_id", "document_version_id", "source_sha256")
        )
        or extracted.get("packet_sha256") != canonical_hash(packet)
    ):
        raise ValueError("table note review/source mismatch")
    checked = validate(
        dict(
            notes=[
                {k: n[k] for k in ("fragment_ids", "target_ids", "kind")}
                for n in extracted["notes"]
            ]
        ),
        packet,
        graph,
        source,
        tenant_id=graph.tenant_id,
    )
    from proofops.adapters.local.table_notes import freeze_note_review, replay_note_reviews

    runtime_artifact = freeze_note_review(
        graph, source, packet, extracted, tenant_id=graph.tenant_id
    )
    runtime_graph = replay_note_reviews(
        (runtime_artifact,), graph, source, tenant_id=graph.tenant_id
    )
    runtime_digest = json.loads(runtime_artifact)["artifact_sha256"]
    checked = deepcopy(checked)
    result, matched = deepcopy(body), set()
    result.setdefault("runtime_note_review_artifacts", []).append(runtime_artifact)
    blocks = {b.source_id: b for b in graph.blocks}
    aliases = {t["id"]: t for t in packet["untrusted_document_data"]["targets"]}
    for table in result["tables"]:
        matches = [
            b
            for b in graph.blocks
            if b.kind == "table"
            and b.source_id in packet["table_ids"]
            and b.page_num == table["page"]
            and any(c.source.source_native_id == f"table-{table['id']}" for c in b.candidates)
        ]
        if not matches:
            continue
        if len(matches) != 1 or "note_review" in table or matches[0].source_id in matched:
            raise ValueError("ambiguous or repeated table note attachment")
        parent = matches[0]
        matched.add(parent.source_id)
        original_cells = [
            blocks[e.source_id]
            for e in graph.edges
            if e.relation == "table_parent" and e.target_id == parent.source_id
        ]
        cells, source_keys = [], {}
        for block in original_cells:
            if block.winner is None:
                raise ValueError("unresolved table cell cannot be projected")
            c = block.candidates[block.winner]
            cells.append(
                (c.row_number, c.column_number, c.row_span or 1, c.column_span or 1, block.raw_text)
            )
            source_keys[block.source_id] = f"r{c.row_number}c{c.column_number}"
        if sorted(cells) != sorted(
            (c["row"], c["column"], c["row_span"], c["column_span"], c["text"])
            for c in table["cells"]
        ):
            raise ValueError("table cells differ from note source graph")
        numeric = discover_candidates_from_cells(table["cells"])
        if canonical_hash(numeric) != canonical_hash(table["numeric"]):
            raise ValueError("numeric review differs from source cells")
        notes = [
            n
            for n in checked["notes"]
            if any(aliases[t]["table_id"] == parent.source_id for t in n["target_ids"])
        ]
        layout = next(
            entry for entry in packet["layout_sources"] if entry["table_id"] == parent.source_id
        )
        table["cell_source_matches"] = cell_source_matches(table["cells"], layout)
        table["note_review"] = dict(
            table_source_id=parent.source_id,
            runtime_artifact_sha256=runtime_digest,
            runtime_graph_sha256=canonical_hash(asdict(runtime_graph)),
            runtime_issue_ids=[
                issue.issue_id
                for issue in runtime_graph.issues
                if parent.source_id in issue.source_ids
            ],
            notes=notes,
            unassigned_notes=[n for n in checked["notes"] if not n["target_ids"]],
            coverage_status="unknown",
            extraction_status=extracted.get("status", "unknown"),
            packet_sha256=checked["packet_sha256"],
            result_sha256=canonical_hash(extracted),
            parse_manifest_id=graph.parse_manifest_id,
            claim_parse_manifest_id=demo["parse_manifest_id"],
            source_alignment="same_document_exact_table_cells",
            graph_sha256=packet["graph_sha256"],
            requests=deepcopy(extracted.get("requests", [])),
            validation_scope=checked["validation_scope"],
        )
        for candidate in table["numeric"]["candidates"]:
            bound = {candidate["value"]["key"]}
            bound.update(h["key"] for h in candidate["headers"].values() if h is not None)
            bound.update(
                h["key"]
                for h in candidate.get("row_headers", []) + candidate.get("column_headers", [])
            )
            linked = [
                n
                for n in notes
                if any(
                    aliases[t]["source_id"] == parent.source_id
                    or source_keys.get(aliases[t]["source_id"]) in bound
                    for t in n["target_ids"]
                )
            ]
            candidate["source_location"] = deepcopy(
                table["cell_source_matches"][candidate["value"]["key"]]
            )
            candidate["note_context"] = dict(
                notes=linked,
                unassigned_notes=deepcopy(table["note_review"]["unassigned_notes"]),
                coverage_status="unknown",
                numeric_status="not_run",
                admission_status="blocked",
                reason=(
                    "footnote_ownership_unresolved"
                    if table["note_review"]["unassigned_notes"]
                    else "footnote_conditions_unresolved"
                    if linked
                    else "note_coverage_unconfirmed"
                ),
            )
    if matched != set(packet["table_ids"]):
        raise ValueError("note page tables are missing from review")
    result["artifact_sha256"] = canonical_hash(result)
    return result


def render_demo(demo):
    """Standalone readable artifact; source text is escaped, never inserted as HTML."""

    def e(value):
        return escape(str(value), quote=True)

    def link(page):
        return (
            f'<a href="source.pdf#page={int(page)}" target="_blank" rel="noopener">'
            f"원본 PDF {int(page)}쪽 ↗</a>"
        )

    cards = []
    review = demo.get("agent_review", {})
    for index, claim in enumerate(demo["claims"], 1):
        annotation = review.get("claims", {}).get(claim["claim_id"], {})
        useful, other = [], []
        for c in claim["search_candidates"]:
            assessment = annotation.get("candidates", {}).get(c["source_id"], {})
            status = assessment.get("status")
            item = (
                f'<details><summary>{link(c["page"])} · '
                f'{e(REVIEW_LABELS.get(status, "미검토 후보"))}</summary>'
                f'<p>{e(assessment.get("reason", "관련성 미검토"))}</p>'
                f'<p>{e(c["text"])}</p><small>귀속 미확정 · {e(c["source_id"])}</small></details>'
            )
            (useful if status in (None, "partial_support") else other).append(item)
        candidates = "".join(useful)
        if not useful:
            candidates = (
                "<p>이 검색 결과에서 직접 연결할 후보를 확보하지 못했습니다. "
                "근거 부재 판정은 아닙니다.</p>"
            )
        if other:
            candidates += (
                f"<details><summary>참고·중복·부적합 후보 {len(other)}개와 검토 이유</summary>"
                + "".join(other)
                + "</details>"
            )
        cards.append(
            f'<section id="claim-{index}"><h2>주장 {index}</h2>'
            f'<blockquote>{e(claim["quote"])}</blockquote>'
            f'{link(claim["source_refs"][0]["page_num"])}'
            '<p class="badge">판정 대기 · 원문 검증과 태깅 필요</p>'
            + (
                f'<p><strong>에이전트 검토</strong> · {e(annotation["note"])}</p>'
                if annotation
                else ""
            )
            + "<p>아래 항목은 검색 결과입니다. 숫자·연도·대상 범위를 확인해야 합니다.</p>"
            f"{candidates}</section>"
        )
    tables = []
    unassigned = {}
    for table in demo["tables"]:
        for note in table.get("note_review", {}).get("unassigned_notes", []):
            key = (
                table["page"],
                table["note_review"]["packet_sha256"],
                tuple(note["fragment_ids"]),
            )
            unassigned[key] = note
        annotation = review.get("tables", {}).get(str(table["id"]), {})
        if annotation.get("role") == "navigation":
            tables.append(f'<p>표 {e(table["id"])} · 탐색 메뉴로 제외: {e(annotation["note"])}</p>')
            continue
        rows, current, cells = [], None, []
        for cell in table["cells"]:
            if current != cell["row"]:
                if cells:
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                current, cells = cell["row"], []
            cells.append(
                f'<td rowspan="{int(cell["row_span"])}" colspan="{int(cell["column_span"])}"'
                f'>{e(cell["text"])}</td>'
            )
        if cells:
            rows.append("<tr>" + "".join(cells) + "</tr>")
        count = sum(c["binding_status"] == "candidate" for c in table["numeric"]["candidates"])
        numeric_rows = []
        for c in table["numeric"]["candidates"]:
            metric = " / ".join(
                h["text"] for h in c.get("section_headers", []) + c.get("row_headers", [])
            )
            qualifier = {
                "target": "목표",
                "actual": "실적",
                "unqualified_bare_year": "연도만 명시",
            }.get(c["period"]["qualifier"], "미확정")
            values = (
                metric,
                c["period"]["year"],
                qualifier,
                " / ".join(h["text"] for h in c.get("column_headers", [])[1:]),
                c["unit"]["literal"],
                c["value"]["text"],
                "후보" if c["binding_status"] == "candidate" else "검토 필요",
                {
                    "unique_proposal": "원문 위치 후보 1곳",
                    "ambiguous": "중복 문구 · 위치 미확정",
                }.get(c.get("source_location", {}).get("status"), "위치 미확정"),
                " → ".join(h["text"] for h in c.get("preceding_row_headers", [])),
                (
                    " / ".join(
                        " ".join(f["text"] for f in n["source_fragments"])
                        for n in c.get("note_context", {}).get("notes", [])
                    )
                    or "각주 범위 미확인"
                )
                + (
                    f" · 귀속 미확정 각주 {len(c['note_context']['unassigned_notes'])}개 별도 검토"
                    if c.get("note_context", {}).get("unassigned_notes")
                    else ""
                ),
            )
            numeric_rows.append("<tr>" + "".join(f"<td>{e(v)}</td>" for v in values) + "</tr>")
        note_review = table.get("note_review", {})
        note_html = "".join(
            "<li>"
            + e(
                " / ".join(
                    "표 전체"
                    if r["source_id"] == note_review.get("table_source_id")
                    else r["quote"]
                    for r in n["target_source_refs"]
                )
                or "귀속 미확정"
            )
            + " — "
            + e(" ".join(f["text"] for f in n["source_fragments"]))
            + "</li>"
            for n in note_review.get("notes", [])
        )
        tables.append(
            f'<details><summary>{link(table["page"])} · 표 {e(table["id"])} · {count}개</summary>'
            f'<p>{e(annotation.get("note", "원본 대조 미실시"))}</p>'
            f'<div class="scroll"><table>{"".join(rows)}</table></div>'
            '<p>파서가 재구성한 표입니다. 원본 대조와 주장 귀속 확인이 필요합니다.</p>'
            + (
                "<h3>각주 조건 검토 필요</h3><p>연결 후보이며 수치 판정은 보류됩니다.</p>"
                f"<ul>{note_html}</ul>"
                if note_review
                else ""
            )
            + '<details><summary>연도·단위·지표 연결 보기</summary><div class="scroll"><table>'
            "<tr><th>대상·지표</th><th>연도</th><th>구분</th><th>열 기준</th>"
            "<th>단위</th><th>원문 값</th><th>상태</th><th>원문 위치(미검증)</th>"
            "<th>선행 행 문맥(부모 미확정)</th>"
            "<th>각주 조건(미확정)</th></tr>"
            + "".join(numeric_rows)
            + "</table></div></details></details>"
        )
    unassigned_html = "".join(
        "<li>"
        + link(key[0])
        + " · "
        + e(" ".join(f["text"] for f in note["source_fragments"]))
        + "</li>"
        for key, note in unassigned.items()
    )
    if unassigned_html:
        unassigned_html = "<h3>페이지의 귀속 미확정 각주</h3><ul>" + unassigned_html + "</ul>"
    coverage = demo["coverage"]
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESG ProofOps · 보고서 검토</title>
<style>
body{{font:16px/1.7 system-ui,sans-serif;background:#f3f6fa;color:#18304a;margin:0}}
main{{max-width:1100px;margin:auto;padding:32px}}h1{{font-size:32px;margin-bottom:8px}}
section,.intro{{padding:26px;background:white;border:1px solid #d8e0eb;border-radius:12px;
margin:24px 0}}
blockquote{{margin:16px 0;border-left:4px solid #346fb5;padding:14px;background:#eef4fb}}
a{{color:#245a91}}
.badge{{display:inline-block;background:#fff1c9;padding:4px 12px;border-radius:6px}}
summary{{cursor:pointer;padding:12px;background:#f5f7fa;margin:5px 0}}
details p{{white-space:pre-wrap}}
small{{overflow-wrap:anywhere}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
.scroll{{overflow:auto;max-height:540px}}table{{border-collapse:collapse;font-size:13px;width:100%}}
td{{border:1px solid #ccd5df;padding:8px;min-width:65px}}nav a{{margin-right:20px}}
</style><main><h1>ESG ProofOps · 보고서 검토</h1>
<p>환경 주장과 ESG 데이터·부록을 원본과 함께 확인합니다.</p>
<nav><a href="#claims">주장과 근거 후보</a><a href="#tables">데이터 표</a>
<a href="review.json">검토 기록 JSON</a></nav>
<div class="intro"><strong>선택 페이지 검토 · 자동 판정 전 단계</strong>
<p>주장 {len(demo['claims'])}개 · 판정 완료 0개 · 표 {len(demo['tables'])}개</p>
<p>파싱한 PDF 페이지: {e(coverage['parsed_pages'])}<br>
미파싱 E 본문 {len(coverage['missing_claim_pages'])}쪽 ·
미파싱 근거 영역 {len(coverage['missing_evidence_pages'])}쪽</p>
<p>저장된 실제 모델 추출을 재생했습니다. 이번 실행은 모델 호출 0건입니다.
표·근거 검색 결과를 승인된 증거로 취급하지 않으며, 태깅과 규칙 판정은 아직 실행되지 않았습니다.</p>
<small>원본 SHA-256: {e(demo['source_sha256'])}</small></div>
<h2 id="claims">주장과 근거 후보</h2>{''.join(cards)}
<section id="tables"><h2>ESG 데이터 표</h2>{unassigned_html}
{''.join(tables) or '<p>표 파싱 자료가 없습니다.</p>'}
</section>
</main></html>"""


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--selection", type=Path, required=True)
    cli.add_argument("--section-map", type=Path, required=True)
    cli.add_argument("--extraction", type=Path, action="append", required=True)
    cli.add_argument("--table-receipt", type=Path)
    cli.add_argument("--table-page", type=int)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--agent-review", type=Path)
    cli.add_argument("--native-source-receipt", type=Path)
    cli.add_argument(
        "--table-note-archive", type=Path, help="Source-bound packet.json/result.json folder"
    )
    cli.add_argument(
        "--table-note-batch", type=Path, help="Exact note parser CandidateBatch snapshot"
    )
    args = cli.parse_args()
    if bool(args.table_note_archive) != bool(args.table_note_batch) or (
        args.table_note_archive and not args.table_receipt
    ):
        cli.error(
            "table notes require --table-note-archive, --table-note-batch and --table-receipt"
        )
    selection = json.loads(args.selection.read_text())
    pdf = Path(selection["source_path"])
    graph = load_graph(pdf, Path(selection["manifest"]))
    native_receipt = None
    if args.native_source_receipt:
        from proofops.adapters.local.source_verification import replay_native_sources

        native_receipt = json.loads(args.native_source_receipt.read_text())
        graph = replay_native_sources(
            native_receipt, graph, pdf.read_bytes(), tenant_id=graph.tenant_id
        )
    records = []
    for folder in args.extraction:
        packet = json.loads((folder / "packet.json").read_text())
        result = json.loads((folder / "result.json").read_text())
        raw = json.loads((folder / "raw_response.json").read_text())
        if (
            result["content_sha256"] != canonical_hash(raw["content"])
            or result["response_sha256"] != raw["response_sha256"]
        ):
            raise ValueError("provider receipt mismatch")
        quotes = json.loads(raw["content"])["claims"]
        if any(s["quote"] not in quotes for s in result["spans"]):
            raise ValueError("extraction result not in provider response")
        records.append((packet, result))
    root = Path(__file__).resolve().parents[1] / "config"
    manifest = yaml.safe_load((root / "rule_pack_manifest.yaml").read_text())
    files = {name: yaml.safe_load((root / name).read_text()) for name in manifest["files"]}
    manifest.update(
        tenant_id=graph.tenant_id,
        approved_by=None,
        approved_at=None,
        rule_pack_id=str(uuid5(UUID(graph.tenant_id), "review-draft-pack")),
    )
    manifest["sha256"] = pack_content_hash(manifest, files)
    if not validate_rulepack(manifest, files, [f"GAP-{i:03}" for i in range(1, 11)]).ok:
        raise ValueError("rulepack invalid")
    demo = build_demo(
        graph,
        json.loads(args.section_map.read_text()),
        records,
        snapshot_from_validated(manifest, files),
    )
    if args.table_receipt:
        if args.table_page is None:
            cli.error("--table-page required with --table-receipt")
        receipt = json.loads(args.table_receipt.read_text())
        source = SourceArtifact(
            graph.tenant_id,
            selection["document_id"],
            graph.document_version_id,
            graph.source_sha256,
            "local-review-source",
            pdf.read_bytes(),
            synthetic=False,
        )
        build_table_batch(
            source,
            receipt,
            physical_pages=(args.table_page,),
            parse_manifest_id=graph.parse_manifest_id,
            tenant_id=graph.tenant_id,
        )
        for element in receipt["raw_response"]["elements"]:
            if element["category"] == "table":
                html = element["content"]["html"]
                demo["tables"].append(
                    dict(
                        id=element["id"],
                        page=args.table_page,
                        cells=parse_table_cells(html),
                        numeric=discover_table_candidates(html),
                        response_sha256=receipt["response_sha256"],
                    )
                )
    if native_receipt is not None:
        demo["native_source_verification"] = native_receipt
    demo["artifact_sha256"] = canonical_hash(demo)
    if args.table_note_archive:
        batch = json.loads(args.table_note_batch.read_text())
        note_graph = fuse_candidates(candidates_from_snapshot([batch]), tenant_id=graph.tenant_id)
        demo = attach_table_notes(
            demo,
            note_graph,
            pdf.read_bytes(),
            json.loads((args.table_note_archive / "packet.json").read_text()),
            json.loads((args.table_note_archive / "result.json").read_text()),
        )
    if args.agent_review:
        demo = attach_agent_review(demo, json.loads(args.agent_review.read_text()))
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (args.output / "review.json").write_text(json.dumps(demo, ensure_ascii=False, indent=2))
    (args.output / "index.html").write_text(render_demo(demo))
    shutil.copyfile(pdf, args.output / "source.pdf")
    if sha256((args.output / "source.pdf").read_bytes()).hexdigest() != graph.source_sha256:
        raise ValueError("source changed during demo export")
    print(json.dumps(dict(output=str(args.output), **demo["coverage"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
