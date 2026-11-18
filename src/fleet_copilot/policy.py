from __future__ import annotations

from .data import Snapshot, TenantView, snapshot_evidence, trend_evidence

LOW_DISK_PCT = 10.0
LOW_DISK_SUSTAINED_PCT = 15.0
RAM_PRESSURE_PCT = 80.0
SUSTAINED_SHARE = 0.8
BATTERY_CYCLE_LIMIT = 1000
BATTERY_CAPACITY_DROP_PCT = 3.0
APPROVED_SOFTWARE = {"Google Chrome", "Slack", "Zoom", "1Password", "Visual Studio Code"}
CHECKS = {"disk_encryption", "screen_lock", "os_up_to_date"}

UPGRADEABLE = {
    "Dell XPS 15": {"ram", "storage", "battery"},
    "ThinkPad X1 Carbon": {"storage", "battery"},
    "MacBook Pro": {"battery"},
    "MacBook Air": {"battery"},
    "Mac mini": set(),
}

KINDS = (
    "storage_low",
    "ram_constrained",
    "battery_eol",
    "compliance_drift",
    "os_outdated",
    "unapproved_software",
    "telemetry_gaps",
)


def _finding(kind: str, s: Snapshot, finding: str, explanation: str, evidence: list[dict], **metrics) -> dict:
    latest = next((e for e in evidence if e["id"] == f"ev:snap:{s.device_id}:{s.date}"), evidence[-1])
    facts = {"model": s.model_name, "employee_id": s.employee_id, **metrics}
    latest["fields"] = {**latest["fields"], **{k: v for k, v in facts.items() if v is not None}}
    return {
        "kind": kind,
        "device_id": s.device_id,
        "hostname": s.hostname,
        "employee_id": s.employee_id,
        "model": s.model_name,
        "finding": finding,
        "explanation": explanation,
        "metrics": metrics,
        "evidence": evidence,
    }


def storage_low(h: list[Snapshot]) -> dict | None:
    last = h[-1]
    if last.disk_free_pct >= LOW_DISK_PCT:
        return None
    share = sum(s.disk_free_pct < LOW_DISK_SUSTAINED_PCT for s in h) / len(h)
    days = max((len(h) - 1), 1)
    slope = round((last.disk_free_gb - h[0].disk_free_gb) / days, 2)
    days_to_full = round(last.disk_free_gb / -slope) if slope < 0 else None
    sustained = share >= SUSTAINED_SHARE
    trend = f", losing {-slope} GB/day (~{days_to_full} days to full)" if days_to_full is not None else ""
    return _finding(
        "storage_low",
        last,
        f"{last.disk_free_pct}% disk free ({last.disk_free_gb} of {last.disk_size_gb} GB){trend}",
        f"Below {LOW_DISK_PCT:g}% free now and below {LOW_DISK_SUSTAINED_PCT:g}% in "
        f"{round(share * 100)}% of snapshots ({'sustained' if sustained else 'recent'}).",
        [
            snapshot_evidence(last, disk_free_pct=last.disk_free_pct, disk_free_gb=last.disk_free_gb),
            trend_evidence(
                "storage_low", h,
                first_free_gb=h[0].disk_free_gb, last_free_gb=last.disk_free_gb,
                share_below_sustained_pct=round(share, 2), slope_gb_per_day=slope,
            ),
        ],
        disk_free_pct=last.disk_free_pct, disk_free_gb=last.disk_free_gb, disk_size_gb=last.disk_size_gb,
        slope_gb_per_day=slope, days_to_full=days_to_full, sustained=sustained,
    )


def ram_constrained(h: list[Snapshot]) -> dict | None:
    share = sum(s.mem_used_pct >= RAM_PRESSURE_PCT for s in h) / len(h)
    if share < SUSTAINED_SHARE:
        return None
    last = h[-1]
    avg = round(sum(s.mem_used_pct for s in h) / len(h), 1)
    peak = max(s.mem_used_pct for s in h)
    return _finding(
        "ram_constrained",
        last,
        f"Memory use at or above {RAM_PRESSURE_PCT:g}% in {round(share * 100)}% of snapshots "
        f"(avg {avg}%, peak {peak}%) on {last.ram_gb} GB RAM",
        f"Sustained pressure: at least {SUSTAINED_SHARE:.0%} of snapshots over the threshold.",
        [
            snapshot_evidence(last, mem_used_pct=last.mem_used_pct, ram_gb=last.ram_gb),
            trend_evidence("ram_constrained", h, share_over_threshold=round(share, 2), avg_used_pct=avg, peak_used_pct=peak),
        ],
        share_over_threshold=round(share, 2), avg_used_pct=avg, peak_used_pct=peak, ram_gb=last.ram_gb,
    )


