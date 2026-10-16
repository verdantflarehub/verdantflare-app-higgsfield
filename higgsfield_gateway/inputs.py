"""Private immutable reference cache; binary streams never enter the job JSON."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from PIL import Image

from .cli import ProviderError
from .credentials import atomic_json


class Inputs:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, reference):
        folder = self.root / reference.project_id
        if folder.is_symlink():
            raise ValueError("invalid_input_path")
        folder.mkdir(exist_ok=True, mode=0o700)
        path = folder / (reference.sha256 + "." + reference.format)
        if path.is_symlink():
            raise ValueError("invalid_input_path")
        return path

    def verified_path(self, reference):
        path = self.path(reference)
        if not path.is_file() or path.stat().st_size != reference.size:
            raise ValueError("input_missing_or_invalid")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != reference.sha256:
                raise ValueError("input_integrity_failed")
        self.validate(path, reference)
        return path

    @staticmethod
    def validate(path, reference):
        if reference.kind == "image":
            with Image.open(path) as image:
                if (image.format.lower() != reference.format or getattr(image, "n_frames", 1) != 1 or
                        image.width * image.height > 16 * 1024 * 1024):
                    raise ValueError("invalid_reference_image")
                image.verify()
        else:
            # Local MP4 only: disable external protocols and bound decoder work.
            try:
                result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file",
                    "-f", "mov", "-show_entries", "stream=codec_type,codec_name,width,height:format=duration",
                    "-of", "json", str(path)], capture_output=True, timeout=30)
            except subprocess.SubprocessError:
                raise ValueError("reference_probe_failed") from None
            if result.returncode or len(result.stdout) > 65536:
                raise ValueError("invalid_reference_video")
            data = json.loads(result.stdout)
            videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
            duration = float(data["format"]["duration"])
            if (len(videos) != 1 or videos[0].get("codec_name") != "h264" or
                    not 0 < videos[0].get("width", 0) * videos[0].get("height", 0) <= 4096 * 2160 or
                    not math.isfinite(duration) or not 0 < duration <= 30):
                raise ValueError("invalid_reference_video")

    async def receive(self, reference, stream):
        from starlette.concurrency import run_in_threadpool
        target = self.path(reference)
        descriptor, temporary = tempfile.mkstemp(prefix=".input-", dir=target.parent)
        temporary = Path(temporary)
        try:
            os.chmod(temporary, 0o600)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(descriptor, "wb") as destination:
                async for chunk in stream:
                    size += len(chunk)
                    if size > reference.size:
                        raise ValueError("input_size_exceeded")
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if size != reference.size or digest.hexdigest() != reference.sha256:
                raise ValueError("input_integrity_failed")
            await run_in_threadpool(self.validate, temporary, reference)
            if target.exists():
                await run_in_threadpool(self.verified_path, reference)
            else:
                os.replace(temporary, target)
            return {"status": "committed", **reference.model_dump()}
        finally:
            temporary.unlink(missing_ok=True)

    def upload(self, reference, cli, workspace):
        path = self.verified_path(reference)
        receipt = path.with_suffix(path.suffix + ".upload.json")
        if receipt.is_symlink():
            raise ValueError("invalid_upload_receipt")
        if receipt.exists():
            data = json.loads(receipt.read_text())
            if data.get("workspace") != workspace or data.get("reference") != reference.model_dump():
                raise ValueError("upload_binding_mismatch")
            identity = data["upload_id"]
        else:
            data = cli.run(["upload", "create", str(path)], timeout=120)
            if not isinstance(data, dict):
                raise ProviderError("upload_protocol_error")
            identity = data.get("id")
            if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
                raise ProviderError("upload_protocol_error")
            atomic_json(receipt, {"workspace": workspace, "reference": reference.model_dump(), "upload_id": identity})
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
            raise ProviderError("upload_protocol_error")
        return identity
