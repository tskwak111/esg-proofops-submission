"""Bounded local catalog snapshots using a caller-owned SQLite transaction."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from typing import Any

SCHEMA_VERSION = 1
CURSOR_TTL_SECONDS = 900
MAX_ROWS_PER_SNAPSHOT = 10_000
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_TENANT_SNAPSHOT_COUNT = 32
MAX_TENANT_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_GLOBAL_SNAPSHOT_COUNT = 256
MAX_GLOBAL_SNAPSHOT_BYTES = 128 * 1024 * 1024


class CatalogSchemaUnsupported(ValueError):
    pass


class InvalidCatalogCursor(ValueError):
    pass


class CatalogCapacityExceeded(ValueError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def initialize(db: sqlite3.Connection) -> None:
    """Initialize schema v1 inside the caller's active transaction."""
    if not db.in_transaction:
        raise ValueError("active transaction required")
    db.execute("CREATE TABLE IF NOT EXISTS catalog_schema (version INTEGER PRIMARY KEY)")
    versions = [row[0] for row in db.execute("SELECT version FROM catalog_schema")]
    if versions and versions != [SCHEMA_VERSION]:
        raise CatalogSchemaUnsupported("unsupported catalog schema")
    db.execute("INSERT OR IGNORE INTO catalog_schema VALUES (?)", (SCHEMA_VERSION,))
    db.execute("""CREATE TABLE IF NOT EXISTS catalog_cursor_key (
        id INTEGER PRIMARY KEY CHECK(id=1), secret BLOB NOT NULL)""")
    db.execute(
        "INSERT OR IGNORE INTO catalog_cursor_key VALUES (1, ?)",
        (secrets.token_bytes(32),),
    )
    db.execute("""CREATE TABLE IF NOT EXISTS catalog_list_snapshots (
        epoch INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT NOT NULL, endpoint TEXT NOT NULL,
        query_hash TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL)""")


def _key(db: sqlite3.Connection) -> bytes:
    row = db.execute("SELECT secret FROM catalog_cursor_key WHERE id=1").fetchone()
    if row is None or not isinstance(row[0], bytes) or len(row[0]) < 32:
        raise CatalogSchemaUnsupported("invalid catalog cursor key")
    return row[0]


def _scope_hash(query: Mapping[str, Any], limit: int) -> str:
    scoped = dict(query)
    if "limit" in scoped and scoped["limit"] != limit:
        raise ValueError("query limit mismatch")
    scoped["limit"] = limit
    return hashlib.sha256(_json(scoped).encode()).hexdigest()


def _decode(
    cursor: str,
    key: bytes,
    *,
    tenant_id: str,
    endpoint: str,
    query_hash: str,
    now: float,
) -> dict[str, Any]:
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        data, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(key, data, "sha256")):
            raise ValueError
        result = json.loads(data)
        expires = result["expires"]
        if (
            set(result) != {"tenant", "endpoint", "query_hash", "expires", "epoch", "after"}
            or result["tenant"] != tenant_id
            or result["endpoint"] != endpoint
            or result["query_hash"] != query_hash
            or isinstance(expires, bool)
            or not isinstance(expires, int | float)
            or not math.isfinite(expires)
            or now >= expires
            or type(result["epoch"]) is not int
            or type(result["after"]) is not int
            or result["epoch"] < 1
            or result["after"] < 0
        ):
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise InvalidCatalogCursor("invalid cursor") from None


def _encode(key: bytes, payload: Mapping[str, Any]) -> str:
    data = _json(payload).encode()
    return base64.urlsafe_b64encode(data + hmac.digest(key, data, "sha256")).decode()


def _materialize(loaded: object) -> tuple[list[Any], int | None, str]:
    snapshot_epoch = None
    if isinstance(loaded, Mapping) and "items" in loaded:
        if set(loaded) != {"items", "snapshot_epoch"}:
            raise ValueError("snapshot loader fields invalid")
        snapshot_epoch = loaded["snapshot_epoch"]
        loaded = loaded["items"]
        if snapshot_epoch is not None and (type(snapshot_epoch) is not int or snapshot_epoch < 0):
            raise ValueError("snapshot epoch invalid")
    if not isinstance(loaded, Iterable) or isinstance(loaded, str | bytes | Mapping):
        raise ValueError("snapshot items must be iterable")
    items: list[Any] = []
    encoded_items: list[str] = []
    prefix = '{"items":['
    suffix = f'],"snapshot_epoch":{_json(snapshot_epoch)}}}'
    payload_bytes = len((prefix + suffix).encode())
    for item in loaded:
        if len(items) == MAX_ROWS_PER_SNAPSHOT:
            raise CatalogCapacityExceeded("catalog snapshot row capacity exceeded")
        encoded = _json(item)
        payload_bytes += len(encoded.encode()) + bool(encoded_items)
        if payload_bytes > MAX_SNAPSHOT_BYTES:
            raise CatalogCapacityExceeded("catalog snapshot byte capacity exceeded")
        items.append(item)
        encoded_items.append(encoded)
    return items, snapshot_epoch, prefix + ",".join(encoded_items) + suffix


