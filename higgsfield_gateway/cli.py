"""Allowlisted official CLI calls with a fixed proxy and bounded private output."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .refresh import snapshot, observe_success

DMIT_PROXY = "http://openclash.openclash.svc.cluster.local:1081"
CLI_VERSION = "1.1.26"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class ProviderError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CLI:
    def __init__(self, executable: Path, state: Path, proxy: str = DMIT_PROXY):
        if proxy != DMIT_PROXY:
            raise ValueError("dmit_proxy_required")
        self.executable, self.state, self.proxy = executable, state, proxy

    def environment(self):
        env = {k: v for k, v in os.environ.items()
               if not k.upper().endswith("_PROXY") and not k.startswith("HIGGSFIELD_")}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = self.proxy
        env.update(NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1",
                   HIGGSFIELD_CREDENTIALS_PATH=str(self.state / "credentials.json"),
                   HIGGSFIELD_CONFIG_PATH=str(self.state / "config.json"),
                   HIGGSFIELD_DISABLE_TELEMETRY="1", HIGGSFIELD_NO_UPDATE_CHECK="1")
        return env

    def run(self, args: list[str], timeout: int = 60):
        before = snapshot(self.state)
        result = self._run(args, timeout)
        observe_success(self.state, before)
        return result

    def _run(self, args: list[str], timeout: int = 60):
        allowed = {("account", "status"), ("account", "transactions"), ("model", "get"), ("workspace", "status"),
                   ("upload", "create"),
                   ("generate", "cost"), ("generate", "create"), ("generate", "get")}
        if tuple(args[:2]) not in allowed:
            raise ProviderError("command_denied")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # stdout/stderr can contain OAuth errors, prompt text or signed URLs.
        # Temporary files are private and are never returned on a failed call.
        with tempfile.TemporaryFile(dir=self.state) as out, tempfile.TemporaryFile(dir=self.state) as err:
            try:
                process = subprocess.Popen([str(self.executable), *args, "--json", "--no-color"],
                                           env=self.environment(), stdout=out, stderr=err,
                                           stdin=subprocess.DEVNULL, shell=False)
            except OSError:
                raise ProviderError("cli_unavailable") from None
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise ProviderError("provider_timeout")
                    if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT_BYTES:
                        raise ProviderError("provider_output_exceeded")
                    time.sleep(0.05)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            # Fast-exiting commands must obey the same bound as running ones.
            if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT_BYTES:
                raise ProviderError("provider_output_exceeded")
            if process.returncode:
                err.seek(0)
                diagnostic = err.read(MAX_OUTPUT_BYTES)
                # Fixed CLI 1.1.26's observed login-required diagnostic. Neither
                # exit code 2 alone nor arbitrary provider text proves auth loss.
                if process.returncode == 2 and b"Hint: Run: hf auth login" in diagnostic.splitlines():
                    raise ProviderError("authorization_required")
                raise ProviderError("provider_call_failed")
            out.seek(0)
            data = out.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ProviderError("provider_output_exceeded")
            # CLI 1.1.26 ignores --json for an unset workspace. This is the
            # official personal subscription context, not a failed login.
            if args == ["workspace", "status"] and data.strip() == b"No workspace selected.":
                return {"context": "personal", "workspace_id": None}
            try:
                return json.loads(data)
            except (ValueError, UnicodeDecodeError):
                raise ProviderError("provider_protocol_error") from None
