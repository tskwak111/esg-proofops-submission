"""Linkage-exchange packet builder for the C1-C4 contract.

This module builds the outbound side of schema 1.1, turning a trusted
local run's confirmed claim/tags into the
sustainability half of a packet, combined with a caller-supplied, fully
explicit `FinancialContext` for the financial half. It does not implement
the `evaluate`/`reconcile` engine and it does not compute or store any E
grade/label — those stay entirely on the existing rules-engine path.

Pure module: no filesystem, no network. Schema validation belongs to
`scripts/linkage_exchange_cli.py`, which loads the
existing schemas and calls `validate.py`'s own validator. This module only
returns a plain dict shaped like the contract; the CLI is responsible for
schema-validating it.

No field in `identity` (dart_corp_code, rcept_no, consolidation,
financial_fiscal_year, financial_document_version, financial_period_*,
financial_published_at) is invented here: the app's Claim/ConfirmedTags model
has no such fields (confirmed by inspection of packages/proofops/application/
claims.py and packages/proofops/domain/rules/engine.py), so every one of them
must come from the caller as an already-validated `FinancialContext`. If the
caller has none, `build_packet` returns a `BlockedPacket` (execution_state
"blocked", reason "missing_financial_context") rather than fabricating a
packet with placeholder identity/financial values.

Trusted identity is never caller-overridable: `tenant_id`, `claim_id`,
`document_version_id` (sustainability), and every source citation on the
sustainability side come only from the trusted `Claim`/`ConfirmedTags`
objects the caller passes in, not from `FinancialContext`. `FinancialContext`
only supplies fields the app genuinely has no source of truth for
(financial-side identity and facts); it cannot substitute a different
tenant/claim/company for the trusted ones.

Byte-level AND locator/quote verification of every `sources[]` entry against
real opened bytes happens in `adapters/local/linkage_reader.py`, not here;
this module only requires the hash/locator/quote to already be present and
well-formed on each `SourceRef`/`FinancialSource` it is given, and never
treats a bare hash match as sufficient on its own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from proofops.application.claims import Claim
from proofops.domain.errors import DomainValidationError
from proofops.domain.reconciliation.common import validate_packet
from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags
from proofops.domain.values import SourceRef, _require_sha256, _require_uuid

ITEMS = ("C1", "C2", "C3", "C4")
FACT_KINDS = (
    "entity_set",
    "facility_set",
    "currency_amount",
    "period",
    "classification",
    "unknown",
)

# Trigger mapping from existing fact ids to C-item names. It maps exact fact
# ids, never an arbitrary/unknown field
# name: a fact whose name is not one of these keys contributes no trigger,
# regardless of its state. See `_verified_triggers`.
#
# `org_boundary` is the CANONICAL fact name every real producer emits for the
# organisational-boundary element (`management.M2` and `goal.G4` in
# packages/proofops/domain/rules/; `config/rubric/{management,goal}.yaml` name
# the same primitive). Verified organisational content makes it
# the source of the `organizational_boundary` trigger, so this is a spelling
# alias for one identical element -- not a widening of what counts as a
# boundary. Without it an accepted M2/G4 review was silently dropped here. The
# older `organizational_boundary` key is kept so any pre-existing stored fact
# under that spelling keeps resolving.
#
# Deliberately NOT mapped, and left as explicit gaps rather than guesses:
#   * `calculation_boundary` (`performance.P3`) -- a calculation boundary is not
#     proven to be an organisational boundary; the contract requires the
#     organisational content to be confirmed first.
#   * `scope` (`goal.G4`'s other primitive, i.e. GHG Scope 1/2/3) -- the
#     contract forbids creating `implementation_scope` from a generic scope or
#     region mention, and no producer emits `implementation_scope` itself.
#   * `currency_amount` / `revenue_share` -- no producer emits these fact names
#     at all, and no approved currency/revenue primitive exists to alias.
TRIGGER_TAG_MAP: dict[str, str] = {
    "org_boundary": "organizational_boundary",
    "organizational_boundary": "organizational_boundary",
    "implementation_scope": "implementation_scope",
    "quantitative_or_qualified_ordinal": "quantitative_value",
    "currency_amount": "currency_amount",
    "revenue_share": "revenue_share",
}

# Only these trigger elements have an unambiguous strict1.1 `kind`; a
# trigger not listed here, or one whose fact carries no normalized_value,
# is explicitly blocked rather than asserted as "unknown" with the raw claim
# quote copied in as if it were a normalized fact value.
TRIGGER_TO_FACT_KIND: dict[str, str] = {
    "organizational_boundary": "entity_set",
    "implementation_scope": "facility_set",
    "quantitative_value": "unknown",  # numeric but no fixed unit/shape assumed
    "currency_amount": "currency_amount",
    "revenue_share": "classification",
}

# Item-specific trigger rules:
# C1: boundary / scope (entity_set, facility_set)
# C2: quantitative performance trigger; compare explicit source-bound periods.
#     Fact normalized_value remains a source literal; C2PeriodContext carries ISO dates.
# C3: currency / capex amount (currency_amount)
# C4: revenue share / classification (classification)
ITEM_TRIGGERS: dict[str, tuple[str, ...]] = {
    "C1": ("organizational_boundary", "implementation_scope"),
    "C2": ("quantitative_value",),
    "C3": ("currency_amount",),
    "C4": ("revenue_share",),
}


@dataclass(frozen=True, slots=True)
class VerifiedTrigger:
    """One confirmed present fact that maps to a C-item trigger, with its
    exact evidence refs AND normalized value preserved (not just a bare
    trigger-name string)."""

    fact_name: str
    trigger_element: str
    evidence_refs: tuple[SourceRef, ...]
    normalized_value: str | None


@dataclass(frozen=True, slots=True)
class FinancialSource:
    """One financial-document citation; caller-supplied and pre-hashed.

    `artifact_sha256` must be the real SHA-256 of the bytes the caller has
    already read from `document_id`'s financial artifact. This dataclass
    does not open any file itself; a hash alone is also not treated as
    sufficient by `adapters/local/linkage_reader.py`, which additionally
    confirms `quote` occurs at `locator` inside those same real bytes for
    supported locator formats and blocks (never accepts) unsupported ones.
    """

    source_id: str
    document_id: str
    artifact_sha256: str
    locator: str
    quote: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id:
            raise DomainValidationError("financial source_id required")
        if not isinstance(self.document_id, str) or not self.document_id:
            raise DomainValidationError("financial document_id required")
        _require_sha256("artifact_sha256", self.artifact_sha256)
        if not isinstance(self.locator, str) or not self.locator:
            raise DomainValidationError("financial locator required")
        if not isinstance(self.quote, str) or not self.quote:
            raise DomainValidationError("financial quote required")


@dataclass(frozen=True, slots=True)
class FinancialFact:
    """One side (sustainability OR financial) of the compared value."""

    raw: str | None
    normalized: str | None
    kind: str
    unit: str | None
    source_id: str | None

    def __post_init__(self) -> None:
        if self.kind not in FACT_KINDS:
            raise DomainValidationError(f"unknown fact kind: {self.kind}")
        for name in ("raw", "normalized", "unit", "source_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise DomainValidationError(
                    f"financial fact {name} must be a nonempty string or null"
                )


@dataclass(frozen=True, slots=True)
class C2PeriodContext:
    """Explicit normalization; authority comes from confirmed fact refs, never this input."""

    normalized: str
    source_id: str
    quote: str

    def __post_init__(self) -> None:
        from proofops.domain.reconciliation.common import parse_period

        parse_period(self.normalized, "c2_period_context.normalized")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise DomainValidationError("c2_period_context.source_id required")
        if not isinstance(self.quote, str) or not self.quote:
            raise DomainValidationError("c2_period_context.quote required")


def period_from_literal(quote: str) -> str:
    """Normalize one explicit ordered range; never combine dates across ranges."""
    import re

    # ponytail: Korean full dates and ISO only; add formats after source-backed examples.
    dates = list(
        re.finditer(
            r"(?<![0-9])(?:([0-9]{4})년\s*([0-9]{1,2})월\s*([0-9]{1,2})일"
            r"|([0-9]{4})-([0-9]{2})-([0-9]{2}))(?![0-9])",
            quote,
        )
    )
    if len(dates) != 2:
        raise DomainValidationError("period requires exactly two explicit dates")
    delimiter = quote[dates[0].end() : dates[1].start()].strip()
    if delimiter not in {"~", "～", "–", "—", "-", "/", "부터", "to"}:
        raise DomainValidationError("period range delimiter required")
    start, end = [date(*map(int, m.groups()[:3] if m[1] else m.groups()[3:])) for m in dates]
    if start > end:
        raise DomainValidationError("period start must be <= end")
    return f"{start.isoformat()}/{end.isoformat()}"


@dataclass(frozen=True, slots=True)
class C3Context:
    currency: str
    target_period_start: str | None
    target_period_end: str | None
    capex_period_start: str | None
    capex_period_end: str | None
    capex_account_ids: tuple[str, ...]
    commitment_source_id: str | None
    funding_plan_source_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency:
            raise DomainValidationError("c3_context.currency required")
        object.__setattr__(self, "capex_account_ids", tuple(self.capex_account_ids))


@dataclass(frozen=True, slots=True)
class C4Context:
    classification_name: str
    definition_source_ids: tuple[str, ...]
    calculation_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.classification_name, str) or not self.classification_name:
            raise DomainValidationError("c4_context.classification_name required")
        object.__setattr__(self, "definition_source_ids", tuple(self.definition_source_ids))
        object.__setattr__(self, "calculation_source_ids", tuple(self.calculation_source_ids))


@dataclass(frozen=True, slots=True)
class FinancialContext:
    """Everything the packet needs that the app's own run cannot supply.

    Every field here is required explicitly by the caller (a human/B-side
    process with actual DART/financial-statement access), never derived or
    guessed from the sustainability run. `synthetic=True` is the ONLY way a
    placeholder value (fixture tenant/company/hashes) may flow through
    `build_packet`; a caller passing real-looking values with
    `synthetic=False` is asserting they are real, and this module does not
    verify that assertion beyond hash/format checks (byte verification is
    `adapters/local/linkage_reader.py`'s job).
    """

    synthetic: bool
    company_id: str
    package_id: str
    dart_corp_code: str
    financial_document_version: str
    financial_fiscal_year: int
    consolidation: str
    financial_period_start: str | None
    financial_period_end: str | None
    financial_published_at: str | None
    rcept_no: str | None
    as_of_date: str
    financial: FinancialFact
    financial_sources: tuple[FinancialSource, ...]
    c3_context: C3Context | None = None
    c4_context: C4Context | None = None

    def __post_init__(self) -> None:
        if type(self.synthetic) is not bool:
            raise DomainValidationError("financial context synthetic flag must be boolean")
        if not isinstance(self.company_id, str) or not self.company_id:
            raise DomainValidationError("company_id required")
        if not isinstance(self.package_id, str) or not self.package_id:
            raise DomainValidationError("package_id required")
        import re

        if not re.fullmatch(r"[0-9]{8}", self.dart_corp_code):
            raise DomainValidationError("dart_corp_code must be exactly 8 digits")
        if (
            not isinstance(self.financial_document_version, str)
            or not self.financial_document_version
        ):
            raise DomainValidationError("financial_document_version required")
        if (
            type(self.financial_fiscal_year) is not int
            or not 1900 <= self.financial_fiscal_year <= 2200
        ):
            raise DomainValidationError("financial_fiscal_year out of range")
        if self.consolidation not in ("consolidated", "separate", "unknown"):
            raise DomainValidationError("invalid consolidation")
        for name in (
            "financial_period_start",
            "financial_period_end",
            "financial_published_at",
        ):
            value = getattr(self, name)
            if value is not None:
                date.fromisoformat(value)
        if self.rcept_no is not None and (not isinstance(self.rcept_no, str) or not self.rcept_no):
            raise DomainValidationError("rcept_no must be a nonempty string or null")
        date.fromisoformat(self.as_of_date)
        if not isinstance(self.financial, FinancialFact):
            raise DomainValidationError("financial fact required")
        sources = tuple(self.financial_sources)
        if any(not isinstance(s, FinancialSource) for s in sources):
            raise DomainValidationError("financial_sources must all be FinancialSource")
        object.__setattr__(self, "financial_sources", sources)
        if self.c3_context is not None and not isinstance(self.c3_context, C3Context):
            raise DomainValidationError("c3_context must be C3Context or null")
        if self.c4_context is not None and not isinstance(self.c4_context, C4Context):
            raise DomainValidationError("c4_context must be C4Context or null")


@dataclass(frozen=True, slots=True)
class BlockedPacket:
    """Explicit refusal to build a packet; never a fabricated stand-in."""

    claim_id: str
    item: str
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self), execution_state="blocked")


def _verified_triggers(tags: ConfirmedTags) -> tuple[VerifiedTrigger, ...]:
    """Map confirmed present facts to C-item triggers, preserving evidence.

    Only `state == "present"` facts with `citation_verified` and
    `binding_accepted` (the rules engine's own present-requires-verified-
    citation invariant, see ConfirmedFact.__post_init__) count, AND only
    when the fact actually carries at least one evidence_ref SourceRef.
    A fact name absent from `TRIGGER_TAG_MAP` contributes nothing — an
    unknown/arbitrary field name is never turned into a trigger by guessing
    or substring matching.
    """
    triggers: list[VerifiedTrigger] = []
    for fact in tags.facts:
        if not isinstance(fact, ConfirmedFact):
            raise DomainValidationError("expected ConfirmedFact, not a raw dict/tag")
        trigger_element = TRIGGER_TAG_MAP.get(fact.name)
        if trigger_element is None:
            continue
        if fact.state != "present" or not (fact.citation_verified and fact.binding_accepted):
            continue
        if not fact.evidence_refs:
            continue
        triggers.append(
            VerifiedTrigger(fact.name, trigger_element, fact.evidence_refs, fact.normalized_value)
        )
    return tuple(sorted(triggers, key=lambda t: t.fact_name))


def _context_to_contract_dict(context: C3Context | C4Context | None) -> dict[str, Any] | None:
    """Emit tuple-backed context fields as arrays required by the packet contract."""
    if context is None:
        return None
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in asdict(context).items()
    }


def build_packet(
    *,
    claim: Claim,
    tags: ConfirmedTags | None,
    financial_context: FinancialContext | None,
    item: str,
    tenant_id: str,
    period_start: str | None,
    period_end: str | None,
    sr_published_at: str | None,
    trusted_company_id: str | None = None,
    c2_period_context: C2PeriodContext | None = None,
) -> dict[str, Any] | BlockedPacket:
    """Build one strict1.1 input packet from a trusted claim + caller context.

    `identity.sustainability_document_version` is ALWAYS `claim.document_version_id`
    -- the trusted local identity -- never a caller-supplied string; there is
    no parameter that could override it. `identity.company_id` is
    `trusted_company_id` when the caller has one from the app's own trusted
    company/run identity (e.g. a real company record tied to this
    tenant/run); when `trusted_company_id` is not supplied, `company_id`
    falls back to `financial_context.company_id` ONLY IF
    `financial_context.synthetic` is True (development/demo use), and
    otherwise this returns a `BlockedPacket` -- a real (non-synthetic)
    packet must never assert a company identity the app has no trusted
    record of, even if the caller supplies one.

    Returns a `BlockedPacket` (never a schema-shaped dict) when required
    context is missing: no financial_context, no confirmed tags for this
    claim, no resolvable company identity, or C5 requested. This function
    performs NO byte verification of `claim.source_sha256` or any
    `FinancialSource.artifact_sha256` itself -- it trusts the caller's
    `Claim`/`FinancialContext` were already constructed from real, checked
    data; independent re-verification against real opened bytes (hash AND
    locator/quote) happens in `adapters/local/linkage_reader.py`.
    """
    _require_uuid("tenant_id", tenant_id)
    if item == "C5" or item not in ITEMS:
        return BlockedPacket(
            claim.claim_id, item, "prohibited_item", "C5 is disabled; only C1-C4 supported"
        )
    if claim.tenant_id != tenant_id:
        return BlockedPacket(
            claim.claim_id,
            item,
            "tenant_mismatch",
            "claim tenant does not match caller tenant",
        )
    if financial_context is None:
        return BlockedPacket(
            claim.claim_id,
            item,
            "missing_financial_context",
            "no caller-supplied FinancialContext; refusing to fabricate identity/financial fields",
        )
    company_id = trusted_company_id
    if company_id is None:
        if not financial_context.synthetic:
            return BlockedPacket(
                claim.claim_id,
                item,
                "unresolved_company_identity",
                "no trusted company identity available for this run; refusing to accept "
                "an unverified company_id for a non-synthetic packet",
            )
        company_id = financial_context.company_id
    elif financial_context.company_id != company_id:
        return BlockedPacket(
            claim.claim_id,
            item,
            "company_mismatch",
            f"financial_context company_id ({financial_context.company_id}) does not match "
            f"trusted company_id ({company_id})",
        )
    if period_start and period_end and period_start > period_end:
        return BlockedPacket(
            claim.claim_id,
            item,
            "period_mismatch",
            f"period_start ({period_start}) must be <= period_end ({period_end})",
        )
    if (
        financial_context.financial_period_start
        and financial_context.financial_period_end
        and financial_context.financial_period_start > financial_context.financial_period_end
    ):
        return BlockedPacket(
            claim.claim_id,
            item,
            "period_mismatch",
            f"financial_period_start ({financial_context.financial_period_start}) must be "
            f"<= financial_period_end ({financial_context.financial_period_end})",
        )
    if tags is None or not tags.facts:
        return BlockedPacket(
            claim.claim_id, item, "missing_confirmed_tags", "no confirmed tags exist for this claim"
        )
    if tags.claim_id != claim.claim_id or tags.tenant_id != tenant_id:
        return BlockedPacket(
            claim.claim_id,
            item,
            "tag_claim_mismatch",
            "confirmed tags do not belong to this claim/tenant",
        )
    if tags.document_version_id != claim.document_version_id:
        return BlockedPacket(
            claim.claim_id,
            item,
            "version_mismatch",
            f"confirmed tags document_version_id ({tags.document_version_id}) does not "
            f"match claim document_version_id ({claim.document_version_id})",
        )
    trigger_elements = _verified_triggers(tags)
    if not trigger_elements:
        return BlockedPacket(
            claim.claim_id,
            item,
            "no_verified_trigger",
            "no confirmed present+verified fact with evidence maps to a C-item trigger",
        )
    allowed_triggers = ITEM_TRIGGERS.get(item, ())
    matching_triggers = [t for t in trigger_elements if t.trigger_element in allowed_triggers]
    if not matching_triggers:
        return BlockedPacket(
            claim.claim_id,
            item,
            "no_matching_item_trigger",
            f"no confirmed present fact matches trigger requirements for {item} (expected "
            f"one of {allowed_triggers})",
        )
    primary_trigger = matching_triggers[0]

    if item == "C3" and financial_context.c3_context is None:
        return BlockedPacket(claim.claim_id, item, "missing_c3_context", "C3 requires c3_context")
    if item == "C4" and financial_context.c4_context is None:
        return BlockedPacket(claim.claim_id, item, "missing_c4_context", "C4 requires c4_context")
    if item == "C2":
        import re

        from proofops.domain.numeric import unit_note_literal

        # P1 includes categorical certifications; a year or ISO number is not a quantity.
        # ponytail: accept existing explicit numeric-unit literals; other formats stay blocked.
        numeric = any(
            unit_note_literal("unit: " + match[1]) is not None
            for match in re.finditer(
                r"[0-9][0-9,.]*\s*([A-Za-z₂³%가-힣㎥][A-Za-z0-9₂³%가-힣㎥]*)(?![A-Za-z가-힣])",
                primary_trigger.normalized_value or "",
            )
        )
        if tags.track != "performance" or not numeric:
            return BlockedPacket(
                claim.claim_id,
                item,
                "quantitative_trigger_unresolved",
                "C2 requires a verified quantitative performance literal",
            )
        if c2_period_context is None:
            return BlockedPacket(
                claim.claim_id,
                item,
                "missing_c2_period_context",
                "C2 requires an explicit source-bound C2PeriodContext; refusing to fabricate "
                "a reporting period from a fact's normalized_value or an arbitrary ISO date",
            )
        if not isinstance(c2_period_context, C2PeriodContext):
            return BlockedPacket(
                claim.claim_id,
                item,
                "invalid_c2_period_context",
                "c2_period_context must be a C2PeriodContext",
            )
        try:
            supported_period = period_from_literal(c2_period_context.quote)
        except (DomainValidationError, ValueError):
            supported_period = None
        if supported_period != c2_period_context.normalized:
            return BlockedPacket(
                claim.claim_id,
                item,
                "c2_period_not_supported_by_source",
                "period must match one explicit range in the cited literal",
            )

    sustainability_document_version = claim.document_version_id
    sources: list[dict[str, Any]] = []
    seen_sources: dict[str, tuple[str, str]] = {}

    def _record_source(
        source_id: str,
        document_id: str,
        artifact_sha256: str,
        locator: str,
        quote: str,
    ) -> BlockedPacket | None:
        if source_id in seen_sources:
            prev_quote, prev_locator = seen_sources[source_id]
            if prev_quote != quote or prev_locator != locator:
                return BlockedPacket(
                    claim.claim_id,
                    item,
                    "ambiguous_source_duplicate",
                    f"source_id {source_id} cited with conflicting quote or locator on same "
                    f"block; refusing to silently discard evidence",
                )
            return None
        seen_sources[source_id] = (quote, locator)
        sources.append(
            dict(
                source_id=source_id,
                document_id=document_id,
                artifact_sha256=artifact_sha256,
                locator=locator,
                quote=quote,
            )
        )
        return None

    for ref in claim.source_refs:
        if ref.document_version_id and ref.document_version_id != sustainability_document_version:
            return BlockedPacket(
                claim.claim_id,
                item,
                "version_mismatch",
                f"claim source ref document_version_id ({ref.document_version_id}) does "
                f"not match claim document_version_id ({sustainability_document_version})",
            )
        sr_source_id = "sr-" + ref.source_id
        loc = f"physical_page={ref.page_num};source_id={ref.source_id}"
        err = _record_source(
            sr_source_id, sustainability_document_version, claim.source_sha256, loc, ref.quote
        )
        if err is not None:
            return err

    # Preserve EVERY triggering fact's evidence refs as their own sources[]
    # entries (not just claim.source_refs[0]): a confirmed fact can cite a
    # different block than the claim's own top-level source, and dropping
    # those refs after using them only to decide trigger_elements would
    # discard exactly the evidence B is meant to review.
    for trigger in trigger_elements:
        for ref in trigger.evidence_refs:
            if (
                ref.document_version_id
                and ref.document_version_id != sustainability_document_version
            ):
                return BlockedPacket(
                    claim.claim_id,
                    item,
                    "version_mismatch",
                    f"trigger evidence ref document_version_id ({ref.document_version_id}) "
                    f"does not match claim document_version_id ({sustainability_document_version})",
                )
            trig_source_id = "sr-" + ref.source_id
            loc = f"physical_page={ref.page_num};source_id={ref.source_id}"
            err = _record_source(
                trig_source_id, sustainability_document_version, claim.source_sha256, loc, ref.quote
            )
            if err is not None:
                return err

    for fs in financial_context.financial_sources:
        if fs.source_id in seen_sources:
            return BlockedPacket(
                claim.claim_id,
                item,
                "duplicate_source_id",
                f"financial source_id collides: {fs.source_id}",
            )
        seen_sources[fs.source_id] = (fs.quote, fs.locator)
        sources.append(asdict(fs))

    fin = financial_context.financial
    if fin.source_id is not None and fin.source_id not in seen_sources:
        return BlockedPacket(
            claim.claim_id,
            item,
            "unknown_financial_source",
            "financial.source_id not in financial_sources",
        )

    # Use the actual selected trigger fact's own normalized value/kind for
    # the sustainability side -- never a bare "unknown" kind with the raw
    # claim quote copied in as if it were already a normalized fact.
    SUPPORTED_SUSTAINABILITY_KINDS = {
        "entity_set",
        "facility_set",
        "currency_amount",
        "period",
        "classification",
    }
    if item == "C2":
        # Period normalization must cite the selected quantitative fact's own evidence.
        assert c2_period_context is not None  # guaranteed by the C2 checks above
        period_source_id = "sr-" + c2_period_context.source_id
        # A random claim/global-report ref cannot authorize a quantitative fact's period.
        if c2_period_context.source_id not in {r.source_id for r in primary_trigger.evidence_refs}:
            return BlockedPacket(
                claim.claim_id,
                item,
                "c2_period_source_not_cited",
                f"c2_period_context.source_id ({c2_period_context.source_id}) does not resolve "
                "to any trusted sustainability source cited on this packet; refusing to bind a "
                "period to an uncited source",
            )
        # Byte/locator verification stays in the CLI's existing source reader.
        cited_quote, _cited_locator = seen_sources[period_source_id]
        if cited_quote != c2_period_context.quote:
            return BlockedPacket(
                claim.claim_id,
                item,
                "c2_period_quote_mismatch",
                "c2_period_context.quote does not match the literal quote cited for its "
                f"source_id ({c2_period_context.source_id}); refusing an unbound period literal",
            )
        sustainability_kind = "period"
        sustainability_normalized: str | None = c2_period_context.normalized
        sustainability_source_id = period_source_id
        primary_evidence_quote = cited_quote
    else:
        sustainability_kind_opt = TRIGGER_TO_FACT_KIND.get(primary_trigger.trigger_element)
        if (
            sustainability_kind_opt is None
            or sustainability_kind_opt not in SUPPORTED_SUSTAINABILITY_KINDS
            or primary_trigger.normalized_value is None
        ):
            return BlockedPacket(
                claim.claim_id,
                item,
                "unsupported_trigger_value",
                f"trigger {primary_trigger.trigger_element!r} has unsupported kind "
                f"{sustainability_kind_opt!r} or no "
                "normalized_value on its confirmed fact; refusing to assert a fabricated "
                "normalized sustainability value",
            )
        sustainability_kind = sustainability_kind_opt
        primary_evidence = primary_trigger.evidence_refs[0]
        sustainability_source_id = "sr-" + primary_evidence.source_id
        sustainability_normalized = primary_trigger.normalized_value
        primary_evidence_quote = primary_evidence.quote

    claim_source = claim.source_refs[0]
    claim_sr_id = "sr-" + claim_source.source_id

    packet = dict(
        schema_version="1.1",
        synthetic=financial_context.synthetic,
        identity=dict(
            tenant_id=tenant_id,
            company_id=company_id,
            claim_id=claim.claim_id,
            package_id=financial_context.package_id,
            period_start=period_start,
            period_end=period_end,
            sustainability_document_version=sustainability_document_version,
            financial_document_version=financial_context.financial_document_version,
            dart_corp_code=financial_context.dart_corp_code,
            financial_fiscal_year=financial_context.financial_fiscal_year,
            consolidation=financial_context.consolidation,
            financial_period_start=financial_context.financial_period_start,
            financial_period_end=financial_context.financial_period_end,
            sr_published_at=sr_published_at,
            financial_published_at=financial_context.financial_published_at,
            rcept_no=financial_context.rcept_no,
            as_of_date=financial_context.as_of_date,
        ),
        item=item,
        sources=sources,
        sustainability=dict(
            raw=primary_evidence_quote,
            normalized=sustainability_normalized,
            kind=sustainability_kind,
            unit=None,
            source_id=sustainability_source_id,
        ),
        financial=dict(
            raw=fin.raw,
            normalized=fin.normalized,
            kind=fin.kind,
            unit=fin.unit,
            source_id=fin.source_id,
        ),
        comparability="unknown",
        explanation=dict(source_id=None, search_complete=False),
        c3_context=_context_to_contract_dict(financial_context.c3_context),
        claim=dict(
            track=tags.track,
            quote=claim_source.quote,
            source_id=claim_sr_id,
            trigger_elements=sorted({t.trigger_element for t in trigger_elements}),
            fiscal_year=financial_context.financial_fiscal_year,
        ),
        search=dict(
            state="not_run",
            coverage_policy_id=None,
            required_document_ids=sorted(
                {sustainability_document_version, financial_context.financial_document_version}
            ),
            reviewed_source_ids=[],
            failed_document_ids=[],
            receipt_id=None,
        ),
        c4_context=_context_to_contract_dict(financial_context.c4_context),
    )
    # Reject malformed typed values at the producer boundary using B's validator.
    try:
        validate_packet(packet)
    except DomainValidationError as exc:
        return BlockedPacket(
            claim.claim_id,
            item,
            "invalid_reconciliation_packet",
            f"packet violates the shared reconciliation contract: {exc}",
        )
    return packet
