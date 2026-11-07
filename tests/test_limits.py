import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest

from pydantic import ValidationError

from test_gateway import FakeCLI, ORG, USER, request
from higgsfield_gateway.jobs import Gateway, GatewayError
from higgsfield_gateway.models import LimitUpdate


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = FakeCLI(self.root / "auth")
        self.gateway = self.reopen()

    def reopen(self, **overrides):
        options = dict(binding="team", organization=ORG, workspace="workspace_test",
                       credit_limit=70, submission_limit=3, max_concurrent=3)
        options.update(overrides)
        return Gateway(self.root, self.cli, **options)

    def change(self, **overrides):
        data = dict(binding_id="team", revision=1, credit_limit="4.5", submission_limit=2,
                    operation_id="limit-change-one")
        data.update(overrides)
        return LimitUpdate(**data)

    def save(self, change, **actor):
        identity = dict(user_id=USER, organization_id=ORG, station_id="station-test")
        identity.update(actor)
        return self.gateway.update_limits(change, **identity)

    def test_update_restart_replay_and_audit_preserve_jobs(self):
        task = self.gateway.submit(request())
        accepted = self.save(self.change(credit_limit="0", submission_limit=0))
        restarted = self.reopen(credit_limit=999, submission_limit=999)
        self.assertEqual(restarted.limits()["credit_limit"], "0")
        self.assertEqual(restarted.limits()["revision"], 2)
        self.assertEqual(restarted.limits()["reserved_credits"], "2")
        self.assertEqual(restarted.limits()["submitted_count"], 1)
        self.assertEqual(restarted.load(task["request_id"])["remote_id"], task["remote_id"])
        self.assertEqual(self.save(self.change(credit_limit="0", submission_limit=0)), accepted)
        with restarted.db() as db:
            rows = db.execute("SELECT record FROM limit_operations").fetchall()
        self.assertEqual(len(rows), 1)
        audit = json.loads(rows[0][0])
        self.assertEqual(audit["actor"]["user_id"], USER)
        self.assertEqual(audit["before"]["credit_limit"], "70")
        self.assertEqual(audit["after"], accepted)
        restarted.submit(request(2))

    def test_migration_initializes_zero_without_resetting_old_journal(self):
        task = self.gateway.submit(request())
        # Recreate a pre-management journal using only this temporary database.
        with self.gateway.db() as db:
            db.execute("DROP TABLE limits")
            db.execute("DROP TABLE limit_operations")
        migrated = self.reopen(credit_limit=0, submission_limit=0)
        self.assertEqual(migrated.limits()["credit_limit"], "0")
        self.assertEqual(migrated.limits()["revision"], 1)
        self.assertEqual(migrated.limits()["submitted_count"], 1)
        self.assertEqual(migrated.load(task["request_id"])["remote_id"], task["remote_id"])
        self.assertTrue(migrated.capabilities()["generation_enabled"])

    def test_replay_does_not_overwrite_newer_revision(self):
        first = self.save(self.change())
        second = self.save(self.change(revision=2, operation_id="second", credit_limit="0"))
        self.assertEqual(self.save(self.change()), first)
        self.assertEqual(self.gateway.limits()["revision"], second["revision"])
        self.assertEqual(self.gateway.limits()["credit_limit"], "0")
        with self.assertRaisesRegex(GatewayError, "idempotency_conflict"):
            self.save(self.change(credit_limit="10"))
        with self.assertRaisesRegex(GatewayError, "idempotency_conflict"):
            self.save(self.change(), user_id="another-user")

    def test_concurrent_edit_only_accepts_matching_revision(self):
        barrier = threading.Barrier(2)
        def edit(number):
            barrier.wait(timeout=5)
            try:
                return self.save(self.change(operation_id=f"update-{number}"))["revision"]
            except GatewayError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(edit, [1, 2]))
        self.assertCountEqual(outcomes, [2, "limits_revision_conflict"])
        with self.gateway.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM limit_operations").fetchone()[0], 1)

    def test_fractional_credits_use_decimal_arithmetic(self):
        self.cli.cost = 0.1
        self.save(self.change(credit_limit="0.3", submission_limit=4))
        for number in [1, 2, 3]:
            self.gateway.submit(request(number))
        self.assertEqual(self.gateway.limits()["reserved_credits"], "0.3")
        with self.assertRaisesRegex(GatewayError, "account_concurrency_exceeded"):
            self.gateway.submit(request(4))

    def test_wrong_binding_and_organization_cannot_replay(self):
        self.save(self.change())
        for change, actor in [(self.change(binding_id="other"), {}),
                              (self.change(), {"organization_id": "other"})]:
            with self.assertRaisesRegex(GatewayError, "account_access_denied"):
                self.save(change, **actor)
        self.assertEqual(self.gateway.limits()["revision"], 2)

    def test_audit_failure_rolls_back_configuration(self):
        with self.gateway.db() as db:
            db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON limit_operations "
                       "BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.save(self.change())
        self.assertEqual(self.gateway.limits()["revision"], 1)
        self.assertEqual(self.gateway.limits()["credit_limit"], "70")

    def test_historical_reservations_never_reduce_provider_balance(self):
        self.gateway.submit(request())
        with self.gateway.db() as db:
            job = json.loads(db.execute("SELECT record FROM jobs").fetchone()[0])
            job.update(status="completed", stage="completed", quoted_credits=1042)
            self.gateway.save(db, job)
        self.save(self.change(credit_limit="1053", submission_limit=1))
        self.cli.cost = 20
        self.assertTrue(self.gateway.cost(request(2).generation)["can_submit"])
        restarted = self.reopen()
        restarted.submit(request(2))
        self.assertEqual(restarted.limits()["submitted_count"], 2)
        self.assertEqual(restarted.limits()["reserved_credits"], "1062")
        self.assertEqual(restarted.limits()["enforcement"], "retired")

    def test_retirement_still_checks_balance_concurrency_and_unknown(self):
        self.save(self.change(credit_limit="0", submission_limit=0))
        self.gateway.submit(request())
        self.gateway.max_concurrent = 1
        self.assertEqual(self.gateway.cost(request(2).generation)["blocked_reason"], "account_concurrency_exceeded")
        self.gateway.max_concurrent = 3
        self.gateway.max_job_credits = 200
        self.cli.cost = 101
        self.assertEqual(self.gateway.cost(request(2).generation)["blocked_reason"], "insufficient_credits")
        with self.assertRaisesRegex(GatewayError, "insufficient_credits"):
            self.gateway.submit(request(2))
        self.cli.cost = 2
        with self.gateway.db() as db:
            job = json.loads(db.execute("SELECT record FROM jobs").fetchone()[0])
            job["stage"] = "submission_unknown"
            self.gateway.save(db, job)
        self.assertEqual(self.gateway.cost(request(2).generation)["blocked_reason"], "account_reconciliation_required")

    def test_input_is_bounded_and_rejects_identity_overrides(self):
        for field, value in [("credit_limit", "NaN"), ("credit_limit", "Infinity"),
                             ("credit_limit", "1e3"), ("credit_limit", "-1"),
                             ("credit_limit", 5), ("credit_limit", "0.0000001"),
                             ("credit_limit", "1000000000000"), ("submission_limit", True),
                             ("submission_limit", -1), ("revision", 0), ("user_id", USER)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                self.change(**{field: value})


if __name__ == "__main__":
    unittest.main()
