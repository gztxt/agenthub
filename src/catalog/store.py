"""catalog store —— 连接、事务、批量 upsert（PT-20261025-12 P0）。

**写者唯一**：本模块只被 hub 进程用；适配器扫出的记录经 `CatalogWriter` 落库。
其它进程一律 `connect(readonly=True)`。

**增量判据**（方案 §8.1）：适配器先用 `existing_states()` 拿到库里
`(mtime,size,hash)`，**三级都相同就不读正文**；只要不同才进 `upsert_objects()`。

**防误删**（方案 §5 命名空间完整性）：`mark_missing()` 只在 `complete=True`
（本 src 的整个命名空间都扫完了）时执行；根目录临时不可读必须传 False，
否则会把几百条对象误标 missing。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import schema as _schema

#: 单事务批量上限（方案 §8.1）
BATCH = 2000
#: 按字索引只覆盖短文档全文；长文档仅索引首个 cid 上的标题/摘要/URI。
UNI_MAX_CHARS = 4000


def default_path() -> Path:
    """编目库位置：优先与 hub 的 DATA_DIR 同目录，保证影子实例不碰生产。"""
    home = os.getenv("AGENTHUB_HOME")
    if home:
        return Path(home) / "data" / "catalog.db"
    try:
        from config import config
        return Path(config.data_dir) / "catalog.db"
    except Exception:  # noqa: BLE001 — 独立 CLI 无 hub 配置时仍可建空库
        data_dir = os.getenv("DATA_DIR")
        if data_dir:
            return Path(data_dir) / "catalog.db"
        return Path.home() / ".agenthub" / "data" / "catalog.db"


def connect(path: Optional[Path] = None,
            readonly: bool = False) -> sqlite3.Connection:
    """打开连接。**只读连接不许建库**（`mode=ro` 遇缺文件直接报错）。"""
    p = Path(path) if path else default_path()
    if readonly:
        if not p.exists():
            raise FileNotFoundError("catalog.db 不存在：%s" % p)
        con = sqlite3.connect("file:%s?mode=ro" % quote(str(p)), uri=True,
                              timeout=5.0)
    else:
        con = sqlite3.connect(str(p), timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CatalogWriter:
    """编目库唯一写者。

    用法：
        with CatalogWriter(path) as w:
            w.upsert_src("skills.disk", "skill", "file", "...", "disk_skills")
            states = w.existing_states("skills.disk")   # 增量：不读未变文件
            w.upsert_objects(records, "skills.disk")
            w.mark_missing("skills.disk", live, complete=True)
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()
        self.con: Optional[sqlite3.Connection] = None

    # ── 生命周期 ────────────────────────────────────────────────────
    def open(self) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = connect(self.path)
        _schema.apply_pragmas(self.con)
        info = _schema.ensure_schema(self.con)
        self.con.execute("PRAGMA foreign_keys=ON")
        return info

    def close(self) -> None:
        if self.con is not None:
            try:
                self.con.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass
            self.con.close()
            self.con = None

    def __enter__(self) -> "CatalogWriter":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _require(self) -> sqlite3.Connection:
        if self.con is None:
            raise RuntimeError("CatalogWriter 未 open()")
        return self.con

    # ── 源登记 ──────────────────────────────────────────────────────
    def upsert_src(self, src_id: str, domain: str, kind: str, uri: str,
                   adapter: str, scan_mode: str = "incremental",
                   authority: int = 0, enabled: int = 1,
                   cfg: Optional[Dict[str, Any]] = None) -> None:
        con = self._require()
        con.execute(
            "INSERT INTO src(src_id,domain,kind,authority,uri,adapter,scan_mode,"
            "enabled,cfg) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(src_id) DO UPDATE SET domain=excluded.domain,"
            "kind=excluded.kind,authority=excluded.authority,uri=excluded.uri,"
            "adapter=excluded.adapter,scan_mode=excluded.scan_mode,"
            "enabled=excluded.enabled,cfg=excluded.cfg",
            (src_id, domain, kind, authority, uri, adapter, scan_mode, enabled,
             json.dumps(cfg or {}, ensure_ascii=False)))

    # ── 增量状态 ────────────────────────────────────────────────────
    def existing_states(self, src_id: str) -> Dict[str, Tuple[Any, Any, Any]]:
        """库里该 src 的全部 `native_key -> (mtime, size, hash)`。

        适配器拿它做**读前判重**：命中且三项全同 ⇒ 连正文都不用读。

        ⚠ **索引不完整的对象，hash 一律置 None**（让判重必然失败一轮）。
        原因：早期版本写过「无正文对象只留 obj 行、不写 chunk」的库；
        此后每次增量都跳过它们 ⇒ 它们永远没有 chunk ⇒ FTS 永远搜不到
        （实测 "claude" 30 条命中全在 skill 域，agent/project 全部缺席）。
        哈希相同**不代表索引完整**，判重必须额外核对 chunk 是否存在。
        代价：这类对象每轮重采集一次（数量极少），修好后自动恢复跳过。
        """
        con = self._require()
        rows = con.execute(
            "SELECT o.native_key AS nk, o.mtime AS mtime, o.size AS size, "
            "o.hash AS hash, (SELECT COUNT(*) FROM chunk c WHERE c.oid=o.oid) "
            "AS nchunk FROM obj o WHERE o.src_id=?", (src_id,)).fetchall()
        out: Dict[str, Tuple[Any, Any, Any]] = {}
        for r in rows:
            if r["nchunk"]:
                out[r["nk"]] = (r["mtime"], r["size"], r["hash"])
            else:
                out[r["nk"]] = (r["mtime"], r["size"], None)
        return out

    # ── 对象 upsert ─────────────────────────────────────────────────
    def upsert_objects(self, records: Sequence[Dict[str, Any]],
                       src_id: str) -> Dict[str, int]:
        """批量落对象 + 分域扩展行 + 分块。单 src 单事务按 BATCH 切分。

        `records` 每项字段：
          domain, native_key             必填
          type, uri, title, summary, lang, mtime, size, hash, status
          meta(dict), ext(dict), chunks(list[str])
        """
        con = self._require()
        added = updated = skipped = healed = 0
        now = _now()
        batch: List[tuple] = []
        for rec in records:
            oid = _schema.oid_of(rec["domain"], src_id, rec["native_key"])
            old = con.execute(
                "SELECT mtime,size,hash FROM obj WHERE oid=?",
                (oid,)).fetchone()
            if (old and old["mtime"] == rec.get("mtime")
                    and old["size"] == rec.get("size")
                    and old["hash"] == rec.get("hash")):
                skipped += 1
                con.execute("UPDATE obj SET last_seen=?, status=? WHERE oid=?",
                            (now, rec.get("status", "ok"), oid))
                # ⚠ 自愈：**哈希没变不代表索引完整**。早期版本写过「无正文对象
                # 只留 obj 行、不写 chunk」的库；此后所有增量轮次都会跳过它们 ⇒
                # 它们永远搜不到（实测 "claude" 30 条命中全在 skill 域，
                # agent/project 全部缺席）。所以跳过前必须核对 chunk 是否存在。
                if not con.execute("SELECT 1 FROM chunk WHERE oid=? LIMIT 1",
                                   (oid,)).fetchone():
                    self._rewrite_chunks(oid, rec.get("chunks") or [],
                                         self._header(rec))
                    healed += 1
                continue
            batch.append((
                oid, rec["domain"], rec.get("type") or rec["domain"], src_id,
                rec["native_key"], rec.get("uri"),
                (rec.get("title") or "")[:300], rec.get("summary"),
                rec.get("lang"), rec.get("mtime"), rec.get("size"),
                rec.get("hash"), rec.get("status", "ok"),
                json.dumps(rec.get("meta") or {}, ensure_ascii=False),
                now,
                json.dumps(rec.get("ext") or {}, ensure_ascii=False),
                rec.get("chunks") or [],
                old is None,
            ))
            if len(batch) >= BATCH:
                added += self._flush(batch)
                batch = []
        if batch:
            added += self._flush(batch)
        updated = len(records) - skipped - added
        return {"added": added, "updated": updated, "skipped": skipped,
                "healed": healed}

    def _flush(self, batch: List[tuple]) -> int:
        """一个 batch 一个事务：obj upsert + 扩展行 + 分块重写 + 双 FTS 同步。"""
        con = self._require()
        added = 0
        con.execute("BEGIN IMMEDIATE")
        try:
            for (oid, domain, typ, src_id, nkey, uri, title, summary, lang,
                 mtime, size, hash_, status, meta, first_seen, ext_json,
                 chunks, is_new) in batch:
                con.execute(
                    "INSERT INTO obj(oid,domain,type,src_id,native_key,uri,title,"
                    "summary,lang,mtime,size,hash,status,meta,first_seen,last_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(oid) DO UPDATE SET uri=excluded.uri,"
                    "title=excluded.title,summary=excluded.summary,"
                    "lang=excluded.lang,mtime=excluded.mtime,size=excluded.size,"
                    "hash=excluded.hash,status=excluded.status,meta=excluded.meta,"
                    "last_seen=excluded.last_seen",
                    (oid, domain, typ, src_id, nkey, uri, title, summary, lang,
                     mtime, size, hash_, status, meta, first_seen, first_seen))
                self._upsert_ext(domain, oid, ext_json)
                header = "\n".join(x for x in (title, summary, uri) if x)
                if chunks or header:
                    self._rewrite_chunks(oid, chunks, header)
                if is_new:
                    added += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return added

    @staticmethod
    def _header(rec: Dict[str, Any]) -> str:
        """对象级索引头：标题 + 摘要 + URI。

        无正文的对象（agent 名录、项目名录）**靠这一行进 FTS** ——
        没有它，这些对象在统一查询里等于不存在。
        """
        return "\n".join(x for x in (rec.get("title"), rec.get("summary"),
                                     rec.get("uri")) if x)

    def _upsert_ext(self, domain: str, oid: str, ext_json: str) -> None:
        """扩展表先删后插（列少；删后插保证消失的列不残留旧值）。"""
        con = self._require()
        tbl = _schema.DOMAIN_TABLE.get(domain)
        if not tbl:
            return
        ext = json.loads(ext_json) if isinstance(ext_json, str) else (ext_json or {})
        con.execute("DELETE FROM %s WHERE oid=?" % tbl, (oid,))
        if not ext:
            return
        cols = [c[1] for c in con.execute(
            "PRAGMA table_info(%s)" % tbl).fetchall() if c[1] != "oid"]
        names = ["oid"] + [c for c in cols if c in ext]
        vals: List[Any] = [oid] + [ext[c] for c in cols if c in ext]
        con.execute("INSERT INTO %s(%s) VALUES(%s)"
                    % (tbl, ",".join(names), ",".join("?" * len(names))), vals)

    def _rewrite_chunks(self, oid: str, chunks: List[str],
                        header: str = "") -> None:
        """重写正文与双 FTS；长文档的按字索引只保留 header，控制体积。

        external-content FTS 的 delete 必须带索引时的原文，否则删改后索引漂移。
        """
        con = self._require()
        old = con.execute(
            "SELECT cid, text FROM chunk WHERE oid=? ORDER BY seq", (oid,)
        ).fetchall()
        for r in old:
            con.execute(
                "INSERT INTO chunk_fts(chunk_fts,rowid,text) VALUES('delete',?,?)",
                (r["cid"], r["text"]))
        old_u = con.execute(
            "SELECT cid, text_uni FROM chunk_uni WHERE oid=?", (oid,)).fetchall()
        for r in old_u:
            con.execute(
                "INSERT INTO chunk_ftsu(chunk_ftsu,rowid,text_uni) "
                "VALUES('delete',?,?)", (r["cid"], r["text_uni"]))
        con.execute("DELETE FROM chunk WHERE oid=?", (oid,))
        con.execute("DELETE FROM chunk_uni WHERE oid=?", (oid,))
        texts = chunks or ([header] if header else [])
        short_doc = sum(len(t) for t in texts) <= UNI_MAX_CHARS
        for seq, text in enumerate(texts):
            cur = con.execute(
                "INSERT INTO chunk(oid,seq,text,chars) VALUES(?,?,?,?)",
                (oid, seq, text, len(text)))
            cid = cur.lastrowid
            con.execute("INSERT INTO chunk_fts(rowid,text) VALUES(?,?)",
                        (cid, text))
            if short_doc:
                uni = "\n".join(x for x in (
                    header if seq == 0 and chunks else "", text) if x)
            else:
                uni = header if seq == 0 else ""
            if uni:
                uni = _schema.cjk_split(uni)
                con.execute(
                    "INSERT INTO chunk_uni(cid,oid,text_uni) VALUES(?,?,?)",
                    (cid, oid, uni))
                con.execute("INSERT INTO chunk_ftsu(rowid,text_uni) VALUES(?,?)",
                            (cid, uni))

    # ── 消失判定 ────────────────────────────────────────────────────
    def mark_missing(self, src_id: str, live_oids: Iterable[str],
                     complete: bool) -> int:
        """把本 src 下**没在 live_oids 里**的对象标 missing。

        ⚠ `complete=False` 时**什么都不做**（方案 §5）：根目录临时不可读不能
        被解释成「文件全没了」。返回标记条数（测试据此断言不误删）。
        """
        if not complete:
            return 0
        con = self._require()
        live = set(live_oids)
        rows = con.execute(
            "SELECT oid FROM obj WHERE src_id=? AND status!='missing'",
            (src_id,)).fetchall()
        gone = [r["oid"] for r in rows if r["oid"] not in live]
        for oid in gone:
            con.execute("UPDATE obj SET status='missing', last_seen=? WHERE oid=?",
                        (_now(), oid))
        return len(gone)

    # ── 扫描账本 ────────────────────────────────────────────────────
    @contextmanager
    def scan_run(self, src_id: str, mode: str):
        """记录一次扫描：无论成功失败都留痕（方案 §6 scan_run）。"""
        con = self._require()
        started = _now()
        t0 = time.monotonic()
        run_id = con.execute(
            "INSERT INTO scan_run(src_id,mode,started_at,status) "
            "VALUES(?,?,?,'running')", (src_id, mode, started)).lastrowid
        stats: Dict[str, Any] = {"added": 0, "updated": 0, "skipped": 0,
                                 "deleted": 0, "errors": 0}
        try:
            yield stats
            status = "ok"
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            stats["note"] = "%s: %s" % (type(e).__name__, str(e)[:200])
            status = "error"
            raise
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            con.execute(
                "UPDATE scan_run SET finished_at=?,status=?,added=?,updated=?,"
                "skipped=?,deleted=?,errors=?,ms=?,note=? WHERE run_id=?",
                (_now(), status, stats.get("added", 0), stats.get("updated", 0),
                 stats.get("skipped", 0), stats.get("deleted", 0),
                 stats.get("errors", 0), ms, stats.get("note"), run_id))
            if status == "ok":
                con.execute("UPDATE src SET last_ok_at=?, last_ms=?, last_err=NULL "
                            "WHERE src_id=?", (_now(), ms, src_id))
            else:
                con.execute("UPDATE src SET last_err=? WHERE src_id=?",
                            (stats.get("note", "unknown"), src_id))
