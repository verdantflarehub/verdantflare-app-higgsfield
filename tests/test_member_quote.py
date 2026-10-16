"""Member quote admission does not expose account data or consume credits."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from test_gateway import FakeCLI, ORG, request
from higgsfield_gateway.jobs import Gateway, GatewayError


class MemberQuoteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cli = FakeCLI(root / 'auth')
        self.gateway = Gateway(root, self.cli, binding='team', organization=ORG,
                               workspace='workspace_test', credit_limit=4, submission_limit=2)

    def tearDown(self):
        self.temp.cleanup()

    def test_quote_is_member_safe_and_expires(self):
        value = self.gateway.cost(request().generation)
        self.assertEqual(set(value), {'credits','can_submit','blocked_reason','observed_at','expires_at'})
        self.assertTrue(value['can_submit'])
        self.assertEqual((datetime.fromisoformat(value['expires_at']) - datetime.fromisoformat(value['observed_at'])).total_seconds(), 60)
        self.assertFalse(any(call[:2] == ['generate','create'] for call in self.cli.calls))
        self.assertEqual(self.gateway.limits()['submitted_count'], 0)

    def test_quote_blocks_budget_and_active_submission(self):
        self.gateway.submit(request())
        self.assertEqual(self.gateway.cost(request().generation)['blocked_reason'], 'account_concurrency_exceeded')
        self.cli.cost = 3
        self.assertEqual(self.gateway.cost(request().generation)['blocked_reason'], 'account_budget_exceeded')

    def test_price_increase_cannot_create_or_reserve(self):
        self.cli.cost = 3
        with self.assertRaisesRegex(GatewayError, 'quote_changed'):
            self.gateway.submit(request(price_ceiling=2))
        self.assertEqual(self.gateway.limits()['submitted_count'], 0)
        self.assertFalse(any(call[:2] == ['generate','create'] for call in self.cli.calls))

    def test_accepted_ceiling_survives_recovery(self):
        first = self.gateway.submit(request(price_ceiling=2))
        self.cli.cost = 3
        second = self.gateway.submit(request(price_ceiling=2))
        self.assertEqual(first['remote_id'], second['remote_id'])
        self.assertEqual(sum(call[:2] == ['generate','create'] for call in self.cli.calls), 1)
