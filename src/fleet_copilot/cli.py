from __future__ import annotations

import argparse
import json
import sys

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from .data import UnknownTenant, all_tenants
from .conversations import Conversations
from .graph import Context, build_graph, durable_checkpointer


def _ask_approvals(payload: dict) -> dict:
    print("\nApproval required. Nothing has executed yet.")
    decisions = {}
    for p in payload["proposals"]:
        print(f"\n  {p['id']} [{p['risk']} risk] {p['tool']} {json.dumps(p['args'])}")
        print(f"     why: {p['justification']}")
        print(f"     evidence: {', '.join(p['evidence_ids'])}")
        reply = input(f"  Approve {p['id']}? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            decisions[p["id"]] = "reject"
            continue
        note = input("     approver note (required for high risk): ").strip() if p["risk"] == "high" else ""
        decisions[p["id"]] = {"decision": "approve", "note": note}
    return decisions


def run_turn(graph, question: str | None, config: dict, context: Context, verbose: bool) -> None:
    payload = {"messages": [HumanMessage(question)]} if question else None
    if payload is None:
        state = graph.get_state(config)
        if not state.interrupts:
            return
        payload = Command(resume=_ask_approvals(state.interrupts[0].value))
    while True:
        for update in graph.stream(payload, config, context=context, stream_mode="updates"):
            for node, delta in update.items():
                if node == "__interrupt__":
                    continue
                if verbose and node == "tools":
                    plan = delta.get("plan")
                    if plan:
                        print(f"  plan [{plan['intent']}]: " + " -> ".join(plan["steps"]))
                    for m in delta.get("messages", []):
                        if m.name not in ("make_plan", "submit_answer"):
                            print(f"  tool {m.name}: {m.content[:140]}{'...' if len(m.content) > 140 else ''}")
        state = graph.get_state(config)
        if state.interrupts:
            payload = Command(resume=_ask_approvals(state.interrupts[0].value))
            continue
        print("\n" + state.values.get("final", ""))
        return


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet-copilot", description="Fleet Copilot for IT administrators")
    ap.add_argument("--tenant", required=True, help=f"one of: {', '.join(sorted(all_tenants()))}")
    ap.add_argument("--actor", default="it-admin", help="who is signed in (recorded in the audit log)")
    ap.add_argument("-q", "--question", help="ask one question and exit")
    ap.add_argument("--quiet", action="store_true", help="hide plan and tool trace")
    ap.add_argument("--session", help="resume one of your saved conversations (see --list)")
    ap.add_argument("--list", action="store_true", help="list your saved conversations and exit")
    args = ap.parse_args(argv)
    if args.tenant not in all_tenants():
        print(f"Unknown tenant {args.tenant}", file=sys.stderr)
        return 2
    conversations = Conversations()
    if args.list:
        for c in conversations.list(args.tenant, args.actor):
            print(f"{c['thread']}  {c['title']}")
        return 0
    if args.session and not conversations.owns(args.session, args.tenant, args.actor):
        print(f"Session {args.session} not found for {args.actor} at {args.tenant}", file=sys.stderr)
        return 2
    session = args.session or conversations.create(args.tenant, args.actor)
    graph = build_graph(durable_checkpointer())
    config = {"configurable": {"thread_id": session}}
    context = Context(tenant_id=args.tenant, actor=args.actor, session=session)
    try:
        if args.session:
            run_turn(graph, None, config, context, not args.quiet)
        if args.question:
            conversations.touch(session, args.question)
            run_turn(graph, args.question, config, context, not args.quiet)
            return 0
        print(f"Fleet Copilot | tenant {args.tenant} | session {session}. Ctrl-D to exit.")
        while True:
            try:
                q = input("\n> ").strip()
            except EOFError:
                return 0
            if q:
                conversations.touch(session, q)
                run_turn(graph, q, config, context, not args.quiet)
    except UnknownTenant as e:
        print(f"Unknown tenant {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
