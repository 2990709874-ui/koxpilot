"""开发期核对：HTTP explain 与 CLI explain 是否逐字一致。

不是单元测试，而是"交付前必须自己跑一遍"的核对脚本：
对给定的若干 kox_id，同时跑 CLI `koxpilot explain` 与 `GET /api/kox/{id}/explain`，
逐项比对 handle/platform/market/followers/verdict，并要求接口 `reason_human` 里的
每一句理由都**逐字**出现在 CLI 输出里（两边共用 gates/humanize.py，本该如此）。

用法::

    python3 api/check_cli_parity.py http://127.0.0.1:8399 KOX-000002 KOX-000004 ...
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli_explain(kox_id: str) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO, "src")
    out = subprocess.run(
        [sys.executable, "-m", "koxpilot.cli", "explain", kox_id],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return out.stdout.decode("utf-8")


def http_explain(base: str, kox_id: str) -> dict:
    with urllib.request.urlopen("%s/api/kox/%s/explain" % (base.rstrip("/"), kox_id), timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list) -> int:
    base = argv[1] if len(argv) > 1 else "http://127.0.0.1:8399"
    ids = argv[2:] or ["KOX-000002", "KOX-000004", "KOX-000005", "KOX-000007", "KOX-001146"]
    bad = 0
    for kox_id in ids:
        cli = cli_explain(kox_id)
        api = http_explain(base, kox_id)
        head = cli.splitlines()[0]
        # KOX-000004 @mega_hub | youtube | AU | 5764 粉 | 组=youtube|nano
        parts = [p.strip() for p in head.split("|")]
        handle = parts[0].split(" ", 1)[1]
        platform, market = parts[1], parts[2]
        followers = int(re.sub(r"[^0-9]", "", parts[3]))
        cli_verdict = cli.splitlines()[1].split("：", 1)[1].split("（", 1)[0]

        problems = []
        prof = api["profile"]
        if prof["handle"] != handle:
            problems.append("handle %s != %s" % (prof["handle"], handle))
        if prof["platform"] != platform:
            problems.append("platform %s != %s" % (prof["platform"], platform))
        if prof["market"] != market:
            problems.append("market %s != %s" % (prof["market"], market))
        if prof["followers"] != followers:
            problems.append("followers %s != %s" % (prof["followers"], followers))
        if api["verdict"] != cli_verdict:
            problems.append("verdict %s != %s" % (api["verdict"], cli_verdict))

        n_hits = 0
        for gate in api["gates"]:
            for hit in gate["hits"]:
                n_hits += 1
                if hit["detail"] not in cli:
                    problems.append("理由未逐字出现在 CLI 输出：%s" % hit["detail"][:40])
                if hit["detail"] not in api["reason_human"]:
                    problems.append("reason_human 漏了这条理由：%s" % hit["detail"][:40])

        if problems:
            bad += 1
            print("✗ %s" % kox_id)
            for p in problems:
                print("    %s" % p)
        else:
            print("✓ %s %s %s | %s 粉 | 判定 %s | 逐字一致的理由 %d 条"
                  % (kox_id, handle, platform, followers, api["verdict"], n_hits))
    print("")
    print("核对 %d 个 id，%d 个有差异" % (len(ids), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
