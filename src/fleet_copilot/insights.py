from __future__ import annotations

import argparse
import json
from datetime import date as Date
from pathlib import Path

from . import audit, policy
from .data import TenantView, all_tenants

SEVERITY = ("critical", "high", "medium", "low")
CRITICAL_DAYS_TO_FULL = 7
CRITICAL_FREE_PCT = 3.0
RISKY_SOFTWARE = {"uTorrent": "file-sharing", "TeamViewer": "remote access"}
CHECK_LABEL = {"disk_encryption": "Disk encryption", "screen_lock": "Screen lock", "os_up_to_date": "OS updates"}


def _day(iso: str) -> str:
    return Date.fromisoformat(iso[:10]).strftime("%-d %b")


def _device(view: TenantView, device_id: str) -> dict:
    s = view.latest(device_id)
    return {"id": device_id, "hostname": s.hostname, "model": s.model_name, "owner": s.employee_id, "os": s.os_label}


def _severity(f: dict) -> str:
    m, kind = f["metrics"], f["kind"]
    if kind == "storage_low":
        days = m.get("days_to_full")
        critical = m["disk_free_pct"] < CRITICAL_FREE_PCT or (days is not None and days <= CRITICAL_DAYS_TO_FULL)
        return "critical" if critical else "high"
    if kind == "compliance_drift":
        return {"disk_encryption": "critical", "screen_lock": "high"}.get(m["check_id"], "medium")
    if kind == "unapproved_software":
        return "high" if m["software"] in RISKY_SOFTWARE else "medium"
    return {"battery_eol": "high", "ram_constrained": "medium", "os_outdated": "medium",
            "telemetry_gaps": "low"}[kind]


def _text(view: TenantView, f: dict) -> tuple[str, str]:
    m, kind, s = f["metrics"], f["kind"], view.latest(f["device_id"])
    if kind == "storage_low":
        eta = f", full in about {m['days_to_full']} days" if m.get("days_to_full") is not None else ""
        return "Disk almost full", f"{round(m['disk_free_pct'])}% free (about {round(m['disk_free_gb'])} GB){eta}."
    if kind == "ram_constrained":
        return "Not enough memory", (f"Memory was over 80% full in {round(m['share_over_threshold'] * 100)}% of daily "
                                     f"readings (average {round(m['avg_used_pct'])}%) on {m['ram_gb']} GB.")
    if kind == "battery_eol":
        bits = [f"battery health is '{m['condition']}'" + (f" since {_day(m['condition_since'])}"
                                                           if m.get("condition_since") else "")
                ] if m["condition"] != "Normal" else []
        if m["cycle_count"] >= m["cycle_limit"]:
            bits.append(f"{m['cycle_count']} charge cycles")
        if m["capacity_drop_pct"] >= policy.BATTERY_CAPACITY_DROP_PCT:
            bits.append(f"capacity down {m['capacity_drop_pct']:g}%")
        text = "; ".join(bits)
        return "Battery wearing out", text[0].upper() + text[1:] + "."
    if kind == "compliance_drift":
        return (f"{CHECK_LABEL[m['check_id']]} turned off",
                f"Passing until {_day(m['last_pass'])}, failing since {_day(m['failing_since'])}.")
    if kind == "os_outdated":
        return "OS updates pending", f"Running {s.os_label}; the OS update check fails."
    if kind == "unapproved_software":
        category = RISKY_SOFTWARE.get(m["software"], "not on the approved list")
        return (f"Unapproved software: {m['software']}",
                f"{m['software']} ({category}) installed since {_day(m['first_seen'])}.")
    return "Telemetry gaps", "Some daily readings are missing battery or network data; treat trends with care."


def _suggestion(view: TenantView, f: dict) -> dict | None:
    d, m, kind = f["device_id"], f["metrics"], f["kind"]
    s = view.latest(d)
    name = f"{s.hostname} ({d})"
    if kind in ("storage_low", "ram_constrained", "battery_eol"):
        component = {"storage_low": "storage", "ram_constrained": "ram", "battery_eol": "battery"}[kind]
        if policy.check_upgrade(view, d, component)[0]:
            label = {"storage": "Order a bigger SSD", "ram": "Order a RAM upgrade",
                     "battery": "Order a replacement battery"}[component]
            return {"label": label, "request": f"{label} for {name}. Check the evidence first."}
        if kind == "storage_low" and policy.check_notify(view, s.employee_id)[0]:
            return {"label": "Ask the owner to free up space",
                    "request": f"Notify the owner of {name} that the disk is almost full and ask them to free up space."}
        if policy.check_replacement(view, d)[0]:
            return {"label": "Flag for replacement",
                    "request": f"Flag {name} for replacement; the {component} cannot be upgraded on this model."}
        return None
    if kind in ("compliance_drift", "os_outdated"):
        check = m.get("check_id", "os_up_to_date")
        if policy.check_remediation(view, d, check)[0]:
            return {"label": "Open a remediation ticket",
                    "request": f"Open a remediation ticket for {name}: {CHECK_LABEL[check].lower()} is failing."}
        return None
    if kind == "unapproved_software" and policy.check_notify(view, s.employee_id)[0]:
        return {"label": "Ask the owner to remove it",
                "request": f"Notify the owner of {name} to remove {m['software']}, which is not approved."}
    return None


