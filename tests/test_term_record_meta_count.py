#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 闸门：录制预算计数表（v0.13.101，用户「彻底消除」）。

背景：v0.13.100 把 `SELECT SUM(bytes) FROM term_recordings`收进进程内缓存，
把逐键p50 从 677ms 压到 0.5ms—— 但**没治根**：`Recorder.__init__` 每次建会话
先 DELETE（同 sid 覆盖）再立刻算预算，而缓存规则是「DELETE 一律作废」⇒ 必然重查。
生产库48 万行上实测建会话端到端 156~210ms，其中约 108ms 是这条 SUM，
同步独占 asyncio事件循环。v0.13.101 改用单行计数表 + 数据库侧触发器。

**本闸门钉住的不变量**（每条都对应一种真实的失效形态）：

  M1 计数恒等于 SUM。计数表最大的风险不是「算错」而是「悄悄偏了」——
     预算判据只会在偏了之后很久才显形（表现为「明明没录满却不再录」），
     属于典型的写错不报错。所以每次涉及写入/删除后都必须对一次真值。
  M2 同 sid 覆盖（DELETE by session_id）后计数归零，不留残值。
  M3 sweep（DELETE by ts）后计数归零。这是**另一条**删除路径，只钉 M2 会漏。
  M4 播种幂等：存量库升级只补算一次；一个真的空库不会每帧重算 SUM
     （判据必须是「有没有这行」而不是「值是否为 0」）。
  M5 逐帧路径上**不许**再出现 SUM：静态扫源码，total_bytes() 函数体里
     不得有 `SUM(bytes)`。这条是本批的**核心不变量**—— 前面几条保证「算得对」，
     这条保证「算得便宜」，少一条就前功尽弃。
  M6 触发器必须存在且成对（INSERT 加 / DELETE 减）。少一个 ⇒ 计数单向漂移。
  M7 ts 索引必须建出来：sweep 的 DELETE WHERE ts<? 走 SCAN 时单次 77~568ms，
     且它挂在会话收尾路径上。
  M8 库异常时 total_bytes() 返回 0 而**不抛异常**（2026-10-09 终审补）。
     它每帧被调用，抛出去就是终端崩；而预算偏松只多录一点，代价不对等。

红向自证（改本文件前先读）：
  · 把 total_bytes() 换回 SUM ⇒ M5 转红；
  · 删掉 trg_termrec_del ⇒ M1/M2/M3 转红（计数只增不减）；
  · 把 ensure_counted 的判据改成「值为 0 就重算」⇒ M4 转红；
  · 把 db.query 挪出 try ⇒ M8/M8b 转红（终审实测，见两条用例的 docstring）。
