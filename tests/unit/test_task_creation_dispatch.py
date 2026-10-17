"""Exercise shipped task/source handlers together without an API or model."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_task_creation_dispatch_preserves_custom_contract_and_source_choice() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", str(root / "scripts/verify_task_creation_dispatch.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    receipt = json.loads(result.stdout)
    assert receipt["passed"] is True
    assert receipt["live_requests"] == 0
