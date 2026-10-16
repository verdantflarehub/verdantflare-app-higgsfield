"""Private credential-rotation observation; no tokens leave this module."""
from datetime import UTC, datetime
import json
from pathlib import Path
import time

from .credentials import atomic_json

OBSERVATION = "refresh-observation.json"


def snapshot(state: Path):
    try:
        path = state / "credentials.json"
        if path.is_symlink():
            return None
        with path.open("rb") as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("auth_version") != 2
                or type(value.get("expires_at")) is not int
                or not all(isinstance(value.get(k), str) and 1 <= len(value[k]) <= 32768
                           for k in ("access_token", "refresh_token"))):
            return None
        return value["access_token"], value["refresh_token"], value["expires_at"]
    except (OSError, ValueError):
        return None


def observe_success(state: Path, before):
    """Called only after a successful CLI command under the credential owner lock.

    Observation failure must never turn a successful create into a retryable error.
    """
    after = snapshot(state)
    if (before is None or after is None or before[0] == after[0]
            or before[1] == after[1] or after[2] <= max(before[2], time.time())):
        return
    try:
        atomic_json(state / OBSERVATION, {
            "schema_version": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "expires_at": after[2],
        })
    except OSError:
        # An older observation no longer matches the current expiry and is not
        # presented as proof of this rotation. Do not alter the command result.
        pass


def last_refresh(state: Path):
    current = snapshot(state)
    if current is None:
        return None
    try:
        path = state / OBSERVATION
        if path.is_symlink():
            return None
        with path.open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict)
                or set(value) != {"schema_version", "observed_at", "expires_at"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["expires_at"]) is not int or value["expires_at"] != current[2]
                or not isinstance(value["observed_at"], str)):
            return None
        observed = datetime.fromisoformat(value["observed_at"])
        if observed.tzinfo is None or observed.timestamp() > time.time():
            return None
        return value["observed_at"]
    except (OSError, ValueError, OverflowError):
        return None
