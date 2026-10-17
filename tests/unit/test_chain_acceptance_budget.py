"""Acceptance tools must retain campaign limits and provenance before POST."""

import json

import pytest
from scripts.run_chain_acceptance import _load_ledger
from scripts.run_department_campaign import BudgetAbort, Ledger


def test_smaller_persisted_cap_and_metadata_survive_reservation(tmp_path):
    path = tmp_path / "ledger.json"
    metadata = {"campaign": "new", "parent_ledger": "old.json", "models_allowed": ["free"]}
    path.write_text(json.dumps(metadata | {"used": 29, "max": 30, "entries": []}))
    ledger = Ledger(path, owner="one")
    ledger.acquire("free")
    actual = json.loads(path.read_text())
    assert actual["used"] == 30
    assert actual["max"] == 30
    assert all(actual[k] == v for k, v in metadata.items())
    with pytest.raises(BudgetAbort):
        ledger.acquire("free")
    assert json.loads(path.read_text())["used"] == 30


def test_requested_smaller_cap_is_also_respected(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"used": 1, "max": 60, "entries": []}))
    ledger = Ledger(path, owner="one", max_submissions=2)
    ledger.acquire("free")
    with pytest.raises(BudgetAbort):
        ledger.acquire("free")


def test_campaign_rejects_reused_run_id(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"used": 1, "max": 30, "entries": [{"owner": "chain-first"}]}))
    with pytest.raises(ValueError, match="run-id"):
        _load_ledger(path, "first")


def test_free_gate_rejects_paid_model_without_spending(tmp_path):
    from scripts.run_chain_acceptance import _FreeSubmissionGate

    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"used": 0, "max": 30, "entries": []}))
    ledger = Ledger(path, owner="one")
    gate = _FreeSubmissionGate(ledger)
    with pytest.raises(BudgetAbort, match="authorized"):
        gate.check_and_reserve("paid/model")
    assert ledger.used == 0
    assert json.loads(path.read_text())["used"] == 0
    gate.check_and_reserve("dots-studio/dots-3-note-preview:free")
    assert ledger.used == 1


def test_another_runner_cannot_take_campaign_lock(tmp_path):
    import fcntl

    from scripts.run_chain_acceptance import main

    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"used": 0, "max": 30, "entries": []}))
    with path.with_suffix(".json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as exc:
            main(["--org", "synthetic-tenant", "--ledger-path", str(path)])
        assert exc.value.code == 2
    assert json.loads(path.read_text())["used"] == 0
