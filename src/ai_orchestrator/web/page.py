"""Assemble trusted assets into one same-origin console without a bundler."""

from pathlib import Path

SCRIPTS = (
    "icons.js",
    "core.js",
    "components.js",
    "navigation.js",
    "work.js",
    "stream.js",
    "management.js",
    "organization.js",
    "agent-blueprints.js",
    "procurement-intake.js",
    "workflows.js",
    "library.js",
    "operations.js",
    "business.js",
    "settings.js",
    "styleguide.js",
    "shell.js",
    "boot.js",
)


def render_console(root: Path) -> str:
    template = (root / "index.html").read_text(encoding="utf-8")
    css = "@layer reset, tokens, base, layout, components, pages, utilities;\n" + "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in (
            "reset.css",
            "tokens.css",
            "fonts.css",
            "foundation.css",
            "legacy-base.css",
            "components.css",
            "styleguide.css",
        )
    )
    # Legacy page rules keep their original order inside one migration layer.
    css += (
        "\n@layer pages {\n"
        + "\n".join(
            (root / name).read_text(encoding="utf-8") for name in ("console.css", "management.css")
        )
        + "\n}"
    )
    css += "\n" + (root / "shell.css").read_text(encoding="utf-8")
    javascript = "\n".join((root / name).read_text(encoding="utf-8") for name in SCRIPTS)
    prepaint = "\n".join(
        (root / name).read_text(encoding="utf-8") for name in ("palettes.js", "preferences.js")
    )
    return (
        template.replace("__CONSOLE_CSS__", css)
        .replace("__CONSOLE_JS__", javascript)
        .replace("__UI_PREPAINT__", prepaint)
        .replace("__UI_ICONS__", (root / "icons.svg").read_text(encoding="utf-8"))
    )
