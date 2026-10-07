"""Close UI phases 5-7 only with current browser receipts and successful checks.

First run the commands in docs/ui-refresh/README.md. Save their output as
verification/{lint,typecheck,tests,e2e,style,components-contract}.log under --out.
This aggregator reads evidence; it never starts services or runs business actions.
"""

import argparse
import hashlib
import html
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/07-final"))
    args = parser.parse_args()
    root = args.out
    sources = {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in Path("src/ai_orchestrator/web").rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    }
    result = {
        "gate": "failed",
        "final_ui_acceptance": False,
        "business_live_acceptance": False,
        "captured_at": datetime.now(UTC).isoformat(),
        "checks": [],
        "receipts": [],
        "source_sha256": sources,
        "scope": "Chromium UI; fixtures are not operational approvals or model evidence",
    }

    def check(name, passed, detail=None):
        result["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            print("FAIL", name, str(detail)[:500])

    captures = []
    receipts = [
        ("routes", root / "report.json"),
        ("foundation", root / "foundation/stage-1-gate.json"),
        ("components", root / "components/stage-2-gate.json"),
        ("shell", root / "shell/stage-3-gate.json"),
        ("work", root / "work/stage-4-gate.json"),
        ("issues", root / "issues/gate.json"),
        ("interactions", root.parent / "06-interactions/report.json"),
        ("performance", root / "performance/performance-and-form.json"),
        ("runtime", root / "runtime-check.json"),
    ]
    for label, file in receipts:
        check(label + "-exists", file.is_file())
        if not file.is_file():
            continue
        data = json.loads(file.read_text())
        checks = data.get("checks", [])
        check(label + "-passed", data.get("gate") == "passed" and bool(checks))
        check(label + "-assertions", all(c["passed"] for c in checks))
        hashes = data.get("source_sha256", data.get("source_hashes", {}))
        stale = [name for name, value in hashes.items() if sources.get(name) != value]
        check(label + "-current-source", bool(hashes) and not stale, stale)
        for name in ["blocked_requests", "errors", "page_errors", "console", "console_errors"]:
            check(label + "-" + name, not data.get(name), data.get(name))
        shots = data.get("screenshots", data.get("captures", []))
        for shot in shots:
            name = shot if isinstance(shot, str) else shot["file"]
            image = file.parent / name
            check(
                label + "-image-" + name,
                image.is_file() and image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
            )
            captures.append((label, image))
        check(label + "-has-screenshots", bool(shots))
        result["receipts"].append(
            {"scope": label, "path": str(file), "checks": len(checks), "screenshots": len(shots)}
        )
        if label == "routes":
            check("route-coverage", len(data.get("routes", [])) >= 57)
            check("route-matrix", len(shots) >= len(data.get("routes", [])) * 4)
            check(
                "route-source-frozen",
                any(c["name"] == "source-frozen-during-gate" and c["passed"] for c in checks),
            )
            result["performance"] = data["performance"]

    patterns = {
        "lint": r"All checks passed![\s\S]*files? (?:already formatted|left unchanged)",
        "typecheck": r"Success: no issues found",
        "tests": r"\d+ passed.*\d+ skipped.*\d+ deselected",
        "e2e": r"\d+ passed",
        "style": r"CSS contract passed",
        "components-contract": r"PASS",
        "test-isolation-and-ui": r"\d+ passed",
    }
    boot_file = root / "commit-boot.json"
    boot = json.loads(boot_file.read_text()) if boot_file.is_file() else {}
    check("intermediate-commits-boot", boot.get("gate") == "passed" and bool(boot.get("commits")))
    check(
        "intermediate-commit-routes",
        bool(boot.get("checks")) and all(c["passed"] for c in boot["checks"]),
    )
    runtime_file = root / "runtime-check.json"
    runtime = json.loads(runtime_file.read_text()) if runtime_file.is_file() else {}
    check("product-restarted-and-ready", runtime.get("gate") == "passed")
    check(
        "product-runtime-assertions",
        bool(runtime.get("checks")) and all(c["passed"] for c in runtime["checks"]),
    )
    result["verification_logs"] = {}
    for name, pattern in patterns.items():
        file = root / "verification" / (name + ".log")
        content = file.read_text() if file.is_file() else ""
        check(name + "-log-passed", bool(re.search(pattern, content)), str(file))
        check(
            name + "-no-failure",
            not re.search(r"\d+ failed|FAILED |Traceback \(most recent", content),
        )
        result["verification_logs"][name] = {
            "path": str(file),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
    result["gate"] = "passed" if all(c["passed"] for c in result["checks"]) else "failed"
    result["final_ui_acceptance"] = result["gate"] == "passed"
    (root / "release-gate.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    # Offline index links only the screenshots referenced by the current receipts.
    cards = []
    for label, image in captures:
        target = html.escape(os.path.relpath(image, root), quote=True)
        caption = html.escape(label + " / " + image.name)
        cards.append(
            f'<figure><a href="{target}"><img loading="lazy" src="{target}" alt="{caption}">'
            f"</a><figcaption>{caption}</figcaption></figure>"
        )
    (root / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width">'
        "<title>UI refresh — final evidence</title><style>body{font:16px system-ui;"
        "margin:24px;background:#f5f5f3;color:#20252b}"
        "main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}figure{margin:0;min-width:0}"
        "img{width:100%;height:300px;object-fit:contain;background:white}figcaption{overflow-wrap:anywhere}</style>"
        f"<h1>UI refresh — {html.escape(result['gate'])}</h1>"
        "<p>Chromium UI evidence. Browser fixtures do not certify live business workflows.</p>"
        '<p><a href="release-gate.json">Acceptance receipt</a> · '
        '<a href="../../../docs/ui-refresh/FINAL-REPORT.md">Report</a></p>'
        + "<main>"
        + "\n".join(cards)
        + "</main></html>\n"
    )
    print(
        json.dumps(
            {"gate": result["gate"], "checks": len(result["checks"]), "screenshots": len(captures)}
        )
    )
    if not result["final_ui_acceptance"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
