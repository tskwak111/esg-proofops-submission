"""Frozen source-only review cases; agent agreement never becomes human accuracy.

This evaluates exact case answers, not exhaustive claim recall or audit accuracy.
Reviewer identity is declared metadata, not an authentication/approval mechanism.
"""

import json

from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_sha256


def freeze_cases(cases: list[dict]) -> dict:
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise ValueError("bounded nonempty cases required")
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "case_id",
            "company_id",
            "task",
            "source_sha256",
            "page",
            "source",
        }:
            raise ValueError("source-only case fields required")
        if any(
            not isinstance(case[k], str) or not case[k].strip() for k in ("case_id", "company_id")
        ):
            raise ValueError("case/company identity required")
        if case["task"] not in ("claim", "table", "link"):
            raise ValueError("unsupported review task")
        _require_sha256("source_sha256", case["source_sha256"])
        if type(case["page"]) is not int or case["page"] < 1:
            raise ValueError("physical page required")
        if not isinstance(case["source"], dict) or not case["source"]:
            raise ValueError("source data required")
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("duplicate case")
    raw = json.dumps(cases, ensure_ascii=False, allow_nan=False)
    if len(raw.encode()) > 2_000_000:
        raise ValueError("review packet too large")
    data = dict(version="source-review-v1", cases=json.loads(raw))
    return {**data, "packet_sha256": canonical_hash(data)}


def score_review(packet: dict, predictions: dict, review: dict) -> dict:
    if packet != freeze_cases(packet["cases"]):
        raise ValueError("changed review packet")
    if not isinstance(review, dict) or set(review) != {
        "packet_sha256",
        "reviewer_kind",
        "reviewer_id",
        "labels",
    }:
        raise ValueError("review metadata required")
    if (
        review["packet_sha256"] != packet["packet_sha256"]
        or review["reviewer_kind"] not in ("agent", "human")
        or not isinstance(review["reviewer_id"], str)
        or not review["reviewer_id"].strip()
    ):
        raise ValueError("review identity mismatch")
    ids = {c["case_id"] for c in packet["cases"]}
    labels = review["labels"]
    if (
        not isinstance(labels, dict)
        or set(labels) != ids
        or not isinstance(predictions, dict)
        or set(predictions) - ids
    ):
        raise ValueError("explicit labels for every case and scoped predictions required")
    # Validate finite JSON and freeze identity before counting; null means unreviewed.
    result = dict(
        packet_sha256=packet["packet_sha256"],
        prediction_sha256=canonical_hash(predictions),
        review_sha256=canonical_hash(review),
        reviewer_kind=review["reviewer_kind"],
        reviewer_id=review["reviewer_id"],
    )

    def counts(selected):
        reviewed = [i for i in selected if labels[i] is not None]
        matches = sum(
            i in predictions and canonical_hash(predictions[i]) == canonical_hash(labels[i])
            for i in reviewed
        )
        return dict(
            total_cases=len(selected),
            reviewed_cases=len(reviewed),
            unreviewed_cases=len(selected) - len(reviewed),
            exact_matches=matches,
            missing_predictions=sum(predictions.get(i) is None for i in reviewed),
            agreement=matches / len(reviewed) if reviewed else None,
            review_coverage=len(reviewed) / len(selected) if selected else None,
        )

    result.update(counts(ids))
    result["human_accuracy"] = result["agreement"] if review["reviewer_kind"] == "human" else None
    for field in ("company", "task"):
        key = "company_id" if field == "company" else field
        result["by_" + field] = {
            value: counts({c["case_id"] for c in packet["cases"] if c[key] == value})
            for value in sorted({c[key] for c in packet["cases"]})
        }
    return result


