"""Caller-authorized local Upstage tagging transport; never approves sources or grades.

The probe owns the only monetary reservation. This adapter is not implicitly
installed in LocalTagRunner (which still requires its synthetic runtime contract).
Receipts are immutable; a failed/unknown transport durably stops this operation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from time import monotonic_ns

from proofops.adapters.local.openrouter import OpenRouterProbe
from proofops.adapters.local.upstage import UPSTAGE_TRANSPORT_STOP_CODES, UpstageProbe
from proofops.application.budget import TokenUsage
from proofops.application.preflight import Preflight, PreflightBlocked
from proofops.application.tagging.service import RawTagResponse, TaggingSettings
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import _require_sha256, _require_uuid, _source_ref_from_dict

from proofops_agent.upstage_extraction import UpstageClaimExtractor

MODEL_PROFILE = "upstage-compact-ids-frozen-unicode-v1"
COVERAGE_PROFILE = "upstage-compact-coverage-unicode-v2"
QUOTE_PROFILE = "upstage-compact-source-quotes-v3"
QUOTE_V4_PROFILE = "upstage-compact-source-quotes-v4"
QUOTE_V5_PROFILE = "upstage-compact-source-quotes-v5"

# The only transport error codes that a locally suppressed, pre-dispatch outcome is
# allowed to carry. This is an explicit allow-list, never an inference from timing:
# a genuinely fast provider failure can round its latency to 0ms and can be missing
# a provider_request_id, so zero metadata alone proves nothing about billing.
#
# The decisive evidence is structural rather than numeric. Every short-circuit in
# `_invoke` returns *before* `directory.mkdir()`, and `invoke`'s lock contention
# returns before `_invoke` runs at all, so a suppressed request leaves no receipt
# directory whatsoever. A request that reached `self._probe.complete` always has
# one, because its `request.json` is written first. Absence of a receipt directory
# therefore proves the provider was never contacted; this allow-list only stops an
# unrelated settled failure from being read as a suppression.
NEVER_SENT_ERROR_CODES = frozenset({"UPSTREAM_UNAVAILABLE"})
SUPPRESSED_ERROR_CODE = "UPSTREAM_UNAVAILABLE"


class TransportResume:
    """Explicitly acknowledged, bounded re-arm of a stopped receipt root.

    A stop is never deleted, rewritten or bypassed. Arming requires that every
    durable failure the root already holds is acknowledged by its exact
    ``request_id``: an acknowledged outcome stays unresolved and is never
    retried, and its receipt, stop file and ledger row are left untouched. Only
    a bounded number of *new* ``request_id`` values is then authorized, counted
    across every root this authorization covers.

    The bound is **job-wide and durable, not per process**. The caller passes
    ``already_dispatched``, recomputed from the receipt tree and the ledger
    against the baseline pinned when the authorization was written, so a crashed
    or restarted recovery resumes with the remainder of its original allowance
    instead of being handed a fresh one. ``remaining`` therefore starts at
    ``max_new_requests - already_dispatched`` and a fully spent authorization
    cannot be constructed at all.

    The first new provider failure calls :meth:`disarm`, which withdraws the
    whole remaining allowance immediately, and the failure also leaves its own
    failed receipt and stop record behind. A later process that arms the same
    root therefore finds an unacknowledged durable failure and refuses again,
    so the stop survives process restarts without a blanket retry.
    """

    def __init__(
        self,
        *,
        acknowledged_failures,
        max_new_requests: int,
        already_dispatched: int = 0,
        expected_root: Path | None = None,
    ) -> None:
        if not isinstance(acknowledged_failures, frozenset):
            raise ValueError("TRANSPORT_RESUME_ACKNOWLEDGEMENT_REQUIRED")
        for identifier in sorted(acknowledged_failures):
            _require_uuid("acknowledged_failure", identifier)
        if type(max_new_requests) is not int or not 1 <= max_new_requests <= 200:
            raise ValueError("TRANSPORT_RESUME_BOUND_INVALID")
        if type(already_dispatched) is not int or already_dispatched < 0:
            raise ValueError("TRANSPORT_RESUME_HISTORY_INVALID")
        if already_dispatched >= max_new_requests:
            raise ValueError("TRANSPORT_RESUME_ALLOWANCE_ALREADY_SPENT")
        self.acknowledged_failures = acknowledged_failures
        self.max_new_requests = max_new_requests
        self.already_dispatched = already_dispatched
        self.remaining = max_new_requests - already_dispatched
        self.expected_root = None if expected_root is None else Path(expected_root)
        self.dispatched: set[str] = set()
        self._armed: dict[str, bool] = {}

    def _scan(self, receipts: Path) -> bool:
        """Is every durable failure already in this root explicitly acknowledged?

        An unreadable receipt, a receipt without a response and any settled
        non-succeeded response all count as unresolved, so a root is armed only
        when the operator named each of them. No file here is written or moved.
        """
        # ponytail: bounded local operation; index receipts if ensembles grow large.
        found: set[str] = set()
        try:
            children = sorted(receipts.iterdir())
        except OSError:
            return False
        for child in children:
            if child.is_dir():
                response = child / "response.json"
                if not response.is_file():
                    return False
                try:
                    usage = json.loads(response.read_text())["usage"]
                    status = usage["status"]
                except (OSError, ValueError, KeyError, TypeError):
                    return False
                if status != "succeeded":
                    found.add(child.name)
            elif child.name.startswith("transport-stop") and child.name.endswith(".json"):
                try:
                    found.add(json.loads(child.read_text())["request_id"])
                except (OSError, ValueError, KeyError, TypeError):
                    return False
        return found <= self.acknowledged_failures

    def armed(self, receipts: Path) -> bool:
        key = str(receipts)
        if key not in self._armed:
            self._armed[key] = self._scan(receipts)
        return self._armed[key]

    def allows(self, receipts: Path) -> bool:
        return self.remaining > 0 and self.armed(receipts)

    def consume(self, receipts: Path, request_id: str) -> None:
        _require_uuid("request_id", request_id)
        if request_id in self.dispatched:
            return
        if not self.allows(receipts):
            raise PreflightBlocked("TRANSPORT_RESUME_ALLOWANCE_EXHAUSTED")
        self.dispatched.add(request_id)
        self.remaining -= 1

    def disarm(self, receipts: Path) -> None:
        """Withdraw the whole allowance on the first new failure; never widen it."""
        self._armed[str(receipts)] = False
        self.remaining = 0


class UpstageTaggingTransport:
    synthetic = False
    MODEL_PROFILE = MODEL_PROFILE
    TRANSPORT_VERSION = "compact-evidence-ids-v1"

    def __init__(
        self,
        probe: UpstageProbe,
        receipts: Path,
        *,
        settings: TaggingSettings,
        tenant_id: str,
        authorize: Callable[[TaggingSettings, dict], Preflight],
        resume: TransportResume | None = None,
    ):
        _require_uuid("tenant_id", tenant_id)
        if (
            not isinstance(probe, UpstageProbe | OpenRouterProbe)
            or not isinstance(settings, TaggingSettings)
            or settings.binding.synthetic
            or settings.model_id != probe.model
            or (
                isinstance(probe, OpenRouterProbe)
                and settings.wire_policy_version != probe.wire_policy_version
            )
            or settings.model_profile
            not in (
                {MODEL_PROFILE, COVERAGE_PROFILE, QUOTE_PROFILE, QUOTE_V4_PROFILE, QUOTE_V5_PROFILE}
                if self.MODEL_PROFILE == MODEL_PROFILE
                else {self.MODEL_PROFILE}
            )
            or settings.region != "provider-managed-unverified"
        ):
            raise ValueError("UPSTAGE_TAGGING_BINDING_INVALID")
        if not callable(authorize):
            raise ValueError("UPSTAGE_TAGGING_AUTHORIZER_REQUIRED")
        if resume is not None and not isinstance(resume, TransportResume):
            raise ValueError("UPSTAGE_TAGGING_RESUME_INVALID")
        if settings.model_profile == COVERAGE_PROFILE:
            self.TRANSPORT_VERSION = "compact-coverage-v2"
        elif settings.model_profile == QUOTE_PROFILE:
            self.TRANSPORT_VERSION = "compact-source-quotes-v3"
        elif settings.model_profile == QUOTE_V4_PROFILE:
            self.TRANSPORT_VERSION = "compact-source-quotes-v4"
        elif settings.model_profile == QUOTE_V5_PROFILE:
            self.TRANSPORT_VERSION = "compact-source-quotes-v5"
        self._authorize = authorize
        self._probe, self._settings, self._tenant = probe, settings, tenant_id
        self._resume = resume
        self._receipts = Path(receipts)
        self._receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_operation_state()

    def _init_operation_state(self):
        self._operation_mutex = Lock()
        self._operation_users = 0
        self._operation_file = None
        self._active_requests = set()

    def _stopped(self) -> bool:
        return (self._receipts / "transport-stop.json").exists() or any(
            self._receipts.glob("transport-stop.*.json")
        )

    def may_dispatch(self) -> bool:
        """Is one more *new* billable request inside the caller's explicit bound?

        Without a resume authorization this is always True, so ordinary
        operation, its stop behavior and its receipt replay are unchanged: the
        stop check inside :meth:`_invoke` remains the only gate. With one, a
        caller can ask before reserving budget, so an exhausted or withdrawn
        allowance costs no reservation and no settled ledger row.
        """
        return self._resume is None or self._resume.allows(self._receipts)

    def invoke(self, request: dict) -> RawTagResponse:
        # Offline workers can load on Windows; paid dispatch still requires this lock.
        import fcntl

        # One operation per receipt root; the shared USD ledger also fences all roots.
        with self._operation_mutex:
            if self._operation_users == 0:
                lock = (self._receipts / ".operation.lock").open("a")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock.close()
                    return self._failed(SUPPRESSED_ERROR_CODE, 0)
                self._operation_file = lock
            self._operation_users += 1
        try:
            return self._invoke(request)
        finally:
            with self._operation_mutex:
                self._active_requests.discard(request["request_id"])
                self._operation_users -= 1
                if self._operation_users == 0:
                    self._operation_file.close()
                    self._operation_file = None

    def count_input_tokens(self, request: dict, *, counter: Callable[[str, str], int]) -> int:
        """Count exactly the sent messages, using a caller-pinned provider counter.

        The counter must include model chat framing; no character/byte estimate
        or tokenizer from another model is supplied here. No receipt or paid call.
        """
        system, user, _, _, _ = self._wire_request(request)
        self._probe.request_body(
            system,
            user,
            request_id=request["request_id"],
            max_tokens=request["max_tokens"],
            json_mode=True,
        )
        return counter(system, user)

    def _authorize_request(self, request: dict) -> Preflight:
        settings = self._settings
        authorization = self._authorize(settings, request)
        if (
            not isinstance(authorization, Preflight)
            or authorization.ready is not True
            or any(check.status == "fail" for check in authorization.checks)
            or not {
                "runtime_approval",
                "consent_approval",
                "model_binding",
                "document_scope",
                "document_rights",
                "data_consent",
                "tagging_settings",
                "selected_document_rights",
            }
            <= {check.name for check in authorization.checks if check.status == "pass"}
        ):
            raise PreflightBlocked("UPSTAGE_TAGGING_AUTHORIZATION_REQUIRED")
        for name in ("tenant_id", "claim_id", "request_id"):
            _require_uuid(name, request[name])
        for name in ("packet_sha256", "request_signature"):
            _require_sha256(name, request[name])
        if (
            request["tenant_id"] != self._tenant
            or request["binding"] != asdict(settings.binding)
            or request["model_id"] != settings.model_id
            or request["model_profile"] != settings.model_profile
            or request["region"] != settings.region
            or request["temperature"] != 0
            or type(request["replicate_id"]) is not int
            or request["replicate_id"] not in (1, 2, 3)
            or type(request["max_tokens"]) is not int
            or not 1 <= request["max_tokens"] <= settings.max_tokens
        ):
            raise ValueError("UPSTAGE_TAGGING_REQUEST_INVALID")
        return authorization

    def _wire_request(self, request: dict) -> tuple[str, str, dict[str, dict], Preflight, str]:
        settings = self._settings
        authorization = self._authorize_request(request)
        system = request["system_prompt"]
        prefix = settings.rendered_system + "\nValidated classification; tag only its elements: "
        if not isinstance(system, str) or not system.startswith(prefix):
            raise ValueError("UPSTAGE_TAGGING_CLASSIFICATION_REQUIRED")
        classification = json.loads(system[len(prefix) :])
        if (
            not isinstance(classification, dict)
            or set(classification) != {"track", "safe_harbor_category"}
            or classification["track"] not in ("goal", "performance", "management")
            or classification["safe_harbor_category"]
            not in (None, "forward_looking", "emissions_estimate", "third_party_information")
        ):
            raise ValueError("UPSTAGE_TAGGING_CLASSIFICATION_INVALID")
        user = json.loads(request["user_json"])
        if (
            not isinstance(user, dict)
            or any(user.get(k) != request[k] for k in ("claim_id", "packet_sha256", "replicate_id"))
            or not isinstance(user.get("untrusted_document_data"), dict)
        ):
            raise ValueError("UPSTAGE_TAGGING_PACKET_MISMATCH")
        if settings.model_profile in (
            COVERAGE_PROFILE,
            QUOTE_PROFILE,
            QUOTE_V4_PROFILE,
            QUOTE_V5_PROFILE,
        ):
            data = user["untrusted_document_data"]
            coverage = data.get("search_coverage", {})
            if (
                not isinstance(coverage, dict)
                or coverage.get("not_found_state", "unknown") != "unknown"
            ):
                raise ValueError("UPSTAGE_TAGGING_COVERAGE_INVALID")
            summary: dict[str, str | int] = {"not_found_state": "unknown"}
            for prefix in ("omitted", "unprocessed"):
                ids = coverage.get(f"{prefix}_source_ids", [])
                if not isinstance(ids, list):
                    raise ValueError("UPSTAGE_TAGGING_COVERAGE_INVALID")
                for identifier in ids:
                    _require_uuid("coverage source_id", identifier)
                summary[f"{prefix}_source_count"] = len(ids)
                summary[f"{prefix}_source_ids_sha256"] = canonical_hash(ids)
            data["search_coverage"] = summary
        data = user["untrusted_document_data"]
        if settings.model_profile == QUOTE_V5_PROFILE and "claim_source_refs" in data:
            data["claim_source_refs"] = [
                {"quote": ref.quote, "page_num": ref.page_num}
                for ref in (_source_ref_from_dict(raw) for raw in data["claim_source_refs"])
            ]
        refs: dict[str, dict] = {}
        for candidate in data.get("evidence_candidates", []):
            if settings.model_profile == QUOTE_V5_PROFILE:
                candidate.pop("source_id", None)
            identifiers = []
            for raw_ref in candidate.get("source_refs", []):
                ref = asdict(_source_ref_from_dict(raw_ref))
                identifier = f"e{len(refs)}"
                refs[identifier] = ref
                identifiers.append(identifier)
            candidate["source_refs"] = identifiers
        catalog_fields = (
            ("quote", "page_num")
            if settings.model_profile == QUOTE_V5_PROFILE
            else ("quote", "page_num", "verification_state")
        )
        data["evidence_catalog"] = {
            key: {field: ref[field] for field in catalog_fields} for key, ref in refs.items()
        }
        schema = json.loads(settings.schema_json)
        schema["$defs"]["SourceRef"] = {"type": "string", "pattern": "^e[0-9]+$"}
        if settings.model_profile in (QUOTE_PROFILE, QUOTE_V4_PROFILE, QUOTE_V5_PROFILE):
            schema["$defs"]["SourceRef"] = {
                "type": "object",
                "required": ["id", "quote"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "enum": list(refs)},
                    "quote": {"type": "string", "minLength": 1},
                },
            }
        allowed = user["untrusted_document_data"].get("allowed_elements")
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(not isinstance(element, str) for element in allowed)
            or len(set(allowed)) != len(allowed)
        ):
            raise ValueError("UPSTAGE_TAGGING_ELEMENTS_REQUIRED")
        schema["properties"]["track"] = {"const": classification["track"]}
        schema["properties"]["safe_harbor_category"] = {
            "const": classification["safe_harbor_category"]
        }
        schema["$defs"]["Element"]["properties"]["element_id"] = {"enum": allowed}
        schema["properties"]["elements"].update(minItems=len(allowed), maxItems=len(allowed))
        instructions = (
            "\nTransport contract compact-evidence-ids-v1: evidence_refs contains only "
            "evidence_catalog IDs such as e0. Select IDs; never repeat or alter source text, "
            "coordinates, offsets or verification state. The server restores those exactly."
        )
        if settings.model_profile in (QUOTE_PROFILE, QUOTE_V4_PROFILE, QUOTE_V5_PROFILE):
            instructions = (
                "\nTransport contract compact-source-quotes-v3: each evidence_refs item is "
                '{"id":"e0","quote":"exact source substring"}. Select an evidence_catalog '
                "ID and a non-empty exact quote occurring only once within that catalog quote. "
                "Include enough context to disambiguate repeated text. Never supply offsets, "
                "coordinates or verification state; the server restores provenance. "
                "A non-null normalized_value must equal one selected quote (NFC/whitespace "
                "normalization only), never a paraphrase or summary. Use a precise value quote "
                "for numerical elements and additional context quotes as needed; qualitative "
                "elements may use null normalized_value while retaining literal evidence. "
                "Exact quotation does not establish claim attribution or semantic sufficiency."
            )
        wire_system = system.replace(settings.schema_json, canonical_json(schema), 1) + instructions
        if settings.model_profile in (
            COVERAGE_PROFILE,
            QUOTE_PROFILE,
            QUOTE_V4_PROFILE,
            QUOTE_V5_PROFILE,
        ):
            wire_system += (
                "\nCoverage v2: omitted/unprocessed source counts and list hashes summarize "
                "unseen identifiers retained by the server. Missing evidence remains unknown; "
                "neither a count nor a hash is evidence or proof of absence."
            )
        wire_user = json.dumps(user, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return wire_system, wire_user, refs, authorization, canonical_json(schema)

    def _restore_ref(self, selection, refs: dict[str, dict]) -> dict:
        if self._settings.model_profile not in (QUOTE_PROFILE, QUOTE_V4_PROFILE, QUOTE_V5_PROFILE):
            return refs[selection]
        if not isinstance(selection, dict) or set(selection) != {"id", "quote"}:
            raise ValueError("invalid source quote selection")
        quote = selection["quote"]
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("non-empty source quote required")
        original = refs[selection["id"]]
        span = UpstageClaimExtractor._locate(quote, original["quote"])
        return original | dict(
            quote=quote,
            char_start=original["char_start"] + span["char_start"],
            char_end=original["char_start"] + span["char_end"],
        )

    @staticmethod
    def _refine_g1_year(element: dict) -> None:
        """Add a unique literal year only inside an already selected citation."""
        if element.get("element_id") != "G1" or element.get("state") != "present":
            return
        norm_val = element.get("normalized_value")
        if (
            not isinstance(norm_val, str)
            or len(norm_val) != 5
            or not norm_val.endswith("년")
            or not all("0" <= ch <= "9" for ch in norm_val[:4])
            or norm_val[0] == "0"
        ):
            return
        refs = element.get("evidence_refs")
        if not isinstance(refs, list):
            return
        if any(r.get("quote") == norm_val for r in refs):
            return
        candidates = []
        for r in refs:
            orig = r.get("quote", "")
            if orig.count(norm_val) > 1:
                return
            try:
                span = UpstageClaimExtractor._locate(norm_val, orig)
                if span["char_start"] > 0 and orig[span["char_start"] - 1].isdigit():
                    continue
                new_ref = r.copy()
                new_ref["quote"] = norm_val
                new_ref["char_start"] = r["char_start"] + span["char_start"]
                new_ref["char_end"] = r["char_start"] + span["char_end"]
                candidates.append(new_ref)
            except ValueError:
                pass
        unique = []
        for c in candidates:
            if c not in unique:
                unique.append(c)
        if len(unique) == 1:
            refs.append(unique[0])

    def _invoke(self, request: dict) -> RawTagResponse:
        settings = self._settings
        system = request["system_prompt"]
        wire_system, wire_user, refs, authorization, wire_schema = self._wire_request(request)
        stop = self._receipts / "transport-stop.json"
        # ponytail: bounded local operation; index receipts if ensembles grow large.
        with self._operation_mutex:
            incomplete = any(
                child.is_dir()
                and child.name not in self._active_requests
                and not (child / "response.json").exists()
                for child in self._receipts.iterdir()
            )
            # A stopped root stays stopped unless an explicit acknowledged authorization
            # re-arms it for a bounded number of new requests; the stop itself is never
            # read as permission and never removed.
            permitted = (
                not self._stopped() if self._resume is None else self._resume.allows(self._receipts)
            )
            if incomplete or not permitted:
                return self._failed(SUPPRESSED_ERROR_CODE, 0)
            if self._resume is not None:
                self._resume.consume(self._receipts, request["request_id"])
            directory = self._receipts / request["request_id"]
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                raise ValueError("TAGGING_RECEIPT_EXISTS") from None
            self._active_requests.add(request["request_id"])
        write = UpstageClaimExtractor._write
        write(
            directory / "request.json",
            canonical_json(
                dict(
                    request=request,
                    request_sha256=canonical_hash(request),
                    model_sha256=settings.model_sha256,
                    prompt_sha256=canonical_hash(system),
                    wire_system=wire_system,
                    wire_user_json=wire_user,
                    wire_prompt_sha256=canonical_hash(wire_system),
                    transport_version=self.TRANSPORT_VERSION,
                    evidence_refs=refs,
                    authorization=authorization.to_dict(),
                )
            ),
        )
        started = monotonic_ns()
        try:
            receipt = self._probe.complete(
                wire_system,
                wire_user,
                request_id=request["request_id"],
                max_tokens=request["max_tokens"],
                json_mode=True,
                **(
                    {"schema_json": wire_schema} if isinstance(self._probe, OpenRouterProbe) else {}
                ),
            )
        except Exception as error:
            code = str(error)
            if code not in UPSTAGE_TRANSPORT_STOP_CODES:
                code = "UPSTREAM_UNAVAILABLE"
            # Exclusive creation prevents overwriting an earlier failure receipt.
            if not stop.exists():
                write(stop, canonical_json(dict(code=code, request_id=request["request_id"])))
            else:
                # An acknowledged stop is immutable, so this new failure is
                # recorded beside it: the next process to arm this root finds an
                # unacknowledged durable failure and refuses again.
                chained = self._receipts / f"transport-stop.{request['request_id']}.json"
                if not chained.exists():
                    write(
                        chained, canonical_json(dict(code=code, request_id=request["request_id"]))
                    )
            if self._resume is not None:
                self._resume.disarm(self._receipts)
            response = self._failed(code, (monotonic_ns() - started) // 1_000_000)
        else:
            try:
                if receipt.get("schema_valid") is False:
                    raise ValueError("provider schema invalid")
                payload = json.loads(receipt["content"])
                for element in payload.get("elements", []):
                    selected = element["evidence_refs"]
                    if not isinstance(selected, list):
                        raise ValueError("invalid references")
                    element["evidence_refs"] = [self._restore_ref(key, refs) for key in selected]
                    if settings.model_profile in (QUOTE_V4_PROFILE, QUOTE_V5_PROFILE):
                        self._refine_g1_year(element)
                expanded = canonical_json(payload)
            except (ValueError, KeyError, TypeError, AttributeError):
                expanded = None
                receipt = {**receipt, "validation_error": "TAGGING_EVIDENCE_ID_INVALID"}
            response = RawTagResponse(
                expanded,
                TokenUsage(
                    receipt["input_tokens"],
                    receipt["output_tokens"],
                    0,
                    0,
                    (monotonic_ns() - started) // 1_000_000,
                    "succeeded",
                    receipt["provider_request_id"],
                ),
                False,
                canonical_json(receipt),
            )
        write(directory / "response.json", canonical_json(asdict(response)))
        return response

    @staticmethod
    def _failed(code: str, latency: int) -> RawTagResponse:
        return RawTagResponse(
            None,
            TokenUsage(None, None, None, None, latency, "failed", None, code),
            False,
            canonical_json(dict(error_code=code)),
        )
