"""验证「系统与校验」页的实时核对按钮：点一下，读回比对条数与差异条数。

用法：python3 scripts/arch_check.py [base_url]
"""

import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4300/"


def main() -> None:
    errors: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(BASE.rstrip("/") + "/index.html#build", wait_until="networkidle")
        pg.wait_for_timeout(4000)

        btn = pg.get_by_role("button", name="现在核对一遍")
        print("button:", btn.first.inner_text())
        btn.first.click()
        pg.wait_for_timeout(6000)

        panel = pg.locator("text=批量判定端点").first
        print("hint visible:", panel.count() > 0)
        line = pg.locator("span.num", has_text="逐条比对").first
        print("parity line:", line.inner_text() if line.count() else "(none)")

        h = pg.evaluate("document.getElementById('build')?.scrollHeight ?? document.documentElement.scrollHeight")
        print("build height:", h)
        pg.screenshot(path="/tmp/shots/online-6-arch-live.png", full_page=False)
        print("console errors:", errors)
        b.close()


if __name__ == "__main__":
    main()
