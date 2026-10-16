import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from test_gateway import FakeCLI, ORG, USER, request
from higgsfield_gateway.cli import ProviderError
from higgsfield_gateway.jobs import Gateway
from higgsfield_gateway.management import create_management_app
from higgsfield_gateway.server import create_app
from higgsfield_gateway.refresh import snapshot, observe_success


class ManagementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = FakeCLI(self.root / "auth")
        self.gateway = Gateway(self.root, self.cli, binding="team", organization=ORG,
                               workspace="workspace_test", credit_limit=0, submission_limit=0)
        self.token = "unit-management-" + "m" * 32
        self.execution_token = "unit-execution-" + "e" * 32
        self.app = create_management_app(self.gateway, token=self.token,
                                         execution_token=self.execution_token, station_id="station-test")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        if self.app.state.management.check_task:
            await self.app.state.management.check_task
        await self.client.aclose()
        self.temp.cleanup()

    def headers(self, **overrides):
        return {"Authorization": "Bearer " + self.token, "X-User-Id": USER,
                "X-Organization-Id": ORG, "X-Station-Id": "station-test", **overrides}

    def change(self, **overrides):
        return dict(binding_id="team", revision=1, credit_limit="4", submission_limit=2,
                    operation_id="management-save", **overrides)

    async def test_authorization_projects_only_persisted_rotation_time(self):
        import json
        import time
        path = self.cli.state / "credentials.json"
        expiry = int(time.time()) + 3600
        path.write_text(json.dumps({"auth_version": 2, "expires_at": expiry,
                                    "access_token": "old-secret", "refresh_token": "old-refresh"}))
        before = snapshot(self.cli.state)
        initial = (await self.client.get("/authorization", headers=self.headers())).json()
        self.assertIsNone(initial["last_refresh_at"])
        path.write_text(json.dumps({"auth_version": 2, "expires_at": expiry + 3600,
                                    "access_token": "new-secret", "refresh_token": "new-refresh"}))
        observe_success(self.cli.state, before)
        checked = await self.check()
        self.assertIsNotNone(checked["last_refresh_at"])
        observed = checked["last_refresh_at"]
        for private in ("old-secret", "new-secret", "old-refresh", "new-refresh", "expires_at"):
            self.assertNotIn(private, json.dumps(checked))
        from higgsfield_gateway.management import Management
        self.assertEqual(Management(self.gateway).authorization()["last_refresh_at"], observed)
        path.unlink()
        self.assertIsNone(Management(self.gateway).authorization()["last_refresh_at"])

    async def test_read_and_write_reject_missing_wrong_and_duplicate_identity(self):
        cases = [{}, self.headers(Authorization="Bearer " + self.execution_token),
                 self.headers(**{"X-Organization-Id": "other", "X-Role": "admin"}),
                 self.headers(**{"X-Station-Id": "other"}), self.headers(**{"X-User-Id": ""}),
                 [*self.headers().items(), ("X-User-Id", USER)],
                 [*self.headers().items(), ("Authorization", "Bearer " + self.token)]]
        for headers in cases:
            for path in ["/overview", "/authorization", "/limits"]:
                response = await self.client.get(path, headers=headers)
                self.assertIn(response.status_code, {401, 403})
                self.assertEqual(set(response.json()), {"code"})
            response = await self.client.put("/limits", json=self.change(), headers=headers)
            self.assertIn(response.status_code, {401, 403})
            response = await self.client.post("/authorization/check", json={}, headers=headers)
            self.assertIn(response.status_code, {401, 403})
            response = await self.client.post("/usage/query", json={}, headers=headers)
            self.assertIn(response.status_code, {401, 403})
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.gateway.limits()["revision"], 1)

    async def test_management_credential_cannot_generate(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.gateway, self.execution_token)),
                                     base_url="http://test") as execution:
            response = await execution.post("/v1/jobs", json=request().model_dump(), headers=self.headers())
            self.assertEqual(response.status_code, 401)
        response = await self.client.post("/v1/jobs", json=request().model_dump(), headers=self.headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.cli.calls, [])
        with self.assertRaisesRegex(ValueError, "independent_management_identity_required"):
            create_management_app(self.gateway, token=self.token, execution_token=self.token, station_id="station-test")

    async def test_limits_conflict_and_idempotent_http_replay(self):
        first = await self.client.put("/limits", json=self.change(), headers=self.headers())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["revision"], 2)
        self.assertEqual(first.headers["cache-control"], "no-store")
        replay = await self.client.put("/limits", json=self.change(), headers=self.headers())
        self.assertEqual(replay.json(), first.json())
        stale = self.change()
        stale["operation_id"] = "second-operation"
        response = await self.client.put("/limits", json=stale, headers=self.headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"code": "LIMITS_REVISION_CONFLICT"})
        overview = (await self.client.get("/overview", headers=self.headers())).json()
        self.assertEqual(overview["generation_status"], "not_verified")
        self.assertEqual(overview["video_channel_status"], "not_checked")
        self.assertEqual(self.cli.calls, [])

    async def check(self):
        self.app.state.management.next_check = 0
        response = await self.client.post("/authorization/check", json={}, headers=self.headers())
        self.assertEqual(response.status_code, 202)
        await asyncio.wait_for(self.app.state.management.check_task, timeout=2)
        return (await self.client.get("/authorization", headers=self.headers())).json()

    async def test_observation_preserves_last_balance_and_distinguishes_auth_loss(self):
        initial = (await self.client.get("/authorization", headers=self.headers())).json()
        self.assertEqual(initial["status"], "unavailable")
        self.assertIsNone(initial["last_success"])
        observed = await self.check()
        self.assertEqual(observed["status"], "authorized")
        self.assertEqual(observed["last_success"]["balance_credits"], "100")
        self.assertIsNone(observed["last_refresh_at"])
        self.assertNotIn("private@example.invalid", str(observed))
        for error, state in [("provider_timeout", "unavailable"), ("authorization_required", "authorization_required")]:
            with patch.object(self.cli, "run", side_effect=ProviderError(error)):
                failed = await self.check()
            self.assertEqual(failed["status"], state)
            self.assertTrue(failed["stale"])
            self.assertEqual(failed["last_success"], observed["last_success"])
        restored = create_management_app(self.gateway, token=self.token, execution_token=self.execution_token,
                                          station_id="station-test")
        self.assertEqual(restored.state.management.authorization()["last_success"], observed["last_success"])
        self.assertEqual(self.gateway.limits()["submitted_count"], 0)

    async def test_repeated_check_coalesces_then_throttles(self):
        started, release = threading.Event(), threading.Event()
        original = self.gateway.account
        def held():
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("test release not received")
            return original()
        try:
            with patch.object(self.gateway, "account", side_effect=held):
                first = await self.client.post("/authorization/check", json={}, headers=self.headers())
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                second = await self.client.post("/authorization/check", json={}, headers=self.headers())
                self.assertEqual(first.json()["check_id"], second.json()["check_id"])
                state = await self.client.get("/authorization", headers=self.headers())
                self.assertEqual(state.json()["pending_check_id"], first.json()["check_id"])
                release.set()
                await asyncio.wait_for(self.app.state.management.check_task, timeout=2)
        finally:
            release.set()
        response = await self.client.post("/authorization/check", json={}, headers=self.headers())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len([call for call in self.cli.calls if call[:2] == ["account", "status"]]), 1)

    async def test_first_connection_is_distinct_from_revoked_authorization(self):
        (self.cli.state / "binding.json").unlink()
        initial = (await self.client.get("/authorization", headers=self.headers())).json()
        self.assertEqual(initial["status"], "unconnected")
        checked = await self.check()
        self.assertEqual(checked["status"], "unconnected")
        self.assertIsNone(checked["last_success"])
        self.assertEqual(self.cli.calls, [])

    async def test_usage_has_no_prompt_material_or_unproven_charge(self):
        await self.client.put("/limits", json=self.change(), headers=self.headers())
        await asyncio.to_thread(self.gateway.submit, request())
        response = await self.client.post("/usage/query", json={"page_size": 1}, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["items"][0]["provider_charge"])
        self.assertEqual(data["items"][0]["reconciliation_status"], "unreconciled")
        self.assertEqual(data["provider_transactions_status"], "not_collected")
        for private in ["paper boat", USER, "private@example.invalid", "result_url", "workspace_test"]:
            self.assertNotIn(private, response.text)

    async def test_malformed_inputs_do_not_modify_or_call_provider(self):
        for content in ['{"revision":1,"revision":2}', '{"credit_limit":NaN}', 'null',
                        '{"user_id":"pretend-admin"}', '{"x":"' + 'x' * 8192 + '"}']:
            response = await self.client.put("/limits", content=content,
                                              headers={**self.headers(), "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
        response = await self.client.get("/authorization?role=admin", headers=self.headers())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.gateway.limits()["revision"], 1)
        self.assertEqual(self.cli.calls, [])

    async def test_transactions_are_safe_persistent_and_never_matched_by_amount(self):
        await self.check()
        usage = (await self.client.post("/usage/query", json={}, headers=self.headers())).json()
        self.assertEqual(usage["items"], [])
        snapshot = usage["provider_transactions"]
        self.assertEqual(snapshot["status"], "observed")
        self.assertFalse(snapshot["stale"])
        self.assertEqual(snapshot["last_success"]["items"], [
            {"action": "spend", "created_at": "2026-10-14T09:00:00+00:00", "credits": "2"}])
        self.assertNotIn("private", str(usage))
        count = len(self.cli.calls)
        await self.client.post("/usage/query", json={}, headers=self.headers())
        self.assertEqual(len(self.cli.calls), count)
        original = self.cli.run
        def unavailable(args, timeout=60):
            if args[:2] == ["account", "transactions"]:
                raise ProviderError("provider_timeout")
            return original(args, timeout)
        with patch.object(self.cli, "run", side_effect=unavailable):
            auth = await self.check()
        self.assertEqual(auth["status"], "authorized")
        restored = create_management_app(self.gateway, token=self.token, execution_token=self.execution_token,
                                          station_id="station-test")
        failed = restored.state.management.transaction_snapshot()
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(failed["last_success"], snapshot["last_success"])

    async def test_zero_credit_limit_pauses_even_a_zero_quote(self):
        change = self.change()
        change["credit_limit"] = "0"
        await self.client.put("/limits", json=change, headers=self.headers())
        overview = (await self.client.get("/overview", headers=self.headers())).json()
        self.assertEqual(overview["generation_status"], "paused")
        self.cli.cost = 0
        from higgsfield_gateway.jobs import GatewayError
        with self.assertRaisesRegex(GatewayError, "account_budget_exceeded"):
            await asyncio.to_thread(self.gateway.submit, request())
        self.assertFalse(any(call[:2] == ["generate", "create"] for call in self.cli.calls))

    async def test_transactions_reject_invalid_provider_payload_without_losing_old_snapshot(self):
        await self.check()
        previous = self.app.state.management.transaction_snapshot()["last_success"]
        original = self.cli.run
        for payload in [{"items": [None]}, {"items": [{}]}, {"items": [{"credits": float("nan"), "created_at": "2026-10-14T09:00:00Z"}]},
                        {"items": [{"credits": True, "created_at": "2026-10-14T09:00:00Z"}]},
                        {"items": [{"credits": 1, "created_at": "2026-10-14"}]}, {"items": [{}] * 21}]:
            def invalid(args, timeout=60):
                return payload if args[:2] == ["account", "transactions"] else original(args, timeout)
            with patch.object(self.cli, "run", side_effect=invalid):
                auth = await self.check()
            self.assertEqual(auth["status"], "authorized")
            snapshot = self.app.state.management.transaction_snapshot()
            self.assertEqual(snapshot["status"], "unavailable")
            self.assertEqual(snapshot["last_success"], previous)


if __name__ == "__main__":
    unittest.main()
