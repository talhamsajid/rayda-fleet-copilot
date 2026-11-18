import pytest

from evals import ground_truth as gt
from fleet_copilot.data import TenantView, contested_devices, load_rows, parse_version
from fleet_copilot.policy import check_replacement, check_upgrade, detect
from fleet_copilot.tools import FindDevicesArgs, ToolContext, find_devices

TENANTS = [gt.ACME, gt.GLOBEX, gt.INITECH]


def devices(tenant, kind):
    return {f["device_id"] for f in detect(TenantView(tenant), kind)}


def test_dataset_shape():
    rows = load_rows()
    assert sum(len(d) for d in rows.values()) == 25 and all(len(h) == 30 for d in rows.values() for h in d.values())
    assert contested_devices() == set()
    assert {t: set(TenantView(t).device_ids()) for t in TENANTS} == gt.DEVICES


@pytest.mark.parametrize("tenant", TENANTS)
@pytest.mark.parametrize("kind,truth", [
    ("storage_low", gt.STORAGE_LOW), ("ram_constrained", gt.RAM_CONSTRAINED), ("battery_eol", gt.BATTERY_EOL)])
def test_hardware_detectors(tenant, kind, truth):
    assert devices(tenant, kind) == truth[tenant]


@pytest.mark.parametrize("tenant", TENANTS)
def test_screen_lock_drift(tenant):
    found = {f["device_id"] for f in detect(TenantView(tenant), "compliance_drift")
             if f["metrics"]["check_id"] == "screen_lock"}
    assert found == gt.SCREEN_LOCK_DRIFT[tenant]


@pytest.mark.parametrize("tenant", TENANTS)
def test_unapproved_software(tenant):
    found = {(f["device_id"], f["metrics"]["software"]) for f in detect(TenantView(tenant), "unapproved_software")}
    assert found == gt.UNAPPROVED_SOFTWARE[tenant]


@pytest.mark.parametrize("tenant", TENANTS)
def test_spec_example_queries(tenant):
    ctx = ToolContext(TenantView(tenant))
    old_mac = find_devices(ctx, FindDevicesArgs(os_name="macOS", os_version_below="15"))["result"]
    assert {d["device_id"] for d in old_mac["devices"]} == gt.MACOS_BELOW_15[tenant]
    high = find_devices(ctx, FindDevicesArgs(form_factor="laptop", failing_severity="high"))["result"]
    assert high["match_count"] == 0 and high["query_evidence_id"] in ctx.ledger
    low = find_devices(ctx, FindDevicesArgs(disk_free_below_pct=10))["result"]
    assert {d["device_id"] for d in low["devices"]} == gt.STORAGE_LOW[tenant]


def test_version_parsing():
    assert parse_version("14.5") < parse_version("15") <= parse_version("15.3")
    assert parse_version("10 22H2") < parse_version("11")


def test_missing_battery_is_not_a_desktop():
    view = TenantView(gt.ACME)
    h = view.history("MT7PJB7N5LRE")
    assert any(s.battery is None for s in h) and h[-1].form_factor == "laptop"
    gaps = {f["device_id"] for f in detect(view, "telemetry_gaps")}
    assert "MT7PJB7N5LRE" in gaps


def test_upgradeability_rules():
    assert check_upgrade(TenantView(gt.ACME), "M4XVHUV1MEPZ", "ram")[0]
    ok, reason, _ = check_upgrade(TenantView(gt.INITECH), "I3JLKILKQTAC", "ram")
    assert not ok and "replacement" in reason
    assert not check_upgrade(TenantView(gt.GLOBEX), "CUXW0BQSV1B7", "ram")[0]
    assert not check_upgrade(TenantView(gt.ACME), "XSU9UUDGU6AC", "ram")[0]
    assert check_replacement(TenantView(gt.ACME), "7VP16KHV88LM")[0]
    assert not check_replacement(TenantView(gt.ACME), "XSU9UUDGU6AC")[0]
    ok, reason, _ = check_replacement(TenantView(gt.GLOBEX), "E4V0SB5I0MYU")
    assert not ok and "upgrade" in reason
