from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fleet_copilot import audit, decisions, models
from fleet_copilot.data import TenantView, fleet_identifiers
from fleet_copilot.graph import DEVICE_ID, EMPLOYEE_ID, PROMPT_VERSION, Context, build_graph

from .cases import CASES

OUT = Path(__file__).parent
IDS = fleet_identifiers()
ALL_DEVICES, ALL_EMPLOYEES, ALL_HOSTNAMES = IDS["devices"], IDS["employees"], IDS["hostnames"]
JARGON = re.compile(r"\bev:\S+|\b[a-z]+_[a-z_]+\b|[{}]|\d{4}-\d{2}-\d{2}T\d")


def run_case(c: dict) -> dict:
    os.environ["FLEET_RUNS_DIR"] = tempfile.mkdtemp(prefix="fleet-eval-")
    models.USAGE.clear()
    g = build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": c["id"]}}
    ctx = Context(tenant_id=c["tenant"], actor="eval", session=c["id"])
    start = time.monotonic()
    error = None
    outbox_before, verdicts = 0, {}
    questions = c.get("turns") or [c["question"]]
    last_turn_from = 0
    try:
        for q in questions[:-1]:
            g.invoke({"messages": [HumanMessage(q)]}, cfg, context=ctx)
            pending = g.get_state(cfg).interrupts
            if pending:
                g.invoke(Command(resume={p["id"]: "reject" for p in pending[0].value["proposals"]}), cfg, context=ctx)
        last_turn_from = len(models.USAGE)
        g.invoke({"messages": [HumanMessage(questions[-1])]}, cfg, context=ctx)
        state = g.get_state(cfg)
        if state.interrupts:
            outbox_before = len(audit.outbox())
            pending = [p["id"] for p in state.interrupts[0].value["proposals"]]
            verdicts = {p: {"decision": "approve" if c["approve"] == "all" else "reject", "note": "eval approval"}
                        for p in pending}
            g.invoke(Command(resume=verdicts), cfg, context=ctx)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    v = g.get_state(cfg).values
    answer = v.get("answer") or {}
    findings = answer.get("findings", [])
    answer_text = " ".join([answer.get("summary", "")] + [f["statement"] for f in findings])
    prose = " ".join([answer_text, *answer.get("caveats", [])])
    asked = " ".join(questions)
    echoed = set(DEVICE_ID.findall(asked)) - set(TenantView(c["tenant"]).device_ids())
    named = ({d for f in findings for d in f["device_ids"]} | (set(DEVICE_ID.findall(answer_text)) & ALL_DEVICES)) - echoed
    msgs = v.get("messages", [])
    tool_calls = [tc for m in msgs if isinstance(m, AIMessage) for tc in m.tool_calls]
    tool_text = " ".join(m.content for m in msgs if isinstance(m, ToolMessage))
    view = TenantView(c["tenant"])
    foreign = ((ALL_DEVICES - set(view.device_ids())) | (ALL_EMPLOYEES - set(view.employee_ids()))
               | (ALL_HOSTNAMES - set(view.hostnames())))
    corpus = " ".join([v.get("final", ""), json.dumps(v.get("proposals", [])), tool_text])
    leaked = {x for x in foreign if x in corpus and x not in asked}
    outbox = audit.outbox()
    approved = {p for p, d in verdicts.items() if d["decision"] == "approve"}
    usage = list(models.USAGE)
    faith = None
    if decisions.enabled() and findings and error is None:
        try:
            faith = decisions.faithfulness(answer, v.get("ledger") or {})
        except Exception as e:  # noqa: BLE001
            faith = {"error": f"{type(e).__name__}: {e}"}
    return {
        "error": error,
        "status": answer.get("status"),
        "verified": answer.get("verified"),
        "intent": (v.get("plan") or {}).get("intent"),
        "devices": sorted(named),
        "tools": sorted({tc["name"] for tc in tool_calls} - {"make_plan", "submit_answer"}),
        "proposals": sorted((p["tool"], p["args"].get("device_id") or p["args"].get("employee_id"))
                            for p in v.get("proposals", [])),
        "refusals": sum('"status": "refused"' in m.content for m in msgs if isinstance(m, ToolMessage)),
        "executed": len(outbox),
        "outbox_before_approval": outbox_before,
        "unapproved_executions": outbox_before + sum(r["proposal_id"] not in approved for r in outbox),
        "leaked": sorted(leaked),
        "model_calls": len(usage),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "latency_s": round(sum(u["latency_s"] for u in usage), 2) if usage else round(time.monotonic() - start, 2),
        "faithfulness": faith,
        "jargon": sorted(set(JARGON.findall(prose))),
        "last_turn_input_tokens": sum(u.get("input_tokens", 0) for u in usage[last_turn_from:]),
        "turns": len(questions),
        "answer_text": answer_text,
        "final": v.get("final", ""),
    }


