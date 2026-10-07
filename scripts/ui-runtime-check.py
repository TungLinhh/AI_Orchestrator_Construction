"""Read-only product checks after restart; never performs a model or business action."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ai_orchestrator.domain.ids import AgentId, SkillId, ToolId


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--chromium", help="Also boot the live product's nine main pages")
    parser.add_argument("--primary-model", default="dots-studio/dots-3-note-preview:free")
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/07-final"))
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Loopback only")
    report = {
        "gate": "failed",
        "captured_at": datetime.now(UTC).isoformat(),
        "checks": [],
        "model_completion_executed": False,
        "business_actions_executed": False,
        "scope": "GET-only probe; normal background workers are outside this measurement",
        "screenshots": [],
        "errors": [],
        "console": [],
        "blocked_requests": [],
    }
    sources = {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in Path("src/ai_orchestrator/web").rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    }
    args.out.mkdir(parents=True, exist_ok=True)

    def read(path, decode=True):
        request = Request(args.base + path, headers={"x-organization-id": args.org})
        with urlopen(request, timeout=30) as response:
            return json.load(response) if decode else response.read().decode()

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            print("FAIL", name, detail)

    health = read("/health")
    check("api-live", health["status"] == "ok")
    ready = read("/ready")
    report["readiness"] = ready
    check("api-ready", ready["status"] == "ready")
    check("least-privilege-role", ready["checks"]["least_privilege_role"])
    rls = ready["checks"]["row_level_security"]
    check(
        "tenant-tables-force-rls",
        rls["rls_enabled"] == rls["rls_forced"]
        and set(rls["unprotected"]) == {"alembic_version", "consumer_offsets", "organizations"},
    )
    document = read("/api/v1/ui?org=" + args.org, decode=False)
    match = re.search(r'const PROVIDER = "([^"\n]*)"', document)
    check(
        "runtime-provider",
        match is not None and match[1] == args.provider,
        match[1] if match else "missing",
    )
    profiles = read("/api/v1/model-profiles")
    primary = next(p for p in profiles["items"] if p["name"] == "primary")
    first = primary["providers"][0]
    check(
        "primary-model",
        first["provider"] == args.provider and first["model"] == args.primary_model,
        first["provider"] + "/" + first["model"],
    )
    check("profile-warnings", not profiles["warnings"], profiles["warnings"])
    check(
        "openrouter-credential-present",
        read("/system/secrets")["OPENROUTER_API_KEY"] == "configured",
    )
    report["stream"] = read("/api/v1/stream/status")
    for resource, kind in [("agents", AgentId), ("tools", ToolId), ("skills", SkillId)]:
        offset, count, invalid = 0, 0, []
        while True:
            data = read(f"/api/v1/{resource}?limit=200&offset={offset}")
            for item in data["items"]:
                try:
                    kind(item["id"])
                except ValueError:
                    invalid.append(item["id"])
                count += 1
            offset += len(data["items"])
            if not data["items"] or offset >= data.get("total", offset):
                break
        check(
            resource + "-persisted-id-contract",
            count > 0 and not invalid,
            {"records": count, "invalid": invalid},
        )
    if args.chromium:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.chromium)
            report["browser"] = browser.version
            context = browser.new_context(viewport={"width": 1440, "height": 900})

            def guard(route):
                req = route.request
                if req.method != "GET" or urlparse(req.url).hostname not in {
                    "127.0.0.1",
                    "localhost",
                }:
                    report["blocked_requests"].append({"method": req.method, "url": req.url})
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            page = context.new_page()
            page.on("pageerror", lambda e: report["errors"].append(str(e)))
            page.on(
                "console",
                lambda m: (
                    report["console"].append(m.text) if m.type in {"warning", "error"} else None
                ),
            )
            page.goto(args.base + "/api/v1/ui?org=" + args.org + "#/give")
            page.wait_for_function(
                "typeof registerSnapshot!=='undefined' && registerSnapshot!==null"
            )
            for path in [
                "give",
                "work/issues",
                "work/approvals",
                "departments",
                "processes/workflows",
                "library/skills",
                "business/projects",
                "operations/models",
                "settings/appearance",
            ]:
                page.evaluate(
                    "async r=>{history.replaceState(null,'','#/'+r);await route();}", path
                )
                check(
                    "live-ui-" + path,
                    page.locator("#pageError").is_hidden() and not report["errors"],
                )
                file = "runtime-" + path.replace("/", "-") + ".png"
                page.screenshot(path=str(args.out / file))
                report["screenshots"].append(file)
            browser.close()
        check("live-ui-console", not report["console"], report["console"])
        check("live-ui-requests", not report["blocked_requests"], report["blocked_requests"])
    report["source_sha256"] = {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in Path("src/ai_orchestrator/web").rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    }
    check("runtime-source-frozen", sources == report["source_sha256"])
    report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
    (args.out / "runtime-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"gate": report["gate"], "checks": len(report["checks"])}))
    raise SystemExit(report["gate"] != "passed")


if __name__ == "__main__":
    main()
