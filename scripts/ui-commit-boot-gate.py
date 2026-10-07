"""Boot each UI refresh commit against read-only localhost API data.

Historical UI files are read into a temporary directory. No checkout, database
write, business action or provider call is made by this probe.
"""

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--after", default="9ed2e84")
    parser.add_argument("--through", default="HEAD")
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/07-final"))
    args = parser.parse_args()
    url = "http://127.0.0.1:8100/api/v1/ui?org=" + args.org
    served = urlopen(url).read().decode()
    constants = {
        "ORG_ID": args.org,
        "AUTH_OFF": re.search(r"const AUTH_OFF = (true|false)", served)[1],
        **{
            k: re.search(r"const " + k + r' = "([^"\n]*)"', served)[1]
            for k in ["BUILD", "PROVIDER"]
        },
    }
    commits = git("rev-list", "--reverse", args.after + ".." + args.through).decode().splitlines()
    report = {"gate": "failed", "checks": [], "commits": commits, "blocked_requests": []}

    with sync_playwright() as p, tempfile.TemporaryDirectory() as folder:
        browser = p.chromium.launch(executable_path=args.chromium)
        for commit in commits:
            root = Path(folder) / commit
            root.mkdir()
            prefix = commit + ":src/ai_orchestrator/web"
            for name in git("ls-tree", "--name-only", prefix).decode().splitlines():
                if "." in name:
                    (root / name).write_bytes(git("show", prefix + "/" + name))
            namespace = {}
            exec(compile((root / "page.py").read_text(), str(root / "page.py"), "exec"), namespace)
            document = namespace["render_console"](root)
            for key, value in constants.items():
                document = document.replace("__" + key + "__", value)
            for script in re.findall(r"<script[^>]*>(.*?)</script>", document, re.S):
                subprocess.run(["node", "--check"], input=script, text=True, check=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})

            def guard(route):
                req = route.request
                if req.method != "GET" or urlparse(req.url).hostname not in {
                    "127.0.0.1",
                    "localhost",
                }:
                    report["blocked_requests"].append({"url": req.url, "method": req.method})
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            context.route(
                "**/api/v1/ui?*",
                lambda route, request, document=document: route.fulfill(
                    body=document, content_type="text/html"
                ),
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.goto(url + "#/give", wait_until="domcontentloaded")
            page.wait_for_selector("#giveList .work-row")
            for route in [
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
                    "async r=>{history.replaceState(null,'','#/'+r);await window.route();}", route
                )
                report["checks"].append(
                    {
                        "commit": commit,
                        "route": route,
                        "passed": page.locator("#pageError").is_hidden() and not errors,
                        "errors": errors[:],
                    }
                )
            context.close()
            print("booted", commit[:7], flush=True)
        browser.close()
    report["gate"] = (
        "passed"
        if commits and not report["blocked_requests"] and all(c["passed"] for c in report["checks"])
        else "failed"
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "commit-boot.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"gate": report["gate"], "commits": len(commits), "checks": len(report["checks"])}
        )
    )
    raise SystemExit(report["gate"] != "passed")


if __name__ == "__main__":
    main()
