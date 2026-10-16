"""Studio-only management surface; it never accepts or returns OAuth credentials.

The runtime must pass the same Gateway to both listeners, retaining one lifetime
credential lock. This factory alone does not open a listener or start a service.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import datetime
import hmac
import json
import re
import sqlite3
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .cli import ProviderError
from .channel import generation_status
from .jobs import GatewayError, now
from .models import LimitUpdate
from .refresh import last_refresh

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class UsageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cursor: str | None = Field(default=None, pattern=r"^video_task_[0-9a-f]{32}$")
    page_size: int = Field(default=20, ge=1, le=100)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


async def read_body(request, maximum=8192):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise GatewayError("invalid_request", 422)
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > maximum:
            raise GatewayError("request_too_large", 422)
    def reject_constant(_):
        raise ValueError("non_finite_json")
    value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object,
                       parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("object_required")
    return value


class Management:
    def __init__(self, gateway):
        self.gateway = gateway
        self.check_task = None
        self.check_id = None
        self.next_check = 0.0
        self.storage_failed = False
        with gateway.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS management_observation "
                       "(id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS management_transactions "
                       "(id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL)")

    def authorization(self):
        with self.gateway.db() as db:
            row = db.execute("SELECT record FROM management_observation WHERE id=1").fetchone()
        unconnected = not any((self.gateway.cli.state / name).exists() for name in ("binding.json", "credentials.json"))
        result = json.loads(row[0]) if row else {
            "status": "unconnected" if unconnected else "unavailable", "observed_at": None, "last_success": None,
            "error_code": "AUTHORIZATION_NOT_CHECKED", "check_id": None,
        }
        result.update(binding_id=self.gateway.binding, refresh_owner="server",
                      last_refresh_at=last_refresh(self.gateway.cli.state),
                      workspace_matches=(result["status"] == "authorized"))
        # A successful account read is evidence of authorization, not proof of
        # an OAuth rotation. Do not invent a last-refresh timestamp.
        result["stale"] = (result["status"] != "authorized" or result["observed_at"] is None)
        if result["observed_at"] and result["status"] == "authorized":
            from datetime import datetime
            result["stale"] = time.time() - datetime.fromisoformat(result["observed_at"]).timestamp() > 300
        return result

    def observe(self, check_id):
        previous = self.authorization()
        observed_at = now()
        try:
            account = self.gateway.account()
            observed_at = now()
            last_success = {"balance_credits": str(Decimal(str(account["credits"]))),
                            "plan": account["plan"], "observed_at": observed_at}
            status, code = "authorized", None
        except (GatewayError, ProviderError) as error:
            last_success = previous["last_success"]
            status = "authorization_required" if error.code == "authorization_required" else "unavailable"
            # Provider diagnostics never become App-visible error text.
            code = "AUTHORIZATION_REQUIRED" if status == "authorization_required" else "AUTHORIZATION_UNAVAILABLE"
            if status == "authorization_required" and previous["status"] == "unconnected":
                status = "unconnected"
        record = {"status": status, "observed_at": now(), "last_success": last_success,
                  "error_code": code, "check_id": check_id}
        with self.gateway.db() as db:
            db.execute("INSERT INTO management_observation VALUES(1,?) "
                       "ON CONFLICT(id) DO UPDATE SET record=excluded.record", (json.dumps(record),))
        self.observe_transactions(allowed=(status == "authorized"))

    def transaction_snapshot(self):
        with self.gateway.db() as db:
            row = db.execute("SELECT record FROM management_transactions WHERE id=1").fetchone()
        result = json.loads(row[0]) if row else {"status": "not_collected", "observed_at": None,
                                                "last_success": None}
        result["stale"] = True
        if result["status"] == "observed" and result["last_success"]:
            from datetime import datetime
            result["stale"] = time.time() - datetime.fromisoformat(result["last_success"]["observed_at"]).timestamp() > 300
        return result

    def observe_transactions(self, *, allowed):
        previous = self.transaction_snapshot()
        last_success, status = previous["last_success"], "unavailable"
        if allowed:
            try:
                items = self.gateway.transactions()
                last_success = {"observed_at": now(), "items": items, "window_size": 20}
                status = "observed"
            except (GatewayError, ProviderError):
                pass
        record = {"status": status, "observed_at": now(), "last_success": last_success}
        with self.gateway.db() as db:
            db.execute("INSERT INTO management_transactions VALUES(1,?) "
                       "ON CONFLICT(id) DO UPDATE SET record=excluded.record", (json.dumps(record),))

    async def check(self):
        if self.check_task is not None and not self.check_task.done():
            return {"check_id": self.check_id, "status": "pending"}
        if time.monotonic() < self.next_check:
            raise GatewayError("authorization_check_throttled", 429)
        self.check_id = str(uuid.uuid4())
        self.next_check = time.monotonic() + 15
        self.check_task = asyncio.create_task(self._run_check(self.check_id))
        return {"check_id": self.check_id, "status": "pending"}

    async def _run_check(self, check_id):
        try:
            await run_in_threadpool(self.observe, check_id)
        except (OSError, sqlite3.Error):
            # No raw storage error or path is returned. The previous observation
            # remains valid only as a timestamped snapshot.
            self.storage_failed = True
        else:
            self.storage_failed = False

    def usage(self, query):
        with self.gateway.db() as db:
            rows = db.execute("SELECT id,record FROM jobs WHERE id>? ORDER BY id LIMIT ?",
                              (query.cursor or "", query.page_size + 1)).fetchall()
        page = rows[:query.page_size]
        items = []
        for _, raw in page:
            job = json.loads(raw)
            items.append({"request_id": job["request_id"], "model": job["generation"]["model"],
                          "status": job["status"], "stage": job["stage"], "created_at": job["created_at"],
                          "quoted_credits": str(Decimal(str(job["quoted_credits"]))),
                          "provider_charge": None, "reconciliation_status": "unreconciled"})
        snapshot = self.transaction_snapshot()
        return {"items": items, "next_cursor": page[-1][0] if len(rows) > query.page_size else None,
                "provider_transactions_status": snapshot["status"], "provider_transactions": snapshot}

    def calls(self, query):
        if set(query) - {'search', 'state', 'offset', 'limit', 'media', 'account_id', 'created_from', 'created_before'}:
            raise ValueError('invalid_filter')
        offset, limit = query.get('offset', 0), query.get('limit', 50)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_pagination')
        for key in ('search', 'state', 'media', 'account_id'):
            if key in query and (not isinstance(query[key], str) or len(query[key]) > 200):
                raise ValueError('invalid_filter')
        dates = {}
        for key in ('created_from', 'created_before'):
            if key in query:
                if not isinstance(query[key], str) or len(query[key]) > 80:
                    raise ValueError('invalid_date')
                dates[key] = datetime.fromisoformat(query[key])
                if dates[key].tzinfo is None:
                    raise ValueError('invalid_date')
        account = self.gateway.channel_account.get()
        with self.gateway.db() as db:
            jobs = [json.loads(row[0]) for row in db.execute('SELECT record FROM jobs')]
        items = []
        for job in jobs:
            state = 'acceptance_unknown' if job['stage'] == 'submission_unknown' else job['status']
            if (query.get('account_id') and query['account_id'] != self.gateway.binding
                    or query.get('media', 'video') not in {'', 'video'}
                    or query.get('state') and query['state'] != state):
                continue
            created = datetime.fromisoformat(job['created_at'])
            if (dates.get('created_from') and created < dates['created_from']
                    or dates.get('created_before') and created >= dates['created_before']):
                continue
            model = job['generation']['model']
            if query.get('search', '') not in job['request_id'] + model + account['name']:
                continue
            items.append({'task_id': job['request_id'], 'account_id': self.gateway.binding,
                'account_name': account['name'], 'account_status': account['status'],
                'model_id': model, 'media': 'video', 'operation': 'generate', 'state': state,
                'created_at': job['created_at'], 'duration_seconds': None,
                'budget': {'amount': str(Decimal(str(job['quoted_credits']))), 'unit': 'Credits', 'source': 'provider_quote'},
                'settlement': {'amount': None, 'unit': 'Credits', 'source': 'unknown'}})
        items.sort(key=lambda row: (row['created_at'], row['task_id']), reverse=True)
        total = sum((Decimal(row['budget']['amount']) for row in items), Decimal(0))
        return {'items': items[offset:offset + limit], 'total': len(items),
            'accounts': [{'account_id': self.gateway.binding, 'name': account['name'], 'status': account['status']}],
            'summary': {'task_count': len(items),
                'budget': {'amount': str(total), 'unit': 'Credits', 'unknown_count': 0},
                'settlement': {'amount': None if items else '0', 'unit': 'Credits', 'unknown_count': len(items)}}}


def create_management_app(gateway, *, token, execution_token, station_id, assets=None, video_channel=None):
    if (len(token) < 32 or len(execution_token) < 32 or
            hmac.compare_digest(token, execution_token) or not IDENTIFIER.fullmatch(station_id)):
        raise ValueError("independent_management_identity_required")
    management = Management(gateway)

    @asynccontextmanager
    async def lifespan(app):
        yield
        # The shared runtime must drain these calls before releasing the one
        # credential lifetime lock. Cancellation cannot stop an active CLI call.
        if management.check_task:
            await asyncio.shield(management.check_task)

    async def handle(request):
        def reply(body, status=200):
            return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
        auth = request.headers.getlist("authorization")
        if len(auth) != 1 or not hmac.compare_digest(auth[0], "Bearer " + token):
            return reply({"code": "MANAGEMENT_UNAUTHORIZED"}, 401)
        if request.url.query or b"%" in request.scope.get("raw_path", b""):
            return reply({"code": "INVALID_REQUEST"}, 422)
        # Studio loads immutable public code with its service identity. Business
        # reads below additionally require the verified end-user binding.
        if request.url.path.startswith("/internal/"):
            if assets is None:
                return reply({"code": "DASHBOARD_UNAVAILABLE"}, 503)
            try:
                if request.url.path == "/internal/dashboard-declaration":
                    data, mime = assets.declaration, "application/json"
                elif request.url.path == "/internal/dashboard-index":
                    data, mime = assets.index, "application/json"
                else:
                    data, mime = await run_in_threadpool(assets.asset, request.path_params["asset"])
                return Response(data, headers={"Content-Type": mime, "Cache-Control": "no-store",
                                               "X-Content-Type-Options": "nosniff"})
            except KeyError:
                return reply({"code": "NOT_FOUND"}, 404)
            except (OSError, ValueError):
                return reply({"code": "DASHBOARD_ASSET_UNAVAILABLE"}, 503)
        actor = {}
        for header, field in (("x-user-id", "user_id"), ("x-organization-id", "organization_id"),
                              ("x-station-id", "station_id")):
            values = request.headers.getlist(header)
            if len(values) != 1 or not IDENTIFIER.fullmatch(values[0]):
                return reply({"code": "MANAGEMENT_DENIED"}, 403)
            actor[field] = values[0]
        if actor["organization_id"] != gateway.organization or actor["station_id"] != station_id:
            return reply({"code": "MANAGEMENT_DENIED"}, 403)
        try:
            path = request.url.path
            if path == '/calls/search':
                result = await run_in_threadpool(management.calls, await read_body(request, 32768))
            elif path == '/accounts/search':
                query = await read_body(request, 32768)
                if set(query) - {'search', 'status', 'offset', 'limit'}:
                    raise ValueError('invalid_filter')
                if (type(query.get('offset', 0)) is not int or query.get('offset', 0) < 0
                        or type(query.get('limit', 50)) is not int or not 1 <= query.get('limit', 50) <= 100
                        or not isinstance(query.get('search', ''), str)
                        or query.get('status', '') not in {'', 'active', 'disabled', 'archived'}):
                    raise ValueError('invalid_filter')
                account = await run_in_threadpool(gateway.channel_account.get)
                account['provider_observation'] = (await run_in_threadpool(management.authorization))['last_success']
                matches = ((account['status'] != 'archived' if not query.get('status') else account['status'] == query['status'])
                           and query.get('search', '') in account['name'])
                result = {'items': [account] if matches and query.get('offset', 0) == 0 else [], 'total': int(matches)}
            elif path.startswith('/accounts/'):
                if request.path_params['account_id'] != gateway.binding:
                    raise GatewayError('account_not_found', 404)
                if request.method == 'GET':
                    result = await run_in_threadpool(gateway.channel_account.get)
                else:
                    result = await run_in_threadpool(gateway.channel_account.update, await read_body(request, 32768), actor,
                                                    archive=request.method == 'DELETE')
            elif path == "/limits":
                if request.method == "PUT":
                    value = LimitUpdate.model_validate(await read_body(request))
                    result = await run_in_threadpool(gateway.update_limits, value, **actor)
                else:
                    result = await run_in_threadpool(gateway.limits)
            elif path == "/authorization/check":
                if await read_body(request) != {}:
                    raise ValueError("empty_request_required")
                return reply(await management.check(), 202)
            elif path == "/usage/query":
                query = UsageQuery.model_validate(await read_body(request))
                result = await run_in_threadpool(management.usage, query)
            else:
                result = await run_in_threadpool(management.authorization)
                if getattr(management, "storage_failed", False):
                    result.update(status="unavailable", stale=True, error_code="STORAGE_UNAVAILABLE")
                if management.check_task is not None and not management.check_task.done():
                    result["pending_check_id"] = management.check_id
                if path == "/overview":
                    limits = await run_in_threadpool(gateway.limits)
                    channel = (await run_in_threadpool(video_channel.read) if video_channel is not None else
                               {"status": "not_checked", "observed_at": None, "last_success": None, "stale": True})
                    result = {"authorization": result, "limits": limits,
                              "video_channel_status": channel["status"], "video_channel": channel,
                              "generation_status": generation_status(result, limits, channel, max_concurrent=gateway.max_concurrent)}
            return reply(result)
        except GatewayError as error:
            return reply({"code": error.code.upper()}, error.status)
        except (ValidationError, ValueError, TypeError, RecursionError):
            return reply({"code": "INVALID_REQUEST"}, 422)
        except (OSError, sqlite3.Error):
            return reply({"code": "STORAGE_UNAVAILABLE"}, 503)

    app = Starlette(routes=[Route("/overview", handle), Route("/authorization", handle),
        Route('/accounts/search', handle, methods=['POST']),
        Route('/accounts/{account_id}', handle, methods=['GET', 'PATCH', 'DELETE']),
        Route('/calls/search', handle, methods=['POST']),
        Route("/authorization/check", handle, methods=["POST"]),
        Route("/usage/query", handle, methods=["POST"]), Route("/limits", handle, methods=["GET", "PUT"]),
        Route("/internal/dashboard-declaration", handle), Route("/internal/dashboard-index", handle),
        Route("/internal/dashboard/{asset:path}", handle)],
        lifespan=lifespan)
    app.state.management = management
    return app
