from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import audit, insights
from .conversations import Conversations
from .data import TenantView, all_tenants, tenant_name
from .graph import MAX_QUESTION, Context, build_graph, durable_checkpointer

STATIC = Path(__file__).parent / "static"
COOKIE = "fleet_session"
SESSION_TTL_S = 12 * 3600
MAX_BODY = 64 * 1024
ACTOR = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
THREAD = re.compile(r"^[0-9a-f]{12}$")
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(Body):
    tenant: str
    actor: str = Field(default="it-admin", max_length=64)


class AskBody(Body):
    thread: str
    question: str = Field(min_length=1, max_length=MAX_QUESTION)


class Decision(Body):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=500)


class ApproveBody(Body):
    thread: str
    token: str
    decisions: dict[str, Decision]


class Sessions:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, tenant TEXT, actor TEXT, "
                        "expires REAL)")
        self.lock = threading.Lock()
        self.running: set[str] = set()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, tenant: str, actor: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                            (self._hash(token), tenant, actor, time.time() + SESSION_TTL_S))
        return token

    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.lock:
            row = self.db.execute("SELECT tenant, actor, expires FROM sessions WHERE token_hash = ?",
                                  (self._hash(token),)).fetchone()
        return {"tenant": row[0], "actor": row[1]} if row and row[2] > time.time() else None

    def delete(self, token: str | None) -> None:
        if token:
            with self.lock, self.db:
                self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash(token),))

    def acquire(self, thread: str) -> bool:
        with self.lock:
            if thread in self.running:
                return False
            self.running.add(thread)
            return True

    def release(self, thread: str) -> None:
        with self.lock:
            self.running.discard(thread)


def approval_token(pending: dict) -> str:
    return hashlib.sha256(json.dumps(pending["proposals"], sort_keys=True, default=str).encode()).hexdigest()[:16]


def _devices(view: TenantView, ids: list[str]) -> list[dict]:
    out = []
    for d in ids:
        s = view.latest(d)
        out.append({"id": d, "hostname": s.hostname, "model": s.model_name, "owner": s.employee_id} if s else {"id": d})
    return out


def turn_view(turn: dict, ledger: dict, tenant: str, pending: dict | None = None) -> dict:
    view = TenantView(tenant)
    answer = turn.get("answer") or {}
    refs: list[str] = []
    for f in answer.get("findings", []):
        refs += [i for i in f["evidence_ids"] if i not in refs]
    outcomes = {r["proposal_id"]: r for r in turn.get("executions", [])}
    return {
        "question": turn.get("question"),
        "status": answer.get("status"),
        "verified": answer.get("verified"),
        "summary": answer.get("summary"),
        "findings": [{"statement": f["statement"], "devices": _devices(view, f["device_ids"]),
                      "refs": [refs.index(i) + 1 for i in f["evidence_ids"]], "evidence_ids": f["evidence_ids"]}
                     for f in answer.get("findings", [])],
        "caveats": answer.get("caveats", []) + (
            ["This request tried to skip or pre-grant approval. Every action still needs an administrator's "
             "decision."] if "approval_bypass_attempt" in turn.get("flags", []) else []),
        "evidence": [ledger.get(i, {"id": i}) for i in refs],
        "proposals": [{**{k: p[k] for k in ("id", "tool", "risk", "args", "justification", "evidence_ids")},
                       "target": (_devices(view, [p["args"]["device_id"]])[0] if p["args"].get("device_id")
                                  else {"employee": p["args"].get("employee_id")}),
                       "outcome": outcomes.get(p["id"])} for p in turn.get("proposals", [])],
        "pending_approval": pending is not None,
        "approval_token": approval_token(pending) if pending else None,
        "usage": turn.get("usage") or {},
        "data_as_of": turn.get("data_as_of"),
    }


def _event(kind: str, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"


def _progress(update: dict):
    for node, delta in update.items():
        if node == "__interrupt__" or not isinstance(delta, dict):
            continue
        if node == "tools":
            if delta.get("plan"):
                yield _event("plan", {"intent": delta["plan"]["intent"], "steps": delta["plan"]["steps"]})
            for m in delta.get("messages", []):
                if isinstance(m, ToolMessage) and m.name not in ("make_plan", "submit_answer"):
                    body = json.loads(m.content)
                    inner = body.get("result", body)
                    status = "error" if "error" in body else inner.get("status", "ok")
                    yield _event("tool", {"name": m.name, "status": status})
        elif node == "verify":
            if delta.get("answer") is None and "verify_attempts" in delta:
                yield _event("verify", {"status": "retry"})
            elif delta.get("answer"):
                yield _event("verify", {"status": "verified" if delta["answer"].get("verified") else "stripped"})


class OriginAndHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        if request.method == "POST" and request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}":
            return await JSONResponse({"error": "Cross-origin request refused"}, status_code=403)(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []),
                                      *((k.lower().encode(), v.encode()) for k, v in HEADERS.items())]
            await send(message)

        await self.app(scope, receive, with_headers)


