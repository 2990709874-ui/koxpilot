"""数据仓：懒加载真产物，并把「还没加载完」这件事如实报出去。

为什么要懒加载
--------------
契约 §3 要求 ``/api/health`` **800ms 内**返回，而读 5,000 条达人库（约 30MB JSON）
在冷启动的小机器上要两三秒。如果 health 阻塞等数据，平台探活会先超时把实例杀掉，
外部评审看到的就是「服务时好时坏」。所以：

- 进程起来后**立刻**在后台线程开始加载；
- 加载完成前 health 返回 ``status="warming"``（契约明确定义的状态，前端按不可用处理并稍后重试）；
- 加载失败**不假装 ready**：状态转为 ``failed`` 并带上原因，业务接口返回 ``ok:false``。

线程模型
--------
只有一个写入者（预热线程），读取方拿到的是一个**不可变快照**对象。
``threading.Event`` 做完成信号，避免读方看到半加载状态。
门禁与预算都是纯函数（不改 records），所以多请求并发读同一批记录是安全的。

口径纪律
--------
数据与阈值都走 ``koxpilot`` 自己的加载函数（``load_eval_inputs`` / ``Thresholds.from_dict``），
**不在服务层另写一份读取逻辑**。服务层只负责「什么时候读」，不负责「怎么解释」。
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import SERVICE_VERSION
from .briefparse import llm_runtime_status
from .paths import data_file, ensure_importable

#: 服务对外声明的引擎名（契约 §2）
ENGINE = "python"

#: 中国标准时区：served_at 用带偏移的 ISO8601，避免「这个时间是哪个时区的」这种歧义
_TZ = datetime.timezone(datetime.timedelta(hours=8))

STATUS_COLD = "cold"
STATUS_WARMING = "warming"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


class Loaded(object):
    """一次性装好的不可变快照。字段全部是真产物，没有任何服务层自造的默认值。"""

    __slots__ = (
        "records",
        "by_id",
        "thresholds",
        "thresholds_source",
        "briefs",
        "briefs_by_id",
        "dataset_meta",
        "dataset_sha256",
        "load_ms",
        "package_source",
    )

    def __init__(
        self,
        records: List[Dict[str, Any]],
        by_id: Dict[str, Dict[str, Any]],
        thresholds: Any,
        thresholds_source: str,
        briefs: List[Dict[str, Any]],
        briefs_by_id: Dict[str, Dict[str, Any]],
        dataset_meta: Dict[str, Any],
        dataset_sha256: str,
        load_ms: float,
        package_source: str,
    ) -> None:
        self.records = records
        self.by_id = by_id
        self.thresholds = thresholds
        self.thresholds_source = thresholds_source
        self.briefs = briefs
        self.briefs_by_id = briefs_by_id
        self.dataset_meta = dataset_meta
        self.dataset_sha256 = dataset_sha256
        self.load_ms = load_ms
        self.package_source = package_source

    @property
    def kox_count(self) -> int:
        return len(self.records)


class DataStore(object):
    """全局单例：管住「数据什么时候可用」这一件事。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._status = STATUS_COLD
        self._error: Optional[str] = None
        self._loaded: Optional[Loaded] = None
        self._thread: Optional[threading.Thread] = None

    # -- 状态 ---------------------------------------------------------------
    @property
    def status(self) -> str:
        return self._status

    @property
    def error(self) -> Optional[str]:
        return self._error

    def is_ready(self) -> bool:
        return self._status == STATUS_READY and self._loaded is not None

    def start(self) -> None:
        """启动后台预热（重复调用无副作用）。"""
        with self._lock:
            if self._thread is not None:
                return
            self._status = STATUS_WARMING
            self._thread = threading.Thread(
                target=self._load_safely, name="koxpilot-warmup", daemon=True
            )
            self._thread.start()

    def wait_ready(self, timeout: float) -> bool:
        """等预热完成。业务接口用它：宁可多等一会儿，也不要返回半成品。"""
        self.start()
        return self._ready.wait(timeout)

    def snapshot(self) -> Optional[Loaded]:
        return self._loaded

    # -- 加载 ---------------------------------------------------------------
    def _load_safely(self) -> None:
        started = time.time()
        try:
            loaded = _load_everything(started)
        except Exception as exc:  # 任何失败都如实登记，不让服务假装健康
            self._error = "%s: %s" % (type(exc).__name__, exc)
            self._status = STATUS_FAILED
            self._ready.set()
            return
        self._loaded = loaded
        self._status = STATUS_READY
        self._error = None
        self._ready.set()

    # -- 元信息 -------------------------------------------------------------
    def meta(self, elapsed_ms: float) -> Dict[str, Any]:
        """契约 §2 的公共 meta。数据没就绪时相关字段如实留空/置 0，不编造。"""
        loaded = self._loaded
        return {
            "engine": ENGINE,
            "engine_version": SERVICE_VERSION,
            "dataset_sha256": loaded.dataset_sha256 if loaded else "",
            "kox_count": loaded.kox_count if loaded else 0,
            "thresholds_source": loaded.thresholds_source if loaded else "",
            "llm_runtime": llm_runtime_status(),
            "served_at": datetime.datetime.now(_TZ).isoformat(timespec="seconds"),
            "elapsed_ms": int(round(elapsed_ms)),
        }


