import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PIL import Image
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from higgsfield_gateway.inputs import Inputs
from higgsfield_gateway.models import Generation, JobRequest, ReferenceMedia
from higgsfield_gateway.jobs import GatewayError
from higgsfield_gateway.server import create_app
import test_gateway as helpers
from test_gateway import PROJECT, REMOTE, request


class ReferenceTests(unittest.TestCase):
    setUp = helpers.GatewayTest.setUp
    tearDown = helpers.GatewayTest.tearDown
    make_gateway = helpers.GatewayTest.make_gateway
    creates = helpers.GatewayTest.creates

    def test_draft_mixed_references_preserve_provider_mode_and_recovery(self):
        _, image = self.image("red")
        video = ReferenceMedia(project_id=PROJECT, sha256="a" * 64, size=100, kind="video", format="mp4")
        gen = Generation(model="seedance_2_5_draft", prompt="dance", duration_seconds=4,
                         resolution="480p", mode="reference_to_video", references=[image, video])
        quote = gen.arguments("cost", media_ids=[REMOTE, REMOTE])
        create = gen.arguments("create", media_ids=[REMOTE, REMOTE])
        self.assertEqual(quote[2:], create[2:])
        self.assertEqual(create[2], "seedance_2_5")
        self.assertEqual(create[create.index("--mode") + 1], "omni_reference")
        self.assertEqual(create[create.index("--draft") + 1], "true")
        self.assertEqual(create[-4:], ["--image-references", REMOTE, "--video-references", REMOTE])
        for flag in ("--start-image", "--end-image", "t2v"):
            self.assertNotIn(flag, create)
        with patch.object(self.gateway.inputs, "upload", return_value=REMOTE):
            task = request(generation=gen.model_dump())
            self.gateway.submit(task)
            restored = self.make_gateway()
            restored.submit(task)
        self.assertEqual(self.creates(), 1)
        original = self.cli.run
        def run(args, timeout=60):
            if args[:2] == ["generate", "get"]:
                return {"id": REMOTE, "job_type": "seedance_2_5", "status": "running"}
            return original(args, timeout)
        self.cli.run = run
        self.assertEqual(restored.status(task.request_id)["stage"], "remote_pending")
        for changes in ({"mode": "text_to_video", "references": []}, {"resolution": "720p"},
                        {"duration_seconds": 3}, {"duration_seconds": 16}, {"references": [image] * 5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Generation.model_validate({**gen.model_dump(), **changes})

    def image(self, color):
        output = io.BytesIO()
        Image.new("RGB", (32, 48), color).save(output, format="PNG")
        data = output.getvalue()
        ref = ReferenceMedia(project_id=PROJECT, sha256=hashlib.sha256(data).hexdigest(),
                             size=len(data), kind="image", format="png")
        return data, ref

    def put(self, client, data, ref):
        return client.put(f"/v1/inputs/{ref.project_id}/{ref.sha256}/{ref.format}", content=data,
            headers={"Authorization": "Bearer " + "t" * 40, "X-Media-Kind": ref.kind})

    def test_stream_integrity_auth_and_retry(self):
        data, ref = self.image("red")
        with TestClient(create_app(self.gateway, "t" * 40)) as client:
            path = f"/v1/inputs/{ref.project_id}/{ref.sha256}/png"
            self.assertEqual(client.put(path, content=data).status_code, 401)
            self.assertEqual(self.put(client, data[:-1] + b"x", ref).status_code, 400)
            self.assertFalse(self.gateway.inputs.path(ref).exists())
            for _ in range(2):
                self.assertEqual(self.put(client, data, ref).json(), {"status": "committed", **ref.model_dump()})
        self.assertEqual(list(self.gateway.inputs.path(ref).parent.glob(".input-*")), [])

    def test_type_and_project_boundaries(self):
        _, ref = self.image("red")
        generation = Generation(model="wan3_0", prompt="dance", duration_seconds=15, resolution="480p",
                                aspect_ratio="9:16", mode="reference_to_video", references=[ref])
        bad = request().model_dump()
        bad.update(project_id="0199abcd-0000-7000-8000-000000000004", generation=generation.model_dump())
        with self.assertRaises(ValueError):
            JobRequest.model_validate(bad)
        for changes in ({"mode": "image_to_video"}, {"model": "seedance_2_5"}, {"references": [ref] * 5}):
            with self.assertRaises(ValueError):
                Generation.model_validate({**generation.model_dump(), **changes})

    def test_upload_receipt_order_and_restart_do_not_resubmit(self):
        refs = []
        with TestClient(create_app(self.gateway, "t" * 40)) as client:
            for color in ("red", "blue"):
                data, ref = self.image(color)
                self.assertEqual(self.put(client, data, ref).status_code, 200)
                refs.append(ref)
        original = self.cli.run
        uploaded = []
        def run(args, timeout=60):
            if args[:2] == ["upload", "create"]:
                uploaded.append(args[2])
                return {"id": f"11111111-1111-4111-8111-{len(uploaded):012d}"}
            return original(args, timeout)
        self.cli.run = run
        gen = Generation(model="wan3_0", prompt="dance", duration_seconds=15, resolution="480p",
                         aspect_ratio="9:16", mode="reference_to_video", references=refs)
        self.gateway.cost(gen)
        self.assertEqual(len(uploaded), 2)
        task = request(generation=gen.model_dump())
        self.gateway.submit(task)
        restored = self.make_gateway()
        restored.submit(task)
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(self.creates(), 1)
        create = next(c for c in self.cli.calls if c[:2] == ["generate", "create"])
        self.assertEqual(create[-4:], ["--image-references", "11111111-1111-4111-8111-000000000001",
                                      "--image-references", "11111111-1111-4111-8111-000000000002"])
        receipt = self.gateway.inputs.path(refs[0]).with_suffix(".png.upload.json")
        data = json.loads(receipt.read_text()); data["workspace"] = "another"
        receipt.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.gateway.cost(gen)

    def test_zero_quote_and_invalid_quotes(self):
        self.cli.cost = 0
        self.assertEqual(self.gateway.submit(request())["quoted_credits"], 0)
        for value in (-1, True, "0", float("nan"), float("inf"), 71):
            self.cli.cost = value
            with self.assertRaises(GatewayError):
                self.gateway.cost(request().generation)

    def test_changed_quote_blocks_create_and_free_still_obeys_submission_limit(self):
        self.gateway.max_job_credits = 30
        self.cli.cost = 15
        self.assertEqual(self.gateway.cost(request().generation)["credits"], 15)
        self.cli.cost = 31
        with self.assertRaises(GatewayError):
            self.gateway.submit(request())
        self.assertEqual(self.creates(), 0)
        self.cli.cost = 0
        self.gateway.submit(request(1))
        self.gateway.submit(request(2))
        with self.assertRaises(GatewayError):
            self.gateway.submit(request(3))
        self.assertEqual(self.creates(), 2)

    def test_video_flag_and_legacy_digest_compatibility(self):
        _, image = self.image("red")
        video = ReferenceMedia(project_id=PROJECT, sha256="a" * 64, size=100, kind="video", format="mp4")
        gen = Generation(model="wan3_0", prompt="dance", duration_seconds=15, mode="reference_to_video", references=[image, video])
        args = gen.arguments("create", media_ids=[REMOTE, REMOTE])
        self.assertEqual(args[-4:], ["--image-references", REMOTE, "--video-references", REMOTE])
        self.gateway.submit(request())
        legacy = request().model_dump()
        legacy.pop("price_ceiling")  # This field did not exist in the old schema.
        legacy["generation"].pop("references")
        expected = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        with self.gateway.db() as db:
            self.assertEqual(db.execute("SELECT digest FROM jobs").fetchone()[0], expected)
