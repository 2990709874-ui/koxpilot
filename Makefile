PY ?= python3
PYTHONPATH := src

export PYTHONPATH

.PHONY: all data gate budget eval multiseed llm promptbench test lint web api-bundle api-serve api-parity clean clean-llm help

help:
	@echo "KOXPilot —— 出海达人营销投前决策智能体"
	@echo ""
	@echo "不需要任何 API key 的部分（确定性计算，任何人都能复跑出完全相同的结果）："
	@echo "  make data        生成合成达人数据集 (5,000) + 3 个 campaign brief（固定种子）"
	@echo "  make gate        标定 25 组分位数阈值并对全库跑四层门禁"
	@echo "  make budget      对 3 个 brief 跑预算分配"
	@echo "  make eval        跑评测 harness，产出 output/metrics.json（六张表 + 消融 + 敏感性 + 价值审计）"
	@echo "  make all         data -> gate -> budget -> eval  ← 只想验证核心结论，跑这一条就够"
	@echo "  make multiseed   12 个种子的稳健性实验：给核心价值主张一个 mean ± std（约 30s，只写 output/multiseed.json）"
	@echo "  make test        pytest 全量（含反数据泄漏的静态扫描与哨兵测试）"
	@echo ""
	@echo "需要 LLM API key 的部分（构建期真调，结果固化进 output/；线上服务只在配了凭据时才为 A1 实时调）："
	@echo "  make llm         批量推理：brief 解析 / 标签错配判定 / 语义适配打分"
	@echo "  make promptbench Prompt v1/v2/v3 x 双模型横评，产出 output/prompt_bench.json"
	@echo "  （先按 .env.example 配置环境变量；不配也不影响上面所有确定性步骤）"
	@echo ""
	@echo "  make web         构建前端 Demo（需 pnpm）"
	@echo ""
	@echo "HTTP 服务（api/，契约见 api/CONTRACT.md）："
	@echo "  make api-bundle  把 src/koxpilot 与必要数据打进 api/_bundle/（部署只上传 api/，不打包就跑不起来）"
	@echo "  make api-serve   本地起服务：http://127.0.0.1:8399（读 api/_bundle 或仓库根，两者等价）"
	@echo "  make api-parity  服务 vs 浏览器 TS 引擎逐条比对 + 服务 vs CLI explain 逐字比对（需服务已在跑）"
	@echo "  make clean       清空确定性产物（data/ 与 output/ 里可重算的那些）"
	@echo "  make clean-llm   另外清 LLM 产物（llm_cache/llm_bench/prompt_bench —— 真花过 token，删了要重新花钱）"

data:
	$(PY) -m koxpilot.cli data

gate:
	$(PY) -m koxpilot.cli gate

budget:
	$(PY) -m koxpilot.cli budget

eval:
	$(PY) -m koxpilot.cli eval

# 多种子稳健性实验：全程内存计算，不读 data/、不改任何既有产物，唯一产物 output/multiseed.json。
# SEEDS 可覆盖（如 `make multiseed SEEDS=24`）；WORKERS>1 走多进程，worker 无任何文件写入。
SEEDS ?= 12
WORKERS ?= 1
multiseed:
	$(PY) -m koxpilot.cli multiseed --seeds $(SEEDS) --workers $(WORKERS)

all:
	$(PY) -m koxpilot.cli all

# ---------------------------------------------------------------------------
# 构建期 LLM 调用
# ---------------------------------------------------------------------------
# 注意：这两个目标会真的花 token。结果带磁盘缓存且幂等，中断后重跑不会重复计费。
# 跑完记得重跑一次 `make eval`：成本审计（cost_audit）和表 6 要读 llm_bench.json，
# 不重跑的话 metrics.json 里的成本账会停留在 status="llm_not_run"。
llm:
	$(PY) -m koxpilot.llm.runner --tasks brief,tag,fit --workers 12
	@echo ""
	@echo ">>> LLM 产物已更新，正在重跑 eval 以接入真实 token 账 ..."
	$(PY) -m koxpilot.cli eval

promptbench:
	$(PY) -m koxpilot.llm.promptbench --n 600 --workers 14

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m compileall -q src

web:
	cd web && pnpm install --frozen-lockfile && pnpm run refresh

# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
# 部署侧只上传 api/ 目录，所以真源码与真数据必须先复制进 api/_bundle/。
# 打完包记得 git add api/_bundle（它是部署产物，必须提交，否则线上没有 koxpilot 包可 import）。
api-bundle:
	$(PY) api/build_bundle.py

api-serve:
	cd api && $(PY) -m uvicorn main:app --host 127.0.0.1 --port 8399

api-parity:
	$(PY) api/check_cli_parity.py http://127.0.0.1:8399
	cd web && node scripts/verify-service-parity.mjs --base http://127.0.0.1:8399

clean:
	rm -rf data/kox_5000.json data/kox_sample_50.json data/briefs.json \
	       output/thresholds.json output/verdicts.json output/gate_results_sample.json \
	       output/budget.json output/metrics.json output/audit.json output/multiseed.json
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

# 单独一个目标：LLM 产物是**真花过 token**的构建期结果（llm_cache.json / llm_bench.json /
# prompt_bench.json），删掉就只能再花钱重跑，所以刻意不放进 `make clean`。
clean-llm:
	rm -rf output/llm_cache.json output/llm_bench.json output/prompt_bench.json