"""
from __future__ import annotations

import atexit
import os
import pathlib
import re
import shutil
import sys
import sqlite3
import tempfile
import threading
import time
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")
os.environ["TERM_RECORD"] = "1"

import db            # noqa: E402
import term_record   # noqa: E402

#: 模块级共享草稿目录（各用例往里写**各自文件名**的库，互不撞名）。
#: ⚠ **必须注册回收**：此前这里是裸 `mkdtemp` 且从不清理，每次**整个测试进程**
#: （包括闸门每跑一次都会做的 `pytest --collect-only` —— 它只 import、不跑
#: `tearDownModule`）就泄漏一个 ~50MB 的 sqlite。实测累计 264 个目录 / 3.2G，
#: 把 `/` 空闲压到 2.9G（< `memindex.DISK_CRITICAL_GB=3.0`）⇒ 越过索引读闸 ⇒
#: **让另一个毫不相干的测试转红**（判例 189「闸门也会中毒」）。
#: 用 `atexit` 而不是 `addModuleCleanup`：后者依赖框架跑到 teardown，
#: 而 collect-only 不会跑到 —— 泄漏照样发生。
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="term-meta-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
_SRC_DB = _REPO / "src" / "db.py"
_SRC_REC = _REPO / "src" / "term_record.py"


def _truth() -> int:
    """真值：绕过计数表直接问表。**只有一个地方允许写 SUM**（本文件）。"""
    rows = db.query("SELECT COALESCE(SUM(bytes),0) AS n FROM term_recordings")
    return int(rows[0]["n"])


class TestSeedRace(unittest.TestCase):
    """并发播种闸门 —— 生产库实测出 11251 字节缺口之后加的。

    ★ 为什么静态断言不够：M5c 能钉住「播种是单条 SQL」这个**形态**，
      但「单条就一定对吗」只能真跑一次才知道。本类用**独立连接 + 真并发
      写入**复刻生产条件：

      · 生产库播种时不是空库 —— 已有 79万行/205MB 存量，
        `SELECT SUM` 要几十~上百毫秒，这个窗口足够 pty 读循环写进几十帧；
      · 那些帧走的是**另一个连接**（生产是 pty 读循环，测试用独立连接等价）。

      旧写法（先 SELECT SUM、再 `SET v = excluded.v` 覆盖）在本测试下
      **必然**丢字节 —— 这就是红向自证：把 _SEED_SQL 换回旧形态，本类必须转红。
      若哪天有人图「代码简洁」把它改回两语句，这里会替他记住那11251 字节。
    """

    def setUp(self):
        self.db_path = _TMP / ("race%s.db" % (self._testMethodName or id(self)))
        for suf in ("", "-wal", "-shm"):
            p = pathlib.Path(str(self.db_path) + suf)
            if p.exists():
                p.unlink()
        db.init_db(self.db_path)

    def tearDown(self):
        self._stop = True
        t = getattr(self, "_writer", None)
        if t is not None:
            t.join(timeout=10)

    def test_seed_under_concurrent_writes_keeps_count_exact(self):
        """并发写入期间播种 ⇒ 计数必须仍等于真 SUM。"""
        # 存量：模拟「升级前已落盘的历史录制」（播种要补算的就是它们）
        blob = b"x" * 700
        #直接用底层连接 executemany 灌存量：db 模块只有 execute（无批量接口），
        # 逐条 execute 会把测试拖慢两个数量级，而存量行数正是本用例的关键
        # （行数太少 ⇒ SUM 太快 ⇒ 抓不到窗口期写入）。
        db._conn.executemany(
            "INSERT INTO term_recordings(session_id,agent_id,seq,ts,direction,"
            "data,bytes) VALUES(?,?,?,?,?,?,?)",
            [("legacy", "shell", i, time.time(), "out", blob, len(blob))
             for i in range(60000)])
        db._conn.commit()
        self.assertEqual(term_record._query_total_bytes(), 60000 * len(blob))
        # 模拟「计数行尚未播种」
        db.execute("DELETE FROM term_rec_meta WHERE k='total_bytes'")

        self._stop = False
        self._written = 0

        def writer():
            """独立连接持续写入，模拟 pty 读循环。"""
            c = sqlite3.connect(str(self.db_path), timeout=30)
            c.execute("PRAGMA busy_timeout=30000")
            n = 0
            while not self._stop and n < 3000:
                try:
                    c.execute("INSERT INTO term_recordings(session_id,agent_id,seq,"
                              "ts,direction,data,bytes) VALUES(?,?,?,?,?,?,?)",
                              ("live", "shell", n, time.time(), "out", blob, len(blob)))
                    c.commit()
                    n += 1
                except sqlite3.Error:
                    break
                time.sleep(0.002)
            c.close()
            self._written = n

        self._writer = threading.Thread(target=writer, daemon=True)
        self._writer.start()
        time.sleep(0.15)          # 让 writer 先落几帧
        # ★ 走**真实代码路径** ensure_counted()，不能直接调 _SEED_SQL：
        #   首版这里写的是 db.execute(term_record._SEED_SQL)，于是把 _seed_counted()
        #   换成旧的两语句写法后，本用例**依然全绿**—— 闸门抓不到自己声称要抓的
        #   东西（红向自证当场暴露）。凡「测并发/竞态」的用例必须穿过被测函数，
        #   直接引用它内部的常量/参数等于绕过被测对象。
        term_record.ensure_counted()
        self._stop = True
        self._writer.join(timeout=10)
        time.sleep(0.2)

        self.assertGreater(self._written, 0, "并发写入一帧都没进去，测的不是并发")
        cnt = term_record.total_bytes()
        truth = _truth()
        self.assertEqual(cnt, truth,
                         "并发播种后计数 %d != 真 SUM %d（缺 %+d 字节）"
                         % (cnt, truth, truth - cnt))


class TestMetaCount(unittest.TestCase):
    def setUp(self):
        self.db_path = _TMP / ("m%d.db" % os.getpid() + str(len(self._testMethodName or "")))
        self.db_path = _TMP / ("m%s.db" % (self._testMethodName or id(self)))
        if self.db_path.exists():
            self.db_path.unlink()
        db.init_db(self.db_path)
        term_record.ensure_counted()

    # ── M1/M2/M3：写入与删除后计数必须等于真值 ──────────────────────────
    def test_M1_count_matches_sum_after_writes(self):
        r = term_record.Recorder("sA", "shell")
        self.assertTrue(r.active, "录制未激活（TERM_RECORD 未生效?）")
        for _ in range(30):
            r.feed("out", b"x" * 700)
        self.assertEqual(term_record.total_bytes(), _truth())
        self.assertEqual(term_record.total_bytes(), 21000)

    def test_M2_same_sid_overwrite_resets_count(self):
        r = term_record.Recorder("sB", "shell")
        for _ in range(20):
            r.feed("out", b"y" * 500)
        self.assertEqual(term_record.total_bytes(), 10000)
        # 同 sid 再建一条 ⇒ Recorder.__init__ 先 DELETE 上一份
        term_record.Recorder("sB", "shell")
        self.assertEqual(term_record.total_bytes(), _truth(),
                         "同 sid 覆盖后计数与真值不符")
        self.assertEqual(term_record.total_bytes(), 0)

    def test_M3_sweep_by_ts_resets_count(self):
        import time
        r = term_record.Recorder("sC", "shell")
        for _ in range(20):
            r.feed("out", b"z" * 500)
        self.assertEqual(term_record.total_bytes(), 10000)
        db.execute("UPDATE term_recordings SET ts=? WHERE session_id='sC'",
                   (time.time() - 8 * 86400,))
        term_record.sweep()
        self.assertEqual(term_record.total_bytes(), _truth(),
                         "sweep 后计数与真值不符")
        self.assertEqual(term_record.total_bytes(), 0)

    def test_M3b_multi_session_counts_sum_up(self):
        """多会话并存：计数必须是各会话之和（排除「只算一条」的实现）。"""
        rs = []
        for sid, n in (("s1", 10), ("s2", 20), ("s3", 30)):
            r = term_record.Recorder(sid, "shell")
            for _ in range(n):
                r.feed("out", b"m" * 100)
            rs.append(r)
        self.assertEqual(term_record.total_bytes(), 6000)
        self.assertEqual(term_record.total_bytes(), _truth())
        # 删掉中间那条，另两条不受影响
        term_record.Recorder("s2", "shell")
        self.assertEqual(term_record.total_bytes(), 4000)
        self.assertEqual(term_record.total_bytes(), _truth())

    # ── M4：播种幂等 ────────────────────────────────────────────────────
    def test_M4_seed_is_idempotent(self):
        r = term_record.Recorder("sD", "shell")
        for _ in range(10):
            r.feed("out", b"w" * 100)
        before = term_record.total_bytes()
        term_record.ensure_counted()
        self.assertEqual(term_record.total_bytes(), before,
                         "二次播种把已有计数覆盖了")
        self.assertEqual(term_record.total_bytes(), _truth())

    def test_M4d_seed_gate_is_presence_not_value(self):
        """播种闸门必须判「计数行在不在」，不能判「值是否为 0」。

        红向自证③：把判据改成 `if rows and v != 0: return`（空库/归零后
        每次调用都重算一遍 SUM）—— M1~M4b **全都不红**，因为计数还是对的，
        只是慢（而慢的那条路每帧都走）。所以必须有一条独立钉住判据形态的用例。

        ★ 判据从「_query_total_bytes 的调用次数」改成「执行过的 SQL 语句数」：
          v0.13.101 修掉播种竞态后，播种已改走**单条** `INSERT…SELECT`，
          不再经过 _query_total_bytes ⇒ 钉调用次数会假红。
          真正要钉的是「反复调用一次 SUM 都不该跑」。
        """
        stmts = {"n": 0}
        orig_exec = db.execute

        def counting(sql, *a, **kw):
            if "SUM(" in sql.upper():
                stmts["n"] += 1
            return orig_exec(sql, *a, **kw)

        db.execute = counting
        try:
            r = term_record.Recorder("sF", "shell")
            r.feed("out", b"p" * 100)
            term_record.Recorder("sF", "shell")      # 覆盖 ⇒ 计数归零
            self.assertEqual(term_record.total_bytes(), 0)
            before = stmts["n"]
            for _ in range(5):
                term_record.ensure_counted()          # 已播种 ⇒ 一次 SUM 都不该跑
            self.assertEqual(stmts["n"], before,
                             "计数行已存在（值为 0）却仍重算 SUM %d 次 —— "
                             "判据错把「值为 0」 当「未播种」" % (stmts["n"] - before))
        finally:
            db.execute = orig_exec

    def test_M4b_empty_db_seeds_once(self):
        """空库播种后必须是 0，且反复调用不改变它。"""
        self.assertEqual(term_record.total_bytes(), 0)
        term_record.ensure_counted()
        term_record.ensure_counted()
        self.assertEqual(term_record.total_bytes(), 0)

    def test_M4c_seed_backfills_legacy_rows(self):
        """存量库升级的**真实形态**：表里已有几十万行，但计数表还没播种。

        做法：清掉 `seeded` 标记 + 计数行（模拟「触发器刚建好、历史数据已存在」，
        而启动时的播种还没跑/ 跑过但没标记），再 ensure_counted ⇒ 必须把历史
        一次性补算进来，而不是从 0 起跳（从 0 起跳会让预算判据以为还有
        512MB 空间，盘上早已超了）。

        ★ 2026-10-09 改：原先只删 `total_bytes` 一行。改用 `seeded` 标记做闸门后，
          单删计数行会被「标记还在 ⇒ 已播种」正确地跳过—— 那是对的语义，
          所以是该测试的前置条件过时了，不是实现错。这里把标记一起清掉，
          才真正模拟「未播种」。
        """
        # 先造历史行（此时计数会被触发器抬高，无妨）
        for i in range(15):
            db.execute("INSERT INTO term_recordings(session_id,agent_id,seq,ts,"
                       "direction,data,bytes) VALUES(?,?,?,?,?,?,?)",
                       ("legacy", "shell", i, 0, "out", b"l", 1234))
        # 模拟「升级前的老库」：既没有计数行，也没有播种标记
        db.execute("DELETE FROM term_rec_meta WHERE k='total_bytes'")
        db.execute("DELETE FROM term_rec_meta WHERE k='seeded'")
        self.assertEqual(term_record.total_bytes(), 0,
                         "计数行删掉后 total_bytes 应为 0（未播种）")
        term_record.ensure_counted()
        self.assertEqual(term_record.total_bytes(), _truth(),
                         "播种后必须等于历史真值，不能从 0 起跳")
        self.assertEqual(term_record.total_bytes(), 15 * 1234)
        self.assertEqual(
            len(db.query("SELECT v FROM term_rec_meta WHERE k='seeded'")), 1,
            "播种成功后必须写 seeded 标记，否则下次启动会重复播种")

    def test_M4e_trigger_row_alone_must_not_fake_seeded(self):
        """并发首帧创建的计数行**不算**「已播种」。

        ★ 这是实测踩到的最严重一条：`trg_termrec_ins` 的
          `INSERT … ON CONFLICT DO UPDATE` 在计数行不存在时会**顺手创建**它。
          若播种闸门判的是「行在不在」，那么「查行」与「写播种值」之间来了
          首帧 ⇒ 闸门误判已播种 ⇒ 跳过 ⇒ **历史全漏算**。
          实测：4200 万字节存量 + 并发首帧 ⇒ 播种后计数仅 14000 字节。
        """
        for i in range(30):
            db.execute("INSERT INTO term_recordings(session_id,agent_id,seq,ts,"
                       "direction,data,bytes) VALUES(?,?,?,?,?,?,?)",
                       ("legacy", "shell", i, 0, "out", b"l", 1000))
        # 只清播种标记，**保留**计数行 —— 这正是「触发器已建、历史已在、
        # 但还没播种」的真实状态
        db.execute("DELETE FROM term_rec_meta WHERE k='seeded'")
        before = term_record.total_bytes()
        self.assertGreater(before, 0, "计数行应仍在（前置条件不对）")
        term_record.ensure_counted()
        self.assertEqual(term_record.total_bytes(), _truth(),
                         "计数行存在但无 seeded 标记时，必须仍然播种（"
                         "缺 %d 字节）" % (_truth() - term_record.total_bytes()))

    # ── M5：逐帧路径不许再有 SUM（核心不变量）──────────────────────────
    def test_M5_total_bytes_has_no_sum(self):
        src = _SRC_REC.read_text(encoding="utf-8")
        m = re.search(r"(?:^|\n)def total_bytes\(\)[^\n]*\n(.*?)(?=\n(?:def |class |#: ))",
                      src, re.S)
        self.assertIsNotNone(m, "找不到 total_bytes() 函数体")
        body = m.group(1)
        #只扫**可执行语句**：docstring 里写着「绝不要改回 SELECT SUM(...)」是留给
        # 后人的说明文字，不是代码。全函数体 grep SUM 会把这些说明也算进去 ⇒ 假红。
        code = "\n".join(ln for ln in body.splitlines()
                        if not ln.lstrip().startswith("#"))
        code = re.sub(r'"""..*?"""', "", code, flags=re.S)
        self.assertNotIn("SUM(", code.upper(),
                         "total_bytes() 的可执行代码里出现了 SUM —— "
                         "逐帧路径上不许有全表聚合")
        self.assertIn("term_rec_meta", code,
                      "total_bytes() 没有读计数表")

    def test_M5b_no_unguarded_sum_in_hot_path(self):
        """Recorder.feed 与 Recorder.__init__ 的预算判定都走 total_bytes()，
        自身不得直接查 SUM（否则绕开了计数表，慢路径复活）。"""
        src = _SRC_REC.read_text(encoding="utf-8")
        for fn in ("feed", "__init__"):
            m = re.search(r"def " + fn + r"\(self[^)]*\)[^\n]*\n(.*?)(?=\n    def )",
                          src, re.S)
            self.assertIsNotNone(m, "找不到 Recorder.%s" % fn)
            body = m.group(1)
            self.assertNotIn("SUM(", body,
                             "Recorder.%s 里直接查了 SUM，应改走 total_bytes()" % fn)

    def test_M5c_seed_is_single_statement(self):
        """播种必须是**一条** SQL，读 SUM 与写计数在同一快照内完成。

        ★ 这条是并发正确性的判据，不是风格偏好。生产库实测：旧的两语句写法
          （先 SELECT SUM、再 `SET v = excluded.v` 覆盖）在播种瞬间丢掉
          **11251 字节**（缺口恒定、不随后续写入变化 ⇒ 一次性偏差，
          不是触发器持续漏算）。方向恒为「计数偏低」⇒ 预算判据偏松。

        钉两件事：
          ① 播种路径上**不许**出现「先查后写」的两语句形态——
             静态上表现为 _seed_counted() 里出现 _query_total_bytes()；
          ② 播种 SQL 必须是 INSERT…SELECT（含 SELECT SUM），且带
             `WHERE true` 绕开 INSERT…SELECT + ON CONFLICT 的语法歧义。
        """
        src = _SRC_REC.read_text(encoding="utf-8")
        m = re.search(r"def _seed_counted\(\) -> None:(\s*.*?)(?=\ndef |\nclass )",
                      src, re.S)
        self.assertIsNotNone(m, "找不到 _seed_counted()")
        body = m.group(1)
        code = re.sub(r'"""..*?"""', "", body, flags=re.S)
        self.assertNotIn("_query_total_bytes", code,
                         "_seed_counted 里又用了「先查 SUM 再写」的两语句形态 —— "
                         "并发下会丢掉窗口期写入的字节（生产实测 11251 字节）")
        sql = term_record._SEED_SQL
        self.assertIn("SELECT", sql.upper())
        self.assertIn("SUM(", sql.upper())
        self.assertIn("FROM term_recordings", sql)
        self.assertIn("ON CONFLICT", sql.upper())
        self.assertIn("WHERE true", sql,
                      "INSERT…SELECT 后接 ON CONFLICT 必须有 WHERE true，"
                      "否则 SQLite 报 near \"DO\": syntax error")
        self.assertNotIn("excluded.v +", sql,
                         "播种只能是「置为 SUM 快照」，不能是「在现值上加 SUM」—— "
                         "占位行存在期间写入的帧已计过一次，再加 SUM 会双计")

    # ── M6/M7：schema 侧 ────────────────────────────────────────────────
    def test_M6_triggers_exist_and_are_paired(self):
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertIn("trg_termrec_ins", names, "缺 INSERT 触发器")
        self.assertIn("trg_termrec_del", names, "缺 DELETE 触发器")
        src = _SRC_DB.read_text(encoding="utf-8")
        self.assertIn("CREATE TRIGGER IF NOT EXISTS trg_termrec_ins", src)
        self.assertIn("CREATE TRIGGER IF NOT EXISTS trg_termrec_del", src)

    def test_M7_ts_index_exists(self):
        idx = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_termrec_ts", idx,
                      "缺 ts 索引 ⇒ sweep 的 DELETE 走 SCAN 全表")
        plan = db.query("EXPLAIN QUERY PLAN DELETE FROM term_recordings "
                        "WHERE ts < ?", (0,))[0]["detail"]
        self.assertIn("idx_termrec_ts", plan,
                      "sweep 的查询计划没有走 ts 索引：%s" % plan)


    # ── M8：库异常时 total_bytes() 必须返回 0 而不是抛异常 ──────────────────
    def test_M8_db_error_returns_zero_not_raises(self):
        """**契约是「返回 0」，不是「抛异常」**。

        现场（2026-10-09 终审）：v0.13.101 把 `try/except` 收窄到只护 `int()`，
        `db.query()` 留在 try 之外。而 `db.query()` 自身**无任何防护**（裸
        `_conn.execute`），`total_bytes()` 的两个调用点又都在**终端热路径**上
        （`Recorder.feed()` 每帧预算检查 + `Recorder.__init__`）
        ⇒ 库一异常，异常从 pty 读回调穿出去，**整个终端会话带崩**。

        实测那次 L0 跑出 10 个 error，traceback 首行就是
        `sqlite3.ProgrammingError: Cannot operate on a closed database`
        （某个用例 rmtree 掉 tmp 库后，后续用例建 Recorder 就炸）。

        为什么这条不能靠"生产上库很少出错"糊过去：代价不对等——
        预算偏松只是多录一点（上限 512MB 仍在），抛异常是终端直接不可用。
        """
        rec = term_record.Recorder("sid-m8", "pi")
        self.assertEqual(term_record.total_bytes(), 0)
        db._conn.close()                      # 模拟库异常（WAL 损坏/磁盘错/连接被换）
        self.assertEqual(term_record.total_bytes(), 0,
                         "库异常时 total_bytes() 必须兜成 0，不能抛异常"
                         "（它每帧都被调用，抛出去就是终端崩）")

    def test_M8b_recorder_survives_db_error(self):
        """Recorder 本身也必须活下来：feed 的预算检查走的就是 total_bytes()。"""
        rec = term_record.Recorder("sid-m8b", "pi")
        db._conn.close()
        rec.feed("out", b"x" * 100)            # 写失败是旁路，不许把调用方带崩
        self.assertIsNotNone(rec)

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            p = str(self.db_path) + suf
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)