from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import policy, skills
from .data import TenantView, parse_version, snapshot_evidence, trend_evidence

MAX_PROPOSALS_PER_TURN = 10
RISK = {"open_remediation_ticket": "low", "notify_employee": "medium",
        "create_upgrade_order": "high", "flag_device_for_replacement": "high"}
Check = Literal["disk_encryption", "screen_lock", "os_up_to_date"]
EvidenceIds = Field(min_length=1, description="Evidence ids from earlier tool results that justify this action.")


@dataclass
class ToolContext:
    view: TenantView
    ledger: dict[str, dict] = field(default_factory=dict)
    proposals: list[dict] = field(default_factory=list)

    def remember(self, evidence: list[dict]) -> list[str]:
        for e in evidence:
            prior = self.ledger.get(e["id"])
            self.ledger[e["id"]] = {**e, "fields": {**prior["fields"], **e["fields"]}} if prior else e
        return [e["id"] for e in evidence]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


class FindDevicesArgs(Args):
    os_name: Literal["macOS", "Windows"] | None = None
    os_version_below: str | None = Field(None, description="e.g. '15' for macOS older than 15, '11' for Windows 10")
    form_factor: Literal["laptop", "desktop"] | None = None
    model_name: str | None = None
    failing_check: Check | None = None
    failing_severity: Literal["high", "medium", "low"] | None = None
    disk_free_below_pct: float | None = None
    mem_used_above_pct: float | None = None
    employee_id: str | None = None
    software: str | None = Field(None, description="Installed application name, exact match")


class DeviceArgs(Args):
    device_id: str


class HistoryArgs(Args):
    device_id: str
    metric: Literal["disk_free_pct", "mem_used_pct", "battery", "compliance", "software"]


class InsightArgs(Args):
    kind: Literal[
        "all", "storage_low", "ram_constrained", "battery_eol", "compliance_drift",
        "os_outdated", "unapproved_software", "telemetry_gaps",
    ] = "all"


class ComplianceArgs(Args):
    check_id: Check | None = None


class UpgradeArgs(Args):
    device_id: str
    component: Literal["ram", "storage", "battery"]
    spec: str = Field(max_length=200, description="Target spec, e.g. '32 GB DDR5' or '1 TB NVMe'")
    evidence_ids: list[str] = EvidenceIds


class RemediationArgs(Args):
    device_id: str
    check_id: Check
    note: str = Field(max_length=500)
    evidence_ids: list[str] = EvidenceIds


class ReplacementArgs(Args):
    device_id: str
    reason: str = Field(max_length=500)
    evidence_ids: list[str] = EvidenceIds


class NotifyArgs(Args):
    employee_id: str
    message: str = Field(max_length=600)
    evidence_ids: list[str] = EvidenceIds


class AnswerFinding(Args):
    statement: str
    device_ids: list[str] = Field(description="Devices this statement is about; empty for fleet-level statements")
    evidence_ids: list[str] = Field(min_length=1)


class SubmitAnswerArgs(Args):
    status: Literal["answered", "refused", "insufficient_data"]
    summary: str
    findings: list[AnswerFinding]
    caveats: list[str] = []


class SkillArgs(Args):
    name: Literal[tuple(skills.all_skills())]


class MakePlanArgs(Args):
    intent: Literal["question", "insight", "action", "out_of_scope"]
    steps: list[str] = Field(min_length=1, max_length=6)
    tools: list[str] = Field(description="Tools you expect to call, in order")


def _device_row(s) -> dict:
    return {
        "device_id": s.device_id,
        "employee_id": s.employee_id,
        "model": s.model_name,
        "form_factor": s.form_factor,
        "os": s.os_label,
        "ram_gb": s.ram_gb,
        "disk_free_pct": s.disk_free_pct,
        "disk_free_gb": s.disk_free_gb,
        "mem_used_pct": s.mem_used_pct,
        "failing_checks": s.failing_checks,
        "collected_at": s.collected_at,
    }


