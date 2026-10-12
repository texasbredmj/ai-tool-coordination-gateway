import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import GatewayError


def now() -> str:
    return datetime.now(UTC).isoformat()


def fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([tool, arguments], sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "gateway.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, idem TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, state TEXT NOT NULL, report TEXT,
                    UNIQUE(actor, idem));
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT,
                    actor TEXT NOT NULL, timestamp TEXT NOT NULL, event TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    expires REAL NOT NULL, state TEXT NOT NULL, used_by TEXT,
                    tool_id TEXT NOT NULL, arguments TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS registry (id TEXT PRIMARY KEY, adapter TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, actor: str, event: str, data: dict[str, Any], call_id: str | None = None):
        with self.connect() as db:
            self._event(db, actor, event, data, call_id)

    def _event(self, db, actor, event, data, call_id=None):
        db.execute(
            "INSERT INTO audit(call_id,actor,timestamp,event,data) VALUES (?,?,?,?,?)",
            (call_id, actor, now(), event, json.dumps(data)),
        )

    def register(self, tool_id: str, adapter: str, actor: str):
        with self.connect() as db:
            try:
                db.execute("INSERT INTO registry VALUES (?,?)", (tool_id, adapter))
            except sqlite3.IntegrityError as exc:
                raise GatewayError(
                    "duplicate_tool", "tool identifier already registered", 409
                ) from exc
            self._event(db, actor, "tool_registered", {"tool_id": tool_id, "adapter": adapter})

    def registry(self) -> dict[str, str]:
        with self.connect() as db:
            return {row["id"]: row["adapter"] for row in db.execute("SELECT * FROM registry")}

    def approval(self, actor: str, tool_id: str, arguments: dict[str, Any], ttl: int) -> str:
        approval_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,NULL,?,?)",
                (
                    approval_id,
                    actor,
                    fingerprint(tool_id, arguments),
                    time.time() + ttl,
                    "PENDING",
                    tool_id,
                    json.dumps(arguments),
                ),
            )
            self._event(
                db,
                actor,
                "approval_requested",
                {"approval_id": approval_id, "tool_id": tool_id, "arguments": arguments},
            )
        return approval_id

    def approval_detail(self, approval_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise GatewayError("unknown_approval", "approval does not exist", 404)
            result = dict(row)
            result["arguments"] = json.loads(result["arguments"])
            return result

    def decide(self, approval_id: str, actor: str, approved: bool):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row:
                raise GatewayError("unknown_approval", "approval does not exist", 404)
            if row["actor"] == actor:
                raise GatewayError("self_approval", "a separate approver is required", 403)
            if row["state"] != "PENDING" or row["expires"] <= time.time():
                raise GatewayError("approval_unavailable", "approval is expired or decided", 409)
            db.execute(
                "UPDATE approvals SET state=? WHERE id=?",
                ("APPROVED" if approved else "REJECTED", approval_id),
            )
            self._event(
                db, actor, "approval_decided", {"approval_id": approval_id, "approved": approved}
            )

    def claim(self, actor, idem, tool_id, arguments, approval_id, requires_approval):
        digest = fingerprint(tool_id, arguments)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM calls WHERE actor=? AND idem=?", (actor, idem)
            ).fetchone()
            if row:
                if row["fingerprint"] != digest:
                    raise GatewayError(
                        "idempotency_conflict", "key belongs to another request", 409
                    )
                return row["id"], False, json.loads(row["report"]) if row["report"] else None
            call_id = str(uuid.uuid4())
            if requires_approval:
                approval = db.execute(
                    "SELECT * FROM approvals WHERE id=?", (approval_id,)
                ).fetchone()
                if (
                    not approval
                    or approval["actor"] != actor
                    or approval["fingerprint"] != digest
                    or approval["state"] != "APPROVED"
                    or approval["expires"] <= time.time()
                    or approval["used_by"]
                ):
                    raise GatewayError("approval_required", "valid matching approval required", 403)
                db.execute("UPDATE approvals SET used_by=? WHERE id=?", (call_id, approval_id))
            db.execute(
                "INSERT INTO calls VALUES (?,?,?,?,?,NULL)",
                (call_id, actor, idem, digest, "RUNNING"),
            )
            self._event(
                db, actor, "call_started", {"tool_id": tool_id, "arguments": arguments}, call_id
            )
            return call_id, True, None

    def finish(self, call_id: str, actor: str, report: dict[str, Any]):
        with self.connect() as db:
            db.execute(
                "UPDATE calls SET state=?, report=? WHERE id=?",
                (report["state"], json.dumps(report), call_id),
            )
            self._event(db, actor, "call_finished", report, call_id)

    def audit(self, actor: str, all_actors: bool = False):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM audit ORDER BY seq"
                if all_actors
                else "SELECT * FROM audit WHERE actor=? ORDER BY seq",
                () if all_actors else (actor,),
            ).fetchall()
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]
