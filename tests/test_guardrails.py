import pytest

from evals import ground_truth as gt
from fleet_copilot import audit
from fleet_copilot.data import TenantView, UnknownTenant
from fleet_copilot.tools import TOOLS, ToolContext, run_tool

FOREIGN = "E4V0SB5I0MYU"


def ctx(tenant=gt.ACME):
    return ToolContext(TenantView(tenant))


def test_unknown_tenant_rejected():
    with pytest.raises(UnknownTenant):
        TenantView("evil-999")


@pytest.mark.parametrize("name,args", [
    ("get_device", {"device_id": FOREIGN}),
    ("get_device_history", {"device_id": FOREIGN, "metric": "disk_free_pct"}),
    ("create_upgrade_order", {"device_id": FOREIGN, "component": "storage", "spec": "1 TB", "evidence_ids": ["x"]}),
    ("open_remediation_ticket", {"device_id": FOREIGN, "check_id": "screen_lock", "note": "n", "evidence_ids": ["x"]}),
    ("flag_device_for_replacement", {"device_id": FOREIGN, "reason": "r", "evidence_ids": ["x"]}),
    ("notify_employee", {"employee_id": "emp-globex-1004", "message": "m", "evidence_ids": ["x"]}),
])
def test_foreign_ids_look_like_missing_ids(name, args):
    foreign = run_tool(ctx(), name, args)["result"]
    missing = run_tool(ctx(), name, {**args, **{k: "NOPE00000000" for k in ("device_id", "employee_id") if k in args}})
    assert foreign["status"] == "not_found"
    assert set(foreign) == set(missing["result"])


@pytest.mark.parametrize("name", [n for n, t in TOOLS.items() if t.kind == "read"])
def test_read_tools_never_return_foreign_devices(name):
    c = ctx()
    args = {"fleet_overview": {}, "find_devices": {}, "get_device": {"device_id": "MT7PJB7N5LRE"},
            "get_device_history": {"device_id": "MT7PJB7N5LRE", "metric": "battery"},
            "detect_insights": {}, "compliance_summary": {}, "load_skill": {"name": "fleet-health-review"}}[name]
    text = str(run_tool(c, name, args))
    for d in gt.DEVICES[gt.GLOBEX] | gt.DEVICES[gt.INITECH]:
        assert d not in text
    assert all(e.get("device_id") in gt.DEVICES[gt.ACME] | {None} for e in c.ledger.values())


def test_invalid_args_are_reported_not_raised():
    r = run_tool(ctx(), "create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "gpu"})
    assert r["error"] == "Invalid arguments"


def _evidence(c, kind):
    findings = run_tool(c, "detect_insights", {"kind": kind})["result"]["findings"]
    return {f["device_id"]: f["evidence_ids"] for f in findings}


def test_action_needs_evidence_from_this_session():
    c = ctx()
    r = run_tool(c, "flag_device_for_replacement",
                 {"device_id": "7VP16KHV88LM", "reason": "old", "evidence_ids": ["ev:trend:battery_eol:7VP16KHV88LM"]})
    assert r["result"]["status"] == "refused" and "not returned" in r["result"]["reason"]


def test_action_needs_evidence_about_the_target():
    c = ctx()
    ev = _evidence(c, "battery_eol")
    r = run_tool(c, "flag_device_for_replacement",
                 {"device_id": "7VP16KHV88LM", "reason": "old", "evidence_ids": ev["8NM23J95R5I6"]})
    assert r["result"]["status"] == "refused"


def test_replacement_refused_for_healthy_device():
    c = ctx()
    run_tool(c, "get_device", {"device_id": "XSU9UUDGU6AC"})
    r = run_tool(c, "flag_device_for_replacement",
                 {"device_id": "XSU9UUDGU6AC", "reason": "old", "evidence_ids": ["ev:snap:XSU9UUDGU6AC:2026-06-12"]})
    assert r["result"]["status"] == "refused" and "No hardware finding" in r["result"]["reason"]
    assert c.proposals == []


def test_valid_proposal_is_queued_and_deduplicated():
    c = ctx()
    ev = _evidence(c, "battery_eol")
    args = {"device_id": "7VP16KHV88LM", "reason": "battery", "evidence_ids": ev["7VP16KHV88LM"]}
    first = run_tool(c, "flag_device_for_replacement", args)["result"]
    again = run_tool(c, "flag_device_for_replacement", args)["result"]
    assert first["status"] == "queued_for_approval" and first["proposal_id"] == "P-1"
    assert again == {"status": "already_proposed", "proposal_id": "P-1"}
    assert audit.outbox() == []


def test_remediation_requires_failing_check():
    c = ctx()
    snap = run_tool(c, "get_device", {"device_id": "XSU9UUDGU6AC"})["result"]["evidence_id"]
    r = run_tool(c, "open_remediation_ticket",
                 {"device_id": "XSU9UUDGU6AC", "check_id": "screen_lock", "note": "n", "evidence_ids": [snap]})
    assert r["result"]["status"] == "refused" and "passes" in r["result"]["reason"]


def test_ram_upgrade_on_soldered_ram_refused():
    c = ctx(gt.INITECH)
    ev = _evidence(c, "ram_constrained")
    r = run_tool(c, "create_upgrade_order", {"device_id": "I3JLKILKQTAC", "component": "ram", "spec": "16 GB",
                                             "evidence_ids": ev["I3JLKILKQTAC"]})
    assert r["result"]["status"] == "refused" and "flag_device_for_replacement" in r["result"]["reason"]


def test_execution_is_idempotent():
    p = {"id": "P-1", "tool": "open_remediation_ticket", "args": {"device_id": "MT7PJB7N5LRE"},
         "evidence_ids": ["e"], "dedupe_key": "k"}
    first = audit.execute(p, tenant=gt.ACME, approved_by="admin", session="s")
    second = audit.execute(p, tenant=gt.ACME, approved_by="admin", session="s")
    assert first["status"] == "executed" and second == {"status": "duplicate", "reference": first["reference"]}
    assert len(audit.outbox()) == 1
