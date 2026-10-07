"""Verify UI phase 0 evidence completeness without changing code or business data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/00-baseline"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    baseline = "68e4718ab9deacd220cdd354fed21f85b07035eb"
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            baseline,
            "--",
            "src",
            "tests",
            "migrations",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    report = json.loads((args.out / "report.json").read_text())
    states = json.loads((args.out / "state-report.json").read_text())
    source = json.loads((args.out / "source-audit.json").read_text())
    fonts = json.loads((args.out / "font-study/coverage.json").read_text())
    for file in source["files"]:
        assert hashlib.sha256((root / file["file"]).read_bytes()).hexdigest() == file["sha256"], (
            f"Product source changed during phase 0: {file['file']}"
        )
    for name in ("00-audit.md", "01-design-plan.md", "PROGRESS.md"):
        assert (root / "docs/ui-refresh" / name).stat().st_size > 500, name
    assert "All checks passed!" in (args.out / "lint.log").read_text()
    assert "Success: no issues found" in (args.out / "typecheck.log").read_text()
    assert "All checks passed!" in (args.out / "lint-final.log").read_text()
    assert "already formatted" in (args.out / "lint-final.log").read_text()
    assert "Success: no issues found" in (args.out / "typecheck-final.log").read_text()
    summary = re.search(r"(\d+ passed, .*? in [^\n]+)", (args.out / "tests.log").read_text())
    assert summary, "Missing completed baseline test summary"
    assert not report["external_requests"] and not report["blocked_writes"]
    assert not states["blocked_requests"] and not states["errors"]
    assert not fonts["external_requests"]
    assert len(fonts["families"]) == 6
    assert all(
        not f["missing"] and all(font["isCustomFont"] for font in f["nfd_browser_fonts"])
        for f in fonts["families"].values()
    )
    for capture in report["captures"] + states["captures"]:
        assert (args.out / capture["file"]).is_file(), capture["file"]
    names = {c["file"] for c in report["captures"]}
    assert len(names) == len(report["captures"]), "Duplicate capture metadata"
    for capture in report["captures"]:
        if capture["file"].startswith("route-") and not capture["route"].startswith("/approval/"):
            assert not capture["open_dialogs"], f"Stale dialog covering {capture['file']}"
    for index in range(len(report["routes"])):
        for mode in ("light", "dark"):
            for width in (1440, 390):
                name = f"route-{index:02d}-{mode}-{width}.png"
                assert name in names, f"Missing route capture: {name}"
    for mode in ("light", "dark"):
        for palette in ("navy", "teal", "indigo", "forest", "copper", "graphite"):
            for density in ("compact", "comfortable"):
                assert f"work-{palette}-{mode}-{density}.png" in names
    assert len(report["palette_matrix"]) == 24, "Incomplete contrast matrix"
    for width in (360, 768, 1920):
        assert f"work-extra-{width}.png" in names
    assert report["performance"]["real_rows"] == report["register"]["total"]
    assert len(report["performance"]["scroll"]["frames"]) == 60
    assert len(report["fixtures"]) == 7 and all(f["verified"] for f in report["fixtures"])
    observed_states = {c.get("state") for c in states["captures"]}
    assert {
        "empty",
        "all-zero",
        "no-results",
        "long-title",
        "long-error",
        "error",
        "loading",
    } <= observed_states
    result = {
        "phase": 0,
        "gate": "passed",
        "product_source_unchanged": True,
        "baseline_commit": baseline,
        "observed_tasks": report["register"]["total"],
        "palette_density_cases": len(report["palette_matrix"]),
        "baseline_tests": summary[1],
        "route_urls": len(report["routes"]),
        "captures": len(report["captures"]) + len(states["captures"]),
        "font_candidates_verified": 6,
        "business_api_writes": 0,
        "baseline_defects": {
            "axe": dict(Counter(v["id"] for c in report["captures"] for v in c.get("axe", []))),
            "horizontal_overflow_captures": sum(
                c["horizontal_overflow"] for c in report["captures"]
            ),
            "console_messages": len(report["console"]),
            "page_errors": len(report["errors"]),
            "contrast_pairs_below_4_5": sum(
                check["ratio"] < 4.5
                for case in report["palette_matrix"]
                for check in case["checks"]
            ),
        },
        "final_ui_acceptance": False,
        "note": "Baseline defects are recorded; phases 1-7 remain.",
    }
    (args.out / "stage-0-gate.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
