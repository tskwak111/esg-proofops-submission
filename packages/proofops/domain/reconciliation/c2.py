"""C2 reporting period.

Periods are compared as real calendar dates, never as year labels, so a non
December fiscal year end and a multi-year table are handled by the same code.
Activity that lies wholly outside the declared sustainability reporting period is
a verified non-application, not a difference to explain.
"""

from __future__ import annotations

from .common import Context, Outcome, completed, parse_period


def _reporting_window(context: Context):
    identity = context.packet["identity"]
    start, end = identity["period_start"], identity["period_end"]
    if start is None or end is None:
        return None
    return parse_period(f"{start}/{end}", "identity reporting period")


def evaluate(context: Context) -> Outcome:
    mismatch = context.require_kind("period")
    if mismatch is not None:
        return mismatch

    unresolved = context.unresolved_values()
    if unresolved is not None:
        return unresolved

    sustainability = parse_period(context.sustainability["normalized"], "sustainability.normalized")
    financial = parse_period(context.financial["normalized"], "financial.normalized")

    window = _reporting_window(context)
    if window is not None:
        measured_start, measured_end = sustainability
        window_start, window_end = window
        if measured_end < window_start or measured_start > window_end:
            return completed("not_applicable", "period_out_of_scope")

    if sustainability == financial:
        return completed("matched", "same_period")

    return context.resolve_difference(explained_reason="period_difference_explained")