def _not_found(ctx: ToolContext, key: str, value: str) -> dict:
    e = _query_evidence("lookup", {key: value}, status="not_found", note="Not in this tenant's fleet")
    ctx.remember([e])
    return {"status": "not_found", key: value, "note": "Not in your tenant's fleet.", "evidence_id": e["id"]}


def _query_evidence(name: str, args: dict, **fields) -> dict:
    digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:8]
    return {"id": f"ev:query:{name}:{digest}", "device_id": None, "fields": {"query": args, **fields},
            "source": "derived from latest snapshot per device"}


COLLECTED_METRICS = [
    "hardware model, processor, RAM size", "OS name and version", "memory used/free", "disk size and free space",
    "disk encryption", "battery condition, cycle count, full-charge capacity, charge percentage",
    "installed software (name, version, publisher)", "network interfaces",
    "compliance checks: disk_encryption (high), screen_lock (medium), os_up_to_date (medium)",
]
NOT_COLLECTED = ["CPU usage", "GPU", "network traffic", "user activity", "location", "warranty", "purchase cost"]


def fleet_overview(ctx: ToolContext, a: NoArgs) -> dict:
    latest = ctx.view.latest_all()
    counts = {
        "devices": len(latest),
        "employees": len(ctx.view.employee_ids()),
        "by_model": dict(sorted(Counter(s.model_name for s in latest).items())),
        "by_os": dict(sorted(Counter(s.os_label for s in latest).items())),
        "by_form_factor": dict(sorted(Counter(s.form_factor for s in latest).items())),
        "collected_metrics": COLLECTED_METRICS,
        "not_collected": NOT_COLLECTED,
    }
    ev = _query_evidence("fleet_overview", {}, **counts)
    return {"result": {**counts, "tenant": ctx.view.name, "data_as_of": ctx.view.data_as_of()},
            "evidence_ids": ctx.remember([ev])}


def find_devices(ctx: ToolContext, a: FindDevicesArgs) -> dict:
    rows, evidence = [], []
    below = parse_version(a.os_version_below) if a.os_version_below else None
    for s in ctx.view.latest_all():
        if a.os_name and s.os_name != a.os_name:
            continue
        if below and parse_version(s.os_version) >= below:
            continue
        if a.form_factor and s.form_factor != a.form_factor:
            continue
        if a.model_name and s.model_name.lower() != a.model_name.lower():
            continue
        if a.employee_id and s.employee_id != a.employee_id:
            continue
        if a.disk_free_below_pct is not None and s.disk_free_pct >= a.disk_free_below_pct:
            continue
        if a.mem_used_above_pct is not None and s.mem_used_pct <= a.mem_used_above_pct:
            continue
        if a.software and not any(n.lower() == a.software.lower() for n, _ in s.software):
            continue
        failing = s.failing_checks
        if a.failing_check and a.failing_check not in failing:
            continue
        if a.failing_severity and a.failing_severity not in failing.values():
            continue
        e = snapshot_evidence(s, model=s.model_name, form_factor=s.form_factor, os=s.os_label,
                              disk_free_pct=s.disk_free_pct, mem_used_pct=s.mem_used_pct, failing_checks=failing)
        evidence.append(e)
        rows.append({**_device_row(s), "evidence_id": e["id"]})
    filters = a.model_dump(exclude_none=True)
    q = _query_evidence("find_devices", filters, match_count=len(rows), device_ids=[r["device_id"] for r in rows],
                        devices_scanned=len(ctx.view.device_ids()))
    ctx.remember(evidence + [q])
    return {"result": {"match_count": len(rows), "devices_scanned": len(ctx.view.device_ids()), "filters": filters,
                       "devices": rows, "query_evidence_id": q["id"]}}


