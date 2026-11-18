import json
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from evals import ground_truth as gt
from fleet_copilot import audit, data, decisions, models
from fleet_copilot.graph import MAX_ROUNDS, Context, build_graph

FULL_QUERY = "ev:query:find_devices:"


def ai(*calls, text=""):
    return AIMessage(content=text, tool_calls=[{"name": n, "args": a, "id": f"r{i}{n}"} for i, (n, a) in enumerate(calls)])


def run_graph(monkeypatch, steps, question="q", tenant=gt.ACME, decide=None):
    script = iter(steps)
    calls = []

    def fake(messages, tools):
        calls.append(messages)
        return next(script)

    monkeypatch.setattr(models, "invoke", fake)
    if decide:
        monkeypatch.setenv("FLEET_DECISIONS", "on")
        monkeypatch.setattr(decisions, "decide", decide)
    g = build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "rt"}}
    g.invoke({"messages": [HumanMessage(question)]}, cfg, context=Context(tenant, "eve", "rt"))
    return g.get_state(cfg).values, calls


def plan():
    return ai(("make_plan", {"intent": "question", "steps": ["a"], "tools": ["find_devices"]}))


def submit(summary, findings=(), caveats=(), status="answered"):
    return ai(("submit_answer", {"status": status, "summary": summary, "findings": list(findings),
                                 "caveats": list(caveats)}))


def all_unsupported(text, questions):
    return {k: {"noul": 0.01} for k in questions}


def test_summary_only_fabrication_is_not_verified(monkeypatch):
    lie = submit("All 10 Acme devices are fully compliant and acme-macbook-2 was stolen.",
                 caveats=["MT7PJB7N5LRE has ransomware and must be replaced today."])
    values, _ = run_graph(monkeypatch, [plan(), ai(("find_devices", {})), lie, lie], decide=all_unsupported)
    assert values["answer"]["verified"] is False
    assert "MT7PJB7N5LRE" not in " ".join(values["answer"]["caveats"])


def test_unfiltered_query_does_not_cover_device_claims(monkeypatch):
    def query_id(values):
        return next(i for i in values["ledger"] if i.startswith(FULL_QUERY))

    first, _ = run_graph(monkeypatch, [plan(), ai(("find_devices", {})), submit("x", status="insufficient_data")])
    qid = query_id(first)
    claim = {"statement": "7VP16KHV88LM and XSU9UUDGU6AC have 2% disk free", "device_ids": ["7VP16KHV88LM", "XSU9UUDGU6AC"],
             "evidence_ids": [qid]}
    values, _ = run_graph(monkeypatch, [plan(), ai(("find_devices", {})), submit("Two devices.", [claim]),
                                        submit("Two devices.", [claim])])
    assert values["answer"]["verified"] is False and values["answer"]["findings"] == []


def test_round_cap_is_hard(monkeypatch):
    loop = [ai(("fleet_overview", {}))] * 50
    values, calls = run_graph(monkeypatch, [plan(), *loop])
    assert len(calls) <= MAX_ROUNDS + 3
    assert values["answer"]["status"] == "insufficient_data" and values["answer"]["verified"] is False


def test_concurrent_execution_writes_one_outbox_row():
    p = {"id": "P-1", "tool": "flag_device_for_replacement", "args": {"device_id": "7VP16KHV88LM"},
         "evidence_ids": ["e"], "dedupe_key": "k"}
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        audit.execute(p, tenant=gt.ACME, approved_by="a", session="s"))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(audit.outbox()) == 1
    assert sorted(r["status"] for r in results) == ["duplicate"] * 7 + ["executed"]


def test_concurrent_audit_sequence_is_unique():
    threads = [threading.Thread(target=lambda: audit.log("x", session="s", tenant="t", actor="a")) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    seqs = [r["seq"] for r in audit.read_log()]
    assert sorted(seqs) == list(range(1, 21))


def test_forged_row_cannot_move_another_tenants_device(tmp_path, monkeypatch):
    rows = data.DATA_PATH.read_text().splitlines()
    victim = json.loads(next(r for r in rows if '"MT7PJB7N5LRE"' in r))
    forged = {**victim, "company_id": gt.GLOBEX, "collected_at": "2026-05-01T00:00:00Z"}
    path = tmp_path / "poisoned.ndjson"
    path.write_text("\n".join([json.dumps(forged), *rows]) + "\n")
    acme, globex = data.TenantView(gt.ACME, path), data.TenantView(gt.GLOBEX, path)
    assert len(acme.history("MT7PJB7N5LRE")) == 30
    assert len(globex.history("MT7PJB7N5LRE")) == 1
    assert all(s.company_id == gt.GLOBEX for d in globex.device_ids() for s in globex.history(d))
    assert data.contested_devices(path) == {"MT7PJB7N5LRE"}


def test_unknown_and_foreign_ids_are_treated_alike(monkeypatch):
    out = {}
    for device in ("E4V0SB5I0MYU", "ZZZZ9999ZZZZ"):
        answer = submit(f"Device {device} is not in your fleet.", status="insufficient_data")
        values, _ = run_graph(monkeypatch, [plan(), ai(("get_device", {"device_id": device[::-1]})), answer, answer],
                              question="look up the reversed id")
        out[device] = (values["answer"]["verified"], values["answer"]["summary"].replace(device, "ID"))
    assert out["E4V0SB5I0MYU"] == out["ZZZZ9999ZZZZ"]


def test_decisions_timeout_fails_open(monkeypatch):
    def boom(*_a, **_k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setenv("FLEET_LLM_MODE", "live")
    with pytest.raises(decisions.DecisionError):
        decisions.decide("x", {"q": {"type": "noul", "instructions": "?"}})
