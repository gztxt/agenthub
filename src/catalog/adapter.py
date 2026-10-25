"""catalog adapter —— 适配器基类 + 注册表（PT-20261025-12 P0）。

**全部适配器只读**（用户令：原生数据保持独立运行）：没有任何一个适配器写回
原生位置——不写文件、不写 TDAI、不写各 agent 家目录、不写 agents.db。

**为什么 collect() 与 run_adapter() 分开**：
`collect(states)` 是纯数据收集（可单测、可复用、可被 CLI 单独跑），
`run_adapter()` 才碰库（事务、扫描账本、消失判定）。
分开之后「采集错了」和「落库错了」能分别定位，不会互相掩盖。

**命名空间完整性**：`collect()` 必须返回 `complete`。
`complete=False` ⇒ `run_adapter` 不调 `mark_missing()`——根目录临时不可读
不能被解释成「文件全没了」（方案 §5，测试有断言）。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

#: 适配器注册表：src_id -> 实例。新增适配器只改这里，不动 hub 其它代码。
REGISTRY: Dict[str, "BaseAdapter"] = {}


def fingerprint(value: Any) -> str:
    """给没有可靠 mtime 的声明/名录记录做稳定内容指纹。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def register(cls: type) -> type:
    """类装饰器：实例化并按 `src_id` 注册。"""
    inst = cls()
    if not inst.src_id:
        raise ValueError("适配器 %s 缺 src_id" % cls.__name__)
    if inst.src_id in REGISTRY:
        raise ValueError("重复注册 src_id=%s" % inst.src_id)
    REGISTRY[inst.src_id] = inst
    return cls


def get_adapter(src_id: str) -> Optional["BaseAdapter"]:
    return REGISTRY.get(src_id)


def all_adapters() -> List["BaseAdapter"]:
    return list(REGISTRY.values())


class BaseAdapter:
    """适配器基类。子类只需实现 `probe()` 与 `collect()`。"""

    #: 源登记字段（见 schema.src 表）
    src_id: str = ""
    domain: str = ""
    kind: str = "file"            # table|file|service|remote|code
    authority: int = 0            # 1 = 该域权威源
    adapter: str = ""
    scan_mode: str = "incremental"
    #: 能力依赖（供 doctor 用）："http:8420" / "dir:~/.claude" / "systemd" …
    requires: Tuple[str, ...] = ()
    #: 该源的代表性 URI（人看）
    uri: str = ""

    # ── 子类实现 ────────────────────────────────────────────────────
    def probe(self) -> Dict[str, Any]:
        """可用性探测。返回 {ok, reason, error}。

        `ok=False` 时 `reason` 必须是机器可判的短标识
        （`no_dir` / `no_service` / `no_dep` …），**不许把事实写进中文文案**。
        """
        return {"ok": True, "reason": None, "error": None}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        """采集本域全部对象。返回 {records, live, complete, note}。

        - `records`：对象记录列表（字段见 `store.upsert_objects` docstring）；
        - `live`：本次见到的 oid 列表（供 mark_missing 判消失）；
        - `complete`：本 src 命名空间是否**完整**扫完；
        - `note`：给人看的一句话摘要。
        """
        raise NotImplementedError

    # ── 通用工具 ────────────────────────────────────────────────────
    def skip(self, states: Dict[str, Tuple[Any, Any, Any]],
             native_key: str, mtime: Any, size: Any,
             hash_: Any = None) -> bool:
        """三级增量判重：native_key 命中且 (mtime,size,hash) 全同 ⇒ 跳过不读正文。"""
        old = states.get(native_key)
        if not old:
            return False
        if old[0] != mtime or old[1] != size:
            return False
        if hash_ is not None and old[2] != hash_:
            return False
        return True


def run_adapter(writer, adapter: BaseAdapter,
                mode: str = "incremental") -> Dict[str, Any]:
    """跑一个适配器：登记 → 探测 → 采集 → 落库 → 消失判定 → 留痕。

    任何异常都变成结构化结果（不向上抛），因为「一个源挂了」不该让
    「其他源的扫描」也停 —— 逐源独立是编目层的基本纪律。
    """
    from . import schema as _schema  # noqa: F401  (延迟导入避免环)
    t0 = time.monotonic()
    writer.upsert_src(adapter.src_id, adapter.domain, adapter.kind,
                      adapter.uri, adapter.adapter or adapter.__class__.__name__,
                      adapter.scan_mode, adapter.authority, 1, {})
    probe = adapter.probe()
    if not probe.get("ok"):
        with writer.scan_run(adapter.src_id, mode) as st:
            st["errors"] = 1
            st["note"] = "%s: %s" % (probe.get("reason"), probe.get("error"))
            raise RuntimeError("%s: %s" % (probe.get("reason"), probe.get("error")))
    states = writer.existing_states(adapter.src_id)
    with writer.scan_run(adapter.src_id, mode) as st:
        out = adapter.collect(states)
        records = out.get("records") or []
        stats = writer.upsert_objects(records, adapter.src_id)
        live = out.get("live") or []
        stats["skipped"] = stats.get("skipped", 0) + max(
            0, len(set(live)) - len(records))
        st.update(stats)
        deleted = writer.mark_missing(adapter.src_id, live,
                                      bool(out.get("complete")))
        st["deleted"] = deleted
    return {
        "src_id": adapter.src_id, "domain": adapter.domain,
        "ok": True, "records": len(records), "added": stats.get("added", 0),
        "updated": stats.get("updated", 0), "skipped": stats.get("skipped", 0),
        "deleted": deleted, "complete": bool(out.get("complete")),
        "note": out.get("note") or "",
        "ms": round((time.monotonic() - t0) * 1000, 1),
    }


def run_all(writer, mode: str = "incremental") -> List[Dict[str, Any]]:
    """跑全部已注册适配器。单个失败不影响其余（结果里 ok=False）。"""
    out: List[Dict[str, Any]] = []
    for ad in all_adapters():
        try:
            out.append(run_adapter(writer, ad, mode))
        except Exception as e:  # noqa: BLE001
            out.append({"src_id": ad.src_id, "domain": ad.domain, "ok": False,
                        "records": 0, "added": 0, "updated": 0, "skipped": 0,
                        "deleted": 0, "complete": False,
                        "note": "%s: %s" % (type(e).__name__, str(e)[:200]),
                        "ms": None})
    return out