def get_device(ctx: ToolContext, a: DeviceArgs) -> dict:
    s = ctx.view.latest(a.device_id)
    if not s:
        return {"result": _not_found(ctx, "device_id", a.device_id)}
    b = s.battery or {}
    detail = {
        **_device_row(s),
        "hostname": s.hostname,
        "processor": s.processor,
        "disk_size_gb": s.disk_size_gb,
        "disk_encrypted": s.disk_encrypted,
        "battery": {k: b[k] for k in ("condition", "cycle_count", "full_charge_capacity", "percentage")} if b else None,
        "software": [f"{n} {v}" for n, v in s.software],
        "checks": {c: {"status": st, "severity": sev} for c, (st, sev) in sorted(s.checks.items())},
    }
    e = snapshot_evidence(s, **{k: v for k, v in detail.items() if k not in ("collected_at", "device_id")})
    return {"result": {**detail, "evidence_id": e["id"]}, "evidence_ids": ctx.remember([e])}


def get_device_history(ctx: ToolContext, a: HistoryArgs) -> dict:
    h = ctx.view.history(a.device_id)
    if not h:
        return {"result": _not_found(ctx, "device_id", a.device_id)}

    def value(s):
        if a.metric == "battery":
            return {k: s.battery[k] for k in ("condition", "cycle_count", "full_charge_capacity")} if s.battery else None
        if a.metric == "compliance":
            return {c: st for c, (st, _) in sorted(s.checks.items())}
        if a.metric == "software":
            return [n for n, _ in s.software]
        return getattr(s, a.metric)

    series = [{"date": s.date, "value": value(s)} for s in h]
    e = trend_evidence(f"history-{a.metric}", h, first=series[0]["value"], last=series[-1]["value"])
    return {"result": {"device_id": a.device_id, "metric": a.metric, "series": series, "evidence_id": e["id"]},
            "evidence_ids": ctx.remember([e])}


def detect_insights(ctx: ToolContext, a: InsightArgs) -> dict:
    findings = policy.detect(ctx.view, a.kind)
    out = []
    for f in findings:
        out.append({k: v for k, v in f.items() if k != "evidence"} | {"evidence_ids": ctx.remember(f["evidence"])})
    return {"result": {"kind": a.kind, "devices_scanned": len(ctx.view.device_ids()), "finding_count": len(out),
                       "findings": out, "thresholds": {
                           "low_disk_pct": policy.LOW_DISK_PCT, "ram_pressure_pct": policy.RAM_PRESSURE_PCT,
                           "sustained_share": policy.SUSTAINED_SHARE, "battery_cycle_limit": policy.BATTERY_CYCLE_LIMIT,
                           "battery_capacity_drop_pct": policy.BATTERY_CAPACITY_DROP_PCT}}}


def compliance_summary(ctx: ToolContext, a: ComplianceArgs) -> dict:
    latest = ctx.view.latest_all()
    checks = [a.check_id] if a.check_id else sorted(policy.CHECKS)
    by_date: dict[str, list] = {}
    for d in ctx.view.device_ids():
        for s in ctx.view.history(d):
            by_date.setdefault(s.date, []).append(s)
    out, evidence = {}, []
    for c in checks:
        failing = sorted(s.device_id for s in latest if s.checks[c][0] == "fail")
        daily = {day: round(sum(s.checks[c][0] == "pass" for s in snaps) / len(snaps), 2)
                 for day, snaps in sorted(by_date.items())}
        days = list(daily.values())
        summary = {
            "severity": latest[0].checks[c][1],
            "failing_now": failing,
            "pass_rate_now": round(1 - len(failing) / len(latest), 2),
            "pass_rate_first_7_days": round(sum(days[:7]) / 7, 2),
            "pass_rate_last_7_days": round(sum(days[-7:]) / 7, 2),
        }
        e = _query_evidence("compliance_summary", {"check_id": c}, **summary)
        evidence.append(e)
        out[c] = {**summary, "evidence_id": e["id"]}
    return {"result": {"devices": len(latest), "checks": out}, "evidence_ids": ctx.remember(evidence)}


def load_skill(ctx: ToolContext, a: SkillArgs) -> dict:
    s = skills.all_skills()[a.name]
    return {"result": {"skill": s["name"], "applies_to": s["applies_to"], "instructions": s["body"]}}


