"""Bounded observation of the real Video adapter; no provider or MCP calls."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
import time

import httpx

from .jobs import now

VIDEO_HEALTH = "http://video-mcp-server.verdantflare-video.svc.cluster.local:8000/health"


class VideoChannel:
    def __init__(self, binding, organization, *, client=None):
        self.digest = hashlib.sha256(json.dumps([binding, organization], separators=(",", ":"),
                                               ensure_ascii=False).encode("utf-8")).hexdigest()
        self.client = client or httpx.Client(trust_env=False, follow_redirects=False, timeout=3)
        self._lock = threading.Lock()
        self.next_read = 0.0
        self.last_success = None
        self.snapshot = {"status": "not_checked", "observed_at": None, "last_success": None, "stale": True}

    def close(self):
        self.client.close()

    def read(self):
        with self._lock:
            if time.monotonic() < self.next_read:
                return deepcopy(self.snapshot)
            self.next_read = time.monotonic() + 15
            status = "unavailable"
            try:
                raw = bytearray()
                started = time.monotonic()
                with self.client.stream("GET", VIDEO_HEALTH, timeout=3, follow_redirects=False) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 16384 or time.monotonic() - started > 3:
                            raise ValueError("invalid_health")
                data = json.loads(raw)
                channel = data["channels"]["higgsfield"]
                if (data["status"] != "ok" or not isinstance(channel, dict)
                        or set(channel) != {"enabled", "configured", "binding_digest"}
                        or type(channel["enabled"]) is not bool or type(channel["configured"]) is not bool
                        or channel["binding_digest"] != self.digest):
                    raise ValueError("invalid_health")
                status = "enabled" if channel["enabled"] and channel["configured"] else "disabled"
                self.last_success = {"status": status, "observed_at": now()}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, RecursionError):
                pass
            self.snapshot = {"status": status, "observed_at": now(), "last_success": self.last_success,
                             "stale": status == "unavailable"}
            return deepcopy(self.snapshot)


def generation_status(authorization, limits, channel, *, max_concurrent):
    if not limits["budget_available"]:
        return "paused"
    if channel["status"] == "disabled" and not channel["stale"]:
        return "channel_paused"
    if channel["status"] != "enabled" or channel["stale"]:
        return "not_verified"
    if authorization["status"] != "authorized" or authorization["stale"]:
        return "authorization_unverified"
    if limits["active_count"] >= max_concurrent:
        return "busy"
    return "eligible"
