"""Bounded Gemini page transcription; native PDF text remains the evidence gate."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path
from time import monotonic
from urllib.request import Request, urlopen
from uuid import uuid4

import pdfplumber
import pypdfium2 as pdfium
from proofops.application.ingest.graph_fusion import CandidateBatch, CandidateBlock, CandidateEdge
from proofops.domain.documents import NativeSource, PageGeometry

MODEL = "google/gemini-3.8-flash"
PROMPT = (
    'Transcribe this report page verbatim into JSON {"blocks":[{"type":'
    '"heading|paragraph|list_item|table|figure_text","text":"",'
    '"section":"","rows":[["cell"]]}]}. Preserve every number, year, '
    "unit and punctuation exactly. Join visual line wraps into complete sentences and "
    "paragraphs, keep each bullet with its text, and give each block its nearest heading "
    "path in section. A table has rows of exact cell strings. Do not infer missing text "
    "or silently complete truncated sentences. Return JSON only."
)
PROMPT_HASH = sha256(PROMPT.encode()).hexdigest()
NUMBERS = re.compile(r"\d[\d,.%/-]*")


def compact(value: str) -> str:
    return "".join(unicodedata.normalize("NFC", value).split())


def fragment(text: str) -> bool:
    """Conservative Korean/English fragment flag, not a grammar verdict."""
    value = text.strip()
    if not value:
        return True
    if value.count('"') % 2 or value.count("“") != value.count("”"):
        return True
    # List labels and nominal bullets are valid when their heading is attached.
    if re.match(r"^(?:[•·-]|\d+[).])\s*", value):
        return False
    last = value.split()[-1]
    if (len(last) > 2 and re.search(r"(?:은|는|이|가|을|를|에|에서|및)$", last)) or re.search(
        r"\b(?:is|are|was|were|has|have|will|can|does|did|and|of|to)\s*$", value, re.I
    ):
        return True
    if len(value) < 25 and not re.search(r"[.!?][^.!?]*$", value):
        return False
    return not bool(
        re.search(r"(?:다|요|음|함|됨|이다|있다|없다|했다|한다)[.!?]?\s*$", value)
        or re.search(r"\b(?:is|are|was|were|has|have|will|can|does|did)\b", value, re.I)
    )


def native_coverage(blocks, native: str) -> float:
    reference = Counter(compact(native))
    return sum((reference & Counter(compact(" ".join(blocks)))).values()) / max(
        1, sum(reference.values())
    )


def weak_pages(graph, selected: tuple[int, ...], pages) -> list[int]:
    routed = []
    for number in selected:
        local = [b for b in graph.blocks if b.page_num == number]
        page = pages[number - 1]
        native = page.extract_text() or ""
        if (
            not native.strip()
            or native_coverage([b.raw_text for b in local], native) < 0.98
            or any(b.kind == "table_cell" and "\n" in b.raw_text for b in local)
            or any(i.page_num == number and i.kind != "table_vision_not_run" for i in graph.issues)
        ):
            routed.append(number)
    return routed


def _ground(
    text: str, words: list[dict], bounds=None
) -> tuple[tuple[float, ...] | None, int | None]:
    target = compact(text)
    if not target:
        return None, None
    # Native word order is the only allowed order. No fuzzy matches or character substitutions.
    parts = [compact(w["text"]) for w in words]
    ends, size = [], 0
    for part in parts:
        size += len(part)
        ends.append(size)
    joined = "".join(parts)
    matches = []
    at = joined.find(target)
    while at >= 0:
        first = bisect_right(ends, at)
        last = bisect_right(ends, at + len(target) - 1)
        matched = words[first : last + 1]
        if matched:
            box = (
                min(w["x0"] for w in matched),
                min(w["top"] for w in matched),
                max(w["x1"] for w in matched),
                max(w["bottom"] for w in matched),
            )
            if NUMBERS.findall(text) == NUMBERS.findall(" ".join(w["text"] for w in matched)) and (
                bounds is None
                or (
                    bounds[0] - 2 <= box[0] < box[2] <= bounds[2] + 2
                    and bounds[1] - 2 <= box[1] < box[3] <= bounds[3] + 2
                )
            ):
                matches.append((box, at))
                if len(matches) > 1:
                    return None, None
        at = joined.find(target, at + 1)
    return matches[0] if matches else (None, None)


def _request(image: bytes, key: str) -> dict:
    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 16384,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(image).decode()
                        },
                    },
                ],
            }
        ],
    }
    request = Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=120) as response:
        result = json.load(response)
    content = result["choices"][0]["message"]["content"]
    return {"blocks": json.loads(content)["blocks"], "usage": result.get("usage", {})}


def _render(source: bytes, number: int) -> bytes:
    document = pdfium.PdfDocument(source)
    try:
        page = document[number - 1]
        image = page.render(scale=150 / 72).to_pil()
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        document.close()


def parse_pages(
    source,
    profile,
    graph,
    selected,
    cache_root: Path,
    *,
    key: str | None = None,
    concurrency: int = 8,
    request=_request,
):
    """Return candidate batch and replayable metrics; caller stores both in manifest."""
    mode = profile.vision_parse
    if mode == "off":
        return None, {"mode": mode, "pages_routed": []}
    with pdfplumber.open(io.BytesIO(source.content)) as document:
        routed = list(selected) if mode == "all" else weak_pages(graph, selected, document.pages)
    cache_root = cache_root / source.tenant_id
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = str(uuid4())
    started = monotonic()

    def cache_path(number):
        return cache_root / (
            f"{source.sha256}-{number}-{MODEL.replace('/', '_')}-{PROMPT_HASH}.json"
        )

    # PDFium uses process-global state; render serially, then overlap network calls.
    images = {
        number: _render(source.content, number)
        for number in routed
        if not cache_path(number).exists()
    }

    def one(number):
        cache = cache_path(number)
        if cache.exists():
            result = json.loads(cache.read_text())
            return number, result, True, 0.0
        if not key:
            raise ValueError("VISION_API_KEY_REQUIRED")
        begin = monotonic()
        try:
            result = request(images[number], key)
        except json.JSONDecodeError:
            result = request(images[number], key)
        if not isinstance(result.get("blocks"), list):
            raise ValueError("VISION_SCHEMA_INVALID")
        temporary = cache.with_name(cache.name + "." + uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(temporary, cache)
        return number, result, False, monotonic() - begin

    results = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in as_completed([pool.submit(one, n) for n in routed]):
            number, result, hit, seconds = future.result()
            results[number] = (result, hit, seconds)
    blocks, edges, page_metrics, vision_only = [], [], [], []
    with pdfplumber.open(io.BytesIO(source.content)) as document:
        for number in routed:
            page = document.pages[number - 1]
            words = page.extract_words()
            geometry = PageGeometry(
                page.width, page.height, page.rotation, tuple(map(float, page.cropbox))
            )
            response, hit, seconds = results[number]
            counts = Counter()

            def add(
                kind,
                text,
                section,
                native_id,
                parent=None,
                row=None,
                column=None,
                table_id=None,
                derived_box=None,
            ):
                if not isinstance(text, str):
                    raise ValueError("VISION_SCHEMA_INVALID")
                # Native box conversion below is valid only for an unrotated full page.
                plain_page = page.rotation == 0 and tuple(page.cropbox[:2]) == (0, 0)
                box, offset = _ground(text, words) if plain_page else (None, None)
                if derived_box is not None and plain_page:
                    box, offset = derived_box, None
                    counts["structure_derived"] += 1
                if box is None:
                    counts["vision_only"] += 1
                    if kind not in {"table", "table_row"} and NUMBERS.search(text):
                        counts["numbers_rejected"] += 1
                    vision_only.append(dict(page=number, kind=kind, text=text, section=section))
                else:
                    counts["grounded"] += 1
                native_box = (
                    (box[0], page.height - box[3], box[2], page.height - box[1]) if box else None
                )
                native = NativeSource(
                    source.document_version_id,
                    profile.parse_manifest_id,
                    run_id,
                    native_id,
                    number,
                    None,
                    native_box,
                    "pdf_bottom_left_points",
                    text,
                    0,
                    len(text),
                )
                blocks.append(
                    CandidateBlock(
                        kind,
                        native,
                        geometry,
                        ("section=" + section, "vision_only")
                        if box is None
                        else (
                            "section=" + section,
                            *(
                                ()
                                if offset is None
                                else (
                                    f"native_char_start={offset}",
                                    f"native_char_end={offset + len(compact(text))}",
                                )
                            ),
                        ),
                        None,
                        "pdf_bottom_left_points",
                        None,
                        table_id,
                        row,
                        column,
                    )
                )
                if parent is not None:
                    edges.append(CandidateEdge(native_id, parent, "table_parent"))
                return box

            def union(boxes):
                return (
                    min(box[0] for box in boxes),
                    min(box[1] for box in boxes),
                    max(box[2] for box in boxes),
                    max(box[3] for box in boxes),
                )

            for index, item in enumerate(response["blocks"]):
                if not isinstance(item, dict) or item.get("type") not in {
                    "heading",
                    "paragraph",
                    "list_item",
                    "table",
                    "figure_text",
                }:
                    raise ValueError("VISION_SCHEMA_INVALID")
                kind = item["type"]
                section = str(item.get("section", ""))
                identifier = f"p{number}-v{index}"
                if kind == "table":
                    rows = item.get("rows", [])
                    if not isinstance(rows, list):
                        raise ValueError("VISION_SCHEMA_INVALID")
                    counts["tables"] += 1
                    table_boxes = []
                    for ri, cells in enumerate(rows, 1):
                        if not isinstance(cells, list):
                            raise ValueError("VISION_SCHEMA_INVALID")
                        row_id = f"{identifier}-r{ri}"
                        row_boxes = []
                        for ci, cell in enumerate(cells, 1):
                            counts["cells"] += 1
                            row_boxes.append(
                                add(
                                    "table_cell",
                                    cell,
                                    section,
                                    f"{row_id}-c{ci}",
                                    row_id,
                                    ri,
                                    ci,
                                    table_id=identifier,
                                )
                            )
                        row_box = union(row_boxes) if row_boxes and all(row_boxes) else None
                        table_boxes.append(row_box)
                        add(
                            "table_row",
                            "\t".join(str(c) for c in cells),
                            section,
                            row_id,
                            identifier,
                            ri,
                            table_id=identifier,
                            derived_box=row_box,
                        )
                    add(
                        "table",
                        "\n".join("\t".join(map(str, row)) for row in rows),
                        section,
                        identifier,
                        derived_box=union(table_boxes)
                        if table_boxes and all(table_boxes)
                        else None,
                    )
                else:
                    counts["fragments"] += (
                        fragment(item.get("text", "")) if kind != "heading" else 0
                    )
                    counts["prose"] += kind != "heading"
                    if kind == "list_item" and not section:
                        counts["orphan_bullets"] += 1
                    add(
                        "paragraph"
                        if kind == "list_item"
                        else "figure"
                        if kind == "figure_text"
                        else kind,
                        item.get("text", ""),
                        section,
                        identifier,
                    )
            page_metrics.append(
                dict(
                    page=number,
                    cache_hit=hit,
                    seconds=seconds,
                    cost=response.get("usage", {}).get("cost", 0),
                    **counts,
                )
            )
    batch = CandidateBatch(
        source.tenant_id,
        source.document_version_id,
        profile.parse_manifest_id,
        source.sha256,
        run_id,
        "gemini_vision",
        MODEL,
        "vision",
        sha256(json.dumps(profile.invocation_snapshot(), sort_keys=True).encode()).hexdigest(),
        tuple(blocks),
        tuple(edges),
    )
    local_prose = [
        block.raw_text
        for block in graph.blocks
        if block.page_num in routed and block.kind == "paragraph"
    ]
    vision_prose = [block.source.raw_text for block in blocks if block.kind == "paragraph"]
    metrics = dict(
        mode=mode,
        model=MODEL,
        prompt_sha256=PROMPT_HASH,
        pages_routed=routed,
        wall_seconds=monotonic() - started,
        pages=page_metrics,
        vision_only=vision_only,
        fragment_rate_local=sum(map(fragment, local_prose)) / max(1, len(local_prose)),
        fragment_rate_vision=sum(map(fragment, vision_prose)) / max(1, len(vision_prose)),
    )
    return batch, metrics
