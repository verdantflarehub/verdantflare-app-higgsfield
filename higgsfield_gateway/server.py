from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import hmac
import json
import logging
import os
from pathlib import Path

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import __version__
from .cli import CLI, ProviderError
from .credentials import lifetime_lock
from .jobs import Gateway, GatewayError
from .media import Media
from .models import Generation, JobRequest, ReferenceMedia

logging.getLogger("httpx").setLevel(logging.WARNING)


@contextmanager
def gateway_owner():
    """One credential owner for all listeners and their background checks."""
    os.umask(0o077)
    root = Path(os.environ.get("HIGGSFIELD_STATE_DIR", "/data/higgsfield"))
    with lifetime_lock(root):
        cli = CLI(Path(os.environ.get("HIGGSFIELD_CLI", "/usr/local/bin/hf")), root / "auth")
        yield Gateway(root, cli,
            binding=os.environ["HIGGSFIELD_BINDING_ID"], organization=os.environ["HIGGSFIELD_ORGANIZATION_ID"],
            workspace=os.environ["HIGGSFIELD_WORKSPACE_ID"],
            credit_limit=int(os.getenv("HIGGSFIELD_CREDIT_LIMIT", "0")),
            submission_limit=int(os.getenv("HIGGSFIELD_SUBMISSION_LIMIT", "0")),
            max_concurrent=int(os.getenv("HIGGSFIELD_MAX_CONCURRENT", "1")),
            max_job_credits=int(os.getenv("HIGGSFIELD_MAX_JOB_CREDITS", "70")),
            media=Media(root / "media"))


def create_app(gateway=None, token=None):
    @asynccontextmanager
    async def lifespan(app):
        if gateway is not None:
            yield
            return
        with gateway_owner() as owned:
            app.state.gateway = owned
            yield

    async def health(request):
        return JSONResponse({"service": "higgsfield-gateway", "version": __version__, "status": "ok"})

    async def handle(request: Request):
        secret = token if token is not None else os.getenv("HIGGSFIELD_INTERNAL_TOKEN", "")
        values = request.headers.getlist("authorization")
        if len(secret) < 32 or len(values) != 1 or not hmac.compare_digest(values[0], "Bearer " + secret):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        g = request.app.state.gateway
        try:
            if request.method == "PUT":
                reference = ReferenceMedia.model_validate({
                    "project_id": request.path_params["project"], "sha256": request.path_params["digest"],
                    "format": request.path_params["format"], "kind": request.headers.get("X-Media-Kind"),
                    "size": int(request.headers.get("Content-Length", "0"))})
                result = await g.inputs.receive(reference, request.stream())
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            data = None
            if request.method == "POST":
                content = bytearray()
                async for chunk in request.stream():
                    content.extend(chunk)
                    if len(content) > 15 * 1024 * 1024:
                        raise GatewayError("request_too_large", 413)
                def unique(pairs):
                    result = {}
                    for key, val in pairs:
                        if key in result:
                            raise ValueError("duplicate_key")
                        result[key] = val
                    return result
                data = json.loads(content, object_pairs_hook=unique)
            if request.url.path == "/v1/account":
                result = await run_in_threadpool(g.account)
            elif request.url.path == "/v1/models":
                result = g.capabilities()
            elif request.url.path == "/v1/cost":
                result = await run_in_threadpool(g.cost, Generation.model_validate(data))
            elif request.url.path == "/v1/jobs":
                result = await run_in_threadpool(g.submit, JobRequest.model_validate(data))
            elif request.url.path.endswith("/content"):
                record = await run_in_threadpool(g.load, request.path_params["identity"])
                if record["status"] != "completed":
                    raise GatewayError("result_not_ready")
                return FileResponse(g.media.path(record["request_id"]), media_type="video/mp4",
                                    headers={"X-Content-SHA256": record["media"]["sha256"], "Cache-Control": "no-store"})
            else:
                result = await run_in_threadpool(g.status, request.path_params["identity"])
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except GatewayError as error:
            return JSONResponse({"error": error.code}, status_code=error.status)
        except ProviderError as error:
            return JSONResponse({"error": error.code}, status_code=503)
        except (ValidationError, ValueError, TypeError, KeyError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        except OSError:
            return JSONResponse({"error": "storage_unavailable"}, status_code=503)

    app = Starlette(routes=[Route("/health", health),
        Route("/v1/account", handle), Route("/v1/models", handle),
        Route("/v1/inputs/{project:str}/{digest:str}/{format:str}", handle, methods=["PUT"]),
        Route("/v1/cost", handle, methods=["POST"]), Route("/v1/jobs", handle, methods=["POST"]),
        Route("/v1/jobs/{identity:str}", handle), Route("/v1/jobs/{identity:str}/content", handle)], lifespan=lifespan)
    app.state.gateway = gateway
    return app


app = create_app()
