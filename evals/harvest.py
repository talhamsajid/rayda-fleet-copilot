from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

SIGNALS = {
    "answer_rejected": lambda e: "verifier rejected: " + "; ".join(e.get("issues", []))[:300],
    "answer_unverified": lambda e: f"no structured answer: {e.get('reason')}",
    "guard_blocked": lambda e: f"guard blocked: {e.get('reason')}",
    "execution_blocked": lambda e: f"approved action blocked on re-check: {e.get('reason')}",
    "decision_unavailable": lambda e: f"Decision 1 unavailable at {e.get('stage')}",
    "web_error": lambda e: f"run failed: {e.get('error')}",
    "tool_call": lambda e: (f"{e['tool']} {e['outcome']}: {e.get('reason') or ''}".strip()
                            if e.get("outcome") in ("refused", "invalid_args", "error") else None),
    "approval_decision": lambda e: f"admin rejected {e['proposal_id']} ({e.get('tool')})" if e["decision"] == "reject"
    else None,
    "response": lambda e: "answer shipped NOT fully verified" if e.get("verified") is False else None,
}


def harvest(rows: list[dict]) -> list[dict]:
    turns: dict[tuple, dict] = {}
    current: dict[str, tuple] = {}
    for e in rows:
        if e["event"] == "question":
            key = (e["session"], e["seq"])
            current[e["session"]] = key
            turns[key] = {"tenant": e["tenant"], "question": e["text"], "signals": [], "prompt_version": None}
            continue
        key = current.get(e["session"])
        if key is None:
            continue
        if e["event"] == "llm_call":
            turns[key]["prompt_version"] = e.get("prompt_version")
        describe = SIGNALS.get(e["event"])
        signal = describe(e) if describe else None
        if signal:
            turns[key]["signals"].append(signal)
    cases: dict[tuple, dict] = {}
    for (session, seq), t in turns.items():
        if not t["signals"]:
            continue
        key = (t["tenant"], t["question"])
        if key in cases:
            cases[key]["observed_signals"] += t["signals"]
            cases[key]["occurrences"] += 1
            continue
        cases[key] = {"id": f"harvest-{session}-{seq}", "category": "regression", "tenant": t["tenant"],
                      "question": t["question"], "approve": "none", "expect": {}, "occurrences": 1,
                      "observed_signals": t["signals"], "prompt_version": t["prompt_version"],
                      "todo": "add expectations (status, devices, proposals), then move into evals/cases.py"}
    return list(cases.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Turn audit-log failures into candidate regression eval cases")
    ap.add_argument("audit_log", nargs="?", default="runs/audit.jsonl")
    ap.add_argument("--out", help="write JSONL here instead of stdout")
    args = ap.parse_args(argv)
    rows = [json.loads(x) for x in Path(args.audit_log).read_text().splitlines() if x.strip()]
    cases = harvest(rows)
    text = "\n".join(json.dumps(c, sort_keys=True) for c in cases)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"{len(cases)} candidate cases -> {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
