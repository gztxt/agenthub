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

        inv = self.client.get("/api/skill/invocations", headers=self._hdr()).json()
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
                         + t["query"] + t["legacy"])
        self.assertEqual(t["named"], t["rows"])
        self.assertIn("口径差异", b["totals_note"])

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
