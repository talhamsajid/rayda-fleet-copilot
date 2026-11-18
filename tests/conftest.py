import pytest


@pytest.fixture(autouse=True)
def isolated_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("FLEET_LLM_MODE", "replay")
    monkeypatch.setenv("FLEET_DECISIONS", "off")
    return tmp_path / "runs"
