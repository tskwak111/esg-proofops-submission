"""Opt-in, run-pinned deterministic M3 fact assembly."""

from dataclasses import asdict, replace

from proofops.application.evidence.report_level import GRI_ASSURED_PAGE_V1, check_report_level
from proofops.application.evidence.span_citations import span_verified_graph, verify_source_ref
from proofops.domain.provenance import canonical_hash
from proofops.domain.rules.engine import MAPPINGS, ConfirmedFact
from proofops.domain.values import SourceRef

POLICY = "report-level-link-v1"
POLICIES = {"M3": GRI_ASSURED_PAGE_V1}
POLICY_HASH = canonical_hash({"policy": POLICY, "bindings": POLICIES})


def _bindings(config, *, replay=False):
    if (config.get("policy"), config.get("policy_hash")) == (POLICY, POLICY_HASH):
        return POLICIES
    raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID")


def strict_fallback(runs, packet, rulepack, tenant_id, tag_revision, profile):
    """Use guarded primitive facts only for an explicitly opted-in strict run."""
    if profile != "strict-v1":
        return None
    from proofops.application.tagging.consensus import PARTIAL_FACTS_V1, form_consensus

    return form_consensus(
        runs,
        packet=packet,
        rulepack=rulepack,
        tenant_id=tenant_id,
        tag_revision=tag_revision,
        profile=PARTIAL_FACTS_V1,
    ).confirmed_tags


def validate_config(config, *, replay=False):
    """The run creator pins exact source refs or post-parse span locators."""
    if not isinstance(config, dict) or set(config) != {"policy", "policy_hash", "refs"}:
        raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID")
    refs = config["refs"]
    if isinstance(refs, dict) and "M2" in refs:
        raise ValueError("REPORT_LEVEL_LINK_M3_ONLY")
    bindings = _bindings(config, replay=replay)
    if not isinstance(refs, dict) or not refs or set(refs) - set(bindings):
        raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID")
    for element_id, raw in refs.items():
        if not isinstance(raw, list) or len(raw) != 3:
            raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID")
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID")
            if "source_id" in item:
                try:
                    ref = SourceRef(**item)
                except (TypeError, ValueError, KeyError) as exc:
                    raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID") from exc
                if ref.verification_state != "candidate":
                    raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID")
            elif (
                not {"page_num", "kind", "quote"} <= set(item)
                or set(item) - {"page_num", "kind", "quote", "block_text"}
                or type(item["page_num"]) is not int
                or item["page_num"] < 1
                or item["kind"] not in {"paragraph", "table_cell", "table_row"}
                or not isinstance(item["quote"], str)
                or not item["quote"].strip()
                or (
                    "block_text" in item
                    and (not isinstance(item["block_text"], str) or not item["block_text"])
                )
            ):
                raise ValueError("REPORT_LEVEL_LINK_CONFIG_INVALID")
    return config


def resolve_refs(graph, configured):
    refs = []
    for item in configured:
        if "source_id" in item:
            refs.append(SourceRef(**item))
            continue
        matches = [
            block
            for block in graph.blocks
            if block.page_num == item["page_num"]
            and block.kind == item["kind"]
            and item["quote"] in block.normalized_text
            and ("block_text" not in item or block.normalized_text == item["block_text"])
        ]
        if len(matches) != 1 or matches[0].normalized_text.count(item["quote"]) != 1:
            raise ValueError("REPORT_LEVEL_LINK_REF_AMBIGUOUS")
        start = matches[0].normalized_text.index(item["quote"])
        refs.append(
            matches[0].source_ref(
                normalized_char_start=start,
                normalized_char_end=start + len(item["quote"]),
            )
        )
    return tuple(refs)


