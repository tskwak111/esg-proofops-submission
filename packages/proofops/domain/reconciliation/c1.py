"""C1 organizational boundary.

The approved identity rule is ``exact_verified_entity_set``: two boundaries agree
only when the verified identifier sets are equal. Cardinality is never a proxy for
identity, and ``policy.allowed_difference_types`` is a taxonomy that tells a
reviewer what kind of explanation to look for -- it is deliberately never read
here, because it must not act as an automatic pass list.
"""

from __future__ import annotations

from .common import SET_KINDS, Context, Outcome, blocked, completed, parse_entity_set


def evaluate(context: Context) -> Outcome:
    mismatch = context.require_kind(*SET_KINDS)
    if mismatch is not None:
        return mismatch

    unresolved = context.unresolved_values()
    if unresolved is not None:
        return unresolved

    sustainability = parse_entity_set(
        context.sustainability["normalized"], "sustainability.normalized"
    )
    financial = parse_entity_set(context.financial["normalized"], "financial.normalized")

    if not sustainability or not financial:
        # An unverifiable "all sites" phrase must never collapse into an empty set
        # that would then compare equal to another empty set.
        return blocked("entity_set_empty")

    if sustainability == financial:
        return completed("matched", "same_verified_entity_set")

    return context.resolve_difference(explained_reason="difference_explained")
