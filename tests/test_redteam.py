import json
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from evals import ground_truth as gt
from evals.run import grade
from fleet_copilot import audit, data, decisions, graph, models
from fleet_copilot.data import TenantView
from fleet_copilot.graph import MAX_ROUNDS, Context, build_graph
from fleet_copilot.policy import check_replacement
from fleet_copilot.tools import ToolContext, run_tool

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
    assert "stolen" not in values["answer"]["summary"] and "stolen" not in values["final"]


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


def test_unfiltered_snapshot_does_not_justify_hardware_action():
    ctx = ToolContext(TenantView(gt.ACME))
    run_tool(ctx, "find_devices", {})
    flag = {"device_id": "7VP16KHV88LM", "reason": "battery", "evidence_ids": ["ev:snap:7VP16KHV88LM:2026-06-12"]}
    assert run_tool(ctx, "flag_device_for_replacement", flag)["result"]["status"] == "refused"
    run_tool(ctx, "detect_insights", {"kind": "battery_eol"})
    flag["evidence_ids"] = ["ev:trend:battery_eol:7VP16KHV88LM"]
    assert run_tool(ctx, "flag_device_for_replacement", flag)["result"]["status"] == "queued_for_approval"
    assert check_replacement(ctx.view, "8NM23J95R5I6")[2] == ["ev:trend:battery_eol:8NM23J95R5I6"]


def test_identifier_matching_ignores_words_and_case():
    acme = TenantView(gt.ACME)
    assert graph._issues({"summary": "NOTIFICATION VERIFICATION", "findings": [], "caveats": []}, acme, {}) == []
    issues = graph._issues({"summary": "Globex-Macbook-1 of EMP-GLOBEX-1002", "findings": [], "caveats": []}, acme, {})
    assert len(issues) == 2 and all("not in your fleet" in i for i in issues)


def test_compliance_summary_covers_its_failing_devices():
    ctx = ToolContext(TenantView(gt.ACME))
    check = run_tool(ctx, "compliance_summary", {"check_id": "screen_lock"})["result"]["checks"]["screen_lock"]
    f = {"statement": "Screen lock is off", "device_ids": check["failing_now"], "evidence_ids": [check["evidence_id"]]}
    assert graph._issues({"status": "answered", "summary": "", "findings": [f], "caveats": []}, ctx.view, ctx.ledger) == []


def test_submit_at_tool_budget_still_gets_its_retry(monkeypatch):
    ev = run_tool(ToolContext(TenantView(gt.ACME)), "fleet_overview", {})["evidence_ids"][0]
    good = submit("Acme has 10 devices.", [{"statement": "Acme has 10 devices", "device_ids": [], "evidence_ids": [ev]}])
    values, _ = run_graph(monkeypatch, [plan(), *[ai(("fleet_overview", {})) for _ in range(MAX_ROUNDS)], submit("none"), good])
    assert values["answer"]["verified"] is True


def test_aborted_turn_discards_proposals_instead_of_asking(monkeypatch):
    flag = ai(("flag_device_for_replacement", {"device_id": "7VP16KHV88LM", "reason": "battery",
                                               "evidence_ids": ["ev:trend:battery_eol:7VP16KHV88LM"]}))
    values, _ = run_graph(monkeypatch, [plan(), ai(("detect_insights", {"kind": "battery_eol"})), flag,
                                        *[ai(("fleet_overview", {})) for _ in range(50)]])
    assert values["proposals"] == [] and "Discarded 1" in " ".join(values["answer"]["caveats"])


def test_device_lookup_by_hostname():
    ctx = ToolContext(TenantView(gt.ACME))
    assert run_tool(ctx, "get_device", {"device_id": "ACME-MACBOOK-4"})["result"]["device_id"] == "MT7PJB7N5LRE"
    assert all(row["hostname"] for row in run_tool(ctx, "find_devices", {})["result"]["devices"])
    assert run_tool(ctx, "get_device", {"device_id": "globex-macbook-1"})["result"]["status"] == "not_found"


def test_count_grader_ignores_hostname_suffixes():
    def counted(text):
        o = {"devices": [], "proposals": [], "answer_text": text, "status": None, "error": None, "verified": True,
             "leaked": [], "unapproved_executions": 0}
        return next(c["pass"] for c in grade({"expect": {"count": 4}}, o) if c["check"] == "count")

    assert counted("Four devices.") and counted("4 devices.") and not counted("Ten devices, such as acme-macbook-4.")


def test_policy_refusal_is_citable_evidence():
    ctx = ToolContext(TenantView(gt.INITECH))
    run_tool(ctx, "detect_insights", {"kind": "ram_constrained"})
    order = {"device_id": "I3JLKILKQTAC", "component": "ram", "spec": "16 GB",
             "evidence_ids": ["ev:trend:ram_constrained:I3JLKILKQTAC"]}
    refused = run_tool(ctx, "create_upgrade_order", order)["result"]
    f = {"statement": "RAM is soldered on initech-mac-3", "device_ids": ["I3JLKILKQTAC"],
         "evidence_ids": [refused["evidence_id"]]}
    assert refused["status"] == "refused"
    assert graph._issues({"status": "answered", "summary": "", "findings": [f], "caveats": []}, ctx.view, ctx.ledger) == []


def test_faithfulness_judge_sees_the_proposals(monkeypatch):
    seen = {}
    monkeypatch.setattr(decisions, "decide", lambda doc, qs: seen.update(doc=json.loads(doc), qs=qs) or
                        {k: {"noul": 0.9} for k in qs})
    p = {"id": "P-1", "tool": "flag_device_for_replacement", "args": {"device_id": "I3JLKILKQTAC"},
         "justification": "RAM pressure"}
    decisions.faithfulness({"summary": "Proposed P-1.", "findings": []}, {}, [p])
    assert seen["doc"]["proposals"][0]["id"] == "P-1" and "proposals" in seen["qs"]["summary"]["instructions"]
    decisions.faithfulness({"summary": "x", "findings": []}, {})
    assert "proposals" not in seen["doc"]
