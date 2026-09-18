"""四场景实测截图（1440x1000，dist 由 python http.server 提供）。

用法：python3 scripts/shots.py <scenario> [base_url]
  scenario: online | warming | offline | apierror | flip
外部负责按场景启停 scripts/mock-api.mjs（环境变量见该文件头部注释）。
输出：/tmp/shots/<scenario>-*.png，并打印页面高度、计算源徽标文案、关键行文案与控制台错误。
"""
import asyncio
import json
import pathlib
import sys

from playwright.async_api import async_playwright

SCEN = sys.argv[1] if len(sys.argv) > 1 else "online"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:4173"
OUT = pathlib.Path("/tmp/shots")
OUT.mkdir(parents=True, exist_ok=True)

PICK = """() => {
  const t = (sel) => Array.from(document.querySelectorAll(sel)).map(e => (e.textContent || '').replace(/\\s+/g, ' ').trim());
  const chips = t('header .chip');
  const body = (document.querySelector('main') || document.body).textContent.replace(/\\s+/g, ' ');
  const grab = (re) => { const m = body.match(re); return m ? m[0] : null; };
  return {
    source_chip: chips.find(x => x.includes('计算源')) || null,
    parity: grab(/Python 服务与浏览器引擎：[^。]{0,120}/),
    verdict: grab(/第一步：一句话需求[^。]{0,200}。/),
    service_err: grab(/服务未能返回结果：[^。]{0,120}。/),
    skeleton: Boolean(document.querySelector('[aria-busy="true"]')),
    height: document.documentElement.scrollHeight,
  };
}"""


async def main() -> None:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 1000})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(f"pageerror:{e}"))
        pg.on("console", lambda m: errs.append(f"console:{m.text}") if m.type == "error" else None)
        report: dict[str, object] = {"scenario": SCEN}

        await pg.goto(f"{BASE}/index.html#run", wait_until="domcontentloaded")
        if SCEN == "warming":
            # 探活未就绪窗口内先抓一张，验证是骨架态而不是先给结论
            await pg.wait_for_timeout(1200)
            report["during_probe"] = await pg.evaluate(PICK)
            await pg.screenshot(path=str(OUT / f"{SCEN}-1-probing.png"), full_page=True)
            await pg.wait_for_timeout(6000)
        await pg.wait_for_timeout(5000)
        report["run"] = await pg.evaluate(PICK)
        await pg.screenshot(path=str(OUT / f"{SCEN}-2-run.png"), full_page=True)

        if SCEN in ("online", "apierror", "flip"):
            # 自由输入一条 brief 并提交（走服务或在服务失败时由浏览器引擎接管）
            ta = pg.locator("main textarea").first
            await ta.fill("巴西和墨西哥的 Instagram 彩妆，18-24 岁女性，预算 8 万美金，避开 Focallure，看互动")
            await pg.get_by_role("button", name="解析并重跑").click()
            await pg.wait_for_timeout(4000)
            report["after_submit"] = await pg.evaluate(PICK)
            await pg.screenshot(path=str(OUT / f"{SCEN}-3-free-brief.png"), full_page=True)

        await pg.goto(f"{BASE}/index.html#build", wait_until="networkidle")
        await pg.wait_for_timeout(3000)
        await pg.screenshot(path=str(OUT / f"{SCEN}-4-architecture.png"), full_page=True)
        report["errors"] = errs
        print(json.dumps(report, ensure_ascii=False, indent=1))
        await b.close()


asyncio.run(main())
