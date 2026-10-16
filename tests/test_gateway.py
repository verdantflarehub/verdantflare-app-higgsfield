import concurrent.futures
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from higgsfield_gateway.cli import CLI, DMIT_PROXY, ProviderError
from higgsfield_gateway.credentials import CredentialError, handover_receipt, import_bundle
from higgsfield_gateway.jobs import Gateway, GatewayError
from higgsfield_gateway.models import Generation, JobRequest, LimitUpdate

ORG = "0199abcd-0000-7000-8000-000000000001"
USER = "0199abcd-0000-7000-8000-000000000002"
PROJECT = "0199abcd-0000-7000-8000-000000000003"
REMOTE = "11111111-1111-4111-8111-111111111111"


class FakeCLI:
    def __init__(self, state):
        self.state = state
        state.mkdir()
        (state / "binding.json").write_text('{"workspace_id":"workspace_test"}')
        self.calls = []
        self.ambiguous = False
        self.cost = 2
        self.result = "pending"

    def run(self, args, timeout=60):
        self.calls.append(args)
        if args[:2] == ["workspace", "status"]:
            return {"workspace_id": "workspace_test"}
        if args[:2] == ["account", "status"]:
            return {"credits": 100, "email": "private@example.invalid", "subscription_plan_type": "plus"}
        if args[:2] == ["account", "transactions"]:
            return {"items": [{"action": "spend", "created_at": "2026-10-14T09:00:00Z", "credits": 2,
                               "display_name": "private prompt", "email": "private@example.invalid"}]}
        if args[:2] == ["generate", "cost"]:
            return {"credits": self.cost}
        if args[:2] == ["generate", "create"]:
            if self.ambiguous:
                raise ProviderError("provider_timeout")
            return [REMOTE]
        if args[:2] == ["generate", "get"]:
            return {"id": REMOTE, "job_type": "grok_video_v15_lite", "status": self.result,
                    "result_url": "https://provider.invalid/private"}
        raise AssertionError("unhandled fake call")