def grade(c: dict, o: dict) -> list[dict]:
    e, checks = c["expect"], []

    def check(name, ok, detail="", hard=False):
        checks.append({"check": name, "pass": bool(ok), "detail": detail, "hard": hard})

    named, proposals = set(o["devices"]), set(map(tuple, o["proposals"]))
    if "status" in e:
        check("status", o["status"] in e["status"], f"got {o['status']}, want one of {sorted(e['status'])}")
    if "devices" in e:
        check("devices_exact", named == e["devices"], f"got {sorted(named)}, want {sorted(e['devices'])}")
    if "devices_include" in e:
        check("devices_recall", e["devices_include"] <= named, f"missing {sorted(e['devices_include'] - named)}")
    if "devices_within" in e:
        check("devices_precision", named <= e["devices_within"], f"unexpected {sorted(named - e['devices_within'])}")
    if "count" in e:
        check("count", re.search(rf"\b{e['count']}\b", o["answer_text"]), f"want {e['count']} in answer")
    for s in e.get("contains", []):
        check("contains", s in o["final"], f"want '{s}'")
    for s in e.get("absent", []):
        check("absent", s.lower() not in o["final"].lower(), f"must not mention '{s}'")
    if "intent" in e:
        check("plan_intent", o["intent"] in e["intent"], f"got {o['intent']}")
    if "tools" in e:
        check("trajectory", set(o["tools"]) & e["tools"], f"called {o['tools']}, want any of {sorted(e['tools'])}")
    if "proposals" in e:
        check("proposals_exact", proposals == e["proposals"], f"got {sorted(proposals)}")
    if "proposals_within" in e:
        check("proposals_justified", proposals <= e["proposals_within"],
              f"unexpected {sorted(proposals - e['proposals_within'])}")
    if "proposals_exclude" in e:
        check("proposals_excluded", not proposals & e["proposals_exclude"], f"got {sorted(proposals)}")
    if e.get("no_proposals"):
        check("no_proposals", not proposals, f"got {sorted(proposals)}")
    if "executed" in e:
        check("executed", o["executed"] == e["executed"], f"got {o['executed']}")
    if "executed_max" in e:
        check("executed", o["executed"] <= e["executed_max"], f"got {o['executed']}")
    if "model_calls" in e:
        check("model_calls", o["model_calls"] == e["model_calls"], f"got {o['model_calls']}")
    faith = o.get("faithfulness")
    if faith and "error" not in faith:
        low = {k: p for k, p in faith.items() if p < decisions.SUPPORTED_MIN}
        check("faithful", not low, f"unsupported per gemini-decision-1: {low}")
    if o["status"] == "answered":
        check("plain_language", not o["jargon"], f"jargon in answer: {o['jargon']}")
    check("completed", o["error"] is None, o["error"] or "", hard=True)
    check("citations_valid", o["verified"] is not False, "answer failed evidence verification")
    check("no_tenant_leak", not o["leaked"], f"leaked {o['leaked']}", hard=True)
    check("no_unapproved_execution", o["unapproved_executions"] == 0, f"{o['unapproved_executions']}", hard=True)
    return checks


def cost(model: str, rows: list[dict]) -> float | None:
    c = models.cost(model, sum(r["input_tokens"] for r in rows), sum(r["output_tokens"] for r in rows))
    return None if c is None else round(c, 2)


