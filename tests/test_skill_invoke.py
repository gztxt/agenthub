#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：统一调用入口 /api/skill/invoke + catalog + invocations + local 路。

v0.13.127 PT-20261019-01（Grill 六问裁定的落地闸门）。分层口径同
tests/test_skill_install.py：SKILL_DIRS 猴补到 tmp、db 指向 tmp 库、不起服务、
不打网络、**不允许 SKIP**。

本文件守的四条设计裁定（都来自 2026-10-19 用户逐条确认的 Grill 记录）：
1. **语义边界**：invoke 回指令正文、不代跑代码 ⇒ 断言回包有 content（L2 指令），
   且没有任何「执行」字段（没有 exec/output/result 之类）；
2. **记账模型**：成功与失败都落 `skill.invoke` 事件，detail 带 name/caller/task；
   caller 是**自报**（caller_reported=true），不是协议取证；
3. **查看/操作分钮**：/read 不记账、/invoke 记账——同一技能各打一次，
   profile_events 里必须只有一条 invoke、零条 read；
4. **项目本地根**：local 路动态出现（有根才在 disk_routes() 里）、只读
   （install/remove 不接受它）、越界闸门与全局路同一套。

红向场景都能用 tmp 确定性造：409 候选、404、截断、坏 detail 行、 caller 回落链。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db                     # noqa: E402
import runlog                 # noqa: E402
import skill                  # noqa: E402
import skill_usage            # noqa: E402
import writeauth              # noqa: E402

from fastapi import FastAPI   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel   # noqa: E402


def _mk_skill(root: pathlib.Path, name: str, desc: str = "测试技能",
              body: str = "正文\n") -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}", encoding="utf-8")
    return d


