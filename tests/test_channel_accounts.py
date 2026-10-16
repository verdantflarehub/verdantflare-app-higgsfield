import tempfile
from pathlib import Path
import unittest

import httpx

from test_gateway import FakeCLI, ORG, USER, request
from higgsfield_gateway.accounts import registration
from higgsfield_gateway.jobs import Gateway, GatewayError
from higgsfield_gateway.management import create_management_app, Management


class BoundAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = FakeCLI(self.root / 'auth')
        self.gateway = self.reopen()
        app = create_management_app(self.gateway, token='m' * 40, execution_token='e' * 40, station_id='station')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.headers = {'Authorization': 'Bearer ' + 'm' * 40, 'X-Organization-Id': ORG,
                        'X-User-Id': USER, 'X-Station-Id': 'station'}

    def reopen(self):
        return Gateway(self.root, self.cli, binding='team', organization=ORG,
                       workspace='workspace_test', credit_limit=70, submission_limit=5, max_concurrent=2)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def test_registration_survives_restart_and_never_changes_runtime_entitlements(self):
        value = {'membership_tier': 'Plus', 'membership_expiry': '2026-11-14',
                 'monthly_price_usd': '59', 'remaining_credits': '345', 'monthly_credits': '1200',
                 'video_concurrency': 6, 'image_concurrency': 8}
        response = await self.client.patch('/accounts/team', headers=self.headers,
            json={'expected_revision': 1, 'registration': value})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.reopen().channel_account.get()['registration'], value)
        self.assertEqual(self.gateway.max_concurrent, 2)
        self.assertEqual(self.cli.calls, [])

    async def test_disabled_and_archived_preserve_original_submission_replay(self):
        original = self.gateway.submit(request())
        for revision, method, body in ((1, 'PATCH', {'status': 'disabled'}), (2, 'DELETE', {})):
            response = await self.client.request(method, '/accounts/team', headers=self.headers,
                json={'expected_revision': revision, **body})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(self.gateway.limits()['budget_available'])
            self.assertEqual(self.gateway.submit(request()), original)
            with self.assertRaisesRegex(GatewayError, 'account_disabled'):
                self.gateway.submit(request(2))
        self.assertEqual(len([c for c in self.cli.calls if c[:2] == ['generate', 'create']]), 1)
        listed = await self.client.post('/accounts/search', headers=self.headers, json={})
        self.assertEqual(listed.json()['total'], 0)
        self.assertEqual(self.reopen().channel_account.get()['status'], 'archived')

    async def test_cross_org_and_revision_conflict(self):
        denied = await self.client.get('/accounts/team', headers=self.headers | {'X-Organization-Id': 'other'})
        self.assertEqual(denied.status_code, 403)
        conflict = await self.client.patch('/accounts/team', headers=self.headers,
            json={'expected_revision': 99, 'status': 'disabled'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['code'], 'REVISION_CONFLICT')

    async def test_log_budget_is_quote_and_settlement_remains_unknown(self):
        self.gateway.submit(request())
        result = await self.client.post('/calls/search', headers=self.headers, json={})
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertEqual(data['summary']['task_count'], 1)
        self.assertEqual(data['summary']['budget']['amount'], str(self.cli.cost))
        self.assertIsNone(data['summary']['settlement']['amount'])
        self.assertEqual(data['items'][0]['budget']['unit'], 'Credits')
        self.assertNotIn('provider_transactions', data)
        self.assertEqual(Management(self.gateway).calls({'media': 'image'})['total'], 0)

    async def test_invalid_manual_fields_rejected(self):
        for field, value in (('remaining_credits', 'NaN'), ('monthly_credits', '-1'),
                             ('video_concurrency', True), ('membership_expiry', 'not-a-date')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                registration({field: value})

    async def test_registration_body_allows_valid_unicode_and_rejects_oversize(self):
        response = await self.client.patch('/accounts/team', headers=self.headers,
            json={'expected_revision': 1, 'registration': {'membership_tier': '会' * 2000, 'note': '注' * 2000}})
        self.assertEqual(response.status_code, 200)
        response = await self.client.patch('/accounts/team', headers=self.headers,
            json={'expected_revision': 2, 'registration': {'note': 'x' * 32768}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'REQUEST_TOO_LARGE')
        self.assertEqual(self.gateway.channel_account.get()['revision'], 2)
