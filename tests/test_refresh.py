import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from higgsfield_gateway.cli import CLI, ProviderError
from higgsfield_gateway.credentials import import_bundle
from higgsfield_gateway.refresh import OBSERVATION, last_refresh


class RefreshObservationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = CLI(Path("unused-test-cli"), self.root)
        self.expiry = int(time.time()) + 3600
        self.write("old-access", "old-refresh", self.expiry)

    def write(self, access, refresh, expiry):
        (self.root / "credentials.json").write_text(json.dumps({
            "auth_version": 2, "access_token": access, "refresh_token": refresh,
            "expires_at": expiry,
        }), encoding="utf-8")

    def run_rotation(self, access="new-access", refresh="new-refresh", expiry=None):
        def invoke(*_):
            self.write(access, refresh, self.expiry + 3600 if expiry is None else expiry)
            return {"credits": 100}
        with patch.object(self.cli, "_run", side_effect=invoke):
            return self.cli.run(["account", "status"])

    def test_actual_rotation_persists_without_tokens_and_survives_new_instance(self):
        self.run_rotation()
        first = last_refresh(self.root)
        self.assertIsNotNone(first)
        record = json.loads((self.root / OBSERVATION).read_text())
        self.assertEqual(set(record), {"schema_version", "observed_at", "expires_at"})
        for secret in ("old-access", "old-refresh", "new-access", "new-refresh"):
            self.assertNotIn(secret, json.dumps(record))
            self.assertNotIn(hashlib.sha256(secret.encode()).hexdigest(), json.dumps(record))
        another = CLI(Path("unused-test-cli"), self.root)
        with patch.object(another, "_run", return_value={"credits": 100}):
            another.run(["account", "status"])
        self.assertEqual(last_refresh(another.state), first)

    def test_reads_expiry_only_and_partial_changes_are_not_rotation(self):
        for access, refresh, expiry in [
            ("old-access", "old-refresh", self.expiry),
            ("old-access", "old-refresh", self.expiry + 3600),
            ("new-access", "old-refresh", self.expiry + 3600),
            ("old-access", "new-refresh", self.expiry + 3600),
            ("new-access", "new-refresh", self.expiry),
            ("new-access", "new-refresh", int(time.time()) - 1),
        ]:
            with self.subTest(access=access, refresh=refresh, expiry=expiry):
                self.write("old-access", "old-refresh", self.expiry)
                self.run_rotation(access, refresh, expiry)
                self.assertIsNone(last_refresh(self.root))
                self.assertFalse((self.root / OBSERVATION).exists())

    def test_unsuccessful_command_does_not_claim_verified_refresh(self):
        def failed(*_):
            self.write("new-access", "new-refresh", self.expiry + 3600)
            raise ProviderError("provider_call_failed")
        with patch.object(self.cli, "_run", side_effect=failed):
            with self.assertRaisesRegex(ProviderError, "provider_call_failed"):
                self.cli.run(["account", "status"])
        self.assertIsNone(last_refresh(self.root))

    def test_observation_storage_failure_never_changes_successful_create(self):
        self.run_rotation()
        def created(*_):
            self.write("third-access", "third-refresh", self.expiry + 7200)
            return ["existing-provider-task"]
        with patch.object(self.cli, "_run", side_effect=created) as call:
            with patch("higgsfield_gateway.refresh.atomic_json", side_effect=OSError("storage unavailable")):
                self.assertEqual(self.cli.run(["generate", "create"]), ["existing-provider-task"])
            self.assertEqual(call.call_count, 1)
        self.assertIsNone(last_refresh(self.root))

    def test_handover_removes_old_authorization_evidence(self):
        self.run_rotation()
        revision = hashlib.sha256((self.root / "credentials.json").read_bytes()).hexdigest()
        (self.root / "binding.json").write_text('{"workspace_id":"test"}')
        import_bundle(self.root, {"credentials": {"auth_version": 2,
            "access_token": "imported-access", "refresh_token": "imported-refresh",
            "expires_at": self.expiry + 3600}, "config": {}, "workspace_id": "test"}, revision)
        self.assertFalse((self.root / OBSERVATION).exists())
        self.assertIsNone(last_refresh(self.root))

    def test_malformed_missing_and_oversized_credentials_do_not_break_commands(self):
        for value in (None, "{", "null", '"string"', "[]", "x" * 131073):
            path = self.root / "credentials.json"
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(value)
            with patch.object(self.cli, "_run", return_value={"ok": True}):
                self.assertEqual(self.cli.run(["account", "status"]), {"ok": True})
            self.assertIsNone(last_refresh(self.root))

    def test_corrupt_or_mismatched_observations_are_unknown(self):
        self.run_rotation()
        good = json.loads((self.root / OBSERVATION).read_text())
        for value in (None, [], {**good, "expires_at": 1}, {**good, "observed_at": "invalid"},
                      {**good, "observed_at": "2099-01-01T00:00:00+00:00"},
                      {**good, "observed_at": "2026-01-01T00:00:00"},
                      {**good, "extra": "no"}, {**good, "schema_version": True}):
            (self.root / OBSERVATION).write_text(json.dumps(value))
            self.assertIsNone(last_refresh(self.root))


if __name__ == "__main__":
    unittest.main()
