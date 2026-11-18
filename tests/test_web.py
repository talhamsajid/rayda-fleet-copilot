import itertools
import json

import pytest
from langchain_core.messages import AIMessage
from starlette.testclient import TestClient

from fleet_copilot import audit, insights, models
from fleet_copilot.web import Sessions, create_app

ORIGIN = {"origin": "http://testserver"}
_ids = itertools.count()
SNAP = "ev:snap:MT7PJB7N5LRE:2026-06-12"
QUESTION = "Open a screen lock ticket for acme-macbook-4"


def ai(*calls):
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"w{next(_ids)}"} for n, a in calls])


def ticket_script():
    while True:
        yield ai(("make_plan", {"intent": "action", "steps": ["look", "propose"], "tools": ["get_device"]}))
        yield ai(("get_device", {"device_id": "MT7PJB7N5LRE"}))
        yield ai(("open_remediation_ticket", {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock",
                                              "note": "re-apply screen lock policy", "evidence_ids": [SNAP]}))
        yield ai(("submit_answer", {"status": "answered", "summary": "I proposed a ticket (P-1).", "caveats": [],
                                    "findings": [{"statement": "Screen lock is off on acme-macbook-4.",
                                                  "device_ids": ["MT7PJB7N5LRE"], "evidence_ids": [SNAP]}]}))


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    script = ticket_script()
    monkeypatch.setattr(models, "invoke", lambda messages, tools: next(script))

    def _make():
        return TestClient(create_app(tmp_path / "checkpoints.sqlite", allowed_hosts=["testserver"], scheduler=False))

    return _make


def login(client, tenant="acme-001", actor="alice"):
    r = client.post("/api/login", json={"tenant": tenant, "actor": actor}, headers=ORIGIN)
    assert r.status_code == 200
    return r


def new_thread(client):
    return client.post("/api/conversations", headers=ORIGIN).json()["thread"]


def events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def ask(client, thread, question=QUESTION):
    return dict(events(client.post("/api/ask", json={"thread": thread, "question": question}, headers=ORIGIN)))


def approve(client, thread, token, decision="approve"):
    return client.post("/api/approve", headers=ORIGIN,
                       json={"thread": thread, "token": token, "decisions": {"P-1": {"decision": decision}}})


def test_requires_sign_in_same_origin_and_known_host(make_client):
    c = make_client()
    assert c.get("/api/session").status_code == 401
    assert c.post("/api/login", json={"tenant": "acme-001"}).status_code == 403
    assert c.post("/api/login", json={"tenant": "acme-001"}, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/login", json={"tenant": "nope", "actor": "a"}, headers=ORIGIN).status_code == 400
    rebind = {"host": "rebind.attacker.test", "origin": "http://rebind.attacker.test"}
    assert c.post("/api/login", json={"tenant": "acme-001", "actor": "a"}, headers=rebind).status_code == 400
    big = c.post("/api/login", content=b"{" + b" " * 70000 + b"}", headers={**ORIGIN, "content-type": "application/json"})
    assert big.status_code == 413
    r = c.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_ask_approve_execute_and_inspect(make_client):
    c = make_client()
    r = login(c)
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    thread = new_thread(c)
    stream = ask(c, thread)
    answer = stream["answer"]
    assert answer["pending_approval"] and answer["proposals"][0]["risk"] == "low"
    assert answer["findings"][0]["devices"][0]["hostname"] == "acme-macbook-4"
    assert audit.outbox() == []
    assert c.post("/api/ask", json={"thread": thread, "question": "again"}, headers=ORIGIN).status_code == 409
    assert approve(c, thread, "stale-token").status_code == 409
    done = approve(c, thread, answer["approval_token"]).json()
    assert done["proposals"][0]["outcome"]["status"] == "executed"
    assert approve(c, thread, answer["approval_token"]).status_code == 409
    assert [row["tool"] for row in audit.outbox()] == ["open_remediation_ticket"]
    assert c.get(f"/api/evidence?thread={thread}&id={SNAP}").json()["device_id"] == "MT7PJB7N5LRE"
    logged = {e["event"] for e in c.get(f"/api/audit?thread={thread}").json()}
    assert {"question", "llm_call", "tool_call", "approval_decision", "execution"} <= logged
    assert c.get("/api/conversations").json()[0]["title"] == QUESTION


def test_conversations_are_private_to_their_admin(make_client):
    owner, colleague, other = make_client(), make_client(), make_client()
    login(owner)
    thread = new_thread(owner)
    token = ask(owner, thread)["answer"]["approval_token"]
    login(colleague, actor="bob")
    login(other, "globex-002")
    for c in (colleague, other):
        assert c.get("/api/conversations").json() == []
        assert c.get(f"/api/conversations/{thread}").status_code == 404
        assert c.get(f"/api/evidence?thread={thread}&id={SNAP}").status_code == 404
        assert c.get(f"/api/audit?thread={thread}").status_code == 404
        assert c.post("/api/ask", json={"thread": thread, "question": "hi"}, headers=ORIGIN).status_code == 404
        assert approve(c, thread, token).status_code == 404
    assert audit.outbox() == []


def test_history_and_pending_approval_survive_restart(make_client):
    first = make_client()
    login(first)
    thread = new_thread(first)
    token = ask(first, thread)["answer"]["approval_token"]
    restarted = make_client()
    restarted.cookies = first.cookies
    conv = restarted.get(f"/api/conversations/{thread}").json()
    assert conv["pending_approval"] and conv["turns"][-1]["question"] == QUESTION
    assert approve(restarted, thread, token).json()["proposals"][0]["outcome"]["status"] == "executed"
    second = ask(restarted, thread, "Open a screen lock ticket for acme-macbook-4 again")
    turns = restarted.get(f"/api/conversations/{thread}").json()["turns"]
    assert len(turns) == 2 and turns[0]["proposals"][0]["outcome"]["status"] == "executed"
    assert second["answer"]["pending_approval"]


def test_one_request_per_conversation_at_a_time(tmp_path):
    s = Sessions(tmp_path / "s.sqlite")
    assert s.acquire("abc") and not s.acquire("abc")
    s.release("abc")
    assert s.acquire("abc")


def test_insights_are_scoped_to_the_signed_in_company(make_client):
    insights.backfill("acme-001")
    insights.backfill("globex-002")
    c = make_client()
    login(c, "globex-002")
    b = c.get("/api/insights").json()
    assert b["tenant"] == "globex-002"
    assert all(i["device"]["hostname"].startswith("globex-") for i in b["issues"])
    assert len(c.get("/api/insights/days").json()) == 30
    assert c.get("/api/insights?date=2026-05-01").status_code == 404
    assert c.get("/api/insights?date=../../x").status_code == 422
