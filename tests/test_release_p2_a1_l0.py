"""P2 A1 · Agent Release manifest 的 L0 闸门。

## 在测什么（每条对应一个真缺口／一次真翻车）

1. **`release_id` 只由语义字段算**：改文件路径、改描述文案、改 detect 规则 ⇒ id **不变**；
   换模型 / 换 ACL effect / 换画像 kind ⇒ id **变**。
   这一条是「版本」这个概念的存亡线 —— id 若随无关噪声漂，「回到哪个组合」就无从谈起。
2. **两次现算逐字节相同**：不吃时间戳/耗时（与 `ctxmanifest`、`resilience` 同一条纪律）。
3. **不可判定报 `unknown` + `degraded=True`，不留空**。
   反例是本模块**真翻过的一次**：`best-effort` 里 `_conn` 为 None 时
   `skills`/`acl` 两维双 unknown，而库里 `mcp_acl` 明明有 4 条 —— 冒烟脚本里
   看到的是 `degraded: True`，很容易被当成「本来就没有」。所以本类同时断言
   **方向**：能读到时必须是 `known`，且 count 与库内行数一致。
4. **stale 必须算得出来**：`current_digest` 传 `None` ⇒ `is_stale` 恒 False
   ⇒ 判据恒假。用一个「发布后磁盘被改」的资产断言 `status=='stale'`
   **且它仍在 `skills` 里**（stale 是**在役**的偏差，不是「没有」）。
5. **`_ensure_db` 只补不抢**：已有连接时**不得**重指（否则进程内测试会打到生产库，
   09-24 已实际污染过一次）。
6. **单 agent 失败不影响批量**：`manifests_for` 里一个 agent 抛异常，其余照常出。

## 红向自证（跑过才认，见 §证据）

- 让 `fingerprint` 把 `dimensions.model.files` 也算进去 ⇒ 用例 1（路径）转红。
- 让 `build_manifest` 在 `unknown` 时仍 `degraded=False` ⇒ 用例 3 转红。
- 让 `_dim_skills` 回到 `effective_status(r, None)` ⇒ 用例 4 转红。
- 删掉 `_ensure_db()` 的 `is_open()` 早退（改成无条件 `init_db`）⇒ 用例 5 转红。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import release  # noqa: E402


def _view(model="m", kind="cli", skill="", acl_effect="enforce"):
    """一个可控的语义视图，形状与 `_semantic_view` 一致（只取指纹关心的部分）。"""
    return {
        "agent_id": "a1",
        "model": {"state": "known", "current": model, "provider": "p",
                  "files": ["/x/y.json"]},
        "profile": {"state": "known", "kind": kind, "cli": "c", "port": 1,
                    "terminal_cmd": "t", "chat_adapter": "ad"},
        "skills": {"state": "known", "count": 0, "skills": []},
        "acl": {"state": "known", "count": 1,
                "rules": [{"server": "s", "tool": "t", "allow": True,
                           "effect": acl_effect}]},
        "execsem": {"state": "known", "sources": ["a"], "outcomes": ["b"]},
    }


class TestFingerprintCoversSemanticsOnly(unittest.TestCase):
    """判据 1：指纹「吃语义、不吃噪声」。两侧都要断言 —— 只证一边等于没证。"""

    def test_path_is_not_part_of_release_id(self):
        """路径不进指纹 —— 但**要测被测对象**：比的是 `_semantic_view` 的产物。

        ⚠ 第一版判据写成「比两个手搓的完整 view」，其中一个只改了 `files`
        ⇒ 它测的是 `fingerprint` 对任意 dict 的行为，**与 `_semantic_view` 是否
        把 files 排除在外无关**；那条判据既会假红（现在）也会假绿（若指纹口径改了
        而 `_semantic_view` 没改）。判据量错对象（判例 185 同型）。
        """
        orig_model = release._dim_model

        def with_path(p):
            def _f(aid):
                d = dict(orig_model(aid))
                if d.get("state") == "known":
                    d["files"] = [p]
                return d
            return _f

        try:
            release._dim_model = with_path("/a/b.json")
            v1 = release._semantic_view("claude")
            release._dim_model = with_path("/totally/different/c.json")
            v2 = release._semantic_view("claude")
        finally:
            release._dim_model = orig_model
        self.assertEqual(release.fingerprint(v1), release.fingerprint(v2),
                         "_semantic_view 把文件路径喂进了指纹")

    def test_model_change_changes_release_id(self):
        self.assertNotEqual(release.fingerprint(_view(model="m1")),
                            release.fingerprint(_view(model="m2")))

    def test_acl_effect_change_changes_release_id(self):
        """enforce ↔ shadow 是**行为变化**（P2 C2 刚把它们分开），必须换版本。"""
        self.assertNotEqual(release.fingerprint(_view(acl_effect="enforce")),
                            release.fingerprint(_view(acl_effect="shadow")))

    def test_profile_kind_change_changes_release_id(self):
        self.assertNotEqual(release.fingerprint(_view(kind="cli")),
                            release.fingerprint(_view(kind="service")))

    def test_key_order_does_not_change_release_id(self):
        """`_canon` 的 sort_keys：同一份语义换个 dict 插入顺序，id 必须稳定。"""
        v1 = _view()
        v2 = dict(reversed(list(_view().items())))
        self.assertEqual(release.fingerprint(v1), release.fingerprint(v2))

    def test_two_computations_are_byte_identical(self):
        """判据 2：现算两次逐字节相同（不吃时间戳/耗时）。"""
        import json
        a = json.dumps(release.build_manifest("claude"), sort_keys=True,
                       ensure_ascii=False)
        b = json.dumps(release.build_manifest("claude"), sort_keys=True,
                       ensure_ascii=False)
        self.assertEqual(a, b)


class TestDegradedIsHonest(unittest.TestCase):
    """判据 3：读不到 ⇒ unknown + degraded；读得到 ⇒ known（方向也要证）。"""

    def setUp(self):
        """需要**真库**的用例自己把连接摆正。

        ⚠ 第一版没做这步，单独跑绿、全量跑红：`test_real_manifest_is_not_degraded`
        拿到的是前序用例（tmp 库）留下的连接，`_ensure_db` **正确地**不重指
        ⇒ 读到空库 ⇒ `acl` unknown ⇒ 假红。
        **判据一旦依赖前置状态，就必须自己把它固定住**，不能指望执行顺序。
        """
        import db
        import config
        self._saved = db._conn
        db.init_db(config.Config().db_path)

    def tearDown(self):
        import db
        db._conn = self._saved

    def test_unknown_dimension_forces_degraded(self):
        orig = release._dim_acl
        release._dim_acl = lambda aid: {"state": "unknown", "reason": "boom"}
        try:
            m = release.build_manifest("claude")
        finally:
            release._dim_acl = orig
        self.assertTrue(m["degraded"])
        self.assertEqual(m["unknown_dimensions"], ["acl"])

    def test_real_manifest_is_not_degraded(self):
        """反向：本机真库读得到 ⇒ 不得报 degraded。

        **这一条就是那次翻车的反例**：修 `_ensure_db` 之前这里会红
        （`degraded=True`，`unknown=['acl','skills']`），而库里 `mcp_acl` 有 4 行。
        """
        m = release.build_manifest("claude")
        self.assertFalse(m["degraded"],
                         "真库应可全读：unknown=%r" % (m["unknown_dimensions"],))
        self.assertEqual(m["unknown_dimensions"], [])

    def test_unknown_has_a_reason_never_empty_dict(self):
        orig = release._dim_execsem
        release._dim_execsem = lambda: {"state": "unknown", "reason": "表不存在"}
        try:
            m = release.build_manifest("claude")
        finally:
            release._dim_execsem = orig
        dim = m["dimensions"]["execsem"]
        self.assertEqual(dim["state"], "unknown")
        self.assertTrue(dim.get("reason"))
        self.assertNotEqual(dim, {})

    def test_manifest_declares_what_id_covers_and_excludes(self):
        """指纹口径必须**自述**，否则没人能判断改什么会换 id。"""
        m = release.build_manifest("claude")
        self.assertTrue(m["release_id_covers"])
        self.assertIn("文件路径", m["release_id_excludes"])
        self.assertIn("时间戳", m["release_id_excludes"])

    def test_schema_is_stamped(self):
        self.assertEqual(release.build_manifest("claude")["schema"],
                         release.RELEASE_SCHEMA)


class TestSkillsStaleIsComputable(unittest.TestCase):
    """判据 4：stale 不能被 `None` digest 静默吃掉。"""

    def setUp(self):
        import tempfile
        import db
        from pathlib import Path as _P
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "myskill"
        self.root.mkdir()
        (self.root / "SKILL.md").write_text("v1", encoding="utf-8")

        self._saved_conn = db._conn
        db.init_db(_P(self.tmp.name) / "t.db")
        import skill_assets
        skill_assets.ensure_schema()
        # ⚠ `upsert` **不收 path**（path 只从 `transition` 进）；第一版测试写
        # `upsert(..., path=...)` 直接 TypeError —— 那是判据错不是实现错。
        skill_assets.upsert("myskill", "owner", "1.0.0", "global", by_whom="test")
        # 状态机是**显式列边**的：draft→tested→reviewed→published，不许跳级。
        for to in ("tested", "reviewed"):
            skill_assets.transition("myskill", to, by="test")
        skill_assets.transition("myskill", "published", path=str(self.root),
                                by="test")
        skill_assets.upsert("adraft", "owner", "0.1.0", "global", by_whom="test")

    def tearDown(self):
        import db
        db._conn = self._saved_conn
        self.tmp.cleanup()

    def test_untouched_published_skill_is_published(self):
        d = release._dim_skills()
        self.assertEqual(d["state"], "known")
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["skills"][0]["status"], "published")

    def test_modified_package_after_publish_shows_stale_and_stays(self):
        """改了 SKILL.md ⇒ `stale`，**且仍在清单里**（在役偏差，不是「没有」）。"""
        (self.root / "SKILL.md").write_text("v2-changed", encoding="utf-8")
        d = release._dim_skills()
        self.assertEqual(d["count"], 1, "stale 资产不得被过滤掉")
        self.assertEqual(d["skills"][0]["status"], "stale")

    def test_draft_is_excluded(self):
        d = release._dim_skills()
        slugs = [s["slug"] for s in d["skills"]]
        self.assertNotIn("adraft", slugs)
        self.assertIn("myskill", slugs)


class TestEnsureDbDoesNotSteal(unittest.TestCase):
    """判据 5：已有连接时**不得**重指（09-24 生产库被污染过）。"""

    def test_open_connection_is_left_alone(self):
        import tempfile
        import db
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            mine = _P(td) / "mine.db"
            db.init_db(mine)
            before = db.current_path()
            release._ensure_db()
            self.assertEqual(db.current_path(), before,
                             "_ensure_db 抢走了别处的连接")

    def test_uninitialized_db_gets_opened(self):
        import db
        saved = db._conn
        db._conn = None
        try:
            release._ensure_db()
            self.assertTrue(db.is_open())
        finally:
            db._conn = saved


class TestAclFieldsAreNotSilentlyNull(unittest.TestCase):
    """判据 7：ACL 的「限哪个工具」不许变成 `None`。

    **这是真翻过的一次**：第一版写 `r.get("tool")`，而列名是 **`tool_pattern`**。
    `.get` 不抛 KeyError（它查的是"不存在的键"），于是每条规则都是
    `{"tool": null}` —— count 正常、字段看着填了、指纹照算，**实际把 tool 丢干净**。
    这种「字段在但值是空」的降级最难看出来，必须用「值本身不可为空」来锁。
    """

    def setUp(self):
        """真库不可被前序用例留下的 tmp 库顶替（`_ensure_db` 正确地不重指）。

        本类断言的是**本机真数据**（`mcp_acl` 有行），故自己把连接摆正。
        """
        import db
        import config
        self._saved = db._conn
        db.init_db(config.Config().db_path)

    def tearDown(self):
        import db
        db._conn = self._saved

    def test_every_rule_has_non_null_tool_and_server(self):
        d = release._dim_acl("claude")
        self.assertEqual(d["state"], "known")
        self.assertGreater(d["count"], 0, "本机 mcp_acl 非空，应读得到规则")
        for r in d["rules"]:
            self.assertIsNotNone(r["tool"], "tool 丢了（列名应是 tool_pattern）")
            self.assertIsInstance(r["effect"], str)
            self.assertIn(r["effect"], ("enforce", "shadow"))

    def test_truthy_tool_values_survive(self):
        """`*` 是真实的通配值 —— 不能因为它是通配就当空处理。"""
        tools = {r["tool"] for r in release._dim_acl("claude")["rules"]}
        self.assertTrue(any(isinstance(t, str) and t for t in tools),
                        "没有任何非空 tool 值：列名很可能又写错了")

    def test_effect_matches_mcpgw_rule(self):
        """`effect` 折算口径必须与 `mcpgw._acl_effect` 一致（缺列/空值 = enforce）。

        本模块为了不进 900ms 的 MCP 栈而**复刻**了那两条规则；复刻就必须对照，
        否则两边会静默漂移。

        ⚠ **第一版本条是空转的**：它把复刻算式**又抄了一遍**在测试里，拿它跟
        `mcpgw` 比 —— 那测的是"我抄的这段对不对"，与 `_dim_acl` **实际吐什么**
        毫无关系。把实现改成 `"effect": "shadow"`（恒定）它照样绿
        （红向自证当场抓到，判例 198）。**判据必须量被测函数的产物，不是量它的抄本。**
        改法：造真行喂给 `_dim_acl`，比它吐出来的 `effect`。
        """
        try:
            import mcpgw
        except Exception as e:  # noqa: BLE001
            self.skipTest("mcpgw 不可导入：%s" % e)
        import db
        orig_q = db.query
        cases = [{"effect": "shadow"}, {"effect": "SHADOW "},
                 {"effect": "enforce"}, {"effect": ""}, {"effect": None},
                 {"effect": "weird"}]
        for row in cases:
            full = {"agent_id": "a", "server_id": "s", "tool_pattern": "*",
                    "allow": 1, **row}          # 缺 effect 的用最后一个 case 覆盖
            db.query = lambda sql, params=(), _r=full: [dict(_r)]
            try:
                got = release._dim_acl("a")["rules"][0]["effect"]
            finally:
                db.query = orig_q
            self.assertEqual(got, mcpgw._acl_effect(full),
                             "_dim_acl 的 effect 与 mcpgw 漂移：%r" % (full,))


class TestEndpointIsReachable(unittest.TestCase):
    """判据 8：`/api/agents/releases` 必须**真的能被路由到**。

    第一版把它注册在 `/api/agents/{agent_id}` **之后**，实测被 `get_agent("releases")`
    接走（未初始化时回 503），新端点**永远打不到且不报错**。
    「加了路由」与「路由生效」是两件事 —— 只断言函数存在会假绿（判例 198）。
    """

    def test_releases_route_wins_over_dynamic_agent_id(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api_agents
        app = FastAPI()
        app.include_router(api_agents.router)
        c = TestClient(app)
        r = c.get("/api/agents/releases")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertIn("manifests", r.json())
        self.assertGreater(r.json()["count"], 0)

    def test_single_agent_release(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api_agents
        app = FastAPI()
        app.include_router(api_agents.router)
        r = TestClient(app).get("/api/agents/releases", params={"agent_id": "claude"})
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertEqual(j["agent_id"], "claude")
        self.assertRegex(j["release_id"], r"^[0-9a-f]{16}$")


class TestBatchIsolation(unittest.TestCase):
    """判据 6：一个 agent 炸掉不拖累整批。

    ⚠ 本类会读**真库**（`claude` 走 `_ensure_db`），故须在干净连接下跑：
    若前一个用例把 `db._conn` 指到了 tmp 库，`_ensure_db` 会**正确地**不重指，
    于是 `claude` 的 skills/acl 两维读的是空 tmp 库 —— 这条用例就会假红。
    第一版正是这么红的（与实现无关）。判据依赖前置状态时必须自己把它固定住。
    """

    def test_one_failure_does_not_kill_the_batch(self):
        import db
        saved = db._conn
        db._conn = None          # 让 _ensure_db 走「补一次」而不是「沿用」
        orig = release.build_manifest

        def fake(aid):
            if aid == "boom":
                raise RuntimeError("炸了")
            return orig(aid)

        release.build_manifest = fake
        try:
            out = release.manifests_for(["claude", "boom"])
        finally:
            release.build_manifest = orig
            db._conn = saved
        self.assertEqual(out["count"], 2)
        self.assertFalse(out["manifests"]["claude"]["degraded"])
        self.assertTrue(out["manifests"]["boom"]["degraded"])
        self.assertIn("炸了", out["manifests"]["boom"]["error"])


if __name__ == "__main__":
    unittest.main()
