"""Assemble trusted assets into one same-origin console without a bundler."""

from pathlib import Path

SCRIPTS = (
    "core.js",
    "navigation.js",
    "work.js",
    "stream.js",
    "management.js",
    "organization.js",
    "agent-blueprints.js",
    "workflows.js",
    "library.js",
    "operations.js",
    "business.js",
    "settings.js",
    "boot.js",
)


def render_console(root: Path) -> str:
    template = (root / "index.html").read_text(encoding="utf-8")
    css = "\n".join(
        (root / name).read_text(encoding="utf-8") for name in ("console.css", "management.css")
    )
    javascript = "\n".join((root / name).read_text(encoding="utf-8") for name in SCRIPTS)
    return template.replace("__CONSOLE_CSS__", css).replace("__CONSOLE_JS__", javascript)
