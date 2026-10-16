import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from higgsfield_gateway import __version__
from higgsfield_gateway.dashboard import build, DashboardAssets
from higgsfield_gateway.jobs import Gateway
from higgsfield_gateway.management import create_management_app
from test_gateway import FakeCLI, ORG


SOURCE = Path(__file__).resolve().parents[1] / "dashboard"


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "dashboard"
        self.declaration = build(SOURCE, self.output, __version__)
        self.assets = DashboardAssets(self.output)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_reproducible_package_references_exact_hashed_bytes(self):
        other = self.root / "other"
        build(SOURCE, other, __version__)
        for name in [*self.assets.files, "asset-index.json", "dashboard-declaration.json"]:
            self.assertEqual((self.output / name).read_bytes(), (other / name).read_bytes())
        self.assertEqual(self.declaration["dashboard"]["artifactDigest"], "sha256:" + hashlib.sha256(self.assets.index).hexdigest())
        html = self.assets.asset("index.html")[0].decode()
        for name in self.assets.files:
            if "model." not in name and name != "index.html":
                self.assertIn("./" + name, html)
        main = next(name for name in self.assets.files if name.endswith(".mjs") and "dashboard." in name)
        model = next(name for name in self.assets.files if "model." in name)
        self.assertIn("./" + Path(model).name, self.assets.asset(main)[0].decode())

    async def test_changed_content_is_rejected_at_read_and_startup(self):
        (self.output / "index.html").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "DASHBOARD_ASSET_MISMATCH"):
            self.assets.asset("index.html")
        with self.assertRaisesRegex(ValueError, "DASHBOARD_ASSET_MISMATCH"):
            DashboardAssets(self.output)

    async def test_index_version_and_paths_are_closed(self):
        with self.assertRaises(ValueError):
            DashboardAssets(self.output, version="9.9.9")
        for name in ["../index.html", "/index.html", "assets/%2e/index.html", "dashboard-declaration.json", "index.html?token=x"]:
            with self.assertRaises(KeyError):
                self.assets.asset(name)
        index = json.loads(self.assets.index)
        index["files"]["../outside.html"] = "0" * 64
        (self.output / "asset-index.json").write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "INVALID_DASHBOARD_ARTIFACT"):
            DashboardAssets(self.output)

    async def test_internal_routes_require_service_identity_and_preserve_bytes(self):
        state = self.root / "state"
        state.mkdir()
        gateway = Gateway(state, FakeCLI(state / "auth"), binding="team", organization=ORG, workspace="workspace_test")
        token, execution = "management-" + "m" * 32, "execution-" + "e" * 32
        app = create_management_app(gateway, token=token, execution_token=execution,
                                    station_id="station-test", assets=self.assets)
        routes = {"/internal/dashboard-declaration": self.assets.declaration,
                  "/internal/dashboard-index": self.assets.index,
                  **{"/internal/dashboard/" + name: self.assets.asset(name)[0] for name in self.assets.files}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path, expected in routes.items():
                for headers in ({}, {"Authorization": "Bearer " + execution}):
                    self.assertEqual((await client.get(path, headers=headers)).status_code, 401)
                response = await client.get(path, headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, expected)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual((await client.get("/overview", headers={"Authorization": "Bearer " + token})).status_code, 403)
            self.assertEqual((await client.get("/internal/dashboard/index.html?x=y", headers={"Authorization": "Bearer " + token})).status_code, 422)
            self.assertEqual((await client.post("/internal/dashboard-index", headers={"Authorization": "Bearer " + token})).status_code, 405)


if __name__ == "__main__":
    unittest.main()
