"""Offline, atomic OAuth handover. Never print a credential or accept one in argv."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


class CredentialError(ValueError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".handover-", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def import_bundle(root: Path, bundle: dict, expected_revision: str | None = None) -> str:
    """Called with the service stopped; the caller holds the same lifetime lock."""
    if set(bundle) != {"credentials", "config", "workspace_id"}:
        raise CredentialError("invalid_handover")
    credentials, config, workspace = (bundle[k] for k in ("credentials", "config", "workspace_id"))
    if not isinstance(config, dict) or not isinstance(credentials, dict):
        raise CredentialError("invalid_handover")
    if not isinstance(workspace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace):
        raise CredentialError("invalid_workspace")
    if (credentials.get("auth_version") != 2 or
            type(credentials.get("expires_at")) is not int or
            not all(isinstance(credentials.get(k), str) and 1 <= len(credentials[k]) <= 32768
                    for k in ("access_token", "refresh_token"))):
        raise CredentialError("invalid_credentials")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise CredentialError("invalid_state_directory")
    os.chmod(root, 0o700)
    target = root / "credentials.json"
    if target.is_symlink():
        raise CredentialError("invalid_credentials_file")
    if target.exists():
        revision = hashlib.sha256(target.read_bytes()).hexdigest()
        if expected_revision is None or revision != expected_revision:
            raise CredentialError("credential_revision_conflict")
        try:
            old_workspace = json.loads((root / "binding.json").read_text(encoding="utf-8"))["workspace_id"]
        except (OSError, ValueError, KeyError):
            raise CredentialError("workspace_binding_missing") from None
        if old_workspace != workspace:
            raise CredentialError("workspace_binding_conflict")
    elif expected_revision is not None:
        raise CredentialError("credential_revision_conflict")
    # Configuration and binding are committed before the credential. A crash
    # cannot make a new token available under an old workspace declaration.
    atomic_json(root / "config.json", config)
    atomic_json(root / "binding.json", {"workspace_id": workspace})
    # A new authorization must not inherit the previous credential's evidence.
    (root / "refresh-observation.json").unlink(missing_ok=True)
    atomic_json(target, credentials)
    revision = hashlib.sha256(target.read_bytes()).hexdigest()
    atomic_json(root / "handover-receipt.json", {"revision": revision, "workspace_id": workspace})
    return revision


def lifetime_lock(root: Path):
    """Linux flock shared by serving and the offline importer, never a stale PID file."""
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = (root / ".owner.lock").open("a+b")
    os.chmod(root / ".owner.lock", 0o600)
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise CredentialError("credential_writer_active") from None
    return stream


def handover_receipt(root: Path, expected_revision: str) -> dict:
    """Confirm an import without reading tokens aloud or refreshing the account."""
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise CredentialError("invalid_credential_revision")
    auth = root / "auth"
    try:
        receipt = json.loads((auth / "handover-receipt.json").read_text(encoding="utf-8"))
        binding = json.loads((auth / "binding.json").read_text(encoding="utf-8"))
        if receipt.get("revision") == expected_revision and receipt.get("workspace_id") == binding.get("workspace_id"):
            return {"imported": True, **receipt}
    except (FileNotFoundError, ValueError):
        pass
    # If import crashed after replacing credentials but before the receipt,
    # repair only under the writer lock and only for the exact pending digest.
    with lifetime_lock(root):
        credential = auth / "credentials.json"
        if credential.is_symlink() or hashlib.sha256(credential.read_bytes()).hexdigest() != expected_revision:
            raise CredentialError("credential_revision_conflict")
        binding = json.loads((auth / "binding.json").read_text(encoding="utf-8"))
        receipt = {"revision": expected_revision, "workspace_id": binding["workspace_id"]}
        atomic_json(auth / "handover-receipt.json", receipt)
        return {"imported": True, **receipt}


def main():
    parser = argparse.ArgumentParser(description="Import an OAuth bundle on stdin while the gateway is stopped")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--expected-revision")
    parser.add_argument("--receipt", action="store_true")
    args = parser.parse_args()
    try:
        root = Path(args.state_dir)
        if args.receipt:
            print(json.dumps(handover_receipt(root, args.expected_revision)))
            return
        payload = sys.stdin.buffer.read(65537)
        if len(payload) > 65536:
            raise CredentialError("handover_too_large")
        with lifetime_lock(root):
            revision = import_bundle(root / "auth", json.loads(payload), args.expected_revision)
        print(json.dumps({"imported": True, "revision": revision}))
    except (ValueError, OSError, TypeError, KeyError):
        print('{"error":"credential_import_failed"}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
