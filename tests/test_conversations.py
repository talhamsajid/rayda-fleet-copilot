import pytest

from fleet_copilot import cli, insights
from fleet_copilot.conversations import Conversations
from fleet_copilot.data import TenantView


def test_conversations_belong_to_one_admin(tmp_path):
    store = Conversations(tmp_path / "c.sqlite")
    mine = store.create("acme-001", "alice")
    store.touch(mine, "Which batteries are failing?")
    store.touch(mine, "Flag those")
    assert store.owns(mine, "acme-001", "alice")
    assert not store.owns(mine, "acme-001", "bob") and not store.owns(mine, "globex-002", "alice")
    assert [c["title"] for c in store.list("acme-001", "alice")] == ["Which batteries are failing?"]
    assert store.list("acme-001", "bob") == []


@pytest.mark.parametrize("tenant,actor", [("globex-002", "alice"), ("acme-001", "mallory")])
def test_cli_refuses_someone_elses_session(tenant, actor, capsys):
    thread = Conversations().create("acme-001", "alice")
    assert cli.main(["--tenant", tenant, "--actor", actor, "--session", thread, "-q", "hi"]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_lists_own_conversations(capsys):
    store = Conversations()
    thread = store.create("acme-001", "alice")
    store.touch(thread, "Which devices are low on disk?")
    assert cli.main(["--tenant", "acme-001", "--actor", "alice", "--list"]) == 0
    assert thread in capsys.readouterr().out


def test_as_of_cuts_history():
    view = TenantView("acme-001", as_of="2026-05-20")
    assert all(s.date <= "2026-05-20" for d in view.device_ids() for s in view.history(d))
    assert view.data_as_of().startswith("2026-05-20")


def test_daily_briefing_flags_drift_on_the_day_it_starts():
    insights.backfill("acme-001")
    day = insights.load("acme-001", "2026-05-22")
    new = [(i["title"], i["device"]["hostname"]) for i in day["issues"] if i["new"]]
    assert new == [("Screen lock turned off", "acme-macbook-2")]
    flagged = [e for e in insights.audit.read_log("daily-briefing")
               if e["event"] == "high_severity_flagged" and e["date"] == "2026-05-22"]
    assert [(e["device_id"], e["title"]) for e in flagged] == [("1LYSSFD074BB", "Screen lock turned off")]


def test_severity_and_suggestions_follow_policy():
    b = insights.briefing("acme-001")
    by = {(i["kind"], i["device"]["hostname"]): i for i in b["issues"]}
    assert by[("storage_low", "acme-thinkpad-8")]["severity"] == "critical"
    assert by[("storage_low", "acme-thinkpad-8")]["suggestion"]["label"] == "Order a bigger SSD"
    assert by[("storage_low", "acme-macbook-4")]["suggestion"]["label"] == "Ask the owner to free up space"
    assert by[("battery_eol", "acme-dell-9")]["severity"] == "high"
    assert by[("unapproved_software", "acme-macbook-2")]["severity"] == "high"
    assert by[("unapproved_software", "acme-macbook-1")]["severity"] == "medium"
    assert [i["severity"] for i in b["issues"]] == sorted((i["severity"] for i in b["issues"]),
                                                          key=insights.SEVERITY.index)
    assert all(i["suggestion"] is None for i in b["issues"] if i["kind"] == "telemetry_gaps")