def _seconds_until(hhmm: str) -> float:
    now = datetime.now()
    h, m = (int(x) for x in hhmm.split(":"))
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return ((nxt if nxt > now else nxt + timedelta(days=1)) - now).total_seconds()


@contextlib.asynccontextmanager
async def daily_briefings(app):
    async def loop():
        await run_in_threadpool(insights.ensure)
        while True:
            await asyncio.sleep(_seconds_until(os.environ.get("FLEET_BRIEFING_TIME", "07:00")))
            for t in sorted(all_tenants()):
                await run_in_threadpool(insights.run, t)

    task = asyncio.create_task(loop())
    yield
    task.cancel()


def create_app(checkpoint_path: Path | None = None, allowed_hosts: list[str] | None = None,
               scheduler: bool = True) -> Starlette:
    path = checkpoint_path or audit.runs_dir() / "checkpoints.sqlite"
    graph = build_graph(durable_checkpointer(path))
    sessions, conversations = Sessions(path), Conversations(path)
    hosts = allowed_hosts or os.environ.get("FLEET_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

    def deny(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    def who(request: Request) -> dict | None:
        return sessions.get(request.cookies.get(COOKIE))

    def owned(s: dict, thread: str) -> bool:
        return bool(THREAD.match(thread or "")) and conversations.owns(thread, s["tenant"], s["actor"])

    async def body(request: Request, model: type[BaseModel]):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return deny(413, "Request too large")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                return deny(413, "Request too large")
        try:
            return model.model_validate(json.loads(raw or b"{}"))
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as e:
            return deny(422, str(e).splitlines()[0][:200])

    def config(s: dict, thread: str) -> tuple[dict, Context]:
        return {"configurable": {"thread_id": thread}}, Context(s["tenant"], s["actor"], thread)

    def snapshot(thread: str) -> tuple[dict, dict | None]:
        state = graph.get_state({"configurable": {"thread_id": thread}})
        return state.values, (state.interrupts[0].value if state.interrupts else None)

    def conversation(s: dict, thread: str) -> dict:
        values, pending = snapshot(thread)
        ledger = values.get("ledger") or {}
        turns = [turn_view(t, ledger, s["tenant"]) for t in values.get("turns") or []]
        if pending:
            turns.append(turn_view({"question": values.get("question"), "answer": values.get("answer"),
                                    "proposals": values.get("proposals", []), "flags": values.get("flags", []),
                                    "data_as_of": TenantView(s["tenant"]).data_as_of()},
                                   ledger, s["tenant"], pending))
        return {"thread": thread, "turns": turns, "pending_approval": pending is not None}

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    async def tenants(request: Request) -> Response:
        return JSONResponse([{"id": t, "name": tenant_name(t)} for t in sorted(all_tenants())])

    async def login(request: Request) -> Response:
        b = await body(request, LoginBody)
        if isinstance(b, Response):
            return b
        if b.tenant not in all_tenants() or not ACTOR.match(b.actor):
            return deny(400, "Unknown company or invalid name")
        token = sessions.create(b.tenant, b.actor)
        audit.log("login", session="web", tenant=b.tenant, actor=b.actor)
        r = JSONResponse({"tenant": b.tenant, "tenant_name": tenant_name(b.tenant), "actor": b.actor})
        r.set_cookie(COOKIE, token, max_age=SESSION_TTL_S, httponly=True, samesite="strict",
                     secure=request.url.scheme == "https", path="/")
        return r

    async def logout(request: Request) -> Response:
        sessions.delete(request.cookies.get(COOKIE))
        r = JSONResponse({"ok": True})
        r.delete_cookie(COOKIE, path="/")
        return r

    async def session(request: Request) -> Response:
        s = who(request)
        if not s:
            return deny(401, "Not signed in")
        return JSONResponse({"tenant": s["tenant"], "tenant_name": tenant_name(s["tenant"]), "actor": s["actor"]})

    async def list_conversations(request: Request) -> Response:
        s = who(request)
        if not s:
            return deny(401, "Not signed in")
        if request.method == "POST":
            return JSONResponse({"thread": conversations.create(s["tenant"], s["actor"])})
        return JSONResponse(conversations.list(s["tenant"], s["actor"]))

    async def get_conversation(request: Request) -> Response:
        s = who(request)
        thread = request.path_params["thread"]
        if not s:
            return deny(401, "Not signed in")
        if not owned(s, thread):
            return deny(404, "Not found")
        return JSONResponse(await run_in_threadpool(conversation, s, thread))

    async def ask(request: Request) -> Response:
        s = who(request)
        if not s:
            return deny(401, "Not signed in")
        b = await body(request, AskBody)
        if isinstance(b, Response):
            return b
        if not owned(s, b.thread):
            return deny(404, "Not found")
        if not sessions.acquire(b.thread):
            return deny(409, "A request is already running in this conversation")
        _, pending = await run_in_threadpool(snapshot, b.thread)
        if pending:
            sessions.release(b.thread)
            return deny(409, "Decide the pending approvals first")
        conversations.touch(b.thread, b.question)
        cfg, ctx = config(s, b.thread)

        def run():
            try:
                for update in graph.stream({"messages": [HumanMessage(b.question)]}, cfg, context=ctx,
                                           stream_mode="updates"):
                    yield from _progress(update)
                result = conversation(s, b.thread)
                yield _event("answer", result["turns"][-1] if result["turns"] else {})
            except Exception as e:  # noqa: BLE001
                audit.log("web_error", session=b.thread, tenant=s["tenant"], actor=s["actor"],
                          error=f"{type(e).__name__}: {str(e)[:300]}")
                yield _event("error", {"message": "The copilot hit an error. Details are in the audit log."})
            finally:
                sessions.release(b.thread)
            yield _event("done", {})

        return StreamingResponse(iterate_in_threadpool(run()), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    async def approve(request: Request) -> Response:
        s = who(request)
        if not s:
            return deny(401, "Not signed in")
        b = await body(request, ApproveBody)
        if isinstance(b, Response):
            return b
        if not owned(s, b.thread):
            return deny(404, "Not found")
        if not sessions.acquire(b.thread):
            return deny(409, "This conversation is busy; reload it and decide again")
        try:
            _, pending = await run_in_threadpool(snapshot, b.thread)
            if not pending:
                return deny(409, "Nothing is waiting for approval")
            if not secrets.compare_digest(b.token, approval_token(pending)):
                return deny(409, "The proposals changed since you loaded them; reload and decide again")
            ids = {p["id"] for p in pending["proposals"]}
            if set(b.decisions) != ids:
                return deny(422, f"Decide every proposal: {', '.join(sorted(ids))}")
            cfg, ctx = config(s, b.thread)
            resume = {pid: d.model_dump() for pid, d in b.decisions.items()}
            await run_in_threadpool(graph.invoke, Command(resume=resume), cfg, context=ctx)
            result = await run_in_threadpool(conversation, s, b.thread)
        finally:
            sessions.release(b.thread)
        return JSONResponse(result["turns"][-1])

    async def evidence(request: Request) -> Response:
        s = who(request)
        thread = request.query_params.get("thread", "")
        if not s:
            return deny(401, "Not signed in")
        if not owned(s, thread):
            return deny(404, "Not found")
        values, _ = await run_in_threadpool(snapshot, thread)
        record = (values.get("ledger") or {}).get(request.query_params.get("id", ""))
        return JSONResponse(record) if record else deny(404, "Not found")

    async def audit_log(request: Request) -> Response:
        s = who(request)
        thread = request.query_params.get("thread", "")
        if not s:
            return deny(401, "Not signed in")
        if not owned(s, thread):
            return deny(404, "Not found")
        rows = await run_in_threadpool(audit.read_log, thread)
        return JSONResponse(rows[-300:])

    async def insights_days(request: Request) -> Response:
        s = who(request)
        if not s:
            return deny(401, "Not signed in")
        return JSONResponse(await run_in_threadpool(insights.days, s["tenant"]))

    async def insights_day(request: Request) -> Response:
        s = who(request)
        if not s:
            return deny(401, "Not signed in")
        day = request.query_params.get("date") or TenantView(s["tenant"]).data_as_of()[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            return deny(422, "date must be YYYY-MM-DD")
        b = await run_in_threadpool(insights.load, s["tenant"], day)
        return JSONResponse(b) if b else deny(404, "No briefing for that day")

    routes = [
        Route("/", index),
        Route("/api/tenants", tenants),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/session", session),
        Route("/api/conversations", list_conversations, methods=["GET", "POST"]),
        Route("/api/conversations/{thread}", get_conversation),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/approve", approve, methods=["POST"]),
        Route("/api/evidence", evidence),
        Route("/api/audit", audit_log),
        Route("/api/insights", insights_day),
        Route("/api/insights/days", insights_days),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=hosts), Middleware(OriginAndHeaders)]
    return Starlette(routes=routes, middleware=middleware, lifespan=daily_briefings if scheduler else None)


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
