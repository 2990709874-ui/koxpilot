"""量各 tab 的实测高度与区块高度（1440x1000 视口，dist 由 python http.server 提供）。

用法：python3 scripts/measure.py [base_url]
"""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4173"

BLOCKS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('main section, main > div > div > section, main .card')) {
    const h = Math.round(el.getBoundingClientRect().height);
    if (h < 40) continue;
    const t = (el.textContent || '').slice(0, 46).replace(/\\s+/g, ' ');
    out.push([h, t]);
  }
  return out.sort((a, b) => b[0] - a[0]).slice(0, 18);
}"""


async def main() -> None:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(f"console:{m.type}:{m.text}") if m.type == "error" else None)
        report = {}
        for tab in ("run", "proof", "build"):
            await pg.goto(f"{BASE}/index.html#{tab}", wait_until="networkidle")
            await pg.wait_for_timeout(5000)
            h = await pg.evaluate("document.documentElement.scrollHeight")
            report[tab] = {"height": h, "top_blocks": await pg.evaluate(BLOCKS)}
        report["errors"] = errs
        print(json.dumps(report, ensure_ascii=False, indent=1))
        await b.close()


asyncio.run(main())
