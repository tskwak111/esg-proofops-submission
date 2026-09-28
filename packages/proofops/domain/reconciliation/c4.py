"""C4 classification basis.

The question is whether the company disclosed a definition and a calculation
basis for the classification it claims, not whether two reported figures agree
and not whether the definition is a sound accounting classification. When the
required search has not been proven complete, a missing basis is unknown rather
than absent.
"""

from __future__ import annotations

from .common import Context, Outcome, blocked, completed

_REASON_BY_EXPLANATION = {
    "definition": "definition_not_found",
    "calculation_basis": "calculation_basis_not_found",
}
_SOURCES_BY_EXPLANATION = {
    "definition": "definition_source_ids",
    "calculation_basis": "calculation_source_ids",
}


def evaluate(context: Context) -> Outcome:
    mismatch = context.require_kind("classification")
    if mismatch is not None:
        return mismatch

    unresolved = context.unresolved_values()
    if unresolved is not None:
        return unresolved

    c4_context = context.packet["c4_context"]
    required = context.policy["c4_required_explanations"]

    cited: list[str] = []
    missing: list[str] = []
    for explanation in required:
        source_ids = c4_context[_SOURCES_BY_EXPLANATION[explanation]]
        if source_ids:
            cited.extend(source_ids)
        else:
            missing.append(_REASON_BY_EXPLANATION[explanation])

    ordered_cited = tuple(dict.fromkeys(cited))

    if not missing:
        return completed("matched", "classification_basis_present", sources=ordered_cited)

    if not context.search_complete:
        return blocked("search_incomplete", sources=ordered_cited)

    return completed("needs_explanation", *missing, sources=ordered_cited)
