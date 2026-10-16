from __future__ import annotations

from fractions import Fraction
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from urllib.parse import urlsplit

import httpx

from .cli import DMIT_PROXY, ProviderError
from .credentials import atomic_json


class Media:
    def __init__(self, root: Path, client=None, hosts=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = client or httpx.Client(proxy=DMIT_PROXY, trust_env=False, follow_redirects=False,
                                             timeout=httpx.Timeout(120, connect=15))
        self.hosts = frozenset(hosts or {"d8j0ntlcm91z4.cloudfront.net"})

    def path(self, identity):
        if not re.fullmatch(r"video_task_[0-9a-f]{32}", identity):
            raise ProviderError("invalid_job_id")
        return self.root / (identity + ".mp4")

    def validate_url(self, url):
        if not isinstance(url, str):
            raise ProviderError("result_url_denied")
        p = urlsplit(url)
        if (p.scheme != "https" or p.hostname not in self.hosts or p.username or p.password or
                p.fragment or p.port not in {None, 443}):
            raise ProviderError("result_url_denied")
        try:
            addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
                raise ValueError
        except (OSError, ValueError):
            raise ProviderError("result_url_denied") from None

    def collect(self, identity, url, generation):
        path = self.path(identity)
        metadata = path.with_suffix(".json")
        if path.exists() and metadata.exists():
            result = json.loads(metadata.read_text())
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != result["sha256"]:
                    raise ProviderError("media_integrity_failed")
            return result
        self.validate_url(url)
        temporary = path.with_suffix(".part")
        try:
            digest, size = hashlib.sha256(), 0
            with self.client.stream("GET", url, headers={"Accept": "video/mp4"}) as response:
                response.raise_for_status()
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] not in {"video/mp4", "application/octet-stream"}:
                    raise ProviderError("media_type_invalid")
                with temporary.open("wb") as stream:
                    os.chmod(temporary, 0o600)
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > 1024 * 1024 * 1024:
                            raise ProviderError("media_size_exceeded")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if not size:
                raise ProviderError("media_empty")
            result = self.probe(temporary, generation)
            result.update(sha256=digest.hexdigest(), size=size, mime="video/mp4")
            os.replace(temporary, path)
            atomic_json(metadata, result)
            return result
        except (httpx.HTTPError, OSError, ValueError, subprocess.SubprocessError, KeyError, StopIteration):
            raise ProviderError("media_collection_failed") from None
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def probe(path, generation):
        p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                           check=True, capture_output=True, text=True, timeout=60)
        data = json.loads(p.stdout)
        video = next(s for s in data["streams"] if s["codec_type"] == "video" and
                     not s.get("disposition", {}).get("attached_pic"))
        fps = float(Fraction(video["avg_frame_rate"]))
        duration = float(data["format"]["duration"])
        if not 0 < fps <= 120 or video["width"] * video["height"] > 4096 * 2160:
            raise ProviderError("media_contract_mismatch")
        if abs(duration - generation["duration_seconds"]) > max(2 / fps, .5):
            raise ProviderError("media_contract_mismatch")
        ratio = Fraction(generation["aspect_ratio"].replace(":", "/"))
        ratio_error = abs(video["width"] - video["height"] * ratio)
        # Official Grok Lite 480p advertises 854x480 but encodes 848x480.
        # Accept only the observed 16-pixel grid rounding, with <=1% error.
        aligned_grok = (generation.get("model") == "grok_video_v15_lite" and
                        video["width"] % 16 == 0 and video["height"] % 16 == 0 and
                        ratio_error <= 8 and ratio_error / (video["height"] * ratio) <= .01)
        if ratio_error > 2 and not aligned_grok:
            raise ProviderError("media_contract_mismatch")
        if min(video["width"], video["height"]) < int(generation["resolution"][:-1]):
            raise ProviderError("media_contract_mismatch")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
                        "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
                       check=True, capture_output=True, timeout=180)
        return {"width": video["width"], "height": video["height"], "frame_rate": fps,
                "duration_ms": round(duration * 1000), "video_codec": video["codec_name"]}
