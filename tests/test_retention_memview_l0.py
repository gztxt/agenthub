"""批次 B 的 L0 闸门：保留期清理 + 内存三口径 + 两级 TTL（PT-20261024-05）。

## 在测什么

1. **两级 TTL 判据**（B2）：
   - 无人观看（viewers 空）+ 静默超 `UNWATCHED_TTL_S` ⇒ 被 kill；
   - 无人观看但 **pty 仍在产出**（last_activity 新）⇒ **不杀**
     （09-19 误杀「agent 明明每 2s 在产出却被回收」的同型坑，必须守住）；
   - 有观看者 ⇒ 任何静默时长都不杀。
2. **保留期清理有界**（B3/B4）：
   - `profile_events` 超过保留期的行被删、当期行不被删；
   - 判据列是 `created_at`（**不是** `ts` —— 实测 `ts` 列不存在）；
   - 删完不跑 VACUUM（SQLite VACUUM 会重写 597MB 整库并长时间持锁）。
3. **内存三口径字段齐全**（B5）：`rss_self_kb` / `cgroup_current_bytes` /
   `cgroup_children_rss_kb`；**拿不到是 null 不是 0**（0 与「测不到」是两件事）；
   字段名带 `_bytes`/`_kb` 后缀（明确是**读数**不是常量 —— 判例 259）。
4. **保留期循环有独立心跳**：不寄生在任何会话生命周期上
   （`sweep()` 只在 `Recorder.close()` 里调 ⇒ 长开会话 ⇒ 表只增不减）。

## 红向自证
判据都写成**可机械断言**的形态（计数变化 / 字段存在 / 布尔结果），
不写「跑起来不报错」这类无法判红的断言。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

os_env_token = "unit-test-token-not-production"   # noqa: N816
import os                                                     # noqa: E402
os.environ.setdefault("TERM_TOKEN", os_env_token)

import db                                                     # noqa: E402
import memview                                                # noqa: E402
import retention                                              # noqa: E402
import term                                                   # noqa: E402


class _Stub:
    """只暴露 `_reap()` 真正用到的那几样（与 tests/test_term_reaper.py 同范式）。"""

    def __init__(self, sid, alive=True, idle_s=0.0, activity_s=None, viewers=None):
        self.id = sid
        self.alive = alive
        self.last_io = time.time() - idle_s
        self.last_activity = (time.time() - activity_s) if activity_s is not None else self.last_io
        self.viewers = viewers if viewers is not None else {}
        self.killed = 0
        self.cleaned = 0

    def kill(self):
        self.killed += 1

    def _cleanup(self):
        self.cleaned += 1


class TestTwoLevelTtl(unittest.TestCase):
    """B2：无人观看档 + 「pty 仍在产出」豁免。"""

    def _run(self, stub):
        with mock.patch.object(term, "_sessions", {stub.id: stub}):
            term._reap()

    def test_unwatched_and_idle_beyond_short_ttl_is_killed(self):
        """无人观看 + 静默超短 TTL ⇒ 必须杀（这是本批的核心收益）。"""
        s = _Stub("u1", idle_s=term.UNWATCHED_TTL_S + 10, viewers={})
        self._run(s)
        self.assertEqual(1, s.killed,
                         "无人观看且静默超 %ds 必须被 kill（485MB 的 agent CLI 不该一直挂着）"
                         % term.UNWATCHED_TTL_S)

    def test_unwatched_but_still_producing_is_not_killed(self):
        """⚠ 关键豁免：pty 仍在产出 ⇒ 不杀（09-19 误杀的同型坑）。"""
        s = _Stub("u2", idle_s=term.UNWATCHED_TTL_S + 10, activity_s=0.0, viewers={})
        self._run(s)
        self.assertEqual(0, s.killed,
                         "agent 还在真干活（last_activity 是新的）时不得被无人观看档误杀")

    def test_watched_session_never_killed_by_ttl(self):
        """有观看者 ⇒ 任何静默时长都不杀。"""
        s = _Stub("u3", idle_s=term.IDLE_TTL_S * 10, activity_s=term.IDLE_TTL_S * 10,
                  viewers={"vid1": object()})
        self._run(s)
        self.assertEqual(0, s.killed, "有人看着的会话绝不能被 TTL 杀掉")

    def test_short_ttl_is_not_longer_than_base(self):
        """无人观看档必须**不长于**基础 TTL（否则反而更宽松 = 反向 bug）。"""
        self.assertLessEqual(term.UNWATCHED_TTL_S, term.IDLE_TTL_S,
                             "UNWATCHED_TTL_S 必须 ≤ IDLE_TTL_S")

    def test_base_ttl_default_is_reduced(self):
        """B2 的另一半：基础 TTL 默认从 45min 收到 10min。"""
        # 用 env 覆盖后的实际值判（测试进程里 term 模块已 import）
        self.assertLessEqual(term.IDLE_TTL_S, 600,
                             "IDLE_TTL_S 默认应收紧到 ≤600s（实测原值 2700）")


class TestProfileEventsRetention(unittest.TestCase):
    """B4：profile_events 滚动保留期。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0ret-"))
        db.init_db(self.tmp / "t.db")

    def _insert(self, created_at, n=1):
        for _ in range(n):
            db.execute(
                "INSERT INTO profile_events(source,subject,trace_id,status,duration_ms,"
                "detail,created_at) VALUES(?,?,?,?,?,?,?)",
                ("hub_chat", "x", None, "success", 1, None, created_at))

    def test_expired_rows_deleted_and_recent_kept(self):
        old = retention._days_ago_iso(40)
        new = retention._days_ago_iso(1)
        self._insert(old, n=5)
        self._insert(new, n=3)
        n = retention.sweep_profile_events(days=30)
        self.assertEqual(5, n, "应删掉 5 行超期记录")
        left = db.query("SELECT created_at FROM profile_events")
        self.assertEqual(3, len(left), "当期 3 行必须留着")
        self.assertTrue(all(r["created_at"] == new for r in left))

    def test_created_at_is_the_evidence_column(self):
        """判据列必须是 created_at —— 实测 ts 列不存在（曾猜错过）。"""
        # ⚠ db.query 返回的是 **dict**（row_factory=sqlite3.Row），不是 tuple
        #   ⇒ 用 r["name"] 取列名；用 r[1] 会 KeyError（实测踩过）。
        cols = {r["name"] for r in db.query("PRAGMA table_info(profile_events)")}
        self.assertIn("created_at", cols)
        self.assertNotIn("ts", cols, "profile_events 没有 ts 列（别再按 ts 写）")

    def test_sweep_is_idempotent(self):
        old = retention._days_ago_iso(40)
        self._insert(old, n=3)
        self.assertEqual(3, retention.sweep_profile_events(days=30))
        self.assertEqual(0, retention.sweep_profile_events(days=30),
                         "第二次扫不应再删（幂等）")

    def test_no_vacuum_in_retention_module(self):
        """不得跑 VACUUM：597MB 整库重写 + 长时间持锁。

        ⚠ 判据只查**可执行语句**，不查注释：模块 docstring 里**故意**写着
        「不跑 VACUUM」这句话，全文 grep 会命中它自己（实测踩过：判红的是
        docstring 里的字样，不是真调用）。⇒ 用 AST 只看字符串常量里
        **不含否定词**的那些（即真正的 SQL）。
        """
        import ast
        src = (ROOT / "src" / "retention.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if "VACUUM" in s.upper() and "不跑" not in s and "不许" not in s:
                    self.fail("retention.py 里出现了可执行的 VACUUM：%r" % s[:120])
        # 反向确认：没有任何 "VACUUM 语句形态
        self.assertNotIn('"VACUUM', src.replace("'", '"'),
                         "retention.py 不许执行 VACUUM")

    def test_retention_days_default_is_sane(self):
        self.assertGreater(retention.DEFAULT_PROFILE_EVENTS_DAYS, 0)
        self.assertLessEqual(retention.DEFAULT_PROFILE_EVENTS_DAYS, 90,
                             "保留期上限 90 天（再长该问「这些审计还要不要」）")


class TestRetentionLoopWiring(unittest.TestCase):
    """B3/B4：保留期清理必须有独立心跳，不能寄生在会话生命周期上。"""

    def test_retention_loop_is_spawned_in_startup(self):
        src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("retention.retention_loop()", src,
                      "startup 里必须 spawn retention_loop（否则长开会话 ⇒ 表只增不减）")
        self.assertIn("_spawn(retention.retention_loop())", src,
                      "必须走 _spawn（裸 create_task 会被 GC 回收 ⇒ 静默停摆）")

    def test_retention_loop_has_own_interval(self):
        self.assertGreater(retention.DEFAULT_INTERVAL_S, 0,
                           "保留期循环必须有正的间隔")
        # 不该密到 1 分钟级（DELETE 不删几行也付一次扫描代价）
        self.assertGreaterEqual(retention.DEFAULT_INTERVAL_S, 600,
                                "间隔不该密于 10 分钟（保留期以天计）")


class TestMemViewThreeScopes(unittest.TestCase):
    """B5：内存三口径。"""

    def test_snapshot_has_three_fields(self):
        snap = memview.memory_snapshot()
        for k in ("rss_self_kb", "cgroup_current_bytes", "cgroup_children_rss_kb"):
            self.assertIn(k, snap, "缺字段 %s（判例 259：三口径缺一即误导）" % k)

    def test_unmeasurable_is_none_not_zero(self):
        """拿不到报 None（不是 0）：0 与「测不到」是两件事。"""
        with mock.patch.object(memview, "_cgroup_dir", return_value=None):
            snap = memview.memory_snapshot()
        self.assertIsNone(snap["cgroup_current_bytes"],
                          "测不到必须报 None；报 0 = 宣称「内存占用为零」")
        self.assertIsNone(snap["cgroup_children_rss_kb"])

    def test_self_rss_is_plausible(self):
        snap = memview.memory_snapshot()
        v = snap["rss_self_kb"]
        self.assertIsInstance(v, int)
        self.assertGreater(v, 1024, "进程 RSS 不可能小于 1MB（判据写错了吧）")

    def test_health_exposes_memory_field(self):
        src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn('out["memory"]', src, "/health 必须暴露 memory 字段")

    def test_health_memory_is_wrapped_in_try(self):
        """取内存读数不许拖垮 /health（其它字段仍要回）。"""
        src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        i = src.find('out["memory"]')
        self.assertGreater(i, 0)
        seg = src[max(0, i - 400):i]
        self.assertIn("try:", seg, "取 memory 必须裹在 try 里（取不到不许 500）")


if __name__ == "__main__":
    unittest.main()
