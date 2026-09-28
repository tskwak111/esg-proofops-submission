"""CLI for C1-C4 linkage exchange.

Two subcommands:

  build-packet   Build one strict1.1 input packet from a trusted local run's
                 claim + the confirmed tags of its LATEST ACCEPTED immutable
                 tag head, plus a caller-supplied JSON FinancialContext file.
                 Writes the packet JSON, or a blocked/not_run report, to
                 stdout. Never calls the reconciliation engine or invents
                 identity/financial fields.

  verify-return  Structurally validate a return triple (packet, policy,
                 result) using the included contract validator, then
                 additionally byte-verify every sources[] entry against real
                 opened local bytes (sustainability side via the app's own
                 UploadService, financial side via explicit caller-supplied
                 local paths -- never a URL and never a path taken from the
                 packet/result itself).

No paid/API calls, no network access, no new third-party dependency (only
stdlib + the already-installed jsonschema).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LOCAL_FIXTURE_CONTRACT_DIR = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/linkage-contract"
)


def resolve_contract_dir(contract_dir: str | Path | None = None) -> Path:
    if contract_dir:
        p = Path(contract_dir)
        try:
            with (p / "validate.py").open("rb"):
                return p
        except (PermissionError, OSError, FileNotFoundError):
            pass
    return LOCAL_FIXTURE_CONTRACT_DIR


CONTRACT_DIR_DEFAULT = resolve_contract_dir()


def _load_financial_context(path: Path):
    from proofops.application.linkage_exchange import (
        C3Context,
        C4Context,
        FinancialContext,
        FinancialFact,
        FinancialSource,
    )

    data = json.loads(path.read_text())
    financial = FinancialFact(**data["financial"])
    sources = tuple(FinancialSource(**s) for s in data.get("financial_sources", []))
    c3 = C3Context(**data["c3_context"]) if data.get("c3_context") else None
    c4 = C4Context(**data["c4_context"]) if data.get("c4_context") else None
    kwargs = {
        k: v
        for k, v in data.items()
        if k not in ("financial", "financial_sources", "c3_context", "c4_context")
    }
    return FinancialContext(
        **kwargs, financial=financial, financial_sources=sources, c3_context=c3, c4_context=c4
    )


def _validate_packet_shape(packet: dict, *, contract_dir: Path | str) -> list[dict]:
    """Structural-only schema check, reusing validate.py's own schema loader.

    This CLI (not the pure application module) owns filesystem access to
    the included contract schemas; it imports `validate.py`
    directly so the SAME schema-loading code path (and any future change to
    it) is used rather than a duplicated copy.
    """
    resolved = resolve_contract_dir(contract_dir)
    sys.path.insert(0, str(resolved))
    from validate import load_schema_validators  # type: ignore[import-not-found]

    validator = load_schema_validators()["input"]
    problems = []
    for err in sorted(validator.iter_errors(packet), key=lambda e: list(e.path)):
        loc = "/".join(str(part) for part in err.path)
        problems.append({"where": loc or "input", "error": "schema", "detail": err.message})
    return problems


class ReviewedHeadRejected(Exception):
    """The stored tag head is malformed, foreign, or not the atomic head.

    Raised instead of silently falling back to an older revision or to the
    original model consensus: a head this CLI cannot fully account for is a
    block, never an accept.
    """


def reviewed_head_tags(claims, claim, *, tenant_id: str, run_id: str, claim_id: str):
    """Confirmed tags of the LATEST ACCEPTED immutable tag head for one claim.

    The previous implementation read `LocalTagStore.load_inputs(...)
    .consensus.confirmed_tags`, which is the ORIGINAL model-agreement snapshot
    frozen when the tag stage published (tag_revision 1). Every later accepted
    human/AI-delegated review writes a new immutable `tag_revision` row and
    moves the atomic `claim_head`; the tag-stage snapshot is never rewritten.
    A consumer reading the snapshot therefore keeps serving pre-review tags
    forever -- including a `present` trigger a later review removed.

    This reuses the same canonical head reader the API and the export path
    use (`LocalClaimStore.current_tag`, one atomic `claim_head` read; never
    `history()[-1]` and never a raw global SQL scan of `tag_revision`), and
    the same integrity pins `export_store` enforces:

    * the head payload's tenant/document version/claim/revision must equal the
      atomic `claim_head`'s own `tag_revision` (read in the same transaction,
      never the row's self-reported `tag_revision`) and this claim's trusted
      identity, so a malformed, cross-tenant or stale-revision row is blocked;
    * when the head names an `input_snapshot_sha256` (every reviewed revision
      does), it must still hash the immutable revision-1 inputs, so the live
      immutable input replay stays pinned;
    * facts are rebuilt through the existing `_source_ref_from_dict` +
      `ConfirmedFact`/`ConfirmedTags` validation (the same helper
      `analysis_store` uses), so the stored ref shape, the required page/offset/
      hash/quote fields and the engine's own present-requires-verified-citation
      invariant are re-checked instead of being trusted as raw JSON. That is a
      shape and invariant check only: it does NOT re-open the original document
      bytes, so it is not a citation replay and is not reported as one.

    Returns `(ConfirmedTags | None, tag_revision | None)`. `None` tags mean the
    head itself carries no confirmed tags (unconfirmed/unknown consensus, or no
    published head at all); that is reported as missing tags by `build_packet`
    and is never backfilled from the stale snapshot. With no review at all the
    head is revision 1, whose stored `confirmed_tags` is exactly the model
    consensus, so the no-review behaviour is unchanged.
    """
    from proofops.domain.errors import DomainValidationError
    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags
    from proofops.domain.values import _source_ref_from_dict

    jobs = claims.store.jobs
    with jobs._transaction() as db:
        current = claims.current_tag(tenant_id, run_id, claim_id, connection=db)
        if current is None:
            return None, None
        tag = current["tag"]
        # `current_tag` returns only tag/decision/epoch, so the row's own
        # `tag_revision` is self-reported. The authority is the atomic
        # `claim_head`, read here in the same transaction; a row that agrees
        # with itself but not with the head is blocked, exactly as
        # `export_store` blocks it.
        head_revision = jobs._get(db, tenant_id, run_id, "claim_head", claim_id)["tag_revision"]
        if tag.get("tag_revision") != head_revision:
            raise ReviewedHeadRejected(
                f"head row tag_revision ({tag.get('tag_revision')}) does not match the atomic "
                f"claim_head tag_revision ({head_revision})"
            )
        raw = tag.get("confirmed_tags")
        if raw is None:
            return None, head_revision
        if not isinstance(raw, dict):
            raise ReviewedHeadRejected("head confirmed_tags is not an object")
        if (
            raw.get("tenant_id"),
            raw.get("document_version_id"),
            raw.get("claim_id"),
            raw.get("tag_revision"),
        ) != (tenant_id, claim.document_version_id, claim_id, head_revision):
            raise ReviewedHeadRejected(
                "head confirmed_tags identity/revision does not match the atomic claim head "
                f"(tenant/version/claim/revision expected {tenant_id}/"
                f"{claim.document_version_id}/{claim_id}/{head_revision})"
            )
        snapshot_sha256 = tag.get("input_snapshot_sha256")
        if snapshot_sha256 is not None:
            initial = jobs._get(db, tenant_id, run_id, "tag_revision", f"{claim_id}:0000000001")
            inputs = tag.get("inputs", (initial or {}).get("inputs"))
            if inputs is None or snapshot_sha256 != canonical_hash(inputs):
                raise ReviewedHeadRejected(
                    "head input_snapshot_sha256 does not match the immutable revision-1 inputs"
                )
    try:
        confirmed = ConfirmedTags(
            **(
                raw
                | {
                    "facts": tuple(
                        ConfirmedFact(
                            **(
                                fact
                                | {
                                    "evidence_refs": tuple(
                                        _source_ref_from_dict(ref) for ref in fact["evidence_refs"]
                                    )
                                }
                            )
                        )
                        for fact in raw["facts"]
                    )
                }
            )
        )
    except (DomainValidationError, TypeError, KeyError, AttributeError) as exc:
        raise ReviewedHeadRejected(f"malformed head confirmed_tags: {exc}") from exc
    return confirmed, head_revision


def _cmd_build_packet(args: argparse.Namespace) -> int:
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.linkage_exchange import BlockedPacket, C2PeriodContext, build_packet
    from proofops.application.registry import Registry
    from proofops.application.uploads import UploadService

    database_path = Path(args.database_path)
    if database_path.is_dir():
        for candidate in ("state.sqlite3", "storage.db", "runs.sqlite"):
            if (database_path / candidate).is_file():
                database_path = database_path / candidate
                break

    registry = Registry.sqlite(database_path)
    uploads_db = database_path
    for candidate in ("uploads.sqlite", "state.sqlite3", "storage.db"):
        if (database_path.parent / candidate).is_file():
            uploads_db = database_path.parent / candidate
            break

    uploads = UploadService(uploads_db, database_path.parent / "objects", registry)
    run_store = LocalSQLiteRunStore(database_path)

    if (database_path.parent / "parser-prepared").is_dir():
        parser_dir = database_path.parent / "parser-prepared"
    elif (database_path.parent / "prepared").is_dir():
        parser_dir = database_path.parent / "prepared"
    else:
        parser_dir = database_path.parent

    parser = OpenDataLoaderParser(parser_dir)
    claims = LocalClaimStore(run_store, uploads, parser)

    try:
        claim = claims.get(args.tenant_id, args.run_id, args.claim_id)
    except (KeyError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "execution_state": "blocked",
                    "reason": "claim_not_found",
                    "detail": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    # Finding 1: trusted company must derive from
    # uploads.version_snapshot(tenant,claim.document_version_id)->document_id
    # then uploads.get_document(tenant,document_id). A CLI --trusted-company-id argument is
    # NOT trusted.
    # Use actual frozen report period, not arbitrary CLI replacement.
    # Company/doc/version/period mismatches reject.
    try:
        snap = uploads.version_snapshot(args.tenant_id, claim.document_version_id)
        doc_id = snap.get("document_id")
        if not doc_id:
            print(
                json.dumps(
                    {
                        "execution_state": "blocked",
                        "reason": "document_not_found",
                        "detail": "version snapshot missing document_id",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        doc = uploads.get_document(args.tenant_id, doc_id)
        trusted_company_id = doc.get("company_id")
        meta = snap.get("metadata") or {}
        frozen_period_start = meta.get("period_start")
        frozen_period_end = meta.get("period_end")
        # Explicit publication metadata only; created_at is NOT report publication date
        sr_published_at = meta.get("sr_published_at") or meta.get("publication_date")
    except Exception as exc:
        print(
            json.dumps(
                {
                    "execution_state": "blocked",
                    "reason": "upload_snapshot_error",
                    "detail": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    cli_trusted_co = getattr(args, "trusted_company_id", None)
    if cli_trusted_co is not None and cli_trusted_co != trusted_company_id:
        print(
            json.dumps(
                {
                    "execution_state": "blocked",
                    "reason": "company_mismatch",
                    "detail": f"CLI --trusted-company-id ({cli_trusted_co}) does not match "
                    "trusted company ({trusted_company_id}) from uploads store",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    cli_period_start = getattr(args, "period_start", None)
    if cli_period_start is not None and cli_period_start != frozen_period_start:
        print(
            json.dumps(
                {
                    "execution_state": "blocked",
                    "reason": "period_mismatch",
                    "detail": f"CLI --period-start ({cli_period_start}) does not match "
                    "frozen period_start ({frozen_period_start}) from uploads store",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    cli_period_end = getattr(args, "period_end", None)
    if cli_period_end is not None and cli_period_end != frozen_period_end:
        print(
            json.dumps(
                {
                    "execution_state": "blocked",
                    "reason": "period_mismatch",
                    "detail": f"CLI --period-end ({cli_period_end}) does not match frozen "
                    "period_end ({frozen_period_end}) from uploads store",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    cli_published_at = getattr(args, "sr_published_at", None)
    if cli_published_at is not None:
        if sr_published_at is None or cli_published_at != sr_published_at:
            print(
                json.dumps(
                    {
                        "execution_state": "blocked",
                        "reason": "period_mismatch",
                        "detail": f"CLI argument --sr-published-at ({cli_published_at}) "
                        "is not an approved trusted publication source",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

    confirmed_tags = None
    try:
        # Latest ACCEPTED immutable tag head, read through the app's own atomic
        # claim_head reader -- not the tag-stage model-consensus snapshot, which
        # keeps serving pre-review tags after every accepted re-review.
        confirmed_tags, _head_revision = reviewed_head_tags(
            claims, claim, tenant_id=args.tenant_id, run_id=args.run_id, claim_id=args.claim_id
        )
    except ReviewedHeadRejected as exc:
        print(
            json.dumps(
                {
                    "execution_state": "blocked",
                    "reason": "unaccountable_review_head",
                    "detail": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    except (KeyError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "execution_state": "blocked",
                    "reason": "review_head_unreadable",
                    "detail": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    financial_context = None
    if args.financial_context:
        financial_context = _load_financial_context(Path(args.financial_context))

    period_context = None
    if getattr(args, "c2_period_context", None):
        try:
            period_context = C2PeriodContext(**json.loads(Path(args.c2_period_context).read_text()))
            if args.item != "C2":
                raise ValueError("period context is only supported for C2")
        except (OSError, ValueError, TypeError) as exc:
            print(
                json.dumps(
                    {
                        "execution_state": "blocked",
                        "reason": "invalid_c2_period_context",
                        "detail": str(exc),
                    }
                )
            )
            return 1

    result = build_packet(
        claim=claim,
        tags=confirmed_tags,
        financial_context=financial_context,
        item=args.item,
        tenant_id=args.tenant_id,
        period_start=frozen_period_start,
        period_end=frozen_period_end,
        sr_published_at=sr_published_at,
        trusted_company_id=trusted_company_id,
        c2_period_context=period_context,
    )
    if isinstance(result, BlockedPacket):
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 1

    # Build-packet should byte-verify before outputting non-synthetic ready packet
    # using existing linkage_reader (explicit financial local paths if required; absent cleanblock).
    if not result.get("synthetic", True):
        from proofops.adapters.local.linkage_reader import (
            LinkageVerificationError,
            verify_packet_sources,
        )

        financial_local_paths = {}
        if getattr(args, "financial_local_paths", None):
            financial_local_paths = json.loads(Path(args.financial_local_paths).read_text())
        try:
            verify_packet_sources(
                result,
                uploads=uploads,
                tenant_id=result["identity"]["tenant_id"],
                financial_local_paths=financial_local_paths,
            )
        except (LinkageVerificationError, Exception) as exc:
            print(
                json.dumps(
                    {
                        "execution_state": "blocked",
                        "reason": "unverified_source_bytes",
                        "detail": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

    problems = _validate_packet_shape(result, contract_dir=resolve_contract_dir(args.contract_dir))
    if problems:
        print(
            json.dumps(
                {"execution_state": "blocked", "reason": "schema_invalid", "detail": problems},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_verify_return(args: argparse.Namespace) -> int:
    contract_dir = resolve_contract_dir(args.contract_dir)
    sys.path.insert(0, str(contract_dir))
    from validate import validate_return  # type: ignore[import-not-found]

    errors = validate_return(args.input, args.policy, args.output)
    if errors:
        print(json.dumps({"status": "rejected", "errors": errors}, ensure_ascii=False, indent=2))
        return 1

    if args.skip_byte_verification:
        print(
            json.dumps(
                {
                    "status": "structurally_valid",
                    "byte_verification": "skipped",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    from proofops.adapters.local.linkage_reader import (
        LinkageVerificationError,
        verify_packet_sources,
    )
    from proofops.application.registry import Registry
    from proofops.application.uploads import UploadService

    packet = json.loads(Path(args.input).read_text())
    financial_local_paths = {}
    if args.financial_local_paths:
        financial_local_paths = json.loads(Path(args.financial_local_paths).read_text())

    database_path = Path(args.database_path)
    registry = Registry.sqlite(database_path)
    uploads = UploadService(database_path, database_path.parent / "objects", registry)

    try:
        verified = verify_packet_sources(
            packet,
            uploads=uploads,
            tenant_id=packet["identity"]["tenant_id"],
            financial_local_paths=financial_local_paths,
        )
    except LinkageVerificationError as exc:
        print(
            json.dumps(
                {"status": "rejected", "errors": [{"where": "sources", "error": str(exc)}]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            {"status": "structurally_valid", "byte_verification": verified},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-packet", help="build one strict1.1 packet from a trusted run")
    build.add_argument("--tenant-id", required=True)
    build.add_argument("--run-id", required=True)
    build.add_argument("--claim-id", required=True)
    build.add_argument("--item", required=True, choices=("C1", "C2", "C3", "C4"))
    build.add_argument(
        "--c2-period-context",
        help="JSON normalized/source_id/quote; must match confirmed fact evidence",
    )
    build.add_argument("--database-path", required=True)
    build.add_argument(
        "--financial-context",
        help="path to a JSON FinancialContext file; omit to see the "
        "missing_financial_context block",
    )
    build.add_argument("--period-start")
    build.add_argument("--period-end")
    build.add_argument("--sr-published-at")
    build.add_argument(
        "--trusted-company-id",
        default=None,
        help="a real company identity from the app's own trusted run/company record, if one "
        "exists. Omit to require financial_context.synthetic=True as the only way to "
        "supply an (unverified) company_id.",
    )
    build.add_argument(
        "--financial-local-paths",
        help=(
            "path to a JSON {source_id: local_path} map for non-sustainability sources "
            "(required for non-synthetic ready packet byte verification)"
        ),
    )
    build.add_argument("--contract-dir", default=str(CONTRACT_DIR_DEFAULT))
    build.set_defaults(func=_cmd_build_packet)

    verify = sub.add_parser(
        "verify-return", help="validate.py return-mode PLUS byte-level source verification"
    )
    verify.add_argument("--input", required=True)
    verify.add_argument("--policy", required=True)
    verify.add_argument("--output", required=True)
    verify.add_argument("--database-path", help="required unless --skip-byte-verification")
    verify.add_argument(
        "--financial-local-paths",
        help="path to a JSON {source_id: local_path} map for non-sustainability sources",
    )
    verify.add_argument("--contract-dir", default=str(CONTRACT_DIR_DEFAULT))
    verify.add_argument(
        "--skip-byte-verification",
        action="store_true",
        help="run only validate.py's structural gate, no byte-level source checks",
    )
    verify.set_defaults(func=_cmd_verify_return)

    args = parser.parse_args(argv)
    if (
        args.command == "verify-return"
        and not args.skip_byte_verification
        and not args.database_path
    ):
        parser.error("verify-return requires --database-path unless --skip-byte-verification")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
