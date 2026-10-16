"""The cleanup proof rejects foreign contracts and non-corpus source bytes."""

import argparse
import io
import json
import zipfile
from pathlib import Path

import pytest
from scripts.archive_synthetic_tasks import digest, load_proof, verify_source


def packet(name: str, content: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, content)
    return out.getvalue()


def test_exact_csv_proof_and_no_false_classification() -> None:
    case = {"source": {"filename": "quotes.csv", "csv_text": "vendor,price\nA,10\n"}}
    assert verify_source(packet("quotes.csv", b"vendor,price\nA,10\n"), case)
    assert not verify_source(packet("quotes.csv", b"vendor,price\nA,11\n"), case)
    assert not verify_source(packet("other.csv", b"vendor,price\nA,10\n"), case)
    assert not verify_source(b"not a zip", case)


def test_docx_paragraph_proof() -> None:
    xml = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>SYNTHETIC</w:t></w:r></w:p></w:body></w:document>'
    docx = packet("word/document.xml", xml)
    case = {"source": {"entry_name": "brief.docx", "paragraphs": ["SYNTHETIC"]}}
    assert verify_source(packet("brief.docx", docx), case)
    case["source"]["paragraphs"] = ["REAL"]
    assert not verify_source(packet("brief.docx", docx), case)


def test_receipt_requires_explicit_matching_tenant_and_corpus(tmp_path: Path) -> None:
    corpus = {"synthetic": True, "cases": [{"id": "case-1"}]}
    receipt = {
        "synthetic": True,
        "corpus": {"sha256": digest(corpus)},
        "attempts": [
            {"task_id": "tsk-exact", "case_id": "case-1"},
        ],
    }
    contract = {"synthetic": True, "organization_id": "org-a", "corpus_sha256": digest(corpus)}
    for name, obj in (("corpus", corpus), ("receipt", receipt), ("contract", contract)):
        (tmp_path / name).write_text(json.dumps(obj))
    args = argparse.Namespace(
        org="org-a",
        corpus=tmp_path / "corpus",
        run_receipt=tmp_path / "receipt",
        contract=tmp_path / "contract",
    )
    _, candidates, _ = load_proof(args)
    assert list(candidates) == ["tsk-exact"]
    args.org = "org-b"
    with pytest.raises(ValueError, match="mismatch"):
        load_proof(args)
    args.org = "org-a"
    corpus["synthetic"] = False
    args.corpus.write_text(json.dumps(corpus))
    with pytest.raises(ValueError, match="mismatch"):
        load_proof(args)


def test_unsupported_guard_proof_is_exact_and_refuses_unrelated_upload() -> None:
    import hashlib

    from scripts.archive_synthetic_tasks import verify_unsupported_source

    from ai_orchestrator.persistence.sources import SourceFile

    source = SourceFile(
        id="src-exact",
        origin="upload",
        state="unsupported",
        filename="firmware.bin",
        content=b"SYNTHETIC",
        sha256=hashlib.sha256(b"SYNTHETIC").hexdigest(),
    )
    case = {
        "kind": "unsupported_source",
        "workflow": "document_review",
        "goal": "test",
        "source": {"bytes_hex": b"SYNTHETIC".hex(), "filename": "firmware.bin"},
        "expected": {"rejection_expected": True},
    }
    attempt = {
        "kind": "unsupported_source",
        "workflow": "document_review",
        "upload_id": source.id,
        "upload_state": "unsupported",
        "source_manifest": {"manifest": None},
    }
    task_input = {"source_workflow": "document_review", "source_selection_mode": "sources"}
    assert verify_unsupported_source(task_input, "test", case, attempt, source)
    assert not verify_unsupported_source(
        task_input, "test", case, {**attempt, "upload_id": "other"}, source
    )
    assert not verify_unsupported_source(
        {**task_input, "source_workflow": "quote_comparison"}, "test", case, attempt, source
    )
    source.content = b"REAL"
    assert not verify_unsupported_source(task_input, "test", case, attempt, source)
