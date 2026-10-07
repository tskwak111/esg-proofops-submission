"""Pre-dispatch controls; usage polling is not an atomic global reservation."""

import json
import math
import os
import threading
import urllib.error
import urllib.request
from collections import deque
from time import monotonic


class LiveError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


# Best effort only: separate Vercel processes/functions have separate memory.
_recent: deque[tuple[float, str]] = deque()
_lock = threading.Lock()


def check_enabled():
    if os.getenv("LIVE_DISABLED") == "1":
        raise LiveError(503, "LIVE_DISABLED")


def _setting(name, default, *, integer=False):
    try:
        value = int(os.getenv(name, default)) if integer else float(os.getenv(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except ValueError as exc:
        raise LiveError(503, "LIMIT_CHECK_UNAVAILABLE") from exc


def daily_usage():
    """GET /key usage_daily is key-wide USD usage, including other models on that key.

    https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key
    No caching: every admitted request checks before any Upstage/model dispatch.
    """
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise LiveError(503, "LIMIT_CHECK_UNAVAILABLE")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise ValueError
        value = json.loads(raw)["data"]["usage_daily"]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError
        return value
    except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        raise LiveError(503, "LIMIT_CHECK_UNAVAILABLE") from exc


def check_limits(forwarded_for: str):
    daily = _setting("LIVE_DAILY_LUNA_USD", "0.50")
    per_ip = _setting("LIVE_IP_PER_MINUTE", "6", integer=True)
    total = _setting("LIVE_TOTAL_PER_MINUTE", "20", integer=True)
    ip = forwarded_for.split(",", 1)[0].strip() or "unknown"
    with _lock:
        now = monotonic()
        while _recent and _recent[0][0] <= now - 60:
            _recent.popleft()
        if len(_recent) >= total or sum(item[1] == ip for item in _recent) >= per_ip:
            raise LiveError(429, "RATE_LIMITED")
        _recent.append((now, ip))
    if daily_usage() >= daily:
        raise LiveError(429, "DAILY_LIMIT")
