from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = Path(os.environ.get("FLEET_DATA", ROOT / "data" / "device-telemetry.ndjson"))
SOURCE_NAME = "data/device-telemetry.ndjson"

FORM_FACTOR = {
    "MacBook Pro": "laptop",
    "MacBook Air": "laptop",
    "ThinkPad X1 Carbon": "laptop",
    "Dell XPS 15": "laptop",
    "Mac mini": "desktop",
}


def tenant_name(company_id: str) -> str:
    return company_id.split("-")[0].title()


MAX_TEXT = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")


def clean_text(value) -> str:
    return " ".join(_CONTROL.sub(" ", str(value)).split())[:MAX_TEXT]


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text))


@dataclass(frozen=True)
class Snapshot:
    line: int
    device_id: str
    company_id: str
    employee_id: str
    collected_at: str
    os_name: str
    os_version: str
    hostname: str
    model_name: str
    processor: str
    ram_gb: int
    mem_used_pct: float
    disk_size_gb: float
    disk_free_gb: float
    disk_free_pct: float
    disk_encrypted: bool
    battery: dict | None
    network_reported: bool
    software: tuple[tuple[str, str], ...]
    checks: dict[str, tuple[str, str]] = field(hash=False)

    @property
    def date(self) -> str:
        return self.collected_at[:10]

    @property
    def form_factor(self) -> str:
        return FORM_FACTOR.get(self.model_name, "unknown")

    @property
    def os_label(self) -> str:
        return f"{self.os_name} {self.os_version}"

    @property
    def failing_checks(self) -> dict[str, str]:
        return {c: sev for c, (status, sev) in self.checks.items() if status == "fail"}

    @property
    def source(self) -> str:
        return f"{SOURCE_NAME}#L{self.line}"


def _snapshot(line: int, r: dict) -> Snapshot:
    mem = r["memory"]
    vol = r["disk_volumes"][0]
    gb = 2**30
    return Snapshot(
        line=line,
        device_id=r["device_id"],
        company_id=r["company_id"],
        employee_id=r["employee_id"],
        collected_at=r["collected_at"],
        os_name=clean_text(r["os"]["product_name"]),
        os_version=clean_text(r["os"]["product_version"]),
        hostname=clean_text(r["os"]["hostname"]),
        model_name=clean_text(r["device_identity"]["model_name"]),
        processor=clean_text(r["device_identity"]["processor"]),
        ram_gb=round(mem["total_memory_bytes"] / 2**30),
        mem_used_pct=round(100 * mem["used_memory_bytes"] / mem["total_memory_bytes"], 1),
        disk_size_gb=round(vol["size_bytes"] / gb, 1),
        disk_free_gb=round(vol["available_bytes"] / gb, 1),
        disk_free_pct=round(100 * vol["available_bytes"] / vol["size_bytes"], 1),
        disk_encrypted=bool(vol["encrypted"]),
        battery=r.get("battery"),
        network_reported=bool(r.get("network")),
        software=tuple(sorted((clean_text(s["name"]), clean_text(s["version"])) for s in r["installed_software"])),
        checks={c["check_id"]: (c["status"], c["severity"]) for c in r["compliance_results"]},
    )


@lru_cache(maxsize=4)
def load_rows(path: Path = DATA_PATH) -> dict[str, dict[str, list[Snapshot]]]:
    rows: dict[str, dict[str, list[Snapshot]]] = defaultdict(lambda: defaultdict(list))
    with open(path) as f:
        for n, raw in enumerate(f, start=1):
            if raw.strip():
                s = _snapshot(n, json.loads(raw))
                rows[s.company_id][s.device_id].append(s)
    for devices in rows.values():
        for snaps in devices.values():
            snaps.sort(key=lambda s: s.collected_at)
    return {c: dict(d) for c, d in rows.items()}


def all_tenants(path: Path = DATA_PATH) -> set[str]:
    return set(load_rows(path))


def fleet_identifiers(path: Path = DATA_PATH) -> dict[str, set[str]]:
    rows = load_rows(path)
    latest = [h[-1] for devices in rows.values() for h in devices.values()]
    return {"devices": {s.device_id for s in latest}, "employees": {s.employee_id for s in latest},
            "hostnames": {s.hostname for s in latest}}


def contested_devices(path: Path = DATA_PATH) -> set[str]:
    seen: dict[str, int] = defaultdict(int)
    for devices in load_rows(path).values():
        for d in devices:
            seen[d] += 1
    return {d for d, n in seen.items() if n > 1}


class UnknownTenant(ValueError):
    pass


class TenantView:
    def __init__(self, tenant_id: str, path: Path = DATA_PATH, as_of: str | None = None):
        devices = load_rows(path).get(tenant_id)
        if not devices:
            raise UnknownTenant(tenant_id)
        self.tenant_id = tenant_id
        self.as_of = as_of
        cut = [(d, [s for s in h if as_of is None or s.date <= as_of]) for d, h in devices.items()]
        self._history = {d: h for d, h in cut if h}

    @property
    def name(self) -> str:
        return tenant_name(self.tenant_id)

    def device_ids(self) -> list[str]:
        return sorted(self._history)

    def history(self, device_id: str) -> list[Snapshot] | None:
        return self._history.get(device_id)

    def latest(self, device_id: str) -> Snapshot | None:
        h = self._history.get(device_id)
        return h[-1] if h else None

    def latest_all(self) -> list[Snapshot]:
        return [self._history[d][-1] for d in self.device_ids()]

    def hostnames(self) -> list[str]:
        return sorted({s.hostname for s in self.latest_all()})

    def label(self, device_id: str) -> str:
        s = self.latest(device_id)
        return f"{s.hostname} ({s.model_name})" if s else device_id

    def employee_ids(self) -> list[str]:
        return sorted({s.employee_id for s in self.latest_all()})

    def devices_of(self, employee_id: str) -> list[str]:
        return [s.device_id for s in self.latest_all() if s.employee_id == employee_id]

    def owns(self, device_id: str) -> bool:
        return device_id in self._history

    def data_as_of(self) -> str:
        return max(s.collected_at for s in self.latest_all()) if self._history else (self.as_of or "")


def snapshot_evidence(s: Snapshot, **fields) -> dict:
    return {
        "id": f"ev:snap:{s.device_id}:{s.date}",
        "device_id": s.device_id,
        "collected_at": s.collected_at,
        "fields": {"hostname": s.hostname, "employee_id": s.employee_id, **fields},
        "source": s.source,
    }


def trend_evidence(kind: str, snaps: list[Snapshot], **stats) -> dict:
    return {
        "id": f"ev:trend:{kind}:{snaps[0].device_id}",
        "device_id": snaps[0].device_id,
        "window": {"from": snaps[0].collected_at, "to": snaps[-1].collected_at, "snapshots": len(snaps)},
        "fields": {"hostname": snaps[-1].hostname, "employee_id": snaps[-1].employee_id, **stats},
        "source": f"{SOURCE_NAME}?device_id={snaps[0].device_id}&from={snaps[0].date}&to={snaps[-1].date}",
    }
