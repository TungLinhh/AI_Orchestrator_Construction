"""Acceptance source integrity is checked before tasks or external writes exist."""

import hashlib
import json
from pathlib import Path

import pytest
from scripts.workflow_acceptance_packet import build_packet, load_brief


def test_packet_is_repeatable_and_keeps_expectations_out_of_model_brief(tmp_path):
    first = build_packet(tmp_path / "first")
    second = build_packet(tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    for kind in ("mep_hiring", "procurement"):
        brief = load_brief(first, kind)
        assert brief["synthetic"] is True
        assert len(brief["acceptance_packet"]["manifest_sha256"]) == 64
        assert "expectations" not in brief
        assert "not_passed_to_models" not in json.dumps(brief)


def test_packet_rejects_changed_document(tmp_path):
    manifest = build_packet(tmp_path)
    (tmp_path / "hiring/mep-test-a.txt").write_text("changed after review")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_brief(manifest, "mep_hiring")


@pytest.mark.parametrize("field,value", [("synthetic", False), ("human_approved", True)])
def test_packet_cannot_claim_live_or_human_review(tmp_path, field, value):
    manifest = build_packet(tmp_path)
    data = json.loads(manifest.read_text())
    data[field] = value
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="explicitly synthetic"):
        load_brief(manifest, "procurement")


def test_packet_rejects_escape_even_with_correct_hash(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not a packet document")
    manifest = build_packet(tmp_path / "packet")
    data = json.loads(manifest.read_text())
    data["files"]["../outside.txt"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="outside packet"):
        load_brief(manifest, "procurement")


def test_packet_rejects_embedded_source_drift_even_if_brief_rehashed(tmp_path):
    manifest = build_packet(tmp_path)
    brief_path = tmp_path / "briefs/mep_hiring.json"
    brief = json.loads(brief_path.read_text())
    brief["interview_hr"]["transcripts"][0]["notes"] = "different embedded record"
    brief_path.write_text(json.dumps(brief))
    data = json.loads(manifest.read_text())
    data["files"]["briefs/mep_hiring.json"] = hashlib.sha256(brief_path.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Embedded source differs"):
        load_brief(manifest, "mep_hiring")


def test_committed_packet_matches_generator(tmp_path):
    generated = build_packet(tmp_path)
    committed = Path("examples/workflow_acceptance/v1/manifest.json")
    assert generated.read_bytes() == committed.read_bytes()
    for kind in ("mep_hiring", "procurement"):
        assert load_brief(committed, kind) == load_brief(generated, kind)


def test_packet_rejects_quotation_drift_even_if_brief_rehashed(tmp_path):
    manifest = build_packet(tmp_path)
    path = tmp_path / "briefs/procurement.json"
    brief = json.loads(path.read_text())
    brief["suppliers"][0]["quotes"][0]["unit_price"] += 1
    path.write_text(json.dumps(brief))
    data = json.loads(manifest.read_text())
    data["files"]["briefs/procurement.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Embedded supplier differs"):
        load_brief(manifest, "procurement")