def _propose(ctx: ToolContext, tool: str, args: BaseModel, targets: list[str], check, hint: str) -> dict:
    cited = args.evidence_ids
    unknown = [i for i in cited if i not in ctx.ledger]
    if unknown:
        return {"status": "refused", "reason": f"Evidence ids not returned by any tool in this session: {unknown}."}
    if not any(ctx.ledger[i].get("device_id") in targets for i in cited):
        return {"status": "refused", "reason": f"None of the cited evidence is about {targets}. Call {hint} first."}
    ok, reason, support = check()
    if not ok:
        return {"status": "refused", "reason": reason}
    if support and not set(cited) & set(support):
        return {"status": "refused",
                "reason": f"Cited evidence does not show the problem. Call {hint} and cite its evidence ids."}
    payload = args.model_dump(exclude={"evidence_ids"})
    key = json.dumps({"tool": tool, **{k: v for k, v in payload.items()
                                       if k in ("device_id", "employee_id", "component", "check_id")}}, sort_keys=True)
    for p in ctx.proposals:
        if p["dedupe_key"] == key:
            return {"status": "already_proposed", "proposal_id": p["id"]}
    if len(ctx.proposals) >= MAX_PROPOSALS_PER_TURN:
        return {"status": "refused", "reason": f"At most {MAX_PROPOSALS_PER_TURN} proposals per request."}
    proposal = {"id": f"P-{len(ctx.proposals) + 1}", "tool": tool, "risk": RISK[tool], "args": payload,
                "evidence_ids": sorted(cited),
                "justification": reason, "dedupe_key": key}
    ctx.proposals.append(proposal)
    return {"status": "queued_for_approval", "proposal_id": proposal["id"], "justification": reason,
            "note": "Nothing has executed. A human administrator must approve this proposal."}


def create_upgrade_order(ctx: ToolContext, a: UpgradeArgs) -> dict:
    if not ctx.view.owns(a.device_id):
        return {"result": _not_found(ctx, "device_id", a.device_id)}
    return {"result": _propose(ctx, "create_upgrade_order", a, [a.device_id],
                               lambda: policy.check_upgrade(ctx.view, a.device_id, a.component),
                               "detect_insights")}


def open_remediation_ticket(ctx: ToolContext, a: RemediationArgs) -> dict:
    if not ctx.view.owns(a.device_id):
        return {"result": _not_found(ctx, "device_id", a.device_id)}
    return {"result": _propose(ctx, "open_remediation_ticket", a, [a.device_id],
                               lambda: policy.check_remediation(ctx.view, a.device_id, a.check_id),
                               "get_device or find_devices")}


def flag_device_for_replacement(ctx: ToolContext, a: ReplacementArgs) -> dict:
    if not ctx.view.owns(a.device_id):
        return {"result": _not_found(ctx, "device_id", a.device_id)}
    return {"result": _propose(ctx, "flag_device_for_replacement", a, [a.device_id],
                               lambda: policy.check_replacement(ctx.view, a.device_id), "detect_insights")}


def notify_employee(ctx: ToolContext, a: NotifyArgs) -> dict:
    devices = ctx.view.devices_of(a.employee_id)
    if not devices:
        return {"result": _not_found(ctx, "employee_id", a.employee_id)}
    return {"result": _propose(ctx, "notify_employee", a, devices,
                               lambda: policy.check_notify(ctx.view, a.employee_id),
                               "detect_insights or get_device for their devices")}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[Args]
    fn: Callable[[ToolContext, Args], dict] | None
    kind: Literal["read", "action", "control"]

    @property
    def risk(self) -> str:
        return RISK.get(self.name, "none")