def _check_capacity(db: sqlite3.Connection, tenant_id: str, payload_bytes: int) -> None:
    if payload_bytes > MAX_SNAPSHOT_BYTES:
        raise CatalogCapacityExceeded("catalog snapshot byte capacity exceeded")
    tenant_count, tenant_bytes = db.execute(
        "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) "
        "FROM catalog_list_snapshots WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    global_count, global_bytes = db.execute(
        "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) "
        "FROM catalog_list_snapshots"
    ).fetchone()
    if (
        tenant_count + 1 > MAX_TENANT_SNAPSHOT_COUNT
        or tenant_bytes + payload_bytes > MAX_TENANT_SNAPSHOT_BYTES
        or global_count + 1 > MAX_GLOBAL_SNAPSHOT_COUNT
        or global_bytes + payload_bytes > MAX_GLOBAL_SNAPSHOT_BYTES
    ):
        raise CatalogCapacityExceeded("catalog snapshot storage capacity exceeded")


def page(
    db: sqlite3.Connection,
    *,
    tenant_id: str,
    endpoint: str,
    query: Mapping[str, Any],
    cursor: str | None,
    limit: int,
    now: float,
    load_items: Callable[[], object],
) -> dict[str, Any]:
    """Return one fixed page without opening or committing a transaction."""
    if not db.in_transaction:
        raise ValueError("active transaction required")
    if not tenant_id or not endpoint or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid catalog page request")
    if isinstance(now, bool) or not isinstance(now, int | float) or not math.isfinite(now):
        raise ValueError("invalid catalog time")
    query_hash = _scope_hash(query, limit)
    key = _key(db)
    db.execute("DELETE FROM catalog_list_snapshots WHERE expires<=?", (now,))
    if cursor is None:
        items, snapshot_epoch, payload = _materialize(load_items())
        expires = now + CURSOR_TTL_SECONDS
        payload_bytes = len(payload.encode())
        existing = db.execute(
            "SELECT epoch,expires FROM catalog_list_snapshots "
            "WHERE tenant_id=? AND endpoint=? AND query_hash=? AND payload=? AND expires>? "
            "ORDER BY epoch DESC LIMIT 1",
            (tenant_id, endpoint, query_hash, payload, now),
        ).fetchone()
        if existing is None:
            _check_capacity(db, tenant_id, payload_bytes)
            epoch = db.execute(
                "INSERT INTO catalog_list_snapshots "
                "(tenant_id,endpoint,query_hash,expires,payload) VALUES (?,?,?,?,?)",
                (tenant_id, endpoint, query_hash, expires, payload),
            ).lastrowid
        else:
            epoch, expires = existing
        offset = 0
    else:
        token = _decode(
            cursor,
            key,
            tenant_id=tenant_id,
            endpoint=endpoint,
            query_hash=query_hash,
            now=now,
        )
        row = db.execute(
            "SELECT expires,payload FROM catalog_list_snapshots "
            "WHERE epoch=? AND tenant_id=? AND endpoint=? AND query_hash=?",
            (token["epoch"], tenant_id, endpoint, query_hash),
        ).fetchone()
        if row is None or now >= row[0]:
            raise InvalidCatalogCursor("invalid cursor")
        epoch, expires, offset = token["epoch"], row[0], token["after"]
        stored = json.loads(row[1])
        if isinstance(stored, list):
            items, snapshot_epoch = stored, None
        elif isinstance(stored, Mapping) and set(stored) == {"items", "epoch"}:
            items, snapshot_epoch = stored["items"], stored["epoch"]
        elif isinstance(stored, Mapping) and set(stored) == {"items", "snapshot_epoch"}:
            items, snapshot_epoch = stored["items"], stored["snapshot_epoch"]
        else:
            raise InvalidCatalogCursor("invalid cursor")
        if (
            not isinstance(items, list)
            or len(items) > MAX_ROWS_PER_SNAPSHOT
            or (
                snapshot_epoch is not None
                and (type(snapshot_epoch) is not int or snapshot_epoch < 0)
            )
        ):
            raise InvalidCatalogCursor("invalid cursor")
    selected = items[offset : offset + limit]
    after = offset + len(selected)
    next_cursor = None
    if after < len(items):
        next_cursor = _encode(
            key,
            {
                "tenant": tenant_id,
                "endpoint": endpoint,
                "query_hash": query_hash,
                "expires": expires,
                "epoch": epoch,
                "after": after,
            },
        )
    return {
        "items": selected,
        "next_cursor": next_cursor,
        "snapshot_epoch": epoch if snapshot_epoch is None else snapshot_epoch,
    }
