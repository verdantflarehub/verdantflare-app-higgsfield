import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_gateway import FakeCLI, ORG
from higgsfield_gateway.cli import ProviderError
from higgsfield_gateway.jobs import Gateway


class TransactionPagesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cli = FakeCLI(root / "auth")
        self.gateway = Gateway(root, self.cli, binding="team", organization=ORG, workspace="workspace_test")

    def tearDown(self):
        self.temp.cleanup()

    def read(self, payload, **kwargs):
        original = self.cli.run
        def reply(args, timeout=60):
            return payload if args[:2] == ["account", "transactions"] else original(args, timeout)
        with patch.object(self.cli, "run", side_effect=reply):
            return self.gateway.transaction_page(**kwargs)

    def test_short_empty_and_null_cursor_pages_do_not_invent_end_of_history(self):
        for payload in ({"items": []}, {"items": [], "next_cursor": None}, {"items": [], "next_cursor": ""},
                        {"items": [], "cursor": None}, {"items": [], "cursor": ""},
                        {"items": [], "cursor": None, "next_cursor": ""}):
            with self.subTest(payload=payload):
                page = self.read(payload)
                self.assertEqual(page["items"], [])
                self.assertEqual(page["pagination"], {"status": "unknown", "next_cursor": None, "has_more": None})

    def test_explicit_cursor_is_preserved_without_inventing_other_pagination_facts(self):
        for paging in ({"cursor": "opaque+/page=="}, {"next_cursor": "opaque+/page=="},
                       {"cursor": "opaque+/page==", "next_cursor": "opaque+/page=="}):
            page = self.read({"items": [], **paging})
            self.assertEqual(page["pagination"], {"status": "known", "next_cursor": "opaque+/page==", "has_more": None})
        page = self.read({"items": [], "has_more": False})
        self.assertEqual(page["pagination"], {"status": "known", "next_cursor": None, "has_more": False})

    def test_observed_provider_schema_preserves_candidate_cursor_without_exposing_display_name(self):
        page = self.read({"cursor": "opaque/second-page=", "items": [
            {"action": "spend", "created_at": "2026-10-28T09:00:00+08:00", "credits": -10,
             "display_name": "private prompt or unverified model label"}]})
        self.assertEqual(page["pagination"]["next_cursor"], "opaque/second-page=")
        self.assertIsNone(page["pagination"]["has_more"])
        self.assertEqual(page["items"][0]["direction"], "debit")
        self.assertEqual(page["items"][0]["credits"], "-10")
        self.assertNotIn("private prompt or unverified model label", str(page))
        self.assertIn({"name": "display_name", "type": "string"}, page["schema"]["item_fields"])

    def test_candidate_cursor_is_forwarded_unchanged_using_verified_cli_option(self):
        original = self.cli.run
        def reply(args, timeout=60):
            return {"items": [], "cursor": None} if args[:2] == ["account", "transactions"] else original(args, timeout)
        with patch.object(self.cli, "run", side_effect=reply) as run:
            page = self.gateway.transaction_page(cursor="opaque+/second==", page_size=7)
        run.assert_any_call(["account", "transactions", "--size", "7", "--cursor", "opaque+/second=="])
        self.assertEqual(page["coverage"], {"kind": "provider_page", "page_size": 7})
        self.assertEqual(page["pagination"]["status"], "unknown")

    def test_malformed_or_contradictory_provider_pagination_is_not_a_valid_page(self):
        for paging in ({"next_cursor": 1}, {"next_cursor": "bad\nvalue"}, {"next_cursor": "\ud800"},
                       {"next_cursor": "x" * 2049}, {"has_more": "false"}, {"has_more": 0},
                       {"has_more": True, "next_cursor": None}, {"has_more": False, "next_cursor": "next"},
                       {"cursor": 1}, {"cursor": "bad\nvalue"}, {"cursor": "\ud800"}, {"cursor": "x" * 2049},
                       {"cursor": "actual", "next_cursor": "different"},
                       {"cursor": None, "next_cursor": "legacy"}, {"cursor": "actual", "next_cursor": None},
                       {"has_more": True, "cursor": None}, {"has_more": False, "cursor": "next"}):
            with self.subTest(keys=list(paging)):
                with self.assertRaisesRegex(ProviderError, "provider_protocol_error"):
                    self.read({"items": [], **paging})

    def test_unknown_direction_and_schema_diagnostics_never_publish_raw_fields(self):
        page = self.read({"items": [{"action": "new_unknown_action", "created_at": "2026-10-28T09:00:00+08:00",
                                    "credits": -3, "prompt": "private prompt", "email": "private@example.invalid",
                                    "private@example.invalid": "account", "https://private.invalid": "link",
                                    "request_id": "unverified-provider-link"}],
                          "secret_token": "private secret", "arbitrary": {"private": "value"}})
        row = page["items"][0]
        self.assertEqual(row["action"], "other")
        self.assertEqual(row["direction"], "unknown")
        self.assertEqual(row["credits"], "-3")
        self.assertEqual(row["created_at"], "2026-10-28T01:00:00+00:00")
        self.assertNotIn("request_id", row)
        for value in ("private prompt", "private@example.invalid", "private.invalid", "private secret",
                      "unverified-provider-link"):
            self.assertNotIn(value, str(page))
        self.assertIn({"name": "request_id", "type": "string"}, page["schema"]["item_fields"])

    def test_schema_diagnostics_are_bounded_and_provider_page_size_is_enforced(self):
        page = self.read({"items": [], **{f"field_{i}": "unpublished" for i in range(80)}})
        self.assertLessEqual(len(page["schema"]["fields"]), 32)
        with self.assertRaisesRegex(ProviderError, "provider_protocol_error"):
            self.read({"items": [{}] * 3}, page_size=2)


if __name__ == "__main__":
    unittest.main()