def battery_eol(h: list[Snapshot]) -> dict | None:
    with_batt = [s for s in h if s.battery]
    if not with_batt or with_batt[-1].form_factor != "laptop":
        return None
    first, last = with_batt[0], with_batt[-1]
    b0, b1 = first.battery, last.battery
    drop = round(100 * (b0["full_charge_capacity"] - b1["full_charge_capacity"]) / b0["full_charge_capacity"], 1)
    reasons, since = [], None
    if b1["condition"] != "Normal":
        since = next(s.date for s in with_batt if s.battery["condition"] == b1["condition"])
        reasons.append(f"condition '{b1['condition']}' since {since}")
    if b1["cycle_count"] >= BATTERY_CYCLE_LIMIT:
        reasons.append(f"{b1['cycle_count']} cycles (limit {BATTERY_CYCLE_LIMIT})")
    if drop >= BATTERY_CAPACITY_DROP_PCT:
        reasons.append(f"full-charge capacity down {drop}% over {len(with_batt)} snapshots")
    if not reasons:
        return None
    return _finding(
        "battery_eol",
        last,
        "Battery approaching end of life: " + "; ".join(reasons),
        "Any of: OS-reported condition not Normal, cycle count at the rated limit, or measurable capacity fade.",
        [
            snapshot_evidence(last, battery_condition=b1["condition"], cycle_count=b1["cycle_count"],
                              full_charge_capacity=b1["full_charge_capacity"]),
            trend_evidence("battery_eol", with_batt, first_capacity=b0["full_charge_capacity"],
                           last_capacity=b1["full_charge_capacity"], capacity_drop_pct=drop,
                           first_cycles=b0["cycle_count"], last_cycles=b1["cycle_count"]),
        ],
        condition=b1["condition"], condition_since=since, cycle_count=b1["cycle_count"],
        cycle_limit=BATTERY_CYCLE_LIMIT, capacity_drop_pct=drop,
    )


def compliance_drift(h: list[Snapshot]) -> list[dict]:
    out = []
    last = h[-1]
    for check in sorted(last.checks):
        status = [s.checks[check][0] for s in h]
        if status[-1] != "fail" or "pass" not in status:
            continue
        start = len(status) - 1
        while start > 0 and status[start - 1] == "fail":
            start -= 1
        streak = h[start:]
        out.append(_finding(
            "compliance_drift",
            last,
            f"{check} ({last.checks[check][1]}) passed earlier, failing since {streak[0].date} "
            f"({len(streak)} consecutive snapshots)",
            "Drift = a check that was passing and is now failing through the latest snapshot.",
            [
                snapshot_evidence(h[start - 1], **{check: "pass"}),
                snapshot_evidence(streak[0], **{check: "fail"}),
                snapshot_evidence(last, **{check: "fail"}),
            ],
            check_id=check, severity=last.checks[check][1], failing_since=streak[0].date,
            last_pass=h[start - 1].date, consecutive_failures=len(streak),
        ))
    return out


def os_outdated(h: list[Snapshot]) -> dict | None:
    last = h[-1]
    if last.checks.get("os_up_to_date", ("pass",))[0] != "fail":
        return None
    share = sum(s.checks["os_up_to_date"][0] == "fail" for s in h) / len(h)
    return _finding(
        "os_outdated",
        last,
        f"{last.os_label} fails os_up_to_date (failing in {round(share * 100)}% of snapshots)",
        "The agent reports the OS as not current; persistent failures need a patch plan.",
        [snapshot_evidence(last, os=last.os_label, os_up_to_date="fail")],
        os=last.os_label, share_failing=round(share, 2),
    )


def unapproved_software(h: list[Snapshot]) -> list[dict]:
    last = h[-1]
    out = []
    for name, version in last.software:
        if name in APPROVED_SOFTWARE:
            continue
        first = next(s for s in h if any(n == name for n, _ in s.software))
        out.append(_finding(
            "unapproved_software",
            last,
            f"{name} {version} installed (first seen {first.date})",
            "Not on the approved software list.",
            [snapshot_evidence(first, installed=name), snapshot_evidence(last, installed=name)],
            software=name, version=version, approved=False, first_seen=first.date,
        ))
    return out


