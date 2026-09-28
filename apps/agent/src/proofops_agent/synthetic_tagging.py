"""Explicit local synthetic transport, not a product model or semantic classifier.

Classification must be supplied separately as source-backed preliminary tags.
This transport emits unknown for every requested element; it invents no evidence,
applicability, numerical check, assurance, track or grade.
"""

import json

from proofops.application.budget import TokenUsage
from proofops.application.tagging.service import RawTagResponse
from proofops.domain.rulepacks import canonical_json


class SyntheticTaggingTransport:
    synthetic = True

    @staticmethod
    def token_counter(text: str) -> int:
        # Explicit synthetic byte counter, not an approved model tokenizer.
        return len(text.encode("utf-8"))

    def invoke(self, request: dict) -> RawTagResponse:
        user = json.loads(request["user_json"])
        classification = json.loads(
            request["system_prompt"].rsplit(
                "\nValidated classification; tag only its elements: ", 1
            )[1]
        )
        raw = canonical_json(
            dict(
                claim_id=request["claim_id"],
                packet_sha256=request["packet_sha256"],
                replicate_id=request["replicate_id"],
                **classification,
                elements=[
                    dict(
                        element_id=element,
                        state="unknown",
                        evidence_refs=[],
                        normalized_value=None,
                        credited_from=None,
                        reason_code="synthetic_unresolved",
                    )
                    for element in user["untrusted_document_data"]["allowed_elements"]
                ],
                superlative_quote=None,
                warnings=["local_synthetic_transport"],
            )
        )
        usage = TokenUsage(
            self.token_counter(request["system_prompt"] + request["user_json"]),
            self.token_counter(raw),
            0,
            0,
            0,
            "succeeded",
            "synthetic:" + request["request_id"],
        )
        return RawTagResponse(raw, usage, True, canonical_json({"synthetic": True, "text": raw}))