def _key(f: dict) -> str:
    m = f["metrics"]
    return ":".join(str(x) for x in (f["kind"], f["device_id"], m.get("check_id") or m.get("software") or ""))


def _encryption_off(view: TenantView) -> list[dict]:
    out = []
    for s in view.latest_all():
        if s.checks.get("disk_encryption", ("pass",))[0] == "fail":
            out.append({"key": f"encryption_off:{s.device_id}", "kind": "encryption_off", "severity": "critical",
                        "title": "Disk encryption is off", "why": f"Disk encryption fails on the {_day(s.date)} reading.",
                        "device": _device(view, s.device_id), "evidence_ids": [f"ev:snap:{s.device_id}:{s.date}"],
                        "suggestion": {"label": "Open a remediation ticket",
                                       "request": f"Open a remediation ticket for {s.hostname} ({s.device_id}): "
                                                  "disk encryption is off."}})
    return out


def briefing(tenant: str, as_of: str | None = None, previous: dict | None = None) -> dict:
    view = TenantView(tenant, as_of=as_of)
    seen = {i["key"] for i in previous["issues"]} if previous else None
    issues = _encryption_off(view)
    for f in policy.detect(view):
        title, why = _text(view, f)
        issues.append({"key": _key(f), "kind": f["kind"], "severity": _severity(f), "title": title, "why": why,
                       "device": _device(view, f["device_id"]), "evidence_ids": [e["id"] for e in f["evidence"]],
                       "suggestion": _suggestion(view, f)})
    for i in issues:
        i["new"] = None if seen is None else i["key"] not in seen
    issues.sort(key=lambda i: (SEVERITY.index(i["severity"]), not i["new"], i["kind"], i["device"]["hostname"]))
    day = view.data_as_of()[:10]
    return {"tenant": tenant, "date": day, "data_as_of": view.data_as_of(),
            "counts": {s: sum(i["severity"] == s for i in issues) for s in SEVERITY},
            "new_urgent": sum(bool(i["new"]) and i["severity"] in ("critical", "high") for i in issues),
            "issues": issues}


def briefing_dir(tenant: str) -> Path:
    d = audit.runs_dir() / "briefings" / tenant
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(tenant: str, day: str) -> dict | None:
    path = briefing_dir(tenant) / f"{day}.json"
    return json.loads(path.read_text()) if path.exists() else None


def days(tenant: str) -> list[dict]:
    out = []
    for p in sorted(briefing_dir(tenant).glob("*.json"), reverse=True):
        b = json.loads(p.read_text())
        out.append({"date": b["date"], "counts": b["counts"], "new_urgent": b["new_urgent"]})
    return out


def run(tenant: str, as_of: str | None = None) -> dict:
    view_day = TenantView(tenant, as_of=as_of).data_as_of()[:10]
    earlier = [d["date"] for d in days(tenant) if d["date"] < view_day]
    b = briefing(tenant, as_of=as_of, previous=load(tenant, earlier[0]) if earlier else None)
    (briefing_dir(tenant) / f"{b['date']}.json").write_text(json.dumps(b, indent=1, sort_keys=True))
    meta = {"session": "daily-briefing", "tenant": tenant, "actor": "system"}
    audit.log("briefing_generated", **meta, date=b["date"], counts=b["counts"])
    for i in b["issues"]:
        if i["new"] and i["severity"] in ("critical", "high"):
            audit.log("high_severity_flagged", **meta, date=b["date"], issue=i["key"], severity=i["severity"],
                      title=i["title"], device_id=i["device"]["id"])
    return b


def backfill(tenant: str) -> list[str]:
    view = TenantView(tenant)
    dates = sorted({s.date for d in view.device_ids() for s in view.history(d)})
    return [run(tenant, as_of=d)["date"] for d in dates]


def ensure(tenants: list[str] | None = None) -> None:
    for t in tenants or sorted(all_tenants()):
        if not days(t):
            backfill(t)
        elif not load(t, TenantView(t).data_as_of()[:10]):
            run(t)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet-copilot-briefing", description="Generate the daily Insights briefing")
    ap.add_argument("--tenant", default="all")
    ap.add_argument("--date", help="as-of date YYYY-MM-DD (default: latest telemetry)")
    ap.add_argument("--backfill", action="store_true", help="generate one briefing per day of telemetry")
    args = ap.parse_args(argv)
    tenants = sorted(all_tenants()) if args.tenant == "all" else [args.tenant]
    for t in tenants:
        if args.backfill:
            print(f"{t}: {len(backfill(t))} daily briefings")
        else:
            b = run(t, as_of=args.date)
            print(f"{t} {b['date']}: {b['counts']} ({b['new_urgent']} new critical/high)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
