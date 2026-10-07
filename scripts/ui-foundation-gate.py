"""Read-only Chromium gate for Phase 1 tokens, local assets and preferences."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def contrast(a: str, b: str) -> float:
    def luminance(value):
        channels = [int(v) / 255 for v in value.removeprefix("rgb(").removesuffix(")").split(",")]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * w for v, w in zip(linear, [0.2126, 0.7152, 0.0722], strict=True))

    x, y = sorted([luminance(a), luminance(b)])
    return (y + 0.05) / (x + 0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/01-foundation"))
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Localhost only")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "gate": "failed",
        "final_ui_acceptance": False,
        "captured_at": datetime.now(UTC).isoformat(),
        "base": args.base,
        "org": args.org,
        "scope": "foundation computed tokens and selected live controls; not full-app a11y",
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path("src/ai_orchestrator/web").rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        },
        "checks": [],
        "matrix": [],
        "blocked_requests": [],
        "page_errors": [],
        "console_errors": [],
        "screenshots": [],
    }

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    url = f"{args.base}/api/v1/ui?org={args.org}"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="vi-VN")

        def guard(route):
            r = route.request
            if urlparse(r.url).hostname not in {"localhost", "127.0.0.1"} or r.method not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                report["blocked_requests"].append({"url": r.url, "method": r.method})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        page = context.new_page()
        page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
        page.on(
            "console",
            lambda m: report["console_errors"].append(m.text) if m.type == "error" else None,
        )
        page.goto(url + "#/give", wait_until="domcontentloaded")
        page.wait_for_function(
            "typeof registerSnapshot !== 'undefined' && registerSnapshot !== null"
        )
        page.evaluate("async()=>{await document.fonts.ready}")
        check("default-language", page.evaluate("document.documentElement.lang") == "vi")
        check(
            "head-prepaint",
            page.evaluate(
                "document.querySelector('[data-ui-prepaint]').compareDocumentPosition("
                "document.querySelector('style')) & Node.DOCUMENT_POSITION_FOLLOWING"
            ),
        )
        palettes = page.evaluate("UIPalettes.items.map(p=>p.id)")
        colors_js = """() => {
          const fixture=document.createElement('div');
          fixture.hidden=true;document.body.append(fixture);
          const names=['bg','surface-1','surface-2','surface-3','nav','text',
            'text-secondary','text-muted',
            'fill','fill-hover','accent','accent-hover','accent-soft','on-accent','focus','border-control'];
          for(const status of ['danger','success','warning','info','neutral'])
            for(const part of ['fg','bg','border'])names.push(status+'-'+part);
          const result={};for(const name of names){
            fixture.style.color='var(--color-'+name+')';
            result[name]=getComputedStyle(fixture).color;}
          fixture.remove();return result;
        }"""
        for palette in palettes:
            for mode in ["light", "dark"]:
                for density in ["comfortable", "compact"]:
                    page.evaluate(
                        "v=>UIPreferences.preview({...UIPreferences.get(),...v})",
                        {"palette": palette, "theme": mode, "density": density},
                    )
                    page.wait_for_function("!document.documentElement.dataset.uiSwitching")
                    colors = page.evaluate(colors_js)
                    pairs = []
                    for fg in ["text", "text-secondary", "text-muted"]:
                        for bg in [
                            "bg",
                            "surface-1",
                            "surface-2",
                            "surface-3",
                            "nav",
                            "fill",
                            "fill-hover",
                            "accent-soft",
                        ]:
                            pairs.append((fg, bg, 4.5))
                    pairs += [("on-accent", "accent", 4.5), ("on-accent", "accent-hover", 4.5)]
                    for fg in ["focus", "border-control", "accent"]:
                        for bg in [
                            "bg",
                            "surface-1",
                            "surface-2",
                            "surface-3",
                            "fill",
                            "fill-hover",
                        ]:
                            pairs.append((fg, bg, 4.5 if fg == "accent" else 3))
                    for status in ["danger", "success", "warning", "info", "neutral"]:
                        pairs += [
                            (status + "-fg", status + "-bg", 4.5),
                            (status + "-border", status + "-bg", 3),
                        ]
                    measured = [
                        {
                            "fg": fg,
                            "bg": bg,
                            "minimum": minimum,
                            "ratio": round(contrast(colors[fg], colors[bg]), 4),
                        }
                        for fg, bg, minimum in pairs
                    ]
                    failures = [m for m in measured if m["ratio"] < m["minimum"]]
                    name = f"{palette}-{mode}-{density}"
                    check(name + "-contrast", not failures, failures)
                    if palette == "graphite":
                        structural = [
                            colors[k]
                            for k in colors
                            if not any(
                                k.startswith(s)
                                for s in ["danger", "success", "warning", "info", "neutral"]
                            )
                        ]
                        check(
                            name + "-achromatic",
                            all(len(set(c[4:-1].split(", "))) == 1 for c in structural),
                        )
                    controls = page.evaluate("""()=>{
                      const b=document.querySelector('.btn.primary'),s=getComputedStyle(b);
                      return {fg:s.color,bg:s.backgroundColor,font:s.fontFamily,
                        minHeight:s.minHeight,
                        hidden:[...document.querySelectorAll('[hidden]')].every(e=>getComputedStyle(e).display==='none'),
                        overflow:document.documentElement.scrollWidth>innerWidth};
                    }""")
                    check(
                        name + "-actual-button",
                        contrast(controls["fg"], controls["bg"]) >= 4.5,
                        controls,
                    )
                    check(name + "-layout", controls["hidden"] and not controls["overflow"])
                    check(
                        name + "-row-content-contained",
                        page.evaluate("""()=>
                      [...document.querySelectorAll('#giveList > .row')].slice(0,30).every(row=>{
                        const bounds=row.getBoundingClientRect();
                        return [...row.children].every(child=>{
                          const content=child.getBoundingClientRect();
                          return content.top>=bounds.top && content.bottom<=bounds.bottom;
                        });
                      })"""),
                    )
                    check(
                        name + "-density",
                        controls["minHeight"] == ("32px" if density == "compact" else "40px"),
                    )
                    file = name + ".png"
                    page.screenshot(path=str(args.out / file))
                    report["screenshots"].append(file)
                    report["matrix"].append(
                        {
                            "palette": palette,
                            "mode": mode,
                            "density": density,
                            "colors": colors,
                            "contrast": measured,
                        }
                    )

        font_samples = page.evaluate("""async()=>{
          const text='Cộng hòa xã hội chủ nghĩa Việt Nam — Đặng Thị Hồng; '+
            'tuyển kỹ sư cơ điện 0123456789';
          const result=[];for(const family of ['Inter','JetBrains Mono'])
          for(const weight of [400,500,600]) {
            await document.fonts.load(weight+' 16px "'+family+'"',text);
            await document.fonts.load(weight+' 16px "'+family+'"',text.normalize('NFD'));
            const el=document.createElement('p');el.textContent=text+' '+text.normalize('NFD');
            el.style.font=weight+' 16px "'+family+'"';
            el.id='font-'+family.replaceAll(' ','')+'-'+weight;
            document.body.append(el);result.push(el.id);
          }return result;
        }""")
        cdp = context.new_cdp_session(page)
        root_id = cdp.send("DOM.getDocument")["root"]["nodeId"]
        cdp.send("DOM.enable")
        cdp.send("CSS.enable")
        report["font_evidence"] = []
        for element_id in font_samples:
            node = cdp.send("DOM.querySelector", {"nodeId": root_id, "selector": "#" + element_id})[
                "nodeId"
            ]
            fonts = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
            check(
                element_id + "-custom", bool(fonts) and all(f["isCustomFont"] for f in fonts), fonts
            )
            report["font_evidence"].append({"element": element_id, "fonts": fonts})
        page.evaluate("ids=>ids.forEach(id=>document.getElementById(id).remove())", font_samples)
        for asset in sorted(Path("src/ai_orchestrator/web/assets/fonts").glob("*.woff2")):
            response = context.request.get(args.base + "/api/v1/ui/assets/" + asset.name)
            check(asset.name, response.status == 200 and response.body() == asset.read_bytes())

        page.evaluate(
            "UIPreferences.save({theme:'dark',palette:'copper',density:'compact',motion:'reduced',home:'organization'});localStorage.setItem('ao-lang-v1','en');localStorage.setItem('ao-sidebar-collapsed','true')"
        )
        page.add_init_script(
            """new MutationObserver((_,o)=>{if(document.body){
              window.firstBodyPreferences={...document.documentElement.dataset,
                lang:document.documentElement.lang,
                collapsed:document.documentElement.classList.contains('sidebar-collapsed')};
              o.disconnect()}}).observe(document,{childList:true,subtree:true});"""
        )
        page.reload(wait_until="domcontentloaded")
        state = page.evaluate("window.firstBodyPreferences")
        check("prepaint-transitions-suppressed", state.get("uiInitializing") == "true")
        check(
            "first-body-preferences",
            all(
                state.get(k) == v
                for k, v in {
                    "colorMode": "dark",
                    "palette": "copper",
                    "density": "compact",
                    "motion": "reduced",
                    "lang": "en",
                    "collapsed": True,
                }.items()
            ),
            state,
        )
        check("saved-home", page.evaluate("UIPreferences.get().home") == "organization")
        page.wait_for_function(
            "!document.documentElement.dataset.uiInitializing && "
            "!document.documentElement.dataset.uiSwitching"
        )
        check(
            "reduced-motion",
            page.evaluate("getComputedStyle(document.querySelector('.btn')).transitionDuration")
            == "0s",
        )
        page.evaluate("UIPreferences.preview({...UIPreferences.get(),theme:'auto',motion:'auto'})")
        page.emulate_media(color_scheme="light")
        page.wait_for_function("document.documentElement.dataset.colorMode==='light'")
        page.emulate_media(color_scheme="dark")
        page.wait_for_function("document.documentElement.dataset.colorMode==='dark'")
        check("auto-system-change", True)
        page.evaluate("UIPreferences.preview({...UIPreferences.get(),theme:'light'})")
        page.emulate_media(color_scheme="light")
        page.emulate_media(color_scheme="dark")
        check(
            "manual-theme-stable",
            page.evaluate("document.documentElement.dataset.colorMode") == "light",
        )
        switching = page.evaluate("""()=>{
          UIPreferences.preview({...UIPreferences.get(),theme:'dark'});
          return {flag:document.documentElement.dataset.uiSwitching,
            duration:getComputedStyle(document.querySelector('.btn')).transitionDuration};
        }""")
        check(
            "theme-change-no-transition", switching == {"flag": "true", "duration": "0s"}, switching
        )
        page.emulate_media(reduced_motion="reduce")
        check(
            "os-reduced-motion",
            page.evaluate("getComputedStyle(document.querySelector('.btn')).transitionDuration")
            == "0s",
        )
        page.evaluate(
            "localStorage.setItem('onx-ui-preferences-v1','{broken');localStorage.removeItem('ao-lang-v1');localStorage.removeItem('ao-sidebar-collapsed')"
        )
        page.reload(wait_until="domcontentloaded")
        check(
            "corrupt-storage-defaults",
            page.evaluate("UIPreferences.get()")
            == {
                "theme": "auto",
                "palette": "navy",
                "density": "comfortable",
                "motion": "auto",
                "home": "campaigns",
            },
        )
        for width in [360, 390]:
            page.set_viewport_size({"width": width, "height": 844})
            for route in ["give", "organization", "settings"]:
                page.goto(url + "#/" + route, wait_until="domcontentloaded")
                page.wait_for_timeout(300)
                check(
                    f"mobile-{width}-{route}",
                    page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
                )
                file = f"mobile-{width}-{route}.png"
                page.screenshot(path=str(args.out / file))
                report["screenshots"].append(file)
        context.close()
        blocked = browser.new_context()
        blocked.route("**/*", guard)
        blocked.add_init_script(
            "Object.defineProperty(window,'localStorage',"
            "{get(){throw new Error('Storage blocked')}})"
        )
        fallback = blocked.new_page()
        fallback.goto(url, wait_until="domcontentloaded")
        check(
            "blocked-storage-default",
            fallback.evaluate(
                "UIPreferences.get().palette==='navy' && document.documentElement.lang==='vi'"
            ),
        )
        fallback.evaluate(
            "UIPreferences.preview({theme:'invalid',palette:'invalid',density:'invalid'})"
        )
        check(
            "invalid-preferences-default",
            fallback.evaluate(
                "UIPreferences.get().theme==='auto' && UIPreferences.get().density==='comfortable'"
            ),
        )
        blocked.close()
        mobile = browser.new_context(
            is_mobile=True, has_touch=True, viewport={"width": 390, "height": 844}
        )
        mobile.route("**/*", guard)
        touch = mobile.new_page()
        touch.goto(url, wait_until="domcontentloaded")
        touch.evaluate("UIPreferences.preview({...UIPreferences.get(),density:'compact'})")
        check(
            "coarse-compact-target",
            touch.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--btn-height').trim()"
            )
            == "44px",
        )
        mobile.close()
        browser.close()
    check("no-writes-or-external-hosts", not report["blocked_requests"])
    check("no-js-errors", not report["page_errors"] and not report["console_errors"])
    report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
    (args.out / "stage-1-gate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "checks": len(report["checks"]),
                "failed": [c for c in report["checks"] if not c["passed"]],
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["gate"] == "passed" else 1)


if __name__ == "__main__":
    main()
