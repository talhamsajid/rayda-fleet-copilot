from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .data import ROOT

PREFIX = {
    "create_upgrade_order": "PO",
    "open_remediation_ticket": "TKT",
    "flag_device_for_replacement": "RPL",
    "notify_employee": "MSG",
}
_thread_lock = threading.Lock()


def runs_dir() -> Path:
    d = Path(os.environ.get("FLEET_RUNS_DIR", ROOT / "runs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _locked(path: Path):
    with _thread_lock, open(path.with_suffix(path.suffix + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _append(path: Path, event: str, **fields) -> None:
    with _locked(path):
        # ponytail: seq = line count under a file lock; a real deployment uses an append-only store with its own sequence
        seq = len(_lines(path)) + 1
        with path.open("a") as f:
            f.write(json.dumps({"seq": seq, "ts": _now(), "event": event, **fields}, sort_keys=True, default=str) + "\n")


def log(event: str, *, session: str, tenant: str, actor: str, **payload) -> None:
    _append(runs_dir() / "audit.jsonl", event, session=session, tenant=tenant, actor=actor, **payload)


def security(event: str, **payload) -> None:
    _append(runs_dir() / "security.jsonl", event, **payload)


def read_log(session: str | None = None) -> list[dict]:
    rows = _lines(runs_dir() / "audit.jsonl")
    return [r for r in rows if session is None or r["session"] == session]


def outbox() -> list[dict]:
    return _lines(runs_dir() / "outbox.jsonl")


def execute(proposal: dict, *, tenant: str, approved_by: str, session: str) -> dict:
    key = hashlib.sha256(json.dumps({"tenant": tenant, "key": proposal["dedupe_key"]}).encode()).hexdigest()[:16]
    path = runs_dir() / "outbox.jsonl"
    with _locked(path):
        for row in _lines(path):
            if row["idempotency_key"] == key:
                return {"status": "duplicate", "reference": row["reference"]}
        reference = f"{PREFIX[proposal['tool']]}-{key[:8].upper()}"
        row = {"reference": reference, "idempotency_key": key, "ts": _now(), "tenant": tenant, "session": session,
               "tool": proposal["tool"], "args": proposal["args"], "proposal_id": proposal["id"],
               "evidence_ids": proposal["evidence_ids"], "approved_by": approved_by}
        with path.open("a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    return {"status": "executed", "reference": reference}