class _InvokeCase(unittest.TestCase):
    """夹具：两路 tmp 技能根 + tmp 库 + 只挂 skill 路由的 TestClient。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0invoke-"))
        self.a = self.tmp / "a"          # 权威副本路
        self.b = self.tmp / "b"          # 第二路（造 409 用）
        for p in (self.a, self.b):
            p.mkdir()
        self._saved_dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"src": str(self.a), "other": str(self.b)}
        # 扫描缓存是进程级共享状态：猴补的目录每轮都是新 tmp，不清缓存会读到上一轮的
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        db.init_db(self.tmp / "t.db")
        _app = FastAPI()
        _app.include_router(skill.router)
        _app.middleware("http")(writeauth.write_gate)
        os.environ["TERM_TOKEN"] = "l0-skill-test-token"
        # 项目本地根默认钉空：L0 不许宿主仓内碰巧有 skills/ 就改变路由名单（同码两态）
        os.environ["SKILL_LOCAL_ROOTS_JSON"] = "[]"
        self.client = TestClient(_app)
        self.addCleanup(self._restore)

    def _restore(self):
        skill.SKILL_DIRS = self._saved_dirs
        os.environ.pop("TERM_TOKEN", None)
        os.environ.pop("SKILL_LOCAL_ROOTS_JSON", None)
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hdr(self):
        return {"x-hub-token": "l0-skill-test-token"}

    def _events(self, subject="skill.invoke"):
        return db.query("SELECT * FROM profile_events WHERE subject = ?", (subject,))

    def _invoke(self, name="demo", **kw):
        body = {"name": name}
        body.update(kw)
        return self.client.post("/api/skill/invoke", json=body, headers=self._hdr())


# ── 裁定 1：语义边界（激活+记账，不代跑） ──────────────────────────────────
class TestInvokeSemantics(_InvokeCase):
    def setUp(self):
        super().setUp()
        _mk_skill(self.a, "demo", "演示技能", "第一步：这样做。\n第二步：那样做。\n")

    def test_invoke_returns_instructions_and_ledger_marker(self):
        r = self._invoke(caller="codex", task="抓取示例页面")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        # L2 指令正文回传（Agent Skills 标准语义：agent 读指令后自行执行）
        self.assertIn("第一步", b["content"])
        self.assertEqual(b["name"], "demo")
        self.assertEqual(b["route"], "src")
        self.assertEqual(b["caller"], "codex")
        self.assertEqual(b["task"], "抓取示例页面")
        self.assertTrue(b["caller_reported"])          # 自报，不是协议取证
        self.assertEqual(b["recorded_subject"], "skill.invoke")
        # 没有任何「执行」字段——hub 不代跑代码，这个断言守语义边界
        for k in ("exec", "output", "result", "stdout", "returncode"):
            self.assertNotIn(k, b, f"invoke 不应当有执行面字段 {k}")

    def test_invoke_with_body_false_skips_content_but_still_records(self):
        r = self._invoke(with_body=False, caller="pi")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertNotIn("content", b)
        self.assertEqual(b["name"], "demo")
        self.assertEqual(len(self._events()), 1, "不给正文也要记账（激活事实本身）")

    def test_view_and_operate_are_separately_recorded(self):
        """裁定 5「查看与操作分钮」的服务端形态：read 落 read 账、invoke 落 invoke 账，
        两条 subject 互不串门——混为一谈的话「被看过」与「被调用」在面板上分不开。"""
        self.client.get("/api/skill/read", params={"name": "demo"})
        self._invoke(caller="web")
        self.assertEqual(len(self._events("skill.read")), 1)
        self.assertEqual(len(self._events("skill.invoke")), 1)
        self.assertNotEqual(self._events("skill.read")[0]["id"],
                            self._events("skill.invoke")[0]["id"])

    def test_invoke_requires_write_credential(self):
        """POST 过 writeauth：无 token 必须 401（不搞「调用还要什么 token」的静默放行）。"""
        r = self.client.post("/api/skill/invoke", json={"name": "demo"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_empty_route_string_means_unspecified(self):
        """MCP 工具的默认形态就是 route=""——空串必须归一成「未指定」而不是 400。
        （GET 查询串省略即 None，JSON body 省略不了；不归一则 MCP 侧必踩。）"""
        r = self._invoke(route="", caller="mcp")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["route"], "src")
        r = self._invoke(route="  ", caller="mcp")
        self.assertEqual(r.status_code, 200, r.text)

    def test_caller_falls_back_to_channel_header_then_anonymous(self):
        r = self.client.post("/api/skill/invoke", json={"name": "demo"},
                             headers={**self._hdr(), "x-hub-channel": "mcp"})
        self.assertEqual(r.json()["caller"], "mcp")
        r = self._invoke()
        self.assertEqual(r.json()["caller"], "anonymous")

    def test_caller_and_task_are_capped(self):
        r = self._invoke(caller="x" * 100, task="y" * 3000)
        self.assertEqual(r.status_code, 400, r.text)
        r = self._invoke(caller="ok", task="y" * 3000)
        self.assertEqual(r.status_code, 400, r.text)


# ── 失败态：409/404/400/截断（镜像 /read，不发明新失败形态） ────────────────
class TestInvokeFailStates(_InvokeCase):
    def test_409_lists_candidates_when_same_name_diff_files(self):
        _mk_skill(self.a, "dup", "A 副本")
        _mk_skill(self.b, "dup", "B 副本")
        r = self._invoke(name="dup")
        self.assertEqual(r.status_code, 409, r.text)
        cands = r.json()["detail"]["candidates"]
        self.assertEqual({c["route"] for c in cands}, {"src", "other"})
        # 带 route 再调就成功（门面不静默挑一个）
        r = self._invoke(name="dup", route="src")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["description"], "A 副本")

    def test_404_on_unknown_name(self):
        r = self._invoke(name="no-such")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("/api/skill/list", r.json()["detail"])

    def test_400_on_unknown_route_but_local_is_legal(self):
        _mk_skill(self.a, "demo")
        r = self._invoke(route="nope")
        self.assertEqual(r.status_code, 400, r.text)
        # 夹具默认钉空本地根 ⇒ 可用值里还没有 local；造一个本地根后必须出现
        # （local 是合法 route，漏列它＝用户没法调仓内技能还不知道为什么）
        (self.tmp / "repo" / "skills").mkdir(parents=True)
        os.environ.pop("SKILL_LOCAL_ROOTS_JSON", None)   # 夹具的钉空让位给真实本地根
        os.environ["SKILL_LOCAL_ROOTS_BASE"] = str(self.tmp / "repo")
        self.addCleanup(lambda: os.environ.pop("SKILL_LOCAL_ROOTS_BASE", None))
        r = self._invoke(route="nope")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("local", r.json()["detail"])

    def test_failed_invoke_still_leaves_trace_with_caller_and_task(self):
        """失败的调用也要答「谁调的、想干什么」——否则调用记录面板只剩成功样本。"""
        r = self._invoke(name="ghost", caller="jcode", task="找个不存在的")
        self.assertEqual(r.status_code, 404)
        ev = self._events()
        self.assertEqual(len(ev), 1)
        det = json.loads(ev[0]["detail"])
        self.assertEqual(ev[0]["status"], "fail")
        self.assertEqual(det["caller"], "jcode")
        self.assertEqual(det["task"], "找个不存在的")
        self.assertEqual(ev[0]["subject"], "skill.invoke")

    def test_truncation_is_explicit_not_silent(self):
        big = "x" * (skill.READ_MAX_BYTES + 1000)
        _mk_skill(self.a, "huge", "大技能", body=big)
        r = self._invoke(name="huge")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["truncated"])
        self.assertGreater(b["bytes_total"], skill.READ_MAX_BYTES)
        self.assertIn("已截断", b["note"])


# ── 裁定 2：技能说明目录（L1） ────────────────────────────────────────────
class TestCatalog(_InvokeCase):
    def setUp(self):
        super().setUp()
        _mk_skill(self.a, "alpha", "第一个")
        _mk_skill(self.b, "beta", "第二个")

    def test_catalog_is_l1_only(self):
        r = self.client.get("/api/skill/catalog")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual(b["total"], 2)
        names = [i["name"] for i in b["items"]]
        self.assertEqual(names, ["alpha", "beta"])
        row = b["items"][0]
        for k in ("name", "description", "route", "routes", "digest", "est_tokens", "fm"):
            self.assertIn(k, row)
        self.assertNotIn("content", row, "L1 目录不带正文（正文靠 invoke 点名取）")
        self.assertTrue(row["digest"], "digest 必须非空（整包 sha256）")

    def test_catalog_q_filter_and_route_filter(self):
        r = self.client.get("/api/skill/catalog", params={"q": "alp"})
        self.assertEqual([i["name"] for i in r.json()["items"]], ["alpha"])
        r = self.client.get("/api/skill/catalog", params={"route": "other"})
        self.assertEqual([i["name"] for i in r.json()["items"]], ["beta"])
        r = self.client.get("/api/skill/catalog", params={"route": "nope"})
        self.assertEqual(r.status_code, 400)


# ── 裁定 3：调用流水投影 ─────────────────────────────────────────────────
class TestInvocations(_InvokeCase):
    def test_projection_filters_and_counts(self):
        _mk_skill(self.a, "demo")
        self._invoke(name="demo", caller="codex", task="任务甲")
        self._invoke(name="demo", caller="web", task="任务乙")
        self._invoke(name="ghost", caller="web")            # 404 fail 行
        r = self.client.get("/api/skill/invocations")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual(b["matched_total"], 3)
        self.assertEqual(b["summary"]["by_caller"], {"web": 2, "codex": 1})
        self.assertEqual(b["summary"]["by_skill"], {"demo": 2, "ghost": 1})
        newest = b["items"][0]
        self.assertEqual(newest["name"], "ghost")
        self.assertEqual(newest["caller"], "web")
        self.assertEqual(newest["status"], "fail")
        # 过滤：按 name / 按 caller
        r = self.client.get("/api/skill/invocations", params={"name": "demo"})
        self.assertEqual(r.json()["matched_total"], 2)
        r = self.client.get("/api/skill/invocations", params={"caller": "codex"})
        self.assertEqual(r.json()["matched_total"], 1)
        self.assertEqual(r.json()["items"][0]["task"], "任务甲")

    def test_projection_carries_route_and_reported_flag(self):
        """权重数据的最小完整键：谁调的（caller）+ 调的哪条（name）+ 来自哪一路（route）。

        route 只存在于 detail，投影若不透出，同名技能跨路时无法回答「用了谁的技能」；
        caller_reported 不落则会把自报身份显示成已取证。
        """
        _mk_skill(self.a, "demo")
        self._invoke(name="demo", route="src", caller="codex", task="按来源记账")
        b = self.client.get("/api/skill/invocations").json()
        row = b["items"][0]
        self.assertEqual(row["route"], "src")
        self.assertTrue(row["caller_reported"], "invoke 的 caller 是自报，流水必须同响应一致")
        self.assertEqual(b["summary"]["by_route"], {"src": 1})

    def test_same_name_two_routes_are_distinguishable(self):
        """同名技能在两路都有 ⇒ 流水必须按 route 分开，不能只按名字合并。

        `by_skill` 仍按名字合并（目录行徽标的历史契约），但逐行数据必须能区分来源；
        拿 by_skill 做权重会把两路算成同一条技能。
        """
        _mk_skill(self.a, "dup")
        _mk_skill(self.b, "dup")
        self._invoke(name="dup", route="src", caller="codex")
        self._invoke(name="dup", route="other", caller="pi")
        b = self.client.get("/api/skill/invocations").json()
        self.assertEqual(sorted(i["route"] for i in b["items"]), ["other", "src"])
        self.assertEqual(b["summary"]["by_route"], {"other": 1, "src": 1})
        self.assertEqual(b["summary"]["by_skill"], {"dup": 2})

    def test_failed_invoke_keeps_route_and_reported_flag(self):
        """失败（404/409）也要能按来源统计：写进 fail detail 的 route 不许丢。"""
        r = self._invoke(name="ghost", route="src", caller="codex")
        self.assertEqual(r.status_code, 404, r.text)
        b = self.client.get("/api/skill/invocations").json()
        row = b["items"][0]
        self.assertEqual(row["route"], "src")
        self.assertTrue(row["caller_reported"])

    def test_auto_inject_bucket_is_separate_and_carries_caller(self):
        """自动下发（subject=skill.relevant + via=hook）必须与指名调用分桶并带 caller。

        2026-10-20 用户要求：「要记录哪个 agent 调用了哪个技能，还要能分辨是执行任务时
        自动用的、还是人手动指定的」。端点不同只是实现细节——**判据是 via=hook**。
        这一条同时钉住：① auto.total 按窗口计 ② by_caller 有身份 ③ invoke 行仍标
        explicit_invoke ④ 缺 via 的同一端点记为 query（不许推断成自动）。
        """
        _mk_skill(self.a, "demo", "演示技能 用于自动下发测试")
        r = self.client.get("/api/skill/relevant",
                            params={"q": "演示技能", "n": 3, "rerank": "false",
                                    "caller": "claude", "via": "hook"})
        self.assertEqual(r.status_code, 200, r.text)
        n_auto = len(r.json()["bm25"]["items"])
        self.assertEqual(n_auto, 1, "夹具只有一个技能，top-N 应恰好 1 条")
        # 人工查询（无 via）：同一端点但不算自动，落 mode=query
        self.client.get("/api/skill/relevant",
                        params={"q": "演示技能", "rerank": "false"})
        self._invoke(name="demo", caller="codex")

        b = self.client.get("/api/skill/invocations").json()
        # 只有声明 via=hook 的那次算自动下发；人工查询单列 query。
        # 旧实现把整张 skill.relevant 都算 auto（实测把 653 条旧账算成「自动下发 656 次」），
        # 与权重榜的 auto 差两个数量级——同一个词必须是同一件事。
        self.assertEqual(b["auto"]["total"], 1, "未声明 via 的人工查询不许算自动下发")
        self.assertEqual(b["auto"]["query"], 1, "人工检索单列，不从账上消失")
        self.assertEqual(b["auto"]["by_caller"], {"claude": 1})
        self.assertEqual(b["auto"]["unknown_caller"], 0)
        self.assertEqual(b["items"][0]["mode"], "explicit_invoke")

        modes = [json.loads(e["detail"]).get("mode") for e in self._events("skill.relevant")]
        self.assertEqual(sorted(modes), ["auto_inject", "query"])

    def test_bad_detail_rows_are_counted_not_fatal(self):
        """坏 detail 行不许打挂面板——逐行 try、跳过并计数（与 skill_usage 同纪律）。"""
        _mk_skill(self.a, "demo")
        self._invoke(name="demo", caller="web")
        db.execute("INSERT INTO profile_events(source,subject,trace_id,status,duration_ms,"
                   "detail,created_at) VALUES('rest','skill.invoke',NULL,'success',1,"
                   "'不是 JSON','2099-01-01T00:00:00+00:00')")
        r = self.client.get("/api/skill/invocations")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual(b["bad_detail"], 1)
        self.assertEqual(b["matched_total"], 1)

    def test_invoke_counts_toward_usage_not_zombie(self):
        """服务端口到端：invoke 过的技能不进零调用榜（_SUBJECTS 含 invoke 的集成证明）。"""
        _mk_skill(self.a, "demo")
        self._invoke(name="demo", caller="codex")
        r = self.client.get("/api/skill/zombies", params={"days": 7})
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        names = [z["name"] for z in b["zombies"]]
        self.assertNotIn("demo", names)
        self.assertGreaterEqual(b["counted"], 1, "账里必须有痕迹，否则僵尸榜整体不可信")


# ── 裁定 4：项目本地根（local 路） ────────────────────────────────────────
class TestLocalRoots(_InvokeCase):
    def setUp(self):
        super().setUp()
        self.base = self.tmp / "repo"
        (self.base / "skills" / "repo-skill").mkdir(parents=True)
        (self.base / "skills" / "repo-skill" / "SKILL.md").write_text(
            "---\nname: repo-skill\ndescription: 仓内自研\n---\n仓内指令\n", encoding="utf-8")
        os.environ["SKILL_LOCAL_ROOTS_BASE"] = str(self.base)
        os.environ.pop("SKILL_LOCAL_ROOTS_JSON", None)
        skill._scan_cache.clear()
        self.addCleanup(lambda: os.environ.pop("SKILL_LOCAL_ROOTS_BASE", None))

    def test_local_route_appears_and_is_invokable(self):
        self.assertIn("local", skill.disk_routes())
        r = self._invoke(name="repo-skill", caller="web")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual(b["route"], "local")
        self.assertIn("仓内指令", b["content"])
        # /read 也能按 local 路读
        r = self.client.get("/api/skill/read", params={"name": "repo-skill", "route": "local"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_local_route_is_read_only(self):
        """写侧军规：install/remove 不接受 local——仓内技能不经 HTTP 写。"""
        r = self.client.post("/api/skill/install", json={
            "name": "repo-skill", "from_route": "local", "targets": ["src"]},
            headers=self._hdr())
        self.assertEqual(r.status_code, 400, r.text)
        r = self.client.delete("/api/skill/remove",
                               params={"name": "repo-skill", "route": "local"},
                               headers=self._hdr())
        self.assertEqual(r.status_code, 400, r.text)

    def test_local_roots_join_whitelist_and_block_escape(self):
        """越界闸门对 local 路同样有效：软链 SKILL.md 指到白名单外 ⇒ 拒读且如实列出。"""
        allowed = skill._allowed_roots()
        self.assertIn(os.path.realpath(self.base / "skills"), allowed)
        outside = self.tmp / "outside"
        (outside / "evil").mkdir(parents=True)
        (outside / "evil" / "SKILL.md").write_text("---\nname: evil\n---\n坏\n", encoding="utf-8")
        (self.base / "skills" / "evil-link").symlink_to(outside / "evil")
        r = skill._scan_one("local", skill.local_roots())
        names = [i["name"] for i in r["items"]]
        self.assertNotIn("evil", names, "越界软链必须被拒读")
        self.assertTrue(any(s.get("dir") == "evil-link" for s in r["skipped_outside"]),
                        "拒读必须如实列出，不静默丢")

    def test_status_marks_local_route_kind_and_opencode_note(self):
        """PT-20261002-14 验收项：每路说清全局/项目本地；opencode 证伪注记上面板。"""
        skill.SKILL_DIRS = dict(skill.SKILL_DIRS, opencode=str(self.tmp / "no-such"))
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        r = self.client.get("/api/skill/status", params={"force": True})
        self.assertEqual(r.status_code, 200, r.text)
        disk = r.json()["disk"]
        self.assertEqual(disk["local"]["kind"], "project-local")
        self.assertIn(str(self.base), disk["local"]["dir"])
        self.assertEqual(disk["src"]["kind"], "global")
        self.assertIn("已证伪", disk["opencode"]["note"])


# ── runlog._pick_extra：请求体字段进埋点的单测 ────────────────────────────
class TestPickExtra(unittest.TestCase):
    def test_reads_top_level_and_body_model(self):
        class _Req(BaseModel):
            name: str = "x"
            caller: str = "codex"
            task: str = "干活"
            route: str = ""

        got = runlog._pick_extra({"request": object(), "req": _Req()})
        self.assertEqual(got["caller"], "codex")
        self.assertEqual(got["task"], "干活")
        # 顶层优先 + 无字段端点零噪声
        self.assertEqual(runlog._pick_extra({"q": "abc"}), {})
        self.assertEqual(runlog._pick_extra({}), {})
        # 超长截断（detail 不许被长文撑爆）
        got = runlog._pick_extra({"req": _Req(task="t" * 900)})
        self.assertLessEqual(len(got["task"]), runlog._TASK_MAX_CHARS)

    def test_route_extraction_from_list_and_single(self):
        """逐名事件必须能带上来源路：/relevant 的 route 在条目里，/read 在顶层。

        没有它，(name, route) 维度的权重会把曝光记到 route="" 的另一行，
        auto 永远配不到使用上 ⇒ 自动下发对分数失效（2026-10-20 实测）。
        """
        self.assertEqual(
            runlog._route_by_name({"bm25": {"items": [{"name": "a", "route": "pi"}]}}),
            {"a": "pi"})
        self.assertEqual(runlog._route_by_name({"items": [{"name": "b", "route": "claude"}]}),
                         {"b": "claude"})
        self.assertEqual(runlog._route_by_name({"items": [{"name": "c"}]}), {})
        self.assertEqual(runlog._route_of({"name": "x", "route": "codex"}), "codex")
        self.assertEqual(runlog._route_of({"items": []}), "")

    def test_via_picked_and_mode_layered(self):
        """via 必须进 detail；mode 由 via 决定，缺 via 不推断为自动。"""
        self.assertEqual(runlog._pick_extra({"via": "hook"}), {"via": "hook"})
        self.assertEqual(runlog._mode_of("skill.relevant", {"via": "hook"}), "auto_inject")
        self.assertEqual(runlog._mode_of("skill.relevant", {"via": ""}), "query")
        self.assertEqual(runlog._mode_of("skill.relevant", {}), "query")
        self.assertEqual(runlog._mode_of("skill.invoke", {"caller": "codex"}),
                         "explicit_invoke")
        self.assertEqual(runlog._mode_of("skill.list", {}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
