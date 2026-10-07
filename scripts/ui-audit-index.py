"""Build an offline screenshot index from UI audit reports; no service requests."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from html import escape
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/00-baseline"))
    args = parser.parse_args()
    report = json.loads((args.out / "report.json").read_text())
    states = json.loads((args.out / "state-report.json").read_text())
    groups = []
    counts = Counter(v["id"] for c in report["captures"] for v in c.get("axe", []))
    for index, route in enumerate(report["routes"]):
        shots = [c for c in report["captures"] if c["file"].startswith(f"route-{index:02d}-")]
        images = "".join(
            f'<a href="{escape(c["file"])}"><img loading="lazy" src="{escape(c["file"])}" '
            f'alt="{escape(c["file"])}"><span>{escape(c["file"])}</span></a>'
            for c in shots
        )
        groups.append(
            f'<section><h2>{escape(route["route"])}</h2><div class="shots">{images}</div></section>'
        )
    extra = [c for c in report["captures"] if not c["file"].startswith("route-")]
    extra += states["captures"]
    for c in extra:
        groups.append(
            f'<section><h2>{escape(c["file"])}</h2><a href="{escape(c["file"])}">'
            f'<img loading="lazy" src="{escape(c["file"])}" alt="{escape(c["file"])}"></a>'
            "<p>Fixture trong browser</p></section>"
            if c.get("browser_get_fixture")
            else f'<section><h2>{escape(c["file"])}</h2><a href="{escape(c["file"])}">'
            f'<img loading="lazy" src="{escape(c["file"])}" '
            f'alt="{escape(c["file"])}"></a></section>'
        )
    html = f"""<!doctype html><html lang="vi"><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1"><title>UI baseline</title>
      <style>body{{font:14px/1.6 system-ui;margin:0;padding:24px;background:#f6f6f5;color:#252a31}}
      header{{max-width:90ch}}h1{{font-size:24px}}h2{{font-size:14px;overflow-wrap:anywhere}}
      section{{background:white;padding:16px;border:1px solid #d8dce0;
        border-radius:8px;margin:20px 0}}
      .shots{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
      img{{max-width:100%;max-height:400px;object-fit:contain;object-position:top;display:block}}
      a{{color:#275781}}span{{display:block;margin-top:8px;font-size:12px}}</style>
      <header><h1>Ảnh baseline · Giai đoạn 0</h1>
      <p>Ảnh giao diện trước khi chỉnh sửa. {len(report["routes"])} URL đã kiểm;
      {len(report["captures"]) + len(states["captures"])} lượt chụp có metadata.
      Những lỗi bên dưới là hiện trạng, chưa phải nghiệm thu giao diện mới.</p>
      <p>Axe: {escape(str(dict(counts)))}</p>
      <p><a href="report.json">Report chính</a> · <a href="state-report.json">Màn phụ/fixture</a> ·
      <a href="font-study/index.html">So sánh font</a> ·
      <a href="layout-study.html">Ba cấu trúc</a></p>
      </header><main>{"".join(groups)}</main></html>"""
    (args.out / "index.html").write_text(html)
    print(f"Indexed {len(groups)} groups in {args.out / 'index.html'}")


if __name__ == "__main__":
    main()