TOOLS = {t.name: t for t in [
    Tool("fleet_overview", "Counts of devices by model, OS and form factor for your tenant, with data-as-of time.",
         NoArgs, fleet_overview, "read"),
    Tool("find_devices", "Filter devices by their LATEST snapshot. All filters are optional and combined with AND. "
         "Use for inventory and point-in-time questions (OS version, low disk now, failing checks, owner).",
         FindDevicesArgs, find_devices, "read"),
    Tool("get_device", "Full latest snapshot of one device: hardware, OS, disk, memory, battery, software, checks.",
         DeviceArgs, get_device, "read"),
    Tool("get_device_history", "Daily time series of one metric for one device over the 30-day window.",
         HistoryArgs, get_device_history, "read"),
    Tool("detect_insights", "Run deterministic trend detectors over the 30-day window: storage_low, ram_constrained, "
         "battery_eol, compliance_drift, os_outdated, unapproved_software, telemetry_gaps (or all). "
         "Each finding has the finding, an explanation and evidence ids.", InsightArgs, detect_insights, "read"),
    Tool("compliance_summary", "Per-check pass rate now and first-vs-last week, and which devices fail now.",
         ComplianceArgs, compliance_summary, "read"),
    Tool("load_skill", "Load the full procedure for a kind of work (see the skills list in your instructions). "
         "Load it before doing that work.", SkillArgs, load_skill, "read"),
    Tool("create_upgrade_order", "PROPOSE a hardware upgrade order (ram, storage or battery). Requires evidence of "
         "the problem and an upgradeable model. Queued for human approval; never executes directly.",
         UpgradeArgs, create_upgrade_order, "action"),
    Tool("open_remediation_ticket", "PROPOSE a remediation ticket for a compliance check failing on the latest "
         "snapshot. Queued for human approval.", RemediationArgs, open_remediation_ticket, "action"),
    Tool("flag_device_for_replacement", "PROPOSE flagging a device for replacement. Requires a hardware finding that "
         "an upgrade cannot fix (battery end of life, or RAM/storage not upgradeable). Queued for human approval.",
         ReplacementArgs, flag_device_for_replacement, "action"),
    Tool("notify_employee", "PROPOSE a message to an employee about an active finding on one of their devices. "
         "Queued for human approval.", NotifyArgs, notify_employee, "action"),
    Tool("submit_answer", "Submit the final answer. Call exactly once, last. Every finding must cite evidence ids "
         "returned by tools in this conversation.", SubmitAnswerArgs, None, "control"),
]}

PLAN_TOOL = Tool("make_plan", "Record your plan before using any data tool.", MakePlanArgs, None, "control")


def _clean(schema: dict, defs: dict | None = None) -> dict:
    defs = schema.get("$defs", defs) or {}
    if "$ref" in schema:
        return _clean(defs[schema["$ref"].split("/")[-1]], defs)
    out = {}
    for k, v in schema.items():
        if k in ("title", "$defs") or (k == "default" and v is None):
            continue
        if k == "anyOf":
            options = [o for o in v if o.get("type") != "null"]
            if len(options) == 1:
                out.update(_clean(options[0], defs))
                continue
            v = [_clean(o, defs) for o in options]
        elif k == "properties":
            v = {p: _clean(s, defs) for p, s in v.items()}
        elif k == "items" and isinstance(v, dict):
            v = _clean(v, defs)
        out[k] = v
    return out


def schema(tool: Tool) -> dict:
    return {"type": "function", "function": {
        "name": tool.name, "description": tool.description, "parameters": _clean(tool.args.model_json_schema())}}


def outcome(result: dict) -> str:
    if "error" in result:
        return "invalid_args" if result["error"] == "Invalid arguments" else "error"
    status = result.get("result", {}).get("status")
    return {"not_found": "not_found", "refused": "refused", "queued_for_approval": "queued",
            "already_proposed": "duplicate"}.get(status, "ok")


def run_tool(ctx: ToolContext, name: str, raw_args: dict) -> dict:
    tool = TOOLS.get(name)
    if not tool or not tool.fn:
        return {"error": f"Unknown tool '{name}'."}
    try:
        args = tool.args.model_validate(raw_args or {})
    except ValidationError as e:
        return {"error": "Invalid arguments", "details": json.loads(e.json(include_url=False, include_context=False))}
    return tool.fn(ctx, args)