def _load_everything(started: float) -> Loaded:
    """真加载：达人库 + brief + 阈值。全部走 koxpilot 自己的函数。"""
    package_source = ensure_importable()

    from koxpilot.eval.harness import load_eval_inputs
    from koxpilot.gates.thresholds import Thresholds
    from koxpilot.io_utils import load_json

    records, dataset_meta, briefs, dataset_sha256 = load_eval_inputs()

    thr_path = data_file("output/thresholds.json")
    if thr_path is not None:
        thresholds = Thresholds.from_dict(load_json(thr_path))
        thresholds_source = "output/thresholds.json"
    else:
        # 与 CLI 同一条兜底路径：现场标定。慢，但绝不让接口用一套"猜的"阈值。
        from koxpilot.gates.thresholds import calibrate

        thresholds = calibrate(records, dataset_meta)
        thresholds_source = "运行时现场标定（未找到 output/thresholds.json）"

    by_id: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        by_id[str(rec.get("kox_id"))] = rec
    briefs_by_id: Dict[str, Dict[str, Any]] = {}
    for brief in briefs:
        briefs_by_id[str(brief.get("brief_id"))] = brief

    return Loaded(
        records=list(records),
        by_id=by_id,
        thresholds=thresholds,
        thresholds_source=thresholds_source,
        briefs=[dict(b) for b in briefs],
        briefs_by_id=briefs_by_id,
        dataset_meta=dict(dataset_meta or {}),
        dataset_sha256=dataset_sha256,
        load_ms=(time.time() - started) * 1000.0,
        package_source=package_source,
    )


#: 进程内唯一实例
STORE = DataStore()


def warmup_timeout() -> float:
    """业务接口等预热的最长时间（秒）。可用环境变量调，默认 60。"""
    try:
        return float(os.environ.get("KOXPILOT_WARMUP_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def health_payload() -> Tuple[Dict[str, Any], float]:
    """health 专用：**不等待**，如实报当前状态。返回 ``(payload, elapsed_ms)``。"""
    started = time.time()
    STORE.start()
    status = STORE.status
    if status == STATUS_COLD:
        status = STATUS_WARMING
    elapsed = (time.time() - started) * 1000.0
    payload: Dict[str, Any] = {
        "ok": True,
        "status": STATUS_READY if STORE.is_ready() else status,
        "meta": STORE.meta(elapsed),
    }
    if status == STATUS_FAILED:
        # ok 仍为 true：health 本身正常返回了，坏的是数据。状态与原因都写在明处。
        payload["status"] = STATUS_FAILED
        payload["detail"] = "数据加载失败：%s" % (STORE.error or "原因未记录")
    elif not STORE.is_ready():
        payload["detail"] = "数据集与阈值仍在加载，加载完成后本接口会返回 ready"
    return payload, elapsed
