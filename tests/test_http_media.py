import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from higgsfield_gateway.cli import ProviderError
from higgsfield_gateway.jobs import Gateway
from higgsfield_gateway.media import Media
from higgsfield_gateway.server import create_app
from test_gateway import FakeCLI, ORG, request


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cli = FakeCLI(root / "auth")
        self.gateway = Gateway(root, self.cli, binding="team", organization=ORG, workspace="workspace_test",
                               credit_limit=70, submission_limit=2)
        self.token = "test-internal-token-" + "a" * 32
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.gateway, self.token)),
                                       base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    async def test_unauthenticated_and_duplicate_credentials_rejected(self):
        for headers in ({}, {"Authorization": "Bearer wrong"},
                        [("Authorization", "Bearer " + self.token), ("Authorization", "Bearer " + self.token)]):
            response = await self.client.post("/v1/jobs", json=request().model_dump(), headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.cli.calls, [])

    async def test_malformed_and_duplicate_json_never_submit(self):
        for data in ('{"model":"x","model":"y"}', 'not json'):
            response = await self.client.post("/v1/cost", content=data, headers=self.headers())
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.cli.calls, [])

    async def test_provider_error_does_not_expose_raw_output(self):
        with patch.object(self.cli, "run", side_effect=ProviderError("provider_call_failed")):
            response = await self.client.get("/v1/account", headers=self.headers())
        self.assertEqual(response.json(), {"error": "provider_call_failed"})
        self.assertEqual(response.status_code, 503)

    async def test_idempotent_api_round_trip_returns_no_prompt_or_member_identity(self):
        response = await self.client.post("/v1/jobs", json=request().model_dump(), headers=self.headers())
        self.assertEqual(response.status_code, 200)
        value = response.json()
        for key in ("generation", "user_id", "organization_id", "project_id", "credentials"):
            self.assertNotIn(key, value)
        second = await self.client.post("/v1/jobs", json=request().model_dump(), headers=self.headers())
        self.assertEqual(second.json(), value)
        count = sum(c[:2] == ["generate", "create"] for c in self.cli.calls)
        self.assertEqual(count, 1)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        def fetch(request):
            self.calls.append(request)
            return httpx.Response(200, content=b"synthetic-mp4", headers={"content-type": "video/mp4"})
        self.client = httpx.Client(transport=httpx.MockTransport(fetch))
        self.media = Media(Path(self.temp.name), client=self.client, hosts={"media.example.invalid"})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_arbitrary_and_internal_urls_are_rejected_without_http(self):
        for url in ("http://media.example.invalid/x", "https://other.invalid/x", "https://user:pass@media.example.invalid/x",
                    "https://127.0.0.1/x", "https://media.example.invalid:8443/x"):
            with self.assertRaises(ProviderError):
                self.media.validate_url(url)
        with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("10.0.0.1", 443))]):
            with self.assertRaises(ProviderError):
                self.media.validate_url("https://media.example.invalid/x")
        self.assertEqual(self.calls, [])

    def test_result_collection_reuses_verified_file_and_detects_tampering(self):
        identity = request().request_id
        with patch.object(self.media, "validate_url"), patch.object(self.media, "probe", return_value={"width": 1280}):
            one = self.media.collect(identity, "https://media.example.invalid/x", {})
            two = self.media.collect(identity, "https://media.example.invalid/x", {})
            self.assertEqual(one, two)
            self.assertEqual(len(self.calls), 1)
            self.media.path(identity).write_bytes(b"tampered")
            with self.assertRaisesRegex(ProviderError, "media_integrity_failed"):
                self.media.collect(identity, "https://media.example.invalid/x", {})
        self.assertEqual(len(self.calls), 1)

    def test_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ProviderError, "invalid_job_id"):
            self.media.path("../credentials")

    def test_grok_encoder_alignment_retains_strict_ratio_boundary(self):
        generation = {"model": "grok_video_v15_lite", "duration_seconds": 2,
                      "resolution": "480p", "aspect_ratio": "16:9"}
        def probe(width, model="grok_video_v15_lite"):
            data = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": width,
                     "height": 480, "avg_frame_rate": "24/1"}], "format": {"duration": "2.042"}}
            with patch("subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(data))):
                return Media.probe(Path("synthetic.mp4"), {**generation, "model": model})
        self.assertEqual(probe(848)["width"], 848)
        self.assertEqual(probe(854)["width"], 854)
        for width, model in [(832, "grok_video_v15_lite"), (848, "seedance_2_5"), (847, "grok_video_v15_lite")]:
            with self.assertRaisesRegex(ProviderError, "media_contract_mismatch"):
                probe(width, model)


if __name__ == "__main__":
    unittest.main()
