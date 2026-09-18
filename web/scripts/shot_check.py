import asyncio, json, re
from playwright.async_api import async_playwright

BRIEF = "彩妆新品投巴西和墨西哥，预算 5 万美元，Instagram 为主，目标 18-24 岁女性，看互动。"

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("http://127.0.0.1:4173/#console", wait_until="networkidle")
        await pg.wait_for_timeout(4000)

        async def snapshot(tag):
            txt = await pg.inner_text("body")
            def grab(pat):
                m = re.search(pat, txt)
                return m.group(0) if m else None
            h = await pg.evaluate("document.documentElement.scrollHeight")
            two = await pg.evaluate("""() => {
              const pick = (needle) => {
                const el = [...document.querySelectorAll('div.space-y-3')].find(d => d.textContent.includes(needle));
                return el ? Math.round(el.getBoundingClientRect().height) : null;
              };
              return {console: pick('① 投放需求'), decision: pick('第二步：同预算下')};
            }""")
            return {"tag": tag, "pageHeight": h, "tabHeights": two,
                    "candidates": grab(r"定向召回 [\d,]+ 人"),
                    "pass": grab(r"判可投 [\d,]+ 人"),
                    "alloc": grab(r"预算落到 [\d,]+ 人"),
                    "spec": grab(r"预算 \$[\d,]+"),
                    }

        before = await snapshot("preset")
        await pg.screenshot(path="/tmp/console_preset.png", full_page=True)

        # 敲一条自己编的 brief
        ta = pg.locator("textarea")
        await ta.fill(BRIEF)
        await pg.get_by_role("button", name="解析并重跑").click()
        await pg.wait_for_timeout(6000)
        after = await snapshot("custom")
        await pg.screenshot(path="/tmp/console_custom.png", full_page=True)

        # 展开证据 & 对照表截图
        for name in ["个字段各自命中了原文的哪个片段", "构建期 LLM 解析 vs 线上规则解析差在哪"]:
            loc = pg.get_by_text(re.compile(name)).first
            if await loc.count():
                await loc.click()
                await pg.wait_for_timeout(400)
        await pg.screenshot(path="/tmp/console_evidence.png", full_page=True)

        print(json.dumps({"before": before, "after": after, "errors": errs}, ensure_ascii=False, indent=1))
        await b.close()

asyncio.run(main())