def apply_report_level_link(
    consensus, *, config, context, graph, attest, fallback_tags=None, replay=False
):
    """Credit only unknown M3 after source attestation and literal binding."""
    validate_config(config, replay=replay)
    bindings = _bindings(config, replay=replay)
    tags = consensus.confirmed_tags or fallback_tags
    if tags is None or tags.track != "management":
        return consensus, ()
    page_texts = {}
    for block in graph.blocks:
        page_texts.setdefault(block.page_num, []).append(block.raw_text)
    claim_refs = tuple(
        verify_source_ref(ref, graph, tenant_id=context.claim.tenant_id)
        for ref in context.claim.source_refs
    )
    elements = list(consensus.candidate_elements)
    facts = {fact.name: fact for fact in tags.facts}
    receipts = []
    for element_id, raw in config["refs"].items():
        index = next((i for i, e in enumerate(elements) if e.element_id == element_id), None)
        if index is None or elements[index].state != "unknown":
            continue
        try:
            refs = resolve_refs(graph, raw)
            verified_graph, source_receipt = attest(refs)
            verified = tuple(
                verify_source_ref(ref, verified_graph, tenant_id=context.claim.tenant_id)
                for ref in refs
            )
        except (ValueError, KeyError, TypeError):
            continue
        credited_from = verified[1].source_id
        if not check_report_level(
            element_id,
            bindings[element_id],
            verified,
            claim_refs=claim_refs,
            claim_quote=context.claim.quote,
            page_texts=page_texts,
            credited_from=credited_from,
        ):
            continue
        elements[index] = replace(
            elements[index],
            state="present",
            evidence_refs=verified,
            normalized_value=None,
            credited_from=credited_from,
            reason_code=bindings[element_id],
        )
        for name in MAPPINGS["management"][element_id]:
            facts[name] = ConfirmedFact(
                name,
                "present",
                verified,
                context.claim.tenant_id,
                True,
                True,
                source_scope="global_bound",
            )
        receipts.append(
            dict(
                element_id=element_id,
                policy=bindings[element_id],
                rule_id=config["policy"],
                rule_hash=config["policy_hash"],
                config_ref_hash=canonical_hash(raw),
                refs=[asdict(ref) for ref in refs],
                credited_from=credited_from,
                source_receipt=source_receipt,
            )
        )
    confirmed = None
    reasons = consensus.reasons
    if consensus.confirmed_tags is not None:
        confirmed = replace(tags, facts=tuple(facts.values()))
        if (
            all(e.state == "present" for e in elements)
            and consensus.review_status == "needs_review"
        ):
            reasons = tuple(
                sorted(
                    {reason for reason in reasons if reason != "REVIEW:M3"}
                    | {"PARTIAL_FACTS_REVIEW_REQUIRED"}
                )
            )
    return replace(
        consensus,
        candidate_elements=tuple(elements),
        confirmed_tags=confirmed,
        review_status=consensus.review_status,
        reasons=reasons,
    ), tuple(receipts)


def replay_report_level_link(consensus, *, config, context, graph, receipts, fallback_tags=None):
    """Rebuild linked facts from frozen refs; storage additionally reattests PDF bytes."""
    validate_config(config, replay=True)
    bindings = _bindings(config, replay=True)
    if not isinstance(receipts, tuple | list) or len(receipts) > len(config["refs"]):
        raise ValueError("REPORT_LEVEL_LINK_REPLAY_MISMATCH")
    by_element = {}
    for receipt in receipts:
        element_id = receipt.get("element_id")
        if element_id in by_element or element_id not in config["refs"]:
            raise ValueError("REPORT_LEVEL_LINK_REPLAY_MISMATCH")
        if (
            receipt.get("policy") != bindings[element_id]
            or receipt.get("rule_id") != config["policy"]
            or receipt.get("rule_hash") != config["policy_hash"]
            or receipt.get("config_ref_hash") != canonical_hash(config["refs"][element_id])
            or canonical_hash(receipt.get("refs"))
            != canonical_hash(
                [asdict(ref) for ref in resolve_refs(graph, config["refs"][element_id])]
            )
            or not isinstance(receipt.get("source_receipt"), dict)
        ):
            raise ValueError("REPORT_LEVEL_LINK_REPLAY_MISMATCH")
        source = receipt["source_receipt"]
        if source.get("artifact_sha256") != canonical_hash(
            {key: value for key, value in source.items() if key != "artifact_sha256"}
        ):
            raise ValueError("REPORT_LEVEL_LINK_REPLAY_MISMATCH")
        by_element[element_id] = receipt

    def attest(refs):
        element_id = "M3"
        if element_id not in by_element:
            raise ValueError("uncredited")
        receipt = by_element[element_id]
        verified = tuple(replace(ref, verification_state="verified") for ref in refs)
        return span_verified_graph(
            graph,
            (*getattr(graph, "verified_spans", ()), *verified),
            receipt["source_receipt"]["artifact_sha256"],
        ), receipt["source_receipt"]

    result, reproduced = apply_report_level_link(
        consensus,
        config=config,
        context=context,
        graph=graph,
        attest=attest,
        fallback_tags=fallback_tags,
        replay=True,
    )
    if canonical_hash(reproduced) != canonical_hash(receipts):
        raise ValueError("REPORT_LEVEL_LINK_REPLAY_MISMATCH")
    return result
