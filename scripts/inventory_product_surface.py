"""Inventory declared product surfaces from source, without starting the application.

Counts describe code topology, not working features or business acceptance.
Hashes let a later review detect which source changed since this snapshot.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ai_orchestrator"


def inventory():
    files = {}
    routes = []
    tools = []
    stages = {}
    hr_templates = []
    paths = [
        *sorted((SOURCE / "api").glob("*.py")),
        SOURCE / "domain" / "business_workflow.py",
        SOURCE / "domain" / "hr_templates.py",
        SOURCE / "tools" / "builtin.py",
    ]
    for path in paths:
        relative = str(path.relative_to(ROOT))
        content = path.read_bytes()
        files[relative] = hashlib.sha256(content).hexdigest()
        tree = ast.parse(content, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    method = decorator.func
                    if (
                        isinstance(method, ast.Attribute)
                        and method.attr in {"get", "post", "put", "patch", "delete", "websocket"}
                        and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)
                    ):
                        routes.append(
                            {
                                "file": relative,
                                "line": node.lineno,
                                "method": method.attr.upper(),
                                "declared_path": decorator.args[0].value,
                                "handler": node.name,
                            }
                        )
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "ToolDefinition":
                for keyword in node.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        tools.append(
                            {"name": keyword.value.value, "file": relative, "line": node.lineno}
                        )
            if node.func.id == "HRTemplate":
                hr_templates.append(ast.literal_eval(node.args[0]))
        if path.name == "business_workflow.py":
            for node in tree.body:
                if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Tuple):
                    continue
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {"HIRING", "PROCUREMENT"}:
                    stages[target.id] = [
                        {
                            "key": ast.literal_eval(call.args[0]),
                            "owner": ast.literal_eval(call.args[2]),
                            "kind": ast.literal_eval(call.args[3]),
                        }
                        for call in node.value.elts
                        if isinstance(call, ast.Call)
                    ]
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "Static declarations only; not runtime acceptance or a feature count",
        "files_sha256": files,
        "routes": routes,
        "route_declarations": len(routes),
        "built_in_tools": tools,
        "ordered_business_stages": stages,
        "hr_template_keys": hr_templates,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inventory()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"routes": result["route_declarations"], "output": str(args.output)}))
