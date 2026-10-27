#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：技能权重（PT-20261020-01）。

钉住的是**口径**不是数字：曝光（auto_inject）只能进分母，使用（explicit/read）才是正样本。
四条判据任一被改，权重就会重新变成「谁被推得多谁最热」：

1. auto=0 时 score == use（手工调用不受惩罚）；use=0 且 auto>0 时 score == 0；
2. auto 增大时 score 单调不增；
3. 按 caller 过滤是**先过滤再算分**（换个人看，分母不许带别人的曝光）；
4. 每条 score 的可加项都能追到具体 event id（`evidence`），且 id 在库里真实存在。

`legacy`（2026-10-20 前无 mode/via 的 relevant）单独计数，不进 use 也不进 auto——
反推归属就是造数据。
"""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db                     # noqa: E402
import skill                  # noqa: E402
import skill_weights          # noqa: E402
import writeauth              # noqa: E402

from fastapi import FastAPI   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _mk_skill(root: pathlib.Path, name: str, desc: str = "测试技能") -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n正文\n", encoding="utf-8")
    return d


class TestFormula(unittest.TestCase):
    """公式本身的性质。纯函数，不碰库也不碰 HTTP。"""

    def test_no_exposure_equals_use(self):
        """从没被自动推过 ⇒ score 就是使用量，不该被惩罚。"""
        self.assertEqual(skill_weights.score(5, 0), (5.0, 1.0))
        self.assertEqual(skill_weights.score(0, 0), (0.0, 0.0))

    def test_exposure_only_scores_zero(self):
        """被推 100 次一次没打开 ⇒ 0 分。这是本公式存在的理由。"""
        self.assertEqual(skill_weights.score(0, 100), (0.0, 0.0))

    def test_auto_is_denominator_only(self):
        """auto 增大时 score 单调不增（不是加分项）。"""
        prev = None
        for auto in (0, 1, 5, 50, 500):
            s, _ = skill_weights.score(6, auto)
            if prev is not None:
                self.assertLessEqual(s, prev)
            prev = s

    def test_use_monotonic(self):
        prev = -1.0
        for use in (0, 1, 3, 10, 100):
            s, _ = skill_weights.score(use, 10)
            self.assertGreaterEqual(s, prev)
            prev = s

    def test_conversion_reported_separately(self):
        """score 低有两种原因（用得少 / 被推得多），必须能分开看。"""
        s1, c1 = skill_weights.score(2, 0)
        s2, c2 = skill_weights.score(2, 100)
        self.assertEqual(c1, 1.0)
        self.assertLess(c2, 0.05)
        self.assertLess(s2, s1)


class _WeightsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0weights-"))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        for p in (self.a, self.b):
            p.mkdir()
        self._saved_dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"src": str(self.a), "other": str(self.b)}
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        db.init_db(self.tmp / "t.db")
        _app = FastAPI()
        _app.include_router(skill.router)
        _app.middleware("http")(writeauth.write_gate)
        self._saved_token = __import__("os").environ.get("TERM_TOKEN")
        __import__("os").environ["TERM_TOKEN"] = "l0-weights-token"
        __import__("os").environ["SKILL_LOCAL_ROOTS_JSON"] = "[]"
        self.client = TestClient(_app)
        self.addCleanup(self._restore)

    def _restore(self):
        import os
        skill.SKILL_DIRS = self._saved_dirs
        if self._saved_token is None:
            os.environ.pop("TERM_TOKEN", None)
        else:
            os.environ["TERM_TOKEN"] = self._saved_token
        os.environ.pop("SKILL_LOCAL_ROOTS_JSON", None)
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hdr(self):
        return {"x-hub-token": "l0-weights-token"}

    def _invoke(self, name="demo", route="src", caller="codex"):
        return self.client.post("/api/skill/invoke",
                                json={"name": name, "route": route, "caller": caller,
                                      "task": "权重测试"}, headers=self._hdr())

    def _read(self, name="demo", route="src"):
        return self.client.get("/api/skill/read",
                               params={"name": name, "route": route}, headers=self._hdr())

    def _relevant(self, q="权重技能演示", caller="claude", via="hook", n=1):
        params = {"q": q, "n": n, "rerank": "false"}
        if caller:
            params["caller"] = caller
        if via:
            params["via"] = via
        return self.client.get("/api/skill/relevant", params=params, headers=self._hdr())

    def _usage(self, **kw):
        r = self.client.get("/api/skill/usage", params=kw, headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestUsageEndpoint(_WeightsCase):
    def test_buckets_and_score_end_to_end(self):
        """一次点名调用 + 一次阅读 + 一次自动下发 + 一次人工查询 + 一条旧账 ⇒ 可逐项核对。"""
        _mk_skill(self.a, "demo", "权重技能演示 用于口径测试")
        self.assertEqual(self._invoke().status_code, 200)
        self.assertEqual(self._read().status_code, 200)
        self.assertEqual(self._relevant().status_code, 200)
        self.assertEqual(self._relevant(caller="", via="").status_code, 200)
        db.execute("INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                   " VALUES('rest','skill.relevant','success',1,?, '2026-10-20T00:00:00+00:00')",
                   (json.dumps({"q": "旧账", "name": "demo", "route": "src"}),))

        b = self._usage(days=30)
        row = next(x for x in b["items"] if x["name"] == "demo")
        self.assertEqual((row["explicit"], row["read"], row["auto"], row["query"], row["legacy"]),
                         (1, 1, 1, 1, 1), row)
        # use = 2*1 + 1*1 = 3；score = 9/(3+1) = 2.25
        self.assertEqual(row["use"], 3)
        self.assertEqual(row["score"], 2.25)
        self.assertEqual(row["conversion"], 0.75)
        self.assertEqual(row["by_caller"]["explicit"], {"codex": 1})
        self.assertEqual(row["by_caller"]["auto"], {"claude": 1})
        self.assertEqual(b["totals"]["legacy"], 1, "旧账必须显式报数，不能静默丢")
        self.assertEqual(b["weights"]["auto"], "denominator-only")

    def test_evidence_ids_exist_in_db(self):
        """score 的每一项都要能追到具体 event id（台账验收：可追溯）。"""
        _mk_skill(self.a, "demo", "权重技能演示")
        self._invoke()
        ids = [e["id"] for e in db.query("SELECT id FROM profile_events")]
        b = self._usage()
        row = b["items"][0]
        got = row["evidence"]["explicit"]
        self.assertEqual(len(got), 1)
        self.assertIn(got[0], ids, "evidence 里的 id 在库里必须真实存在")

    def test_caller_filter_recomputes_not_post_filters(self):
        """按 caller 过滤 = 只拿这个人的事件重算。codex 没有自动下发 ⇒ auto=0、conversion=1。"""
        _mk_skill(self.a, "demo", "权重技能演示")
        self._invoke(caller="codex")
        self._relevant(caller="claude", via="hook")
        codex = next(x for x in self._usage(caller="codex")["items"] if x["name"] == "demo")
        self.assertEqual((codex["explicit"], codex["auto"]), (1, 0))
        self.assertEqual(codex["conversion"], 1.0)
        claude = next(x for x in self._usage(caller="claude")["items"] if x["name"] == "demo")
        self.assertEqual((claude["explicit"], claude["auto"], claude["score"]), (0, 1, 0.0))

    def test_scan_source_counted_and_direct_read_is_read(self):
        """v0.13.166：转录采集（`source=scan`）必须进榜单。

        回退点（2026-10-27 生产实测）：只读 `source='rest'` 时，7 天里 180 条 scan
        `skill.invoke`（`agent-dispatch` 一条就 42 条）在 `/api/skill/usage` 上仍报
        `explicit=0`——agent 直读磁盘的使用在页面上完全不可见。
        同时：scan 一律写 `mode=direct_read`，只有 `evidence=skill_tool` 才是真调工具
        （explicit=2），读盘/命令行命中按 read=1 计，否则整榜权重被系统性抬高。
        """
        import datetime as _dt
        _mk_skill(self.a, "demo", "权重技能演示")
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for cid, ev in (("c1", "skill_tool"), ("c2", "read")):
            db.execute(
                "INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                " VALUES('scan','skill.invoke','success',1,?,?)",
                (json.dumps({"name": "demo", "caller": "opencode", "via": "transcript",
                             "evidence": ev, "session": "s.jsonl", "call_id": cid,
                             "mode": "direct_read"}), now))
        row = next(x for x in self._usage(days=7)["items"] if x["name"] == "demo")
        self.assertEqual((row["explicit"], row["read"]), (1, 1), row)
        self.assertEqual(row["by_caller"]["explicit"], {"opencode": 1})
        self.assertEqual(row["use"], 3)          # 2*1 + 1*1
        self.assertEqual(row["route"], "", "转录采集不带 route，榜上留空而不是编一个")

    def test_identity_is_name_and_route(self):
        """同名两路必须是两行；by_name 汇总列出两条 route（合并会掩盖死链）。"""
        _mk_skill(self.a, "dup", "权重技能演示")
        _mk_skill(self.b, "dup", "权重技能演示")
        self.assertEqual(self._invoke(name="dup", route="src").status_code, 200)
        self.assertEqual(self._invoke(name="dup", route="other", caller="pi").status_code, 200)
        b = self._usage()
        rows = [x for x in b["items"] if x["name"] == "dup"]
        self.assertEqual(sorted(x["route"] for x in rows), ["other", "src"])
        self.assertTrue(all(x["explicit"] == 1 for x in rows))
        name_row = next(x for x in b["by_name"] if x["name"] == "dup")
        self.assertEqual(sorted(name_row["routes"]), ["other", "src"])
        self.assertEqual(name_row["explicit"], 2)
        self.assertEqual(name_row["use"], 4)

    def test_route_filter_keeps_name_rollup_only_if_route_present(self):
        _mk_skill(self.a, "dup", "权重技能演示")
        _mk_skill(self.b, "dup", "权重技能演示")
        self._invoke(name="dup", route="src")
        b = self._usage(route="src")
        self.assertEqual({x["route"] for x in b["items"]}, {"src"})
        self.assertTrue(all("src" in x["routes"] for x in b["by_name"]))

    def test_route_filter_also_scopes_totals(self):
        """`route` 与 `caller` 同口径：**先过滤再统计**，`totals` 也必须跟着缩。

        2026-10-20 实测缺陷：`?route=techdocs` 只筛了 `items`，顶部 `totals` 仍是全机
        （点名 20 / 曝光 8），于是**同一个屏幕里有两份真相**——列表说这一路，汇总说全机。
        页面把 `totals` 直接铺在汇总带里，用户看不出哪个数是这一路的。
        """
        _mk_skill(self.a, "one", "权重技能演示")
        _mk_skill(self.b, "two", "权重技能演示")
        self._invoke(name="one", route="src")
        self._invoke(name="one", route="src")
        self._invoke(name="two", route="other")

        g = self._usage()
        self.assertEqual(g["totals"]["explicit"], 3, g["totals"])

        s = self._usage(route="src")
        self.assertEqual(s["totals"]["explicit"], 2,
                         "汇总没跟着 route 缩 ⇒ 同一屏两份真相")
        self.assertEqual({x["route"] for x in s["items"]}, {"src"})
        # 过滤掉的事件要能解释去哪了，不能凭空消失
        self.assertGreaterEqual(s["totals"]["filtered_out"], 1, s["totals"])

    def test_legacy_relevant_not_counted_as_auto(self):
        """无 mode/via 的旧事件不许算曝光：那可能是 UI 人工查询。"""
        db.execute("INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                   " VALUES('rest','skill.relevant','success',1,?, '2026-10-20T00:00:00+00:00')",
                   (json.dumps({"name": "ghost", "route": "src"}),))
        b = self._usage()
        row = next(x for x in b["items"] if x["name"] == "ghost")
        self.assertEqual((row["auto"], row["use"], row["score"]), (0, 0, 0.0))
        self.assertEqual(row["legacy"], 1)
        self.assertEqual(row["usage_class"], "unknown",
                         "只有旧账的技能不能标成 used/exposed_only")

    def test_invocations_auto_matches_usage_auto(self):
        """两个端点的 `auto` 必须是同一个量。

        2026-10-20 实测 `/invocations.auto.total=656` 而 `/usage.totals.auto=6`——
        因为前者把整张 skill.relevant（含 653 条无 mode 旧账 + 人工查询）都算自动下发。
        同一个词在两个页面指两件事，读的人必然做错判断。判据统一走 classify()。
        """
        _mk_skill(self.a, "demo", "权重技能演示")
        self._relevant(caller="claude", via="hook")     # auto
        self._relevant(caller="", via="")               # query（人工）
        db.execute("INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                   " VALUES('rest','skill.relevant','success',1,?, '2026-10-20T00:00:00+00:00')",
                   (json.dumps({"name": "demo", "route": "src"}),))   # legacy

        # ⚠ 窗口必须显式对齐：`/invocations` 默认 7 天，而下面那行 fixture 的
        # created_at 是**写死的** 2026-10-20。两者不打断言就成了一颗定时炸弹 ——
        # 2026-10-27 02:0x UTC 起，7 天窗口滑过 10-20T00:00，用例开始判红，
        # 而两个端点的实现一个字都没改（实测：改成 days=365 立刻转绿）。
        # 同一条纪律：**比两个量之前，先把口径（此处是时间窗）对齐**。
        inv = self.client.get("/api/skill/invocations?days=365", headers=self._hdr()).json()
        usage = self._usage(days=365)
        self.assertEqual(inv["auto"]["total"], usage["totals"]["auto"])
        self.assertEqual(inv["auto"]["query"], usage["totals"]["query"])
        self.assertEqual(inv["auto"]["legacy"], usage["totals"]["legacy"])
        self.assertEqual((inv["auto"]["total"], inv["auto"]["query"], inv["auto"]["legacy"]),
                         (1, 1, 1), inv["auto"])

    def test_totals_events_bucket_sum_and_named_are_two_numbers(self):
        """`events` 与 `named` 口径不同时必须显式分开报（654 行 vs 666 计数不是丢数据）。"""
        _mk_skill(self.a, "demo", "权重技能演示")
        self._invoke()
        b = self._usage()
        t = b["totals"]
        self.assertEqual(t["events"], t["explicit"] + t["read"] + t["auto"]
                         + t["auto_use"] + t["query"] + t["legacy"])
        self.assertEqual(t["named"], t["rows"])
        self.assertIn("口径差异", b["totals_note"])

    def test_totals_reconcile_per_bucket_with_unnamed(self):
        """每桶都要能对账：`totals[b] == named_by_bucket[b] + unnamed_by_bucket[b]`。

        2026-10-20 生产实测：点名汇总 26、把 items 加起来只有 24，legacy 1276 vs 632。
        前者差 2 条无名事件，后者还多一层 `limit` 截断。只报一个全局 `no_name`
        解释不了任何一桶，读的人只能猜——这里把两笔账逐桶拆开并钉住恒等式。
        """
        _mk_skill(self.a, "demo", "权重技能演示")
        self._invoke()                                        # 有名 explicit=1
        db.execute("INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                   " VALUES('rest','skill.invoke','success',1,?, '2026-10-20T00:00:00+00:00')",
                   (json.dumps({"route": "src", "caller": "codex"}),))   # 无名 explicit
        b = self._usage()
        t = b["totals"]
        for k in ("explicit", "read", "auto_use", "auto", "query", "legacy"):
            self.assertEqual(t[k], t["named_by_bucket"][k] + t["unnamed_by_bucket"][k],
                             "%s 桶对不上账：%s" % (k, t))
        self.assertEqual(t["explicit"], 2)
        self.assertEqual(t["named_by_bucket"]["explicit"], 1)
        self.assertEqual(t["unnamed_by_bucket"]["explicit"], 1)
        self.assertEqual(t["listed_items"], len(b["items"]))
        self.assertEqual(t["dropped_by_limit"], 0)
        self.assertEqual(t["listed_by_bucket"]["explicit"], 1,
                         "榜内只应有点名那条（无名事件不进榜）")

    def test_limit_truncation_is_attributed_not_silent(self):
        """`limit` 截掉的行必须能算出来：named - listed == dropped_by_limit。

        生产实测 limit=100 时 legacy 从 858 掉到 630（少 228 行）——若响应不说
        这 228 行去哪了，读的人只能得出「汇总在算没发生的事」或「列表丢数据」。
        """
        for n in ("aa", "bb", "cc"):
            _mk_skill(self.a, n, "权重技能演示")
            self.assertEqual(self._invoke(name=n).status_code, 200)
        b = self._usage(limit=1)
        t = b["totals"]
        self.assertEqual(t["listed_items"], 1)
        self.assertEqual(t["dropped_by_limit"], 2, t)
        self.assertEqual(t["named_by_bucket"]["explicit"], 3)
        self.assertEqual(t["listed_by_bucket"]["explicit"], 1)
        self.assertEqual(t["named_by_bucket"]["explicit"] - t["listed_by_bucket"]["explicit"],
                         t["dropped_by_limit"])
        # 拿掉 limit 后必须逐位复原（差额只来自分页，不来自聚合）
        full = self._usage(limit=500)["totals"]
        self.assertEqual(full["dropped_by_limit"], 0)
        self.assertEqual(full["listed_by_bucket"]["explicit"], 3)

    def test_usage_class_separates_cold_from_exposure(self):
        """「没人用」有两种：被推过没打开（exposed_only）与用过后冷掉（stale）。"""
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(days=45)).isoformat()
        fresh = (now - timedelta(days=2)).isoformat()
        rows = [
            {"id": 1, "subject": "skill.invoke", "created_at": old,
             "detail": json.dumps({"name": "cold", "route": "src", "caller": "codex"})},
            {"id": 2, "subject": "skill.invoke", "created_at": fresh,
             "detail": json.dumps({"name": "warm", "route": "src", "caller": "codex"})},
            {"id": 3, "subject": "skill.relevant", "created_at": fresh,
             "detail": json.dumps({"name": "pushed", "route": "src", "caller": "pi",
                                   "mode": "auto_inject"})},
        ]
        b = skill_weights.build(rows, now=now)
        got = {x["name"]: (x["usage_class"], x["idle_days"], x["score"]) for x in b["items"]}
        self.assertEqual(got["cold"], ("stale", 45, 2.0))
        self.assertEqual(got["warm"], ("used", 2, 2.0))
        self.assertEqual(got["pushed"], ("exposed_only", None, 0.0))
        self.assertGreater(skill_weights.STALE_AFTER_DAYS, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
