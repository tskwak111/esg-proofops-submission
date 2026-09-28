"""C3 investment commitment versus disclosed capital expenditure.

Every amount is an exact ``Decimal``. The threshold test is written as a
multiplication, ``commitment <= threshold * capex``, so no division is performed
and a ratio can never be coerced into infinity or zero. Nothing is decided until
the operator has actually approved a threshold and an account mapping: an
unapproved policy blocks, it does not fall back to the 5.0 figure that appears as
an illustration in the specification.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext

from .common import Context, Outcome, blocked, completed, parse_decimal

_CONTEXT_PERIOD_KEYS = (
    "target_period_start",
    "target_period_end",
    "capex_period_start",
    "capex_period_end",
)


def _within_threshold(commitment: Decimal, threshold: Decimal, capex: Decimal) -> bool:
    """``commitment <= threshold * capex`` evaluated without rounding either side.

    The default decimal context keeps 28 significant digits, which is not enough
    for disclosed KRW figures; the product is therefore computed in a local
    context wide enough to hold every digit of both operands exactly.
    """
    digits = sum(len(value.as_tuple().digits) for value in (commitment, threshold, capex))
    with localcontext() as context:
        context.prec = digits + 1
        return commitment <= threshold * capex


def _with_sources(outcome: Outcome, extra: tuple[str, ...]) -> Outcome:
    if not extra:
        return outcome
    return replace(outcome, extra_source_ids=tuple(extra) + outcome.extra_source_ids)


def _policy_resolved(context: Context) -> bool:
    policy = context.policy
    if policy["c3_threshold"] is None:
        return False
    if not policy["c3_account_mapping_approved"]:
        return False
    allowed = set(policy["allowed_capex_account_ids"])
    if not allowed:
        return False
    requested = context.packet["c3_context"]["capex_account_ids"]
    if not requested:
        return False
    return set(requested) <= allowed


def evaluate(context: Context) -> Outcome:
    claim = context.packet["claim"]
    if claim["track"] != "goal" or "currency_amount" not in claim["trigger_elements"]:
        # A plain emissions target is not an investment commitment; C3 simply does
        # not apply to it, which is a verified fact rather than a failure.
        return completed("not_applicable", "c3_trigger_absent")

    c3_context = context.packet["c3_context"]
    extra = tuple(
        value
        for value in (c3_context["commitment_source_id"], c3_context["funding_plan_source_id"])
        if value is not None
    )

    mismatch = context.require_kind("currency_amount")
    if mismatch is not None:
        return _with_sources(mismatch, extra)

    unresolved = context.unresolved_values()
    if unresolved is not None:
        return _with_sources(unresolved, extra)

    currency = c3_context["currency"]
    if context.sustainability["unit"] != currency or context.financial["unit"] != currency:
        return _with_sources(blocked("c3_currency_mismatch"), extra)

    if not _policy_resolved(context):
        return _with_sources(blocked("c3_policy_unapproved"), extra)

    if any(c3_context[key] is None for key in _CONTEXT_PERIOD_KEYS):
        return _with_sources(blocked("c3_period_unresolved"), extra)

    commitment = parse_decimal(context.sustainability["normalized"], "sustainability.normalized")
    capex = parse_decimal(context.financial["normalized"], "financial.normalized")
    if commitment <= 0:
        return _with_sources(blocked("c3_commitment_not_positive"), extra)
    if capex <= 0:
        return _with_sources(blocked("c3_capex_not_positive"), extra)

    if c3_context["commitment_source_id"] is not None:
        return _with_sources(completed("matched", "commitment_disclosed"), extra)

    threshold = parse_decimal(context.policy["c3_threshold"], "policy.c3_threshold")
    if _within_threshold(commitment, threshold, capex):
        return _with_sources(completed("matched", "capex_within_threshold"), extra)

    return _with_sources(context.resolve_difference(explained_reason="difference_explained"), extra)
