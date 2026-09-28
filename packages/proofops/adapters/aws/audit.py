"""DynamoDB audit append transaction with immutable event and HEAD CAS (TASK-022)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from proofops.domain.audit import AuditConflict, AuditEvent, AuditHead, ChangeSet, new_audit_event


class DynamoAuditClient(Protocol):
    def transact_write_items(self, **kwargs: Any) -> Mapping[str, Any]: ...


AttributeValue = dict[str, str | bool]


def _string(value: str) -> AttributeValue:
    return {"S": value}


def _number(value: int) -> AttributeValue:
    return {"N": str(value)}


def _optional_string(value: str | None) -> AttributeValue:
    return {"NULL": True} if value is None else {"S": value}


def _partition_key(event: AuditEvent) -> str:
    return f"T#{event.tenant_id}#RUN#{event.run_id}"


def _event_item(event: AuditEvent) -> dict[str, AttributeValue]:
    return {
        "PK": _string(_partition_key(event)),
        "SK": _string(f"EVENT#{event.sequence:015d}"),
        "schema_version": _number(1),
        "tenant_id": _string(event.tenant_id),
        "run_id": _string(event.run_id),
        "sequence": _number(event.sequence),
        "event_id": _string(event.event_id),
        "actor_sub": _string(event.actor_sub),
        "action": _string(event.action),
        "target_id": _string(event.target_id),
        "before_hash": _optional_string(event.before_hash),
        "after_hash": _string(event.after_hash),
        "revision": _number(event.revision),
        "previous_event_hash": _optional_string(event.previous_event_hash),
        "event_hash": _string(event.event_hash),
        "timestamp": _string(event.timestamp),
        "reason": _optional_string(event.reason),
    }


def _head_put(table_name: str, event: AuditEvent, expected_head: AuditHead) -> dict[str, object]:
    values: dict[str, AttributeValue] = {}
    if expected_head.sequence == 0:
        condition = "attribute_not_exists(#sequence) AND attribute_not_exists(#event_hash)"
    else:
        condition = "#sequence = :expected_sequence AND #event_hash = :expected_hash"
        values = {
            ":expected_sequence": _number(expected_head.sequence),
            ":expected_hash": _string(expected_head.event_hash or ""),
        }
    put: dict[str, object] = {
        "TableName": table_name,
        "Item": {
            "PK": _string(_partition_key(event)),
            "SK": _string("HEAD"),
            "schema_version": _number(1),
            "tenant_id": _string(event.tenant_id),
            "run_id": _string(event.run_id),
            "sequence": _number(event.sequence),
            "event_hash": _string(event.event_hash),
            "updated_at": _string(event.timestamp),
        },
        "ConditionExpression": condition,
        "ExpressionAttributeNames": {"#sequence": "sequence", "#event_hash": "event_hash"},
    }
    if values:
        put["ExpressionAttributeValues"] = values
    return put


def _is_conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    details = response.get("Error")
    code = details.get("Code") if isinstance(details, Mapping) else None
    if code == "ConditionalCheckFailedException":
        return True
    reasons = response.get("CancellationReasons")
    return (
        code == "TransactionCanceledException"
        and isinstance(reasons, list)
        and any(
            isinstance(reason, Mapping) and reason.get("Code") == "ConditionalCheckFailed"
            for reason in reasons
        )
    )


def append_audit_transaction(
    client: DynamoAuditClient,
    *,
    table_name: str,
    change: ChangeSet,
    expected_head: AuditHead,
    event_id_factory: Callable[[], str] = lambda: str(uuid4()),
    clock: Callable[[], str] = lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"),
) -> tuple[AuditEvent, AuditHead]:
    """Append an event and replace its stream HEAD atomically or fail closed."""
    if not isinstance(table_name, str) or not table_name:
        raise ValueError("table_name must be a non-empty string")
    event, new_head = new_audit_event(
        change,
        expected_head,
        event_id=event_id_factory(),
        timestamp=clock(),
    )
    request = {
        "TransactItems": [
            {
                "Put": {
                    "TableName": table_name,
                    "Item": _event_item(event),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            },
            {"Put": _head_put(table_name, event, expected_head)},
        ],
        "ClientRequestToken": event.event_id,
    }
    try:
        client.transact_write_items(**request)
    except Exception as error:
        if _is_conditional_failure(error):
            raise AuditConflict("audit HEAD is stale; no event was appended") from None
        raise
    return event, new_head
