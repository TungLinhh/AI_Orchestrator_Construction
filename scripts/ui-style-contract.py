"""Check the shared CSS contract; --migrate converts legacy spacing to scale tokens.

Geometry (widths, strokes and media breakpoints) is deliberately not spacing.
No selector is removed by this tool. Dead rules require a separate caller audit.
"""

import argparse
import re
from pathlib import Path

ROOT = Path("src/ai_orchestrator/web")
INLINE = re.compile(r'\bstyle="([^"]*)"')
SCALE = (2, 4, 8, 12, 16, 20, 24, 32, 40, 56)
SPACING = re.compile(
    r"(?P<key>\b(?:margin|padding)(?:-(?:top|right|bottom|left|block|inline)(?:-(?:start|end))?)?|\b(?:gap|row-gap|column-gap))\s*:(?P<value>[^;{}]+)(?=[;}]|$)"
)
PIXEL = re.compile(r"(?<![\w.-])(-?\d+(?:\.\d+)?)px\b")


def migrate(value):
    def replace(match):
        number = float(match[1])
        if number == 0:
            return "0"
        nearest = min(SCALE, key=lambda n: (abs(n - abs(number)), -n))
        token = f"var(--space-{nearest})"
        return f"calc(-1 * {token})" if number < 0 else token

    # Existing variables, expressions and developer supplied custom tokens are authoritative.
    if "var(" in value or "calc(" in value:
        return value
    return PIXEL.sub(replace, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    errors = []
    for file in sorted(ROOT.glob("*.css")):
        if file.name == "tokens.css":
            continue
        text = file.read_text()
        if args.migrate:
            text = SPACING.sub(lambda m: m["key"] + ":" + migrate(m["value"]), text)
            file.write_text(text)
        for match in SPACING.finditer(text):
            if PIXEL.search(match["value"]):
                errors.append(f"{file}:{text[: match.start()].count(chr(10)) + 1}: {match[0]}")
        if re.search(r"#[\da-fA-F]{3,8}\b|rgba?\(", text):
            errors.append(f"{file}: raw color outside tokens")
        if "!important" in text:
            errors.append(f"{file}: !important")
    for file in sorted([*ROOT.glob("*.js"), *ROOT.glob("*.html")]):
        text = file.read_text()
        if args.migrate:
            text = INLINE.sub(
                lambda attr: (
                    'style="'
                    + SPACING.sub(lambda m: m["key"] + ":" + migrate(m["value"]), attr[1])
                    + '"'
                ),
                text,
            )
            file.write_text(text)
        for attr in INLINE.finditer(text):
            for match in SPACING.finditer(attr[1]):
                if PIXEL.search(match["value"]):
                    errors.append(f"{file}: inline spacing outside tokens: {match[0]}")
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print("CSS contract passed: scale spacing, token colors, no !important")


if __name__ == "__main__":
    main()
