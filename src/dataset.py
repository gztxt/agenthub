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

#: 评估研判三态（与 P0-4 `evidence.py` / P0-3 `verifier.py` 同口径）
EVAL_VERDICTS = ("pass", "fail", "insufficient")

#: D5：轨迹行的**统一 schema 版本**。落库前必须过 `normalize_trajectory()`。
TRAJECTORY_SCHEMA = "trajectory/1"

#: D16：回写到 `dataset_items` 的四列（幂等 ALTER 补建；旧行 NULL = 未判，不回填）
_ITEM_COLUMNS = (
    ("last_eval_id", "TEXT"),
    ("last_verdict", "TEXT"),
    ("last_score", "REAL"),
    ("last_eval_at", "TEXT"),
)

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
-- D13：版本推进的**只读快照**（每次 bump 记一行；eval_runs.dataset_version 引用它）
CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    db.execute_script(SCHEMA)
    # D16：存量 `dataset_items` 幂等补列（不动既有行；旧行 last_verdict 保持 NULL=未判）。
    have = {r["name"] for r in db.query("PRAGMA table_info(dataset_items)")}
    for col, decl in _ITEM_COLUMNS:
        if col not in have:
            db.execute("ALTER TABLE dataset_items ADD COLUMN %s %s" % (col, decl))


# ── D5：轨迹行 schema ────────────────────────────────────────────────
def normalize_trajectory(traj: dict) -> dict:
    """把轨迹折成**统一结构**（补 `seq` 与 `metrics`）。幂等：已规范的直接返回。

    D5 的痛点不是「没存轨迹」，而是「每条轨迹形状各不同」⇒ 追得到结果追不到原因。
    """
    if not isinstance(traj, dict):
        raise ValueError("轨迹必须是对象")
    if traj.get("schema") == TRAJECTORY_SCHEMA and isinstance(traj.get("steps"), list):
        return traj
    events = traj.get("events") or []
    steps, errors, total = [], 0, 0
    for i, e in enumerate(events):
        e = e or {}
        status = e.get("status")
        dur = e.get("duration_ms")
        steps.append({"seq": i, "event": e.get("event"), "status": status,
                      "duration_ms": dur, "detail": e.get("detail"),
                      "at": e.get("created_at") or e.get("at")})
        if status not in (None, "", "success", "ok"):
            errors += 1
        if isinstance(dur, int):
            total += dur
    return {"schema": TRAJECTORY_SCHEMA,
            "session_id": traj.get("session_id"), "source": traj.get("source"),
            "cwd": traj.get("cwd"), "steps": steps,
            "metrics": {"steps": len(steps), "errors": errors, "total_ms": total},
            "source_ref": traj.get("source_ref") or ("trajectory:%s" % traj.get("session_id"))}


def validate_trajectory(d: dict) -> List[str]:
    """返回**缺口清单**（空 = 合格）。不抛异常 —— 调用方决定是拒还是记。"""
    if not isinstance(d, dict):
        return ["轨迹不是对象"]
    gaps = []
    if d.get("schema") != TRAJECTORY_SCHEMA:
        gaps.append("schema 不是 %s" % TRAJECTORY_SCHEMA)
    steps = d.get("steps")
    if not isinstance(steps, list) or not steps:
        gaps.append("steps 缺失或为空")
    else:
        for i, s in enumerate(steps):
            if not isinstance(s, dict) or s.get("seq") != i:
                gaps.append("steps[%d].seq 不是 %d" % (i, i))
                break
    return gaps


def dumps_trajectory(traj: dict) -> str:
    return json.dumps(normalize_trajectory(traj), ensure_ascii=False, sort_keys=True)


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


def list_datasets() -> List[dict]:
    return db.query("SELECT * FROM datasets ORDER BY created_at DESC, id")


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