def telemetry_gaps(h: list[Snapshot]) -> dict | None:
    last = h[-1]
    no_batt = [s.date for s in h if last.form_factor == "laptop" and not s.battery]
    no_net = [s.date for s in h if not s.network_reported]
    if not no_batt and not no_net:
        return None
    parts = []
    if no_batt:
        parts.append(f"battery block missing on {len(no_batt)} snapshots ({', '.join(no_batt)})")
    if no_net:
        parts.append(f"network missing on {len(no_net)} snapshots")
    return _finding(
        "telemetry_gaps",
        last,
        "Telemetry gaps: " + "; ".join(parts),
        "Treat as missing data, not as a removed battery or an offline device; conclusions on these dates are weaker.",
        [trend_evidence("telemetry_gaps", h, missing_battery_dates=no_batt, missing_network_count=len(no_net))],
        missing_battery=len(no_batt), missing_network=len(no_net),
    )


def detect(view: TenantView, kind: str = "all") -> list[dict]:
    kinds = KINDS if kind == "all" else (kind,)
    out = []
    for device_id in view.device_ids():
        h = view.history(device_id)
        for k in kinds:
            r = DETECTORS[k](h)
            out.extend(r if isinstance(r, list) else [r] if r else [])
    return out


DETECTORS = {
    "storage_low": storage_low,
    "ram_constrained": ram_constrained,
    "battery_eol": battery_eol,
    "compliance_drift": compliance_drift,
    "os_outdated": os_outdated,
    "unapproved_software": unapproved_software,
    "telemetry_gaps": telemetry_gaps,
}

HARDWARE = {"storage": storage_low, "ram": ram_constrained, "battery": battery_eol}


def _evidence_ids(f: dict | None) -> list[str]:
    return [e["id"] for e in f["evidence"]] if f else []


def _proof(f: dict) -> list[str]:
    return [e["id"] for e in f["evidence"] if e["id"].startswith("ev:trend:")]


def check_upgrade(view: TenantView, device_id: str, component: str) -> tuple[bool, str, list[str]]:
    h = view.history(device_id)
    finding = HARDWARE[component](h)
    if not finding:
        return False, f"No {component} problem in telemetry for {device_id}; an upgrade is not justified.", []
    model = h[-1].model_name
    if component not in UPGRADEABLE.get(model, set()):
        return False, (f"{component} is not field-upgradeable on {model}; "
                       "consider flag_device_for_replacement instead."), []
    return True, finding["finding"], _proof(finding)


def check_replacement(view: TenantView, device_id: str) -> tuple[bool, str, list[str]]:
    h = view.history(device_id)
    model = h[-1].model_name
    reasons, ids, fixable = [], [], []
    for component, detector in HARDWARE.items():
        f = detector(h)
        if not f:
            continue
        if component == "battery" or component not in UPGRADEABLE.get(model, set()):
            reasons.append(f["finding"])
            ids += _proof(f)
        else:
            fixable.append(component)
    if reasons:
        return True, "; ".join(reasons), ids
    if fixable:
        return False, f"Issues are fixable by upgrade ({', '.join(fixable)}) on {model}; use create_upgrade_order.", []
    return False, f"No hardware finding (battery, RAM, storage) supports replacing {device_id}.", []


def check_remediation(view: TenantView, device_id: str, check_id: str) -> tuple[bool, str, list[str]]:
    if check_id not in CHECKS:
        return False, f"Unknown check_id '{check_id}'. Known: {', '.join(sorted(CHECKS))}.", []
    last = view.latest(device_id)
    status, severity = last.checks[check_id]
    if status != "fail":
        return False, f"{check_id} passes on the latest snapshot ({last.date}); nothing to remediate.", []
    return True, f"{check_id} ({severity}) failing on {last.date}", [f"ev:snap:{device_id}:{last.date}"]


def check_notify(view: TenantView, employee_id: str) -> tuple[bool, str, list[str]]:
    reasons, ids = [], []
    for device_id in view.devices_of(employee_id):
        h = view.history(device_id)
        for k in ("storage_low", "ram_constrained", "battery_eol", "unapproved_software", "compliance_drift"):
            r = DETECTORS[k](h)
            for f in r if isinstance(r, list) else [r] if r else []:
                reasons.append(f"{device_id}: {f['finding']}")
                ids += _evidence_ids(f)
        for check in sorted(h[-1].failing_checks):
            ids.append(f"ev:snap:{device_id}:{h[-1].date}")
            reasons.append(f"{device_id}: {check} failing")
    if not reasons:
        return False, f"No active finding on any device of {employee_id}; a notification is not justified.", []
    return True, "; ".join(reasons), sorted(set(ids))
