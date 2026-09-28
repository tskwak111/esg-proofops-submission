"""Callable route: stored real parser graph + declared opinion -> published assurance.

The one end-to-end path R06a asked for:

    trusted graph (load_run_graph, already-committed run)
      -> caller declares an opinion boundary by source_id
      -> UpstageAssuranceExtractor round-trips ONE real model call (receipts
         retained before any trust decision)
      -> extract_assurance re-validates every returned quote against the
         graph's own raw text (never trusts the model's offsets)
      -> LocalAssuranceStore.publish re-verifies again against the run's
         committed graph and stores immutably
      -> already-existing GET /v1/runs/{run_id}/assurance serves it (no API
         change needed; composition.py already wires LocalAssuranceStore).

This module makes no model/API call on its own authority: `run_assurance_producer`
takes an already-authorized `probe` (or a synthetic one for local/offline use)
from the caller, exactly like `UpstageAssuranceExtractor`/`UpstageClaimExtractor`
require. No credentials are read here. Coordinator/user-owned callers decide
whether `probe` is a real paid `UpstageProbe` or a local synthetic stand-in.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from proofops.adapters.local.assurance_store import LocalAssuranceStore
from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.adapters.local.upstage_assurance import UpstageAssuranceExtractor
from proofops.application.assurance import extract_assurance
from proofops.application.assurance_producer import select_opinion_boundary
from proofops.application.ports.models import ModelBinding


def run_assurance_producer(
    *,
    store,
    uploads,
    parser,
    tenant_id: str,
    run_id: str,
    source_ids: Sequence[str],
    probe,
    receipts: Path | str,
    statement_id: str | None = None,
    request_id: str | None = None,
    binding: ModelBinding | None = None,
) -> dict:
    """Run the whole producer route once and publish the result.

    Returns a small, honest status dict — it never claims "covered" or
    "verified" beyond what `extract_assurance`/`LocalAssuranceStore.publish`
    themselves determined. On any failure (transport, schema, re-verification)
    this raises rather than returning a partial/guessed status; the caller
    must treat that as not_run, never as "no assurance found".
    """
    graph = load_run_graph(store, uploads, parser, tenant_id=tenant_id, run_id=run_id)
    boundary, _texts = select_opinion_boundary(graph, source_ids)
    extractor = UpstageAssuranceExtractor(probe, receipts)
    tagged_fields = extractor.extract_tagged_fields(
        graph, boundary, request_id=request_id or str(uuid4())
    )
    if not tagged_fields:
        return {
            "status": "not_run",
            "reason": "model returned no recognized assurance fields for this boundary",
            "statement_id": None,
            "semantic_hash": None,
        }
    # Preserve the declared opinion, including uncited qualifications/exclusions.
    # Model-selected fields alone cannot establish that the opinion was readable.
    blocks = {block.source_id: block for block in graph.blocks}
    selected_refs = tuple(blocks[source_id].source_ref() for source_id in boundary.source_ids)
    statement = extract_assurance(
        graph,
        selected_refs,
        binding or ModelBinding(f"upstage-{extractor.model_sha256[:12]}", "assurance", False),
        tagged_fields=tagged_fields,
        tenant_id=tenant_id,
        statement_id=statement_id or str(uuid4()),
        model_sha256=extractor.model_sha256,
        prompt_sha256=extractor.prompt_sha256,
        replicate_id=1,
    )
    assurance_store = LocalAssuranceStore(store, uploads, parser)
    semantic_hash = assurance_store.publish(tenant_id, run_id, statement)
    return {
        "status": "published",
        "statement_id": statement.statement_id,
        "semantic_hash": semantic_hash,
        "unresolved_fields": list(statement.unresolved_fields),
        "provider": statement.provider,
        "level": statement.level,
        "reporting_period": statement.reporting_period,
    }


def _cli(argv: list[str] | None = None) -> int:
    """Thin CLI wrapper over an existing local SQLite composition directory.

    Coordinator/user owns the actual paid invocation decision; this refuses
    to run rather than silently falling back to a fabricated result if no
    --key-file is supplied. The key is read from a local file (same
    UPSTAGE_API_KEY=... line convention as evaluation/cross_report_probe.py
    and evaluation/preliminary_probe.py) and is never accepted as a
    command-line argument (would leak via process listings/shell history)
    and never printed or logged. Composes storage directly (LocalSQLiteRunStore/
    UploadService/OpenDataLoaderParser/RunService/Registry) rather than
    importing apps_api.composition, so this stays a small, dependency-light
    script rather than pulling in the FastAPI app just to run one producer call.
    """
    parser_args = argparse.ArgumentParser(description=__doc__)
    parser_args.add_argument("--tenant-id", required=True)
    parser_args.add_argument("--run-id", required=True)
    parser_args.add_argument(
        "--database-path", required=True, help="Same LOCAL_DATABASE_PATH the app/worker used"
    )
    parser_args.add_argument(
        "--source-id",
        action="append",
        required=True,
        dest="source_ids",
        help="Repeatable: one graph source_id per opinion block (declared boundary)",
    )
    parser_args.add_argument("--receipts", required=True)
    parser_args.add_argument("--model", choices=("solar-pro3", "solar-pro4"), default="solar-pro4")
    parser_args.add_argument(
        "--ledger",
        default=str(Path(__file__).resolve().parents[1] / ".local/upstage/budget.sqlite3"),
        help="Shared USD20-cumulative Upstage budget ledger. Defaults to the SAME "
        "ledger every other real evaluation script in this repo uses "
        "(APP/.local/upstage/budget.sqlite3) so this call is counted against the "
        "one true cumulative total rather than opening an isolated pool.",
    )
    parser_args.add_argument(
        "--key-file",
        type=Path,
        default=Path(__file__).resolve().parents[3] / ".env.upstage.local",
        help="File containing a UPSTAGE_API_KEY=... line (same convention as "
        "evaluation/cross_report_probe.py / preliminary_probe.py). Defaults to "
        "ROOT/.env.upstage.local (the actual file already on disk here), not "
        "APP/.env.upstage.local (which does not exist). Never pass the key "
        "itself on the command line.",
    )
    args = parser_args.parse_args(argv)

    ledger_path = Path(args.ledger)
    if not ledger_path.is_file():
        print(f"shared ledger not found at {ledger_path}; refusing to run (no paid call made)")
        return 1
    if not args.key_file.is_file():
        print(f"no key file at {args.key_file}; refusing to run (no paid call made)")
        return 1
    try:
        api_key = next(
            line.split("=", 1)[1].strip().strip('"').strip("'")
            for line in args.key_file.read_text().splitlines()
            if line.startswith("UPSTAGE_API_KEY=")
        )
    except StopIteration:
        print("no UPSTAGE_API_KEY= line in key file; refusing to run (no paid call made)")
        return 1

    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.adapters.local.upstage import UpstageProbe
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.registry import Registry
    from proofops.application.runs import RunService
    from proofops.application.uploads import UploadService

    database_path = Path(args.database_path)
    registry = Registry.sqlite(database_path)
    uploads = UploadService(database_path, database_path.parent / "objects", registry)
    run_store = LocalSQLiteRunStore(database_path)
    runs = RunService(run_store, uploads, registry)
    parser = OpenDataLoaderParser(database_path.parent / "parser-prepared")
    probe = UpstageProbe(api_key, ledger_path, model=args.model)
    del api_key  # do not retain the key in a local past this point
    result = run_assurance_producer(
        store=runs.store,
        uploads=uploads,
        parser=parser,
        tenant_id=args.tenant_id,
        run_id=args.run_id,
        source_ids=args.source_ids,
        probe=probe,
        receipts=Path(args.receipts),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
