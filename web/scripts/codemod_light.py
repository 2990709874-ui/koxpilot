#!/usr/bin/env python3
"""
暗色 → 亮色 类名 codemod。

背景：原前端是近黑底 + cyan 发光的「终端风」，506 处颜色类散在 6 个 tab 里。
亮色化不可能手改，但也不能无脑替换——亮底上的可读性规则和暗底完全相反：
暗底用浅色文字（slate-300）、亮底必须用深色文字（slate-700），
暗底用半透明白描边（white/10）、亮底必须用实色浅灰描边（slate-200）。

所以这里按「语义角色」建映射，而不是按色值机械平移：
  - 文字：越亮的暗色文字 → 越深的亮色文字（反向映射）
  - 填充：低透明度彩色蒙版 → 该色系的 50/100 实色底
  - 描边：半透明白 → slate-200/300 实色
  - cyan（原「浏览器内真算」标识色）→ live-*（天蓝），与主色 brand（靛蓝）分工

用法：python3 scripts/codemod_light.py [--dry]
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
DRY = "--dry" in sys.argv

# ---------------------------------------------------------------- 文字颜色
# 暗底：数字越小越亮（slate-100 近白，用作标题）
# 亮底：数字越大越深（slate-900 近黑，用作标题）—— 因此是反向映射
SLATE_TEXT = {
    "50": "900", "100": "900", "200": "800",
    "300": "700", "400": "600", "500": "500", "600": "500",
}
# 语义色文字：暗底用 100~300 的浅色，亮底必须落到 600~800 才够对比度
SEMANTIC_TEXT = {
    "50": "800", "100": "800", "200": "700",
    "300": "600", "400": "600", "500": "600",
}
SEMANTIC_HUES = ["emerald", "amber", "rose", "indigo", "fuchsia", "sky", "blue", "violet", "red", "green", "teal"]

# ---------------------------------------------------------------- 填充/描边
def alpha_of(tok: str) -> float:
    """把 /10、/[0.06] 这两种写法统一成 float。"""
    m = re.search(r"/\[?([0-9.]+)\]?$", tok)
    if not m:
        return 1.0
    v = float(m.group(1))
    return v if v <= 1 else v / 100.0


RULES: list[tuple[str, str]] = []


def add(pat: str, rep: str) -> None:
    RULES.append((pat, rep))


# --- cyan 全线改嫁到 live-*（保留「这是浏览器内真算」的语义色位）
def cyan_text(m: re.Match) -> str:
    shade = m.group(1)
    return "text-live-" + {"50": "800", "100": "700", "200": "700", "300": "600", "400": "600"}.get(shade, "600")


# --- 逐类替换器（顺序敏感：先精确后泛化）
def build() -> list[tuple[re.Pattern, object]]:
    out: list[tuple[re.Pattern, object]] = []

    # 1) ink-*（原自定义暗色画布）→ 白卡
    out.append((re.compile(r"\bbg-ink-\d+(?:/\[?[0-9.]+\]?)?"), "bg-white"))
    out.append((re.compile(r"\bborder-ink-\d+(?:/\[?[0-9.]+\]?)?"), "border-slate-200"))

    # 2) 半透明白描边 → 实色浅灰。透明度越高说明原本想要越强的分隔
    def white_border(m: re.Match) -> str:
        a = alpha_of(m.group(0))
        return "border-slate-200/70" if a <= 0.08 else ("border-slate-200" if a <= 0.12 else "border-slate-300")

    out.append((re.compile(r"\bborder-white/\[?[0-9.]+\]?"), white_border))

    # 3) 半透明白填充 → slate 实色底（原本是「比背景稍亮一点」，现在要「比白稍沉一点」）
    def white_bg(m: re.Match) -> str:
        a = alpha_of(m.group(0))
        return "bg-slate-50" if a <= 0.06 else ("bg-slate-100" if a <= 0.12 else "bg-slate-200")

    out.append((re.compile(r"\bbg-white/\[?[0-9.]+\]?"), white_bg))

    # 4) 黑色蒙版：小透明度原本是「内嵌凹槽」，亮色下用浅灰；大透明度是 Drawer 遮罩，保留为深色半透明
    def black_bg(m: re.Match) -> str:
        a = alpha_of(m.group(0))
        return "bg-slate-900/40" if a >= 0.5 else "bg-slate-100"

    out.append((re.compile(r"\bbg-black/\[?[0-9.]+\]?"), black_bg))

    # 5) cyan → live
    out.append((re.compile(r"\btext-cyan-(\d+)(/\[?[0-9.]+\]?)?"), cyan_text))

    def live_bg(m: re.Match) -> str:
        a = alpha_of(m.group(0))
        if a >= 0.4:
            return "bg-live-500"
        return "bg-live-50" if a <= 0.12 else "bg-live-100"

    out.append((re.compile(r"\bbg-cyan-\d+(?:/\[?[0-9.]+\]?)?"), live_bg))

    def live_border(m: re.Match) -> str:
        a = alpha_of(m.group(0))
        return "border-live-200" if a <= 0.3 else "border-live-300"

    out.append((re.compile(r"\bborder-cyan-\d+(?:/\[?[0-9.]+\]?)?"), live_border))
    out.append((re.compile(r"\bring-cyan-\d+(?:/\[?[0-9.]+\]?)?"), "ring-live-300"))
    out.append((re.compile(r"\b(from|to|via)-cyan-\d+(?:/\[?[0-9.]+\]?)?"), r"\1-live-100"))

    # 6) slate 文字反向映射
    def slate_text(m: re.Match) -> str:
        return "text-slate-" + SLATE_TEXT.get(m.group(1), m.group(1)) + (m.group(2) or "")

    out.append((re.compile(r"\btext-slate-(\d+)(/\[?[0-9.]+\]?)?"), slate_text))

    # 7) 语义色文字反向映射
    def sem_text(m: re.Match) -> str:
        hue, shade, suffix = m.group(1), m.group(2), m.group(3) or ""
        return f"text-{hue}-{SEMANTIC_TEXT.get(shade, shade)}{suffix}"

    out.append((re.compile(rf"\btext-({'|'.join(SEMANTIC_HUES)})-(\d+)(/\[?[0-9.]+\]?)?"), sem_text))

    # 8) 语义色填充：低透明蒙版 → 50/100 实色
    def sem_bg(m: re.Match) -> str:
        hue = m.group(1)
        a = alpha_of(m.group(0))
        if a >= 0.4:
            return f"bg-{hue}-500"
        return f"bg-{hue}-50" if a <= 0.12 else f"bg-{hue}-100"

    out.append((re.compile(rf"\bbg-({'|'.join(SEMANTIC_HUES)}|slate)-\d+/\[?[0-9.]+\]?"), sem_bg))

    # 9) 语义色描边
    def sem_border(m: re.Match) -> str:
        hue = m.group(1)
        a = alpha_of(m.group(0))
        return f"border-{hue}-200" if a <= 0.3 else f"border-{hue}-300"

    out.append((re.compile(rf"\bborder-({'|'.join(SEMANTIC_HUES)}|slate)-\d+/\[?[0-9.]+\]?"), sem_border))

    out.append((re.compile(rf"\b(from|to|via)-({'|'.join(SEMANTIC_HUES)}|slate)-\d+/\[?[0-9.]+\]?"), r"\1-\2-100"))

    # 10) SVG fill：图表里的浅灰文字同样要反向
    def fill_slate(m: re.Match) -> str:
        return "fill-slate-" + SLATE_TEXT.get(m.group(1), m.group(1))

    out.append((re.compile(r"\bfill-slate-(\d+)"), fill_slate))
    out.append((re.compile(rf"\bfill-({'|'.join(SEMANTIC_HUES)})-(\d+)"),
                lambda m: f"fill-{m.group(1)}-{SEMANTIC_TEXT.get(m.group(2), m.group(2))}"))
    out.append((re.compile(r"\bdecoration-rose-\d+(?:/\[?[0-9.]+\]?)?"), "decoration-rose-400"))

    # 11) 半透明白文字：原本压在深色卡上，亮色下一律给深色
    def white_text(m: re.Match) -> str:
        a = alpha_of(m.group(0))
        return "text-slate-900" if a >= 0.8 else ("text-slate-700" if a >= 0.5 else "text-slate-500")

    out.append((re.compile(r"\btext-white/\[?[0-9.]+\]?"), white_text))

    return out


def main() -> None:
    rules = build()
    files = sorted(list(SRC.rglob("*.tsx")) + list(SRC.rglob("*.ts")))
    total = 0
    for f in files:
        src = f.read_text(encoding="utf-8")
        out = src
        for pat, rep in rules:
            out = pat.sub(rep, out)
        if out != src:
            n = sum(1 for a, b in zip(src.split(), out.split()) if a != b)
            total += 1
            print(f"  {str(f.relative_to(SRC.parent)):34s} 改动 token ≈ {n}")
            if not DRY:
                f.write_text(out, encoding="utf-8")
    print(f"\n{'[dry-run] ' if DRY else ''}涉及文件 {total} 个")


if __name__ == "__main__":
    main()
