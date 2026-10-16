"""Durable single-account journal. Ambiguous create responses are never retried."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from PIL import Image

from .cli import ProviderError
from .models import Generation, JobRequest, LimitUpdate, MODELS, provider_model
from .inputs import Inputs


class GatewayError(RuntimeError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(UTC).isoformat()


class Gateway:
    def __init__(self, root: Path, cli, *, binding: str, organization: str,
                 workspace: str, credit_limit: int = 0, submission_limit: int = 0,
                 max_concurrent: int = 1, max_job_credits: int = 70, media=None):
        if min(credit_limit, submission_limit, max_job_credits) < 0 or max_concurrent < 1:
            raise ValueError("invalid_limits")
        if not all((binding, organization, workspace)):
            raise ValueError("account_binding_required")
        self.root, self.cli = root, cli
        self.binding, self.organization, self.workspace = binding, organization, workspace
        initial = LimitUpdate(binding_id=binding, revision=1, credit_limit=str(credit_limit),
                              submission_limit=submission_limit, operation_id="initial-limits")
        self.max_concurrent, self.max_job_credits = max_concurrent, max_job_credits
        self.media = media
        self._lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.inputs = Inputs(root / "inputs")
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            identity = json.dumps([binding, organization, workspace])
            old = db.execute("SELECT value FROM binding WHERE id=1").fetchone()
            if old and old[0] != identity:
                raise GatewayError("account_binding_changed")
            db.execute("INSERT OR IGNORE INTO binding VALUES(1, ?)", (identity,))
            db.execute("CREATE TABLE IF NOT EXISTS limits (id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS limit_operations "
                       "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, record TEXT NOT NULL)")
            # Environment limits initialize new journals only; a restart must
            # never discard a management update or reset existing reservations.
            initial_record = initial.model_dump(exclude={"operation_id"})
            initial_record.update(updated_at=now(), updated_by=None)
            db.execute("INSERT OR IGNORE INTO limits VALUES(1, ?)", (json.dumps(initial_record),))
            # Any process death after durable intent may already have spent credits.
            for row in db.execute("SELECT record FROM jobs").fetchall():
                record = json.loads(row[0])
                if record["stage"] == "submitting":
                    record.update(stage="submission_unknown", error="submission_unknown", updated_at=now())
                    self.save(db, record)
        os.chmod(root / "jobs.sqlite3", 0o600)
        from .accounts import BoundAccount
        self.channel_account = BoundAccount(self)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "jobs.sqlite3", timeout=120)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def save(db, record):
        db.execute("UPDATE jobs SET record=? WHERE id=?", (json.dumps(record), record["request_id"]))

    def load(self, request_id):
        with self.db() as db:
            row = db.execute("SELECT record FROM jobs WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise GatewayError("job_not_found", 404)
        return json.loads(row[0])

    @staticmethod
    def _limits(db):
        row = db.execute("SELECT record FROM limits WHERE id=1").fetchone()
        if not row:
            raise GatewayError("limits_unavailable", 503)
        return json.loads(row[0])

    def limits(self):
        with self.db() as db:
            value = self._limits(db)
            jobs = [json.loads(row[0]) for row in db.execute("SELECT record FROM jobs")]
            reserved = sum((Decimal(str(job["quoted_credits"])) for job in jobs), Decimal(0))
            active = [job for job in jobs if job["status"] not in {"completed", "failed"}]
            value.update(submitted_count=len(jobs), reserved_credits=str(reserved),
                         active_count=len(active),
                         active_reserved_credits=str(sum((Decimal(str(job["quoted_credits"])) for job in active), Decimal(0))),
                         budget_available=(self.channel_account.read(db)['status'] == 'active' and
                                           Decimal(value["credit_limit"]) > 0 and len(jobs) < value["submission_limit"] and
                                           reserved <= Decimal(value["credit_limit"])))
            return value

    def update_limits(self, change: LimitUpdate, *, user_id: str, organization_id: str, station_id: str):
        # The management listener supplies these identities only after checking
        # its Studio service credential. No identity comes from the request body.
        if change.binding_id != self.binding or organization_id != self.organization:
            raise GatewayError("account_access_denied", 403)
        if not user_id or not station_id or len(user_id) > 128 or len(station_id) > 128:
            raise GatewayError("account_access_denied", 403)
        actor = {"user_id": user_id, "organization_id": organization_id, "station_id": station_id}
        payload = {"change": change.model_dump(), "actor": actor}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self._lock, self.db() as db:
            prior = db.execute("SELECT digest,record FROM limit_operations WHERE id=?", (change.operation_id,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise GatewayError("idempotency_conflict")
                return json.loads(prior[1])["after"]
            before = self._limits(db)
            if change.revision != before["revision"]:
                raise GatewayError("limits_revision_conflict")
            after = change.model_dump(exclude={"operation_id"})
            after.update(revision=before["revision"] + 1, updated_at=now(), updated_by=user_id)
            audit = {"actor": actor, "before": before, "after": after}
            db.execute("UPDATE limits SET record=? WHERE id=1", (json.dumps(after),))
            db.execute("INSERT INTO limit_operations VALUES(?,?,?)", (change.operation_id, digest, json.dumps(audit)))
            return after

    def check_binding(self):
        path = self.cli.state / "binding.json"
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
            if actual.get("workspace_id") != self.workspace:
                raise ValueError
        except (OSError, ValueError, AttributeError):
            raise GatewayError("authorization_required", 503) from None
        # Verify the CLI actually selected the bound workspace, not just our metadata.
        value = self.cli.run(["workspace", "status"])
        if self.workspace == "personal":
            if value != {"context": "personal", "workspace_id": None}:
                raise GatewayError("workspace_binding_mismatch", 503)
            return
        def matches(item):
            if not isinstance(item, dict):
                return False
            return any((key in {"id", "workspace_id", "active_workspace_id"} and val == self.workspace)
                       or (isinstance(val, dict) and matches(val)) for key, val in item.items())
        if not matches(value):
            raise GatewayError("workspace_binding_mismatch", 503)

    def account(self):
        with self._lock:
            return self._account()

    def _account(self):
        self.check_binding()
        data = self.cli.run(["account", "status"])
        if (not isinstance(data, dict) or type(data.get("credits")) not in (int, float) or
                not math.isfinite(data["credits"]) or data["credits"] < 0):
            raise ProviderError("provider_protocol_error")
        return {"credits": data["credits"], "plan": str(data.get("subscription_plan_type", "unknown")),
                "binding_id": self.binding, "authorized": True}

    def transactions(self):
        """Recent account snapshot, never an inferred task-level charge."""
        with self._lock:
            self.check_binding()
            data = self.cli.run(["account", "transactions", "--size", "20"])
            if not isinstance(data, dict) or not isinstance(data.get("items"), list) or len(data["items"]) > 20:
                raise ProviderError("provider_protocol_error")
            items = []
            for item in data["items"]:
                try:
                    if (not isinstance(item, dict) or type(item.get("credits")) not in (int, float)
                            or not math.isfinite(item["credits"]) or abs(item["credits"]) > 10**12
                            or not isinstance(item.get("created_at"), str) or len(item["created_at"]) > 64):
                        raise ValueError
                    created = datetime.fromisoformat(item["created_at"])
                    if created.tzinfo is None:
                        raise ValueError
                    items.append({"action": item.get("action") if item.get("action") in ("grant", "spend") else "other",
                                  "created_at": created.astimezone(UTC).isoformat(),
                                  "credits": str(Decimal(str(item["credits"])))})
                except (ValueError, TypeError, OverflowError):
                    raise ProviderError("provider_protocol_error") from None
            return items

    @contextmanager
    def source(self, generation):
        if generation.source is None:
            yield None
            return
        data = generation.source.content()
        try:
            with Image.open(io.BytesIO(data)) as image:
                if (image.format.lower() != generation.source.format or
                        getattr(image, "n_frames", 1) != 1 or image.width * image.height > 16 * 1024 * 1024):
                    raise ValueError
                width, height = image.size
                a, b = map(int, generation.aspect_ratio.split(":"))
                if abs(width * b - height * a) > max(a, b):
                    raise ValueError
                image.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise GatewayError("source_integrity_failed", 400) from None
        import tempfile
        with tempfile.TemporaryDirectory(prefix="input-", dir=self.root) as directory:
            path = Path(directory) / ("frame." + generation.source.format)
            path.write_bytes(data)
            os.chmod(path, 0o600)
            yield path

    def reference_ids(self, generation):
        return [self.inputs.upload(ref, self.cli, self.workspace) for ref in generation.references]

    def quote(self, generation: Generation, path=None, media_ids=None):
        data = self.cli.run(generation.arguments("cost", path, media_ids))
        if not isinstance(data, dict) or type(data.get("credits")) not in (int, float) or not 0 <= data["credits"] <= self.max_job_credits:
            raise GatewayError("cost_not_allowed")
        return data["credits"]

    def cost(self, generation):
        with self._lock:
            if self.channel_account.get()['status'] != 'active':
                raise GatewayError('account_disabled')
            account = self.account()
            with self.source(generation) as path:
                credits = self.quote(generation, path, self.reference_ids(generation))
            reason = None
            with self.db() as db:
                limits = self._limits(db)
                jobs = [json.loads(row[0]) for row in db.execute("SELECT record FROM jobs")]
                active = [job for job in jobs if job["status"] not in {"completed", "failed"}]
                if any(job["stage"] == "submission_unknown" for job in jobs):
                    reason = "account_reconciliation_required"
                elif (Decimal(limits["credit_limit"]) == 0 or len(jobs) >= limits["submission_limit"] or
                      sum((Decimal(str(job["quoted_credits"])) for job in jobs), Decimal(0)) + Decimal(str(credits)) > Decimal(limits["credit_limit"])):
                    reason = "account_budget_exceeded"
                elif len(active) >= self.max_concurrent:
                    reason = "account_concurrency_exceeded"
                elif sum(job["quoted_credits"] for job in active) + credits > account["credits"]:
                    reason = "insufficient_credits"
            observed = datetime.now(UTC)
            return {"credits": credits, "can_submit": reason is None, "blocked_reason": reason,
                    "observed_at": observed.isoformat(), "expires_at": (observed + timedelta(seconds=60)).isoformat()}

    def submit(self, request: JobRequest):
        if request.binding_id != self.binding or request.organization_id != self.organization:
            raise GatewayError("account_access_denied", 403)
        # Image bytes never enter the durable journal, only their immutable digest.
        body = request.model_dump()
        if request.price_ceiling is None:
            body.pop("price_ceiling", None)  # Preserve all existing immutable digests.
        if not request.generation.references:
            # Preserve existing durable request digests across this schema upgrade.
            body["generation"].pop("references", None)
        if request.generation.source:
            body["generation"]["source"].pop("content_base64")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self._lock:
            with self.db() as db:
                old = db.execute("SELECT digest,record FROM jobs WHERE id=?", (request.request_id,)).fetchone()
                if old:
                    if old[0] != digest:
                        raise GatewayError("idempotency_conflict")
                    return self.public(json.loads(old[1]))
                if self.channel_account.read(db)['status'] != 'active':
                    raise GatewayError('account_disabled')
            account = self.account()
            with self.source(request.generation) as path:
                media_ids = self.reference_ids(request.generation)
                cost = self.quote(request.generation, path, media_ids)
                if request.price_ceiling is not None and cost > request.price_ceiling:
                    raise GatewayError("quote_changed")
                with self.db() as db:
                    if self.channel_account.read(db)['status'] != 'active':
                        raise GatewayError('account_disabled')
                    jobs = [json.loads(row[0]) for row in db.execute("SELECT record FROM jobs")]
                    if any(job["stage"] == "submission_unknown" for job in jobs):
                        raise GatewayError("account_reconciliation_required")
                    limits = self._limits(db)
                    if (Decimal(limits["credit_limit"]) == 0 or len(jobs) >= limits["submission_limit"] or
                            sum((Decimal(str(job["quoted_credits"])) for job in jobs), Decimal(0)) + Decimal(str(cost)) > Decimal(limits["credit_limit"])):
                        raise GatewayError("account_budget_exceeded")
                    active = [job for job in jobs if job["status"] not in {"completed", "failed"}]
                    if len(active) >= self.max_concurrent:
                        raise GatewayError("account_concurrency_exceeded", 429)
                    # Balance may already include active deductions; conservative double
                    # reservation is deliberate when provider accounting is not final.
                    if sum(job["quoted_credits"] for job in active) + cost > account["credits"]:
                        raise GatewayError("insufficient_credits")
                    record = {**body, "status": "running", "stage": "submitting", "remote_id": None,
                              "reference_upload_ids": media_ids,
                              "quoted_credits": cost, "created_at": now(), "updated_at": now(), "error": None}
                    db.execute("INSERT INTO jobs VALUES(?,?,?)", (request.request_id, digest, json.dumps(record)))
                try:
                    response = self.cli.run(request.generation.arguments("create", path, media_ids), timeout=90)
                    # Fixed CLI 1.1.26 returns a list of job UUIDs for create.
                    if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], str):
                        raise ValueError
                    identity = str(uuid.UUID(response[0]))
                    if identity != response[0]:
                        raise ValueError
                    record.update(remote_id=identity, stage="remote_pending")
                except (ProviderError, ValueError, TypeError):
                    record.update(stage="submission_unknown", error="submission_unknown")
                record["updated_at"] = now()
                with self.db() as db:
                    self.save(db, record)
                return self.public(record)

    def status(self, request_id):
        with self._lock:
            record = self.load(request_id)
            if record["stage"] == "submission_unknown" or record["status"] in {"completed", "failed"}:
                return self.public(record)
            self.check_binding()
            data = self.cli.run(["generate", "get", record["remote_id"]])
            if not isinstance(data, dict) or data.get("id") != record["remote_id"] or data.get("job_type") != provider_model(record["generation"]["model"]):
                raise ProviderError("provider_protocol_error")
            status = data.get("status")
            if status == "completed":
                if self.media is None:
                    raise GatewayError("media_collector_unavailable", 503)
                record.update(stage="collecting", updated_at=now())
                with self.db() as db:
                    self.save(db, record)
                try:
                    result = self.media.collect(request_id, data.get("result_url"), record["generation"])
                except ProviderError as error:
                    if error.code != "media_contract_mismatch":
                        raise
                    record.update(status="failed", stage="media_rejected", error=error.code)
                else:
                    record.update(status="completed", stage="completed", media=result, error=None)
            elif status in {"failed", "cancelled", "canceled", "error"}:
                record.update(status="failed", stage="failed", error="provider_generation_failed")
            elif status in {"pending", "queued", "running", "processing", "in_progress", "created"}:
                record.update(stage="remote_pending")
            else:
                raise ProviderError("provider_protocol_error")
            record["updated_at"] = now()
            with self.db() as db:
                self.save(db, record)
            return self.public(record)

    @staticmethod
    def public(record):
        keys = ("request_id", "binding_id", "status", "stage", "remote_id", "quoted_credits",
                "created_at", "updated_at", "error", "media")
        return {key: record[key] for key in keys if key in record}

    def capabilities(self):
        limits = self.limits()
        return {"binding_id": self.binding, "models": MODELS,
                "generation_enabled": limits["budget_available"],
                "readiness": "not_checked"}