def review_html(packet: dict) -> str:
    """Offline source-only review form; downloads annotations, never approves evidence."""
    if packet != freeze_cases(packet["cases"]):
        raise ValueError("changed review packet")
    data = json.dumps(packet, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return (
        """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESG ProofOps · 원문 검토</title>
<style>
body{font:15px/1.6 system-ui,sans-serif;background:#f4f6f9;color:#162338;margin:0}
header,main{max-width:1150px;margin:auto;padding:24px}h1{margin:0;font-size:28px}
header{position:sticky;top:0;background:#f4f6f9;z-index:1;border-bottom:1px solid #ccd4df}
section{background:white;border:1px solid #d5dce6;border-radius:10px;padding:24px;margin:0 0 24px}
label{display:inline-block;margin:8px 14px 8px 0}
input,select,button,textarea{font:inherit;padding:8px}
button{background:#173f77;color:white;border:0;border-radius:5px;cursor:pointer}
.table{overflow:auto;max-height:420px}table{border-collapse:collapse;width:100%;font-size:13px}
td{border:1px solid #d5dce6;padding:7px;min-width:64px;white-space:pre-wrap}
td.target{background:#fff0ac;outline:2px solid #a65b00;outline-offset:-2px}
textarea{display:block;width:100%;box-sizing:border-box;min-height:110px;margin-top:8px}
pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#536278}#status{margin-left:12px}
</style><header><h1>ESG ProofOps · 원문 검토</h1>
<p>노란 셀의 지표·단위·연도·목표/실적을 원문에서 확인합니다. 모델 답안은 표시하지 않습니다.
표는 파서가 재구성한 결과입니다. 최종 검토는 원본 PDF와 대조하세요.
이 파일은 검토 초안이며 원문 근거 승인이나 등급을 생성하지 않습니다.</p>
<label>검토자 이름 <input id="reviewer" autocomplete="off"></label>
<label>기업 <select id="company"><option value="">전체</option></select></label>
<button id="export">검토 JSON 내려받기</button><span id="status" role="status"></span>
<p><small>작성 내용은 자동 저장되지 않습니다. 종료 전에 내려받으세요.
판단 불가·미검토는 null로 남깁니다.</small></p>
</header><main id="cases"></main><script type="application/json" id="packet">"""
        + data
        + """</script>
<script>
const packet=JSON.parse(document.querySelector('#packet').textContent);
const container=document.querySelector('#cases'), fields=new Map();
function node(tag,text){
 const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;
}
for(const company of [...new Set(packet.cases.map(c=>c.company_id))].sort()){
 const option=node('option',company);option.value=company;
 document.querySelector('#company').append(option);
}
for(const c of packet.cases){
 const card=node('section');card.dataset.company=c.company_id;
 card.append(node('h2',c.company_id+' · PDF '+c.page+'쪽 · '+c.case_id));
 card.append(node('small','원본 SHA-256: '+c.source_sha256));
 if(Array.isArray(c.source.cells)){
  const scroll=node('div');scroll.className='table';const table=node('table');let row=-1,tr;
  for(const cell of c.source.cells){
   if(cell.row!==row){tr=node('tr');table.append(tr);row=cell.row;}
   const td=node('td',cell.text);td.rowSpan=cell.row_span;td.colSpan=cell.column_span;
   if('r'+cell.row+'c'+cell.column===c.source.target)td.className='target';tr.append(td);
  }scroll.append(table);card.append(scroll);
 }else card.append(node('pre',JSON.stringify(c.source,null,2)));
 const label=node('label','검토 답안 JSON · 원문 표현 그대로');
 const textarea=node('textarea');textarea.value='null';textarea.id='answer-'+c.case_id;
 label.htmlFor=textarea.id;card.append(label,textarea);
 card.append(node('small','형식: {"metric_path":["상위 지표","하위 항목"],' +
 '"unit":"원문 단위","year":"2024",' +
 '"qualifier":"target 또는 actual 또는 unqualified_bare_year","value":"원문 값"}'));
 fields.set(c.case_id,textarea);container.append(card);
}
document.querySelector('#company').onchange=e=>{
 for(const card of container.children)
  card.hidden=!!e.target.value&&card.dataset.company!==e.target.value;
};
document.querySelector('#export').onclick=()=>{
 const status=document.querySelector('#status');
 const reviewer=document.querySelector('#reviewer').value.trim();
 try{
  if(!reviewer)throw Error('검토자 이름을 입력하세요.');
  const labels={};for(const [id,field] of fields){
   try{labels[id]=JSON.parse(field.value);}catch{
    field.focus();throw Error(id+': JSON 형식을 확인하세요.');}
  }
  const review={packet_sha256:packet.packet_sha256,reviewer_kind:'human',
   reviewer_id:reviewer,labels};
  const url=URL.createObjectURL(
   new Blob([JSON.stringify(review,null,2)],{type:'application/json'}));
  const link=node('a');link.href=url;link.download='human-review.json';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);status.textContent='검토 파일을 내려받았습니다.';
 }catch(error){status.textContent=error.message;}
};
</script></html>"""
    )