def bump_version(dataset_id: str, note: Optional[str] = None) -> int:
    """D13：推进数据集版本（**只增不减**）并记一行快照。返回新版本号。"""
    ds = get_dataset(dataset_id)
    if ds is None:
        raise KeyError("dataset 不存在: %s" % dataset_id)
    new_v = int(ds["version"]) + 1
    db.execute("UPDATE datasets SET version=? WHERE id=?", (new_v, dataset_id))
    db.execute("INSERT INTO dataset_versions(dataset_id,version,note,created_at)"
               " VALUES(?,?,?,?)", (dataset_id, new_v, note, _now()))
    return new_v


def list_versions(dataset_id: str) -> List[dict]:
    return db.query("SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version",
                    (dataset_id,))


def eval_is_stale(eval_id: str) -> bool:
    """评估是否陈旧（D13）：跑完才发现题集被改过 ⇒ 两个分数不可比。

    拿不到评估行 ⇒ 也回 True（判不了按陈旧算 —— 与 P0-4「判不了不放行」同向）。
    """
    ev = get_eval_run(eval_id)
    if not ev:
        return True
    ds = get_dataset(ev["dataset_id"])
    if not ds:
        return True
    return int(ev["dataset_version"]) != int(ds["version"])


def record_eval_result(eval_id: str, item_id: int, *, verdict: str,
                       score: Optional[float] = None) -> None:
    """D16：把评估结果**回写**到 `dataset_item`（badcase 池从库里长出来）。

    三条硬闸：verdict 必须是 `EVAL_VERDICTS`；题必须存在且属于该评估的数据集；
    评估**不得陈旧**（版本变了就不许回写 —— 那条结果对不上题目集）。
    """
    if verdict not in EVAL_VERDICTS:
        raise ValueError("verdict 必须是 %s，得到 %r" % (EVAL_VERDICTS, verdict))
    ev = get_eval_run(eval_id)
    if ev is None:
        raise KeyError("eval_run 不存在: %s" % eval_id)
    item = get_item(item_id)
    if item is None or item.get("dataset_id") != ev["dataset_id"]:
        raise KeyError("样本 %r 不属于数据集 %s" % (item_id, ev["dataset_id"]))
    dataset_id = ev["dataset_id"]
    # 这一行必须在**写之前**判：否则陈旧结果会先污染再被纠正
    if eval_is_stale(eval_id):
        raise ValueError("评估 %s 已陈旧（版本已变）—— 结果不可回写" % eval_id)
    db.execute("UPDATE dataset_items SET last_eval_id=?,last_verdict=?,last_score=?,"
               "last_eval_at=?,updated_at=? WHERE id=?",
               (eval_id, verdict, score, _now(), _now(), item_id))
    db.execute("UPDATE eval_runs SET status=? WHERE id=?", ("scored", eval_id))


def badcases(dataset_id: str) -> List[dict]:
    """badcase 池 = 回写过且 **verdict != pass** 的题（含 insufficient：判不了也是候选）。
    没跑过的（`last_verdict IS NULL`）不算 —— 不能把「没测」当成「测坏了」。"""
    return db.query(
        "SELECT * FROM dataset_items WHERE dataset_id=? AND last_verdict IS NOT NULL "
        "AND last_verdict != 'pass' ORDER BY id", (dataset_id,))


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


def search_experiences(q: str, limit: int = 20) -> List[dict]:
    """在**活跃**经验里按关键词召回（LIKE；空查询返回空 —— 不给「全部」）。

    这是经验库的「召回」路径：只召回 `retired_at IS NULL` 的条目，退役的只能
    按 id 追溯（`get_experience`），不会汿回来。
    """
    needle = str(q or "").strip()
    if not needle:
        return []
    like = "%" + needle + "%"
    return db.query(
        "SELECT * FROM experiences WHERE retired_at IS NULL AND ("
        "situation LIKE ? OR action LIKE ? OR precondition LIKE ? OR kind LIKE ?)"
        " ORDER BY created_at DESC, id LIMIT ?",
        (like, like, like, like, int(limit)))