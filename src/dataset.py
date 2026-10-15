"""数据集 / 评估 / 经验库存储（P0-1 切片 A：四表 + `grain` 唯一键 + `split` 隔离）。

【它在闭环里的位置】
    轨迹（hook/profile_events）→ **数据集** → 评估（eval_run）→ **经验库**（experiences）
本模块只落**确定性地基**：四张表、入样、隔离断言。管线与经验挖掘在切片 B/C
（`scripts/dataset_pipeline.py` / `scripts/experience_mine.py`），本模块不调模型、不联网。

【四条不变量（都被 tests/test_dataset.py 钉住）】
1. **`grain` 唯一键**：同一逻辑样本（同 `grain_key`）重复写入 ⇒ 复用同一行。
   否则同一件事会被灌进数据集多次，指标随之失真。
2. **`split` 隔离**：`holdout` 样本**永不**出现在优化（train）批次；
   `assert_train_only()` 是给管线用的**硬闸**（掺入即抛）。且已入样样本的 split **不可改**
   —— 否则「隔离」可被一次改写抹掉。
3. **字段映射显式**（手册 D7）：`input`/`output`/`trajectory` 是三列，各有其义；
   `input` 空 ⇒ 拒（别把最终答案与轨迹接反）。
4. **版本快照**（D13）：`eval_runs` 记 `dataset_version`，比较期间不追加/修改。

术语：`grain` = 该数据集的**唯一键口径**（如 `task`、`session+step`）；`grain_hash` =
`sha256(grain + 规范化 grain_key)` 前 32 位。
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import db

SPLITS = ("train", "holdout")

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    grain TEXT NOT NULL,              -- 唯一键口径说明
    version INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    split TEXT NOT NULL,              -- train|holdout
    grain_hash TEXT NOT NULL,
    input TEXT NOT NULL,              -- 输入（题面/上下文）
    output TEXT,                      -- 期望输出（答案）；与 input 分开列
    trajectory TEXT,                  -- 轨迹（JSON，schema 见手册 D5，本切片不展开）
    trace_id TEXT, session_id TEXT, source_ref TEXT,   -- 溯源（D4）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dataset_id, grain_hash)
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_version INTEGER NOT NULL, -- 版本快照（D13）：比较期间不改
    split TEXT NOT NULL,
    model TEXT, config_hash TEXT,     -- 被测配置 hash（D4）
    status TEXT NOT NULL DEFAULT 'pinned',
    metrics TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS experiences (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,               -- pitfall|recipe|...
    situation TEXT, action TEXT, precondition TEXT,    -- 情况→处理 + 适用条件（D17）
    source_trace_ids TEXT,            -- 回源 Trace 核对
    created_at TEXT NOT NULL,
    retired_at TEXT, superseded_by TEXT               -- Dreaming：删过期/解冲突
);
CREATE INDEX IF NOT EXISTS idx_items_ds_split ON dataset_items(dataset_id, split);
CREATE INDEX IF NOT EXISTS idx_experiences_live ON experiences(retired_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    db.execute_script(SCHEMA)


def grain_hash(grain: str, key_fields: dict) -> str:
    """`sha256(grain + 规范化 key_fields)` 前 32 位。key 顺序无关（sort_keys）。"""
    payload = json.dumps(key_fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(("%s\n%s" % (grain, payload)).encode("utf-8")).hexdigest()[:32]


# ── datasets ─────────────────────────────────────────────────────────
def create_dataset(name: str, *, grain: str, note: Optional[str] = None) -> str:
    """按 name 幂等建数据集（同名复用，不重复建）。"""
    rows = db.query("SELECT id FROM datasets WHERE name=?", (name,))
    if rows:
        return rows[0]["id"]
    did = uuid.uuid4().hex[:10]
    db.execute("INSERT INTO datasets(id,name,grain,version,note,created_at)"
               " VALUES(?,?,?,1,?,?)", (did, name, grain, note, _now()))
    return did


def get_dataset(dataset_id: str) -> Optional[dict]:
    rows = db.query("SELECT * FROM datasets WHERE id=?", (dataset_id,))
    return rows[0] if rows else None


# ── dataset_items ────────────────────────────────────────────────────
def add_item(dataset_id: str, *, split: str, input: str, output: str = "",
             trajectory: str = "", trace_id: str = "", session_id: str = "",
             source_ref: str = "", grain_key: Optional[dict] = None) -> int:
    """入样（幂等）。

    - `split` 必须是 train|holdout；已存在同 grain 样本时**沿用原 split**（改即抛）。
    - `grain_key` 缺省用 `{input, trace_id}`；显式给出可表达「同题不同轨迹算同一样本」。
    """
    if split not in SPLITS:
        raise ValueError("split 必须是 %s，得到 %r" % (SPLITS, split))
    if not str(input or "").strip():
        raise ValueError("input 不能为空（别把最终答案与轨迹接反）")
    ds = get_dataset(dataset_id)
    if ds is None:
        raise KeyError("dataset 不存在: %s" % dataset_id)
    key = grain_key if grain_key is not None else {"input": input, "trace_id": trace_id}
    gh = grain_hash(ds["grain"], key)
    existing = db.query("SELECT id,split FROM dataset_items"
                        " WHERE dataset_id=? AND grain_hash=?", (dataset_id, gh))
    if existing:
        if existing[0]["split"] != split:
            raise ValueError("已入样样本的 split 不可改（%s → %s）"
                             % (existing[0]["split"], split))
        return existing[0]["id"]
    now = _now()
    db.execute(
        "INSERT INTO dataset_items(dataset_id,split,grain_hash,input,output,trajectory,"
        "trace_id,session_id,source_ref,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (dataset_id, split, gh, input, output, trajectory, trace_id, session_id,
         source_ref, now, now))
    return db.query("SELECT id FROM dataset_items WHERE dataset_id=? AND grain_hash=?",
                    (dataset_id, gh))[0]["id"]


def get_item(item_id: int) -> Optional[dict]:
    rows = db.query("SELECT * FROM dataset_items WHERE id=?", (item_id,))
    return rows[0] if rows else None


def list_items(dataset_id: str, split: Optional[str] = None) -> List[dict]:
    if split:
        return db.query("SELECT * FROM dataset_items WHERE dataset_id=? AND split=?"
                        " ORDER BY id", (dataset_id, split))
    return db.query("SELECT * FROM dataset_items WHERE dataset_id=? ORDER BY id",
                    (dataset_id,))


def training_items(dataset_id: str) -> List[dict]:
    """优化批次 = train 切分。**永不**含 holdout（验收②）。"""
    return list_items(dataset_id, split="train")


def counts(dataset_id: str) -> dict:
    rows = db.query(
        "SELECT split, COUNT(*) n FROM dataset_items WHERE dataset_id=? GROUP BY split",
        (dataset_id,))
    by = {r["split"]: r["n"] for r in rows}
    return {"train": by.get("train", 0), "holdout": by.get("holdout", 0),
            "total": sum(by.values())}


def assert_train_only(dataset_id: str, item_ids) -> None:
    """硬闸：给定样本里只要有一个不是 train（或不属于本数据集），即抛 ValueError。

    管线的「确定性过滤→采样」步在交给模型之前**必须**过这一道 ——
    否则 holdout 会被无声地拿去优化，「隔离」就只剩一句口号。
    """
    ids = list(item_ids or [])
    if not ids:
        return
    ph = ",".join("?" * len(ids))
    rows = db.query("SELECT id,split FROM dataset_items WHERE dataset_id=? AND id IN (%s)"
                    % ph, (dataset_id, *ids))
    seen = {r["id"] for r in rows}
    foreign = sorted(set(ids) - seen)
    if foreign:
        raise ValueError("这些样本不属于该数据集: %s" % foreign)
    bad = sorted(r["id"] for r in rows if r["split"] != "train")
    if bad:
        raise ValueError("优化批次掺入非 train 样本: %s" % bad)


# ── eval_runs ────────────────────────────────────────────────────────
def pin_eval(dataset_id: str, *, split: str, model: Optional[str] = None,
             config_hash: Optional[str] = None) -> str:
    """钉一次评估：记下 `dataset_version`（快照），比较期间不改数据集（D13）。"""
    ds = get_dataset(dataset_id)
    if ds is None:
        raise KeyError("dataset 不存在: %s" % dataset_id)
    if split not in SPLITS:
        raise ValueError("split 必须是 %s，得到 %r" % (SPLITS, split))
    eid = uuid.uuid4().hex[:10]
    db.execute(
        "INSERT INTO eval_runs(id,dataset_id,dataset_version,split,model,config_hash,"
        "status,created_at) VALUES(?,?,?,?,?,?,'pinned',?)",
        (eid, dataset_id, ds["version"], split, model, config_hash, _now()))
    return eid


def get_eval_run(eval_id: str) -> Optional[dict]:
    rows = db.query("SELECT * FROM eval_runs WHERE id=?", (eval_id,))
    return rows[0] if rows else None


# ── experiences ──────────────────────────────────────────────────────
def record_experience(kind: str, *, situation: str = "", action: str = "",
                      precondition: str = "", source_trace_ids=None) -> str:
    """入库一条经验（情况→处理 + 适用条件 + 回源 trace）。"""
    eid = uuid.uuid4().hex[:10]
    db.execute(
        "INSERT INTO experiences(id,kind,situation,action,precondition,source_trace_ids,"
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (eid, kind, situation, action, precondition,
         json.dumps(list(source_trace_ids or []), ensure_ascii=False), _now()))
    return eid


def get_experience(exp_id: str) -> Optional[dict]:
    rows = db.query("SELECT * FROM experiences WHERE id=?", (exp_id,))
    return rows[0] if rows else None


def list_experiences(active_only: bool = True) -> List[dict]:
    if active_only:
        return db.query("SELECT * FROM experiences WHERE retired_at IS NULL"
                        " ORDER BY created_at DESC, id")
    return db.query("SELECT * FROM experiences ORDER BY created_at DESC, id")


def retire_experience(exp_id: str, superseded_by: Optional[str] = None) -> None:
    """退役一条经验（Dreaming 的「删过期」）：不物理删，记 `retired_at`/`superseded_by`。"""
    db.execute("UPDATE experiences SET retired_at=?, superseded_by=? WHERE id=?",
               (_now(), superseded_by, exp_id))