def request(number=1, **changes):
    data = dict(request_id="video_task_" + f"{number:032x}", binding_id="team", organization_id=ORG,
                user_id=USER, project_id=PROJECT,
                generation=dict(model="grok_video_v15_lite", prompt="paper boat", duration_seconds=2))
    data.update(changes)
    return JobRequest.model_validate(data)


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = FakeCLI(self.root / "auth")
        self.gateway = self.make_gateway()

    def tearDown(self):
        self.temp.cleanup()

    def make_gateway(self, **changes):
        options = dict(binding="team", organization=ORG, workspace="workspace_test", credit_limit=70,
                       submission_limit=2, max_concurrent=2)
        options.update(changes)
        return Gateway(self.root, self.cli, **options)

    def creates(self):
        return len([c for c in self.cli.calls if c[:2] == ["generate", "create"]])

    def test_idempotency_survives_new_process(self):
        one = self.gateway.submit(request())
        two = self.make_gateway().submit(request())
        self.assertEqual(one["remote_id"], two["remote_id"])
        self.assertEqual(self.creates(), 1)

    def test_rejected_media_is_terminal_without_retry_or_new_charge(self):
        self.gateway.submit(request())
        self.cli.result = "completed"
        calls = []
        def reject(*args):
            calls.append(args)
            raise ProviderError("media_contract_mismatch")
        self.gateway.media = SimpleNamespace(collect=reject)
        result = self.gateway.status(request().request_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "media_contract_mismatch")
        self.assertEqual(result["remote_id"], REMOTE)
        restored = self.make_gateway()
        self.assertEqual(restored.status(request().request_id), result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.creates(), 1)
        self.assertEqual(restored.load(request().request_id)["quoted_credits"], 2)

    def test_changed_request_is_rejected(self):
        self.gateway.submit(request())
        new = request()
        new.generation.prompt = "different"
        with self.assertRaisesRegex(GatewayError, "idempotency_conflict"):
            self.gateway.submit(new)
        self.assertEqual(self.creates(), 1)

    def test_timeout_is_never_resubmitted_and_blocks_new_charges(self):
        self.cli.ambiguous = True
        self.assertEqual(self.gateway.submit(request())["stage"], "submission_unknown")
        other = self.make_gateway()
        other.submit(request())
        with self.assertRaisesRegex(GatewayError, "account_reconciliation_required"):
            other.submit(request(2))
        self.assertEqual(self.creates(), 1)

    def test_restart_during_submit_becomes_unknown(self):
        self.gateway.submit(request())
        record = self.gateway.load(request().request_id)
        record.update(stage="submitting", remote_id=None)
        with self.gateway.db() as db:
            self.gateway.save(db, record)
        other = self.make_gateway()
        self.assertEqual(other.status(request().request_id)["stage"], "submission_unknown")
        self.assertEqual(self.creates(), 1)

    def test_concurrent_requests_obey_atomic_budget(self):
        gateway = self.gateway
        gateway.update_limits(LimitUpdate(binding_id="team", revision=1, credit_limit="2",
                              submission_limit=2, operation_id="test-budget"),
                              user_id=USER, organization_id=ORG, station_id="station-test")
        def submit(n):
            try:
                return gateway.submit(request(n))["status"]
            except GatewayError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, [1, 2]))
        self.assertCountEqual(results, ["running", "account_budget_exceeded"])
        self.assertEqual(self.creates(), 1)

    def test_submission_count_is_independent_of_credit_balance(self):
        self.gateway.submit(request(1))
        self.gateway.submit(request(2))
        with self.assertRaisesRegex(GatewayError, "account_budget_exceeded"):
            self.gateway.submit(request(3))
        self.assertEqual(self.creates(), 2)

    def test_cross_organization_denied_before_provider(self):
        with self.assertRaisesRegex(GatewayError, "account_access_denied"):
            self.gateway.submit(request(organization_id="0199abcd-0000-7000-8000-000000000099"))
        self.assertEqual(self.cli.calls, [])

    def test_workspace_change_cannot_reuse_journal(self):
        with self.assertRaisesRegex(GatewayError, "account_binding_changed"):
            self.make_gateway(workspace="other")

    def test_account_response_does_not_include_email(self):
        self.assertNotIn("email", self.gateway.account())

    def test_auth_failure_before_submit_creates_no_job_or_reservation(self):
        with patch.object(self.cli, "run", side_effect=ProviderError("authorization_required")):
            with self.assertRaisesRegex(ProviderError, "authorization_required"):
                self.gateway.submit(request())
        with self.gateway.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.creates(), 0)

    def test_auth_failure_during_poll_keeps_original_task_for_recovery(self):
        original = self.gateway.submit(request())
        before = self.gateway.load(original["request_id"])
        with patch.object(self.cli, "run", side_effect=ProviderError("authorization_required")):
            with self.assertRaisesRegex(ProviderError, "authorization_required"):
                self.gateway.status(original["request_id"])
        self.assertEqual(self.gateway.load(original["request_id"]), before)
        recovered = self.make_gateway().status(original["request_id"])
        self.assertEqual(recovered["remote_id"], original["remote_id"])
        self.assertEqual(self.creates(), 1)

    def test_zero_limits_disable_generation(self):
        self.gateway.update_limits(LimitUpdate(binding_id="team", revision=1, credit_limit="0",
                                   submission_limit=0, operation_id="test-pause"),
                                   user_id=USER, organization_id=ORG, station_id="station-test")
        with self.assertRaisesRegex(GatewayError, "account_budget_exceeded"):
            self.make_gateway(credit_limit=0, submission_limit=0).submit(request())
        self.assertEqual(self.creates(), 0)

    def test_cli_proxy_cannot_be_overridden_by_environment(self):
        cli = CLI(Path("hf"), self.root)
        with patch.dict(os.environ, {"NO_PROXY": "*", "https_proxy": "http://wrong", "HIGGSFIELD_API_URL": "http://wrong"}):
            env = cli.environment()
        self.assertEqual(env["HTTPS_PROXY"], DMIT_PROXY)
        self.assertEqual(env["https_proxy"], DMIT_PROXY)
        self.assertNotIn("*", env["NO_PROXY"])
        self.assertNotIn("HIGGSFIELD_API_URL", env)
        with self.assertRaisesRegex(ValueError, "dmit_proxy_required"):
            CLI(Path("hf"), self.root, proxy="http://wrong")

    def test_official_personal_workspace_text_requires_exact_output(self):
        for output, valid in ((b"No workspace selected.\n", True), (b"Other workspace selected.\n", False)):
            def process(*args, **kwargs):
                kwargs["stdout"].write(output)
                kwargs["stdout"].flush()
                return SimpleNamespace(returncode=0, poll=lambda: 0, wait=lambda: 0)
            with patch("higgsfield_gateway.cli.subprocess.Popen", side_effect=process):
                if valid:
                    self.assertEqual(CLI(Path("hf"), self.root).run(["workspace", "status"]),
                                     {"context": "personal", "workspace_id": None})
                else:
                    with self.assertRaisesRegex(ProviderError, "provider_protocol_error"):
                        CLI(Path("hf"), self.root).run(["workspace", "status"])

    def test_personal_binding_rejects_switch_to_workspace(self):
        (self.cli.state / "binding.json").write_text('{"workspace_id":"personal"}')
        gateway = Gateway(self.root / "personal-journal", self.cli, binding="team", organization=ORG, workspace="personal")
        with patch.object(self.cli, "run", return_value={"context": "personal", "workspace_id": None}):
            gateway.check_binding()
        with self.assertRaisesRegex(GatewayError, "workspace_binding_mismatch"):
            gateway.check_binding()

    def test_token_command_is_not_available(self):
        with self.assertRaisesRegex(ProviderError, "command_denied"):
            CLI(Path("hf"), self.root).run(["auth", "token"])

    def test_official_login_hint_is_mapped_without_private_diagnostic(self):
        cases = [
            (2, b"Error: private diagnostic\nHint: Run: hf auth login\n", "authorization_required"),
            (2, b"Error: network unavailable\n", "provider_call_failed"),
            (1, b"Hint: Run: hf auth login\n", "provider_call_failed"),
            (2, b"Error: echoed text: Hint: Run: hf auth login\n", "provider_call_failed"),
            (2, b"private" * 600000, "provider_output_exceeded"),
        ]
        for code, diagnostic, expected in cases:
            with self.subTest(code=code, expected=expected):
                def process(*args, **kwargs):
                    kwargs["stderr"].write(diagnostic)
                    kwargs["stderr"].flush()
                    return SimpleNamespace(returncode=code, poll=lambda: code, wait=lambda: code)
                with patch("higgsfield_gateway.cli.subprocess.Popen", side_effect=process):
                    with self.assertRaises(ProviderError) as caught:
                        CLI(Path("hf"), self.root).run(["account", "status"])
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(str(caught.exception), expected)

    def test_invalid_parameters_cannot_be_passed_as_cli_flags(self):
        with self.assertRaises(ValueError):
            Generation(model="grok_video_v15_lite", prompt="boat", duration_seconds=2, arbitrary="--token")
        with self.assertRaises(ValueError):
            Generation(model="grok_video_v15_lite", prompt="boat", duration_seconds=True)

    def test_credential_import_requires_current_revision(self):
        auth = self.root / "new_auth"
        bundle = {"credentials": {"auth_version": 2, "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh", "expires_at": 123},
                  "config": {"workspace_id": "workspace_test"}, "workspace_id": "workspace_test"}
        revision = import_bundle(auth, bundle)
        with self.assertRaisesRegex(CredentialError, "credential_revision_conflict"):
            import_bundle(auth, bundle)
        bundle["credentials"]["refresh_token"] = "synthetic-rotated"
        new = import_bundle(auth, bundle, revision)
        self.assertNotEqual(new, revision)
        with self.assertRaisesRegex(CredentialError, "credential_revision_conflict"):
            import_bundle(auth, bundle, revision)
        bundle["workspace_id"] = "other"
        with self.assertRaisesRegex(CredentialError, "workspace_binding_conflict"):
            import_bundle(auth, bundle, new)

    def test_receipt_confirms_original_handover_after_token_rotation(self):
        auth = self.root / "auth"
        bundle = {"credentials": {"auth_version": 2, "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh", "expires_at": 123},
                  "config": {}, "workspace_id": "workspace_test"}
        revision = import_bundle(auth, bundle)
        (auth / "credentials.json").write_text('{"refresh_token":"synthetic-rotated"}')
        with patch("higgsfield_gateway.credentials.lifetime_lock") as lock:
            receipt = handover_receipt(self.root, revision)
        self.assertEqual(receipt, {"imported": True, "revision": revision, "workspace_id": "workspace_test"})
        lock.assert_not_called()

    def test_receipt_crash_repair_requires_exact_digest_and_exclusive_writer(self):
        auth = self.root / "auth"
        bundle = {"credentials": {"auth_version": 2, "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh", "expires_at": 123},
                  "config": {}, "workspace_id": "workspace_test"}
        revision = import_bundle(auth, bundle)
        (auth / "handover-receipt.json").unlink()
        with patch("higgsfield_gateway.credentials.lifetime_lock", side_effect=CredentialError("credential_writer_active")):
            with self.assertRaisesRegex(CredentialError, "credential_writer_active"):
                handover_receipt(self.root, revision)
        with patch("higgsfield_gateway.credentials.lifetime_lock", return_value=nullcontext()):
            with self.assertRaisesRegex(CredentialError, "credential_revision_conflict"):
                handover_receipt(self.root, "0" * 64)
            self.assertTrue(handover_receipt(self.root, revision)["imported"])


if __name__ == "__main__":
    unittest.main()
