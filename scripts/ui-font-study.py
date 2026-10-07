"""Download pinned audit-only font assets, verify glyphs and capture Vietnamese specimens.

uv run --with playwright --with fonttools --with brotli python scripts/ui-font-study.py
No product files or runtime configuration are changed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
import unicodedata
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from playwright.sync_api import sync_playwright

FAMILIES = ("inter", "geist", "be-vietnam-pro", "jetbrains-mono", "geist-mono", "ibm-plex-mono")
SAMPLES = (
    "Tuyển kỹ sư MEP đến onboarding",
    "Đề xuất lựa chọn sau hai vòng",
    "Phê duyệt",
    "ệ ữ ặ ẫ ỗ ử",
)


def download(root: Path) -> None:
    records = []
    for name in FAMILIES:
        package, version = "@fontsource/" + name, "5.3.0"
        with urllib.request.urlopen(f"https://registry.npmjs.org/{package}/{version}") as response:
            entry = json.load(response)
        with urllib.request.urlopen(entry["dist"]["tarball"]) as response:
            raw = response.read()
        dest = root / name
        dest.mkdir(parents=True, exist_ok=True)
        names = {
            f"{name}-{subset}-{weight}-normal.woff2"
            for subset in ("latin", "latin-ext", "vietnamese")
            for weight in (400, 500, 600)
        }
        names |= {"LICENSE", "LICENSE.txt", "OFL.txt", "metadata.json"}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive.getmembers():
                filename = Path(member.name).name
                if filename in names:
                    (dest / filename).write_bytes(archive.extractfile(member).read())
        records.append(
            {
                "family": name,
                "package": package,
                "version": version,
                "license": entry["license"],
                "tarball": entry["dist"]["tarball"],
                "sha256": hashlib.sha256(raw).hexdigest(),
                "files": sorted(p.name for p in dest.iterdir()),
            }
        )
    (root / "sources.json").write_text(json.dumps(records, indent=2) + "\n")
    with urllib.request.urlopen("https://registry.npmjs.org/axe-core/4.10.3") as response:
        entry = json.load(response)
    with urllib.request.urlopen(entry["dist"]["tarball"]) as response:
        raw = response.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for name in ("axe.min.js", "LICENSE"):
            (root / ("axe-" + name)).write_bytes(archive.extractfile("package/" + name).read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("artifacts/ui-shots/00-baseline/font-study")
    )
    parser.add_argument(
        "--download", action="store_true", help="Development-time network download only"
    )
    parser.add_argument(
        "--chromium",
        default=str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.download:
        download(args.out)
    css, coverage, specimens = [], {}, []
    for family in FAMILIES:
        glyphs = set()
        for path in sorted((args.out / family).glob("*.woff2")):
            with TTFont(path) as font:
                cmap = font.getBestCmap()
                glyphs.update(cmap)
                unicode_range = ",".join(f"U+{point:X}" for point in sorted(cmap))
                weight = path.name.split("-")[-2]
                css.append(
                    f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};"
                    f"font-display:swap;src:url('{family}/{path.name}') format('woff2');"
                    f"unicode-range:{unicode_range};}}"
                )
        alphabet = "".join(chr(c) for c in range(0x1EA0, 0x1EFA)) + "ĂăÂâÊêÔôƠơƯưĐđ"
        required = "".join(SAMPLES) + alphabet
        missing = sorted(set(ord(c) for c in required) - glyphs)
        coverage[family] = {
            "missing": [f"U+{c:04X}" for c in missing],
            "glyph_count": len(glyphs),
            "nfd_unmapped_before_shaping": [
                f"U+{c:04X}"
                for c in sorted(set(map(ord, unicodedata.normalize("NFD", alphabet))) - glyphs)
            ],
        }
        specimens.append(
            f"<section style=\"font-family:'{family}'\"><h2>{family}</h2>"
            + "".join(
                f"<p style='font-size:{size}px;font-weight:{weight}'>{sample}</p>"
                for size, weight, sample in (
                    (20, 600, SAMPLES[0]),
                    (14, 400, SAMPLES[1]),
                    (13, 500, SAMPLES[2]),
                    (14, 400, SAMPLES[3]),
                )
            )
            + f"<p data-nfd='{family}'>{unicodedata.normalize('NFD', SAMPLES[3])}</p>"
            + "<p class='log'>0 O o · 1 l I |<br>tsk_01m49qy02cte3jhgz2kez3eje1 · 1.188 / 351<br>"
            "2026-10-07 07:00:00 Phê duyệt · [INFO] ệ ữ ặ ẫ ỗ ử</p></section>"
        )
    html = (
        "<!doctype html><html lang='vi'><meta charset='utf-8'>"
        "<title>Vietnamese font evidence</title><style>"
        + "\n".join(css)
        + """
      *{box-sizing:border-box}body{background:#f4f4f3;color:#202227;margin:0;padding:32px;
      font:14px/1.6 system-ui}header{margin-bottom:24px}
      main{display:grid;grid-template-columns:1fr 1fr;
      gap:16px}section{background:white;border:1px solid #d8d9dc;border-radius:12px;padding:24px;
      min-width:0}h1{font-size:24px}h2{font:13px/1.5 system-ui;color:#515964;margin:0 0 16px}
      p{margin:8px 0;line-height:1.6}.log{font-size:12px;overflow-wrap:anywhere}
      @media(max-width:640px){body{padding:16px}main{grid-template-columns:1fr}section{padding:16px}}
      </style><header><h1>Thử chữ Việt / Vietnamese specimens</h1>
      <p>Font study only · Six local fonts · No product data</p></header><main>"""
        + "".join(specimens)
        + "</main></html>"
    )
    (args.out / "index.html").write_text(html)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        requests = []

        def serve(route):
            relative = route.request.url.split("http://localhost/", 1)[-1]
            if ":" in relative or ".." in Path(relative).parts:
                requests.append(route.request.url)
                route.abort()
                return
            path = args.out / relative
            route.fulfill(
                path=path, content_type="font/woff2" if path.suffix == ".woff2" else "text/html"
            )

        page.route("**/*", serve)
        page.goto("http://localhost/index.html")
        page.evaluate("document.fonts.ready")
        client = page.context.new_cdp_session(page)
        client.send("DOM.enable")
        client.send("CSS.enable")
        document = client.send("DOM.getDocument")["root"]["nodeId"]
        for family in FAMILIES:
            node = client.send(
                "DOM.querySelector", {"nodeId": document, "selector": f'[data-nfd="{family}"]'}
            )["nodeId"]
            coverage[family]["nfd_browser_fonts"] = client.send(
                "CSS.getPlatformFontsForNode", {"nodeId": node}
            )["fonts"]
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 1050})
            page.screenshot(path=str(args.out / f"specimens-{width}.png"), full_page=True)
        (args.out / "coverage.json").write_text(
            json.dumps({"families": coverage, "external_requests": requests}, indent=2) + "\n"
        )
        browser.close()
    if requests or any(
        v["missing"] or any(not f["isCustomFont"] for f in v["nfd_browser_fonts"])
        for v in coverage.values()
    ):
        raise SystemExit("Font coverage or local-only verification failed")
    print("Six fonts cover every Vietnamese specimen; desktop/mobile screenshots saved.")


if __name__ == "__main__":
    main()
