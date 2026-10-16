import hashlib
import unittest
from unittest.mock import patch

import httpx

from higgsfield_gateway.channel import VideoChannel, VIDEO_HEALTH, generation_status


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.data = {"status": "ok", "channels": {"higgsfield": {"enabled": True, "configured": True,
            "binding_digest": hashlib.sha256(b'["team","org"]').hexdigest()}}}
        self.response = lambda: httpx.Response(200, json=self.data)
        def respond(request):
            self.calls.append(request)
            self.assertEqual(str(request.url), VIDEO_HEALTH)
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.method, "GET")
            return self.response()
        self.client = httpx.Client(transport=httpx.MockTransport(respond))
        self.observer = VideoChannel("team", "org", client=self.client)

    def tearDown(self):
        self.observer.close()

    def read(self):
        self.observer.next_read = 0
        return self.observer.read()

    def test_real_switches_and_cache_without_provider_calls(self):
        self.assertEqual(self.read()["status"], "enabled")
        self.assertEqual(self.observer.read()["status"], "enabled")
        self.assertEqual(len(self.calls), 1)
        for key in ("enabled", "configured"):
            self.data["channels"]["higgsfield"][key] = False
            self.assertEqual(self.read()["status"], "disabled")
            self.data["channels"]["higgsfield"][key] = True

    def test_failure_retains_timestamped_old_snapshot_but_never_enables(self):
        success = self.read()["last_success"]
        for response in [httpx.Response(503), httpx.Response(200, json={}),
                         httpx.Response(200, content=b"x" * 16385),
                         httpx.Response(302, headers={"Location": "https://untrusted.invalid"})]:
            self.response = lambda: response
            state = self.read()
            self.assertEqual(state["status"], "unavailable")
            self.assertTrue(state["stale"])
            self.assertEqual(state["last_success"], success)
        self.assertEqual(len(self.calls), 5)

    def test_binding_mismatch_and_string_booleans_are_not_trusted(self):
        for key, value in [("binding_digest", "0" * 64), ("enabled", "true"), ("configured", 1)]:
            old = self.data["channels"]["higgsfield"][key]
            self.data["channels"]["higgsfield"][key] = value
            self.assertEqual(self.read()["status"], "unavailable")
            self.data["channels"]["higgsfield"][key] = old

    def test_transport_is_fixed_and_does_not_inherit_proxy_or_follow_redirects(self):
        with patch("higgsfield_gateway.channel.httpx.Client") as factory:
            VideoChannel("team", "org")
        factory.assert_called_once_with(trust_env=False, follow_redirects=False, timeout=3)

    def test_generation_requires_all_independent_conditions(self):
        auth = {"status": "authorized", "stale": False}
        limits = {"budget_available": True, "active_count": 0}
        channel = {"status": "enabled", "stale": False}
        def state():
            return generation_status(auth, limits, channel, max_concurrent=1)
        self.assertEqual(state(), "eligible")
        limits["budget_available"] = False
        self.assertEqual(state(), "paused")
        limits["budget_available"] = True
        channel["status"] = "disabled"
        self.assertEqual(state(), "channel_paused")
        channel.update(status="enabled", stale=True)
        self.assertEqual(state(), "not_verified")
        channel["stale"] = False
        auth["stale"] = True
        self.assertEqual(state(), "authorization_unverified")
        auth["stale"] = False
        limits["active_count"] = 1
        self.assertEqual(state(), "busy")


if __name__ == "__main__":
    unittest.main()
