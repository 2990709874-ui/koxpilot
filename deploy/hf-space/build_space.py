#!/usr/bin/env python3
"""把 api/ 那份服务打包成可以直接推到 Hugging Face Spaces 的目录。

产物在 deploy/hf-space/_out/，内容 = api/ 的服务代码与数据 + Space 需要的
Dockerfile 与 README front matter。

用法（在仓库根执行）：

    python3 deploy/hf-space/build_space.py

设计取舍
--------
* 不改 api/ 里的任何东西：Space 版和内网版跑的是同一份 koxpilot_service 与同一份 _bundle，
  避免出现「两个部署两套代码」——那是这个项目一直在避免的事。
* Space 用 uvicorn 直接起 main:app，不走 api/run.sh（那个是内网 FaaS 运行时专用的）。
* Space 默认端口 7860，且必须监听 0.0.0.0。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "api"
OUT = Path(__file__).resolve().parent / "_out"

# Space 的 README 必须带 YAML front matter，HF 靠它识别 SDK 与端口
SPACE_README = """---
title: KOXPilot API
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# KOXPilot 服务

出海达人营销投前决策智能体的 HTTP 服务。门禁 / 预算 / 审计 / 证据句全部来自
`src/koxpilot`，与命令行同一套实现。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 探活。首个请求会懒加载 5,000 条数据集，可能返回 `status: warming` |
| POST | `/api/plan` | 一句话 brief → 达人清单 + 预算分配 + 证据链 |
| GET | `/api/kox/{kox_id}/explain` | 单个达人的完整证据链 |
| POST | `/api/gate/batch` | 批量跑四层门禁，用于双实现一致性核对 |

契约见仓库 `api/CONTRACT.md`。

## 试一下

```bash
curl -s $SPACE_URL/api/health

curl -s -X POST $SPACE_URL/api/plan \\
  -H 'Content-Type: application/json' \\
  -d '{"brief_text":"巴西和墨西哥的 Instagram 彩妆，18-24 女性，预算 8 万美金，避开 Focallure"}'
```

## 说明

* 数据集是固定种子的合成数据，不含真实达人信息；服务只读，不写任何东西。
* A1 的 brief 解析：配了模型凭据走模型，没配走确定性规则解析（`parse_path` 字段会如实标明），
  两种情况接口都不报错。
* 源码：https://github.com/{github_repo}
"""

DOCKERFILE = """# Hugging Face Spaces（Docker runtime）
# 与内网部署跑同一份服务代码，只是换了运行时入口。
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Space 约定：监听 0.0.0.0:7860
ENV PORT=7860
ENV PYTHONUNBUFFERED=1
EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
"""

# Space 用 3.11，不需要 api/ 那份为内网 FaaS 3.8 运行时锁的老版本
REQUIREMENTS = """fastapi==0.115.6
uvicorn==0.34.0
httpx==0.28.1
"""

# 要从 api/ 搬过去的东西。_bundle 里已经含降级后的 koxpilot 包与数据产物。
COPY_ITEMS = ["main.py", "koxpilot_service", "_bundle", "CONTRACT.md"]


def main() -> int:
    if not (API / "main.py").exists():
        print(f"找不到服务入口 {API / 'main.py'}，请在仓库根执行本脚本", file=sys.stderr)
        return 1

    bundle_info = API / "_bundle" / "BUNDLE_INFO.json"
    if not bundle_info.exists():
        print("_bundle 不存在，先跑 python3 api/build_bundle.py 生成", file=sys.stderr)
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    for item in COPY_ITEMS:
        src = API / item
        if not src.exists():
            print(f"缺少 {src}", file=sys.stderr)
            return 1
        dst = OUT / item
        if src.is_dir():
            shutil.copytree(
                src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env")
            )
        else:
            shutil.copy2(src, dst)

    github_repo = "<你的用户名>/koxpilot"
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if "github.com" in url:
            github_repo = url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
    except Exception:
        pass

    (OUT / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (OUT / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (OUT / "README.md").write_text(
        SPACE_README.replace("{github_repo}", github_repo), encoding="utf-8"
    )
    (OUT / ".gitignore").write_text("__pycache__/\n*.pyc\n.env\n", encoding="utf-8")

    info = json.loads(bundle_info.read_text(encoding="utf-8"))
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"打包完成：{OUT}")
    print(f"  文件数 {sum(1 for f in OUT.rglob('*') if f.is_file())}，合计 {total / 1024 / 1024:.1f} MB")
    # BUNDLE_INFO 的 data 是「路径 -> {bytes, sha256, ...}」，没有顶层 dataset_sha256。
    # 之前按顶层 key 取，取不到就退化成打印一段 JSON 片段，等于没有指纹可核。
    # 这里直接取达人库那一项的 sha256——它就是 /api/health 里 meta.dataset_sha256，
    # 部署完可以拿两边对一眼，确认推上去的数据和本地是同一份。
    data_info = info.get("data") or {}
    entry = data_info.get("data/kox_5000.json") if isinstance(data_info, dict) else None
    sha = entry.get("sha256") if isinstance(entry, dict) else None
    if sha:
        print(f"  数据集指纹 sha256 {sha[:16]}…（应与 /api/health 的 meta.dataset_sha256 一致）")
    else:
        print("  数据集指纹：未能从 BUNDLE_INFO.json 读到 data/kox_5000.json 的 sha256，请检查 api/build_bundle.py")
    print()
    print("下一步：按 deploy/hf-space/README.md 第 2 步推到 Space")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