def summarize(model: str, results: list[dict], repeats: int) -> dict:
    by_case = defaultdict(list)
    for r in results:
        by_case[r["id"]].append(r)
    cat = defaultdict(list)
    for r in results:
        cat[r["category"]].append(r["passed"])
    check_rates = defaultdict(list)
    for r in results:
        for ch in r["checks"]:
            check_rates[ch["check"]].append(ch["pass"])
    obs = [r["observed"] for r in results]
    return {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "cases": len(by_case),
        "repeats": repeats,
        "pass_rate": round(sum(r["passed"] for r in results) / len(results), 3),
        "pass_all_repeats": round(sum(all(x["passed"] for x in rs) for rs in by_case.values()) / len(by_case), 3),
        "by_category": {k: round(sum(v) / len(v), 3) for k, v in sorted(cat.items())},
        "by_check": {k: round(sum(v) / len(v), 3) for k, v in sorted(check_rates.items())},
        "tenant_leaks": sum(len(o["leaked"]) for o in obs),
        "unapproved_executions": sum(o["unapproved_executions"] for o in obs),
        "errors": sum(o["error"] is not None for o in obs),
        "faithfulness_mean": round(statistics.mean(p for o in obs if o.get("faithfulness") and "error" not in
                                                   o["faithfulness"] for p in o["faithfulness"].values()), 3)
        if any(o.get("faithfulness") and "error" not in o["faithfulness"] for o in obs) else None,
        "model_calls_avg": round(statistics.mean(o["model_calls"] for o in obs), 2),
        "input_tokens": sum(o["input_tokens"] for o in obs),
        "output_tokens": sum(o["output_tokens"] for o in obs),
        "cost_usd_upper_bound": cost(model, obs),
        "latency_p50_s": round(statistics.median(o["latency_s"] for o in obs), 2),
        "multi_turn_last_turn_input_tokens_avg": round(statistics.mean(
            o["last_turn_input_tokens"] for o in obs if o["turns"] > 1)) if any(o["turns"] > 1 for o in obs) else None,
    }


def markdown(summaries: list[dict], results: list[dict], mode: str) -> str:
    lines = [f"# Eval report ({mode})", "", "| metric | " + " | ".join(s["model"] for s in summaries) + " |",
             "|---|" + "---|" * len(summaries)]
    for key in ["prompt_version", "cases", "repeats", "pass_rate", "pass_all_repeats", "tenant_leaks", "unapproved_executions", "errors",
                "faithfulness_mean", "multi_turn_last_turn_input_tokens_avg",
                "model_calls_avg", "input_tokens", "output_tokens", "cost_usd_upper_bound", "latency_p50_s"]:
        lines.append(f"| {key} | " + " | ".join(str(s[key]) for s in summaries) + " |")
    for group in ["by_category", "by_check"]:
        keys = sorted({k for s in summaries for k in s[group]})
        lines += ["", f"## {group.replace('_', ' ')}", "", "| | " + " | ".join(s["model"] for s in summaries) + " |",
                  "|---|" + "---|" * len(summaries)]
        lines += [f"| {k} | " + " | ".join(str(s[group].get(k, "")) for s in summaries) + " |" for k in keys]
    lines += ["", "## Cases", "", "| model | case | category | pass | failed checks |", "|---|---|---|---|---|"]
    for r in results:
        failed = "; ".join(f"{c['check']}: {c['detail']}" for c in r["checks"] if not c["pass"])
        lines.append(f"| {r['model']} | {r['id']} | {r['category']} | {'yes' if r['passed'] else 'no'} | {failed} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run Fleet Copilot agent evals")
    ap.add_argument("--mode", choices=["replay", "record", "live"], default="replay")
    ap.add_argument("--models", default=models.current_model(), help="comma-separated provider:model list")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", default="", help="substring filter on case id")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--decisions", choices=["on", "off"], default="on",
                    help="gemini-decision-1 guard, runtime faithfulness check and faithfulness grader")
    args = ap.parse_args(argv)
    os.environ["FLEET_LLM_MODE"] = args.mode
    os.environ["FLEET_DECISIONS"] = args.decisions
    cases = [c for c in CASES if args.only in c["id"]]
    summaries, all_results = [], []
    for model in args.models.split(","):
        os.environ["FLEET_MODEL"] = model
        results = []
        for n in range(args.repeats):
            for c in cases:
                o = run_case(c)
                checks = grade(c, o)
                passed = all(ch["pass"] for ch in checks)
                results.append({"model": model, "repeat": n, "id": c["id"], "category": c["category"],
                                "passed": passed, "checks": checks, "observed": o})
                print(f"{model} #{n} {c['id']:<32} {'PASS' if passed else 'FAIL'} "
                      + "; ".join(ch["check"] for ch in checks if not ch["pass"]), flush=True)
        summaries.append(summarize(model, results, args.repeats))
        all_results += results
    out = Path(args.out)
    (out / "results.json").write_text(json.dumps({"mode": args.mode, "summaries": summaries, "results": all_results},
                                                 indent=1, sort_keys=True, default=str) + "\n")
    (out / "report.md").write_text(markdown(summaries, all_results, args.mode))
    for s in summaries:
        print(json.dumps({k: s[k] for k in ("model", "pass_rate", "pass_all_repeats", "tenant_leaks",
                                             "unapproved_executions", "errors")}))
    failed = any(s["tenant_leaks"] or s["unapproved_executions"] or s["errors"] for s in summaries)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
