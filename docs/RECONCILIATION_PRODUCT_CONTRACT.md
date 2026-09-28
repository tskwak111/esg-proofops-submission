# Reconciliation local product integration v1

2026-09-21. User authorizes the remaining local product work and real OpenDART
collection with the supplied credential. Preserve G/P/M decisions. No inferred
accounting approvals, paid model calls, cloud deployment, or public release.

## Compatibility and migration before implementation

Keep input/policy/result 1.1 unchanged. C5 remains a separate disabled envelope.
Add local SQLite tables with the `reconciliation_` prefix and an explicit schema
version; never modify or delete existing runs, tags, decisions or exports.
Store immutable draft/import snapshots, review/policy audit events and result
revisions. A head revision advances under a transaction and If-Match. Old readers
can ignore these new tables; rollback disables the new router/UI and preserves
all snapshots. Unknown storage schema versions fail closed.

## Trust

Registration of a prepared case is a server/operator import, never an HTTP
request containing a trusted registry or local filesystem path. Imported document,
fact and coverage bindings remain draft until an authenticated reviewer confirms
the exact snapshot. Imported policy approval records are not trusted: an admin
must explicitly approve the bound policy. Synthetic approvals cannot authorize
real packets. Missing policy or review produces a visible blocked outcome.

Case identity must match the authorized run, its company/document version and an
existing verified claim. Preserve the claim/tag revision that supplied the input.
Source bytes are copied into a controlled immutable store, checked against hashes
and format-aware locators; no external URLs or arbitrary client filesystem reads.
Candidate extraction records original/derived hashes and exact locator lineage;
candidates do not become trusted facts automatically. Human review can confirm
facts and search coverage, never choose a result status or grade directly.

## HTTP surface

Existing cookie session, tenant authorization, CSRF/origin and rate limiting apply.
Mutations require Idempotency-Key and If-Match of the case head revision (quoted
integer). Repeat keys with different input conflict. Cross-tenant reads return
not found. Sources and snapshots use Cache-Control: no-store.

- `GET /v1/runs/{run_id}/claims/{claim_id}/reconciliation`: viewer, `{items: CaseDetail[]}`.
- `GET /v1/reconciliation/cases/{case_id}`: viewer, CaseDetail and ETag.
- `POST /v1/reconciliation/cases/{case_id}/review`: reviewer; body contains
  `reason`, `confirm_source_bindings: true`, `confirm_decision_bindings: true`,
  `confirm_search_coverage: boolean`. Confirms reviewed facts, not policy or verdict.
- `POST /v1/reconciliation/cases/{case_id}/policy-approval`: admin; `approved: boolean`,
  `reason`. Records actual actor/time and policy hash; no default C3 threshold.
- `POST /v1/reconciliation/cases/{case_id}/evaluate`: editor; empty object;
  recomputes with server-owned reviewed snapshots, appends immutable result, returns CaseDetail.
- `GET /v1/reconciliation/cases/{case_id}/revisions/{revision}`: viewer; immutable
  result/projection/provenance snapshot suitable for JSON export.
- `GET /v1/reconciliation/cases/{case_id}/sources/{source_id}/content`: viewer;
  hash-verified original bytes as attachment, never active HTML.

CaseDetail fields shared with UI: `case_id`, `run_id`, `claim_id`, `item`, `revision`,
`synthetic`, `review_state` (pending/reviewed), `policy_approved`, `packet`, `policy`,
`sources`, `latest_result` (null or `{revision, result, projection, created_at}`).
An evaluation can succeed at HTTP level with a blocked result. Inputs and operator
assertions must be distinguishable from verified result sources in the UI.

## Ownership and acceptance

공시 파이프라인과 재무 대사 연계는 동일한 신뢰 경계를 사용한다.

Acceptance: actual HTTP authorization/CSRF/CAS/idempotency, immutable replay,
cross-tenant/source-tamper rejection, unchanged GPM, reviewed vs pending behavior,
policy-unapproved C3, real-source lineage, browser failure and stale-response cases,
type/lint/build and focused regression. Real collection and actual-company semantic
accuracy are separate results. Missing external approval remains visibly pending.

## A-side C2 period input (2026-09-22)

`linkage_exchange_cli.py build-packet --item C2 --c2-period-context FILE` accepts
only `normalized`, `source_id`, `quote`. Dates use `YYYY-MM-DD/YYYY-MM-DD` and
must match one explicit ordered range in the literal. Korean full dates and ISO
dates are supported; ambiguous/multiple ranges remain blocked. No approval flag
is accepted. This adds no HTTP/DB field or migration; older C1/C3/C4 callers are
unchanged. C2 without a supported period stays blocked, replacing the former
incompatible `facility_set` output.

C2 requires a confirmed performance fact with an explicit numeric/unit literal;
categorical certification and year-only literals do not create a trigger. Units
are limited to the existing numeric domain's unit vocabulary. Period evidence
must belong to that fact, match its quote, and retain current-head identity and
source-byte verification. A separate report-wide period cannot silently become
an individual metric's period. Source-verified operator drafts remain distinct
from engine-ready packets and cannot approve policy, promote an unconfirmed tag,
or clear `local_synthetic`.
