"""`src/settings_bundle.py` 的 L0 闸门（PT-20260927-15 / P2）。

**零网络、零第三方依赖**（L0 用例本仓 `tests/` 下的既有 db 夹具，见 `test_db_*` 的
`_L0_TMP` 口径）。

覆盖按**能区分什么**组织：

A. **导出边界**（什么进包/什么不进）—— 这是本功能最容易出事的地方：把凭据或
   内网地址抄进一个会被人随手转发到聊天窗口的 JSON 文件。红向：把 `mcp_servers`
   塞进导出 ⇒ A2 转红。
B. **导出形状与确定性** —— 两次导出逐字节相同（否则没法 diff）。
C. **导入幂等** —— 同一包导两次，表里**不出现第二行**；且不清本机独有条目。
   红向：把 upsert 改成 INSERT ⇒ C2 转红。
D. **先校验后写** —— 包不合法时**库一个字节都不动**（半途失败会留下脏配置）。
   红向：把 `_validate` 挪到 `apply_bundle` 末尾 ⇒ D2 转红。
E. **未来版本显式拒绝** —— 不做「尽力解析」（字段改名会变成静默丢配置）。
F. **写门** —— 导出是 GET 但按写方法判（批量读＝数据外流，同 `audit.py` 口径）。
   红向：把 `_require_auth` 里的 "POST" 改成 "GET" ⇒ F2 转红。
G. **形状校验的边界** —— 超长文本、非白名单偏好键、缺 id、坏 JSON。
"""

import json
import pathlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db            # noqa: E402
import mcpgw         # noqa: E402 —— mcp_acl/mcp_servers 的建表归它（不在 db.SCHEMA 里）
import settings_bundle as SB  # noqa: E402
import tiers         # noqa: E402


class _DbCase(unittest.TestCase):
    """把 db 换到 tmp 文件库；cleanup 必须**关掉再还原**全局连接。

    三处口径都照抄本仓既有范式（`tests/test_asset_audit.py`）：
    · `db._conn` 是模块级全局，不还原会污染同进程后续用例；
    · 夹具目录走 `tiers.l0_fixture_sweep`（**不能用 `/tmp`**——那是精度闸的排除
      路径前缀，夹具住那儿会被先杀光，测不到排除逻辑本身；见 tiers 注释）；
    · `mcp_acl`/`mcp_servers` **不在 `db.SCHEMA` 里**（建表归 `mcpgw.ensure_schema`，
      见 main.py:1180），所以换库后必须补调一次，否则「no such table」。
    """

    def setUp(self):
        self.tmp = tiers.l0_fixture_sweep(self)
        self._saved_conn = db._conn
        db.init_db(self.tmp / "t.db")
        mcpgw.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = self._saved_conn


def _fresh():
    """清空本测试要动的三张表。**只清自己的**，不动资产审计等旁表。"""
    for t in ("custom_agents", "mcp_acl", "app_prefs"):
        db.execute("DELETE FROM %s" % t)


def _mk_agent(aid="a1", name="自定义工具", cmd="/usr/bin/foo", env=None):
    db.execute(
        "INSERT INTO custom_agents(id,name,type,command,args,working_dir,env,"
        "port,endpoint,description,source,auto_restart,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,'manual',0,?,?)",
        (aid, name, "custom", cmd, '["--x"]', "/tmp", env,
         8080, "", "一个描述", "2026-01-01T00:00:00", "2026-01-01T00:00:00"))


class TestExportBoundary(_DbCase):
    """A. 什么进包、什么不进。"""

    def setUp(self):
        super().setUp()          # 必须调父类：否则 db._conn 仍是 None
        _fresh()

    def test_custom_agents_exported(self):
        _mk_agent("a1")
        _mk_agent("a2", name="第二个")
        b = SB.collect()
        self.assertEqual(b["counts"]["custom_agents"], 2)
        self.assertEqual({a["id"] for a in b["custom_agents"]}, {"a1", "a2"})

    def test_acl_and_prefs_exported(self):
        db.execute("INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow) "
                   "VALUES('manager','srv','read_*',1)")
        db.execute("INSERT INTO app_prefs(key,value,updated_at) VALUES(?,?,?)",
                   ("projects.lp", json.dumps({"stars": ["/a"], "hidden": []}),
                    "2026-01-01T00:00:00"))
        b = SB.collect()
        self.assertEqual(b["counts"]["mcp_acl"], 1)
        self.assertEqual(b["counts"]["prefs"], 1)
        self.assertEqual(b["prefs"][0]["key"], "projects.lp")
        # prefs 存的是 JSON 字符串，导出必须是**结构**而不是字符串 repr
        self.assertIsInstance(b["prefs"][0]["value"], dict)
        self.assertEqual(b["prefs"][0]["value"]["stars"], ["/a"])

    def test_mcp_server_urls_and_tokens_never_exported(self):
        """**导出的核心边界**：server 地址与 env 里的 token 是机器事实 + 凭据，不是配置。

        红向本组：把 `mcp_servers` 加进 collect() 的返回 ⇒ 转红。
        """
        db.execute(
            "INSERT INTO mcp_servers(id,name,transport,command,args,env,url,enabled,"
            "description,created_at,updated_at) "
            "VALUES(?,?,'http','', '[]', ?, ?, 1,'','2026-01-01','2026-01-01')",
            ("srv1", "内网服务", json.dumps({"TOKEN": "ghp_" + ("Zz1Yy2Xx3Ww4Vv5Uu6Tt7Ss8" * 2)[:36]}),
             "http://10.0.0.9:9999/mcp"))
        blob = SB.to_json(SB.collect())
        self.assertNotIn("10.0.0.9", blob, "内网 server 地址不该出现在导出件里")
        self.assertNotIn("ghp_Zz1Y", blob, "server token 绝不该出现在导出件里")
        self.assertNotIn("mcp_servers", blob)

    def test_secrets_in_free_text_are_redacted(self):
        """自定义 agent 的 env/description 里可能有人写了 token —— 过既有脱敏器。

        样本必须用**真实形态**：`sessions_export._PATTERNS` 判 `ghp_` 要求后跟
        **36 位**（GitHub PAT 的实际长度）。第一版用 16 位样本，测试红了，
        查下去是**样本不对**而不是脱敏坏了 —— 这正是「红灯不等于我的实现错」。
        """
        real_ish = "ghp_" + ("Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8" * 2)[:36]   # ghp_ + 36 位（程序算，别手数）
        _mk_agent("a1", env=json.dumps({"API_KEY": real_ish}),
                  name="带凭据的 agent")
        blob = SB.to_json(SB.collect())
        self.assertNotIn(real_ish, blob, "自由文本里的 PAT 没过脱敏器")
        # 对照：不能把整个字段抹掉（那会毁掉可迁移性）
        self.assertIn("API_KEY", blob)

    def test_filename_marked_sensitive(self):
        self.assertIn(SB.SENSITIVE_HINT, SB.suggested_filename())


class TestExportShape(_DbCase):
    """B. 形状与确定性。"""

    def setUp(self):
        super().setUp()          # 必须调父类：否则 db._conn 仍是 None
        _fresh()
        _mk_agent("a1")
        db.execute("INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow) "
                   "VALUES('manager',NULL,'read_*',1)")

    def test_kind_and_version_present(self):
        b = SB.collect()
        self.assertEqual(b["kind"], SB.BUNDLE_KIND)
        self.assertEqual(b["version"], SB.BUNDLE_VERSION)

    def test_two_exports_byte_identical(self):
        """没有确定性就没法「diff 两个导出件看改了什么」—— 那是本功能最基本的手艺。
        （`exported_at` 会变，所以比的是**除该字段外**的正文。）"""
        b1, b2 = SB.collect(), SB.collect()
        b1.pop("exported_at"), b2.pop("exported_at")
        self.assertEqual(SB.to_json(b1), SB.to_json(b2))

    def test_json_roundtrip(self):
        b = SB.collect()
        self.assertEqual(json.loads(SB.to_json(b))["kind"], SB.BUNDLE_KIND)


class TestImportIdempotent(_DbCase):
    """C. 幂等 + 不清本机独有条目。"""

    def setUp(self):
        super().setUp()          # 必须调父类：否则 db._conn 仍是 None
        _fresh()

    def test_double_import_creates_no_duplicate(self):
        _mk_agent("a1")
        bundle = SB.collect()
        SB.apply_bundle(bundle, "tester")
        n1 = db.query("SELECT COUNT(*) c FROM custom_agents WHERE id='a1'")[0]["c"]
        SB.apply_bundle(bundle, "tester")
        n2 = db.query("SELECT COUNT(*) c FROM custom_agents WHERE id='a1'")[0]["c"]
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1, "重复导入产生了第二行 —— upsert 没生效")

    def test_import_updates_existing_row(self):
        _mk_agent("a1", cmd="/usr/bin/old")
        bundle = SB.collect()
        bundle["custom_agents"][0]["command"] = "/usr/bin/new"
        SB.apply_bundle(bundle, "tester")
        row = db.query("SELECT command FROM custom_agents WHERE id='a1'")[0]
        self.assertEqual(row["command"], "/usr/bin/new")

    def test_import_does_not_delete_local_only_rows(self):
        """「导入一份包」不该顺手删掉目标机上已有的自定义 agent。"""
        _mk_agent("keepme")
        bundle = SB.collect()
        db.execute("DELETE FROM custom_agents WHERE id='keepme'")
        _mk_agent("other")
        SB.apply_bundle(bundle, "tester")
        ids = {r["id"] for r in db.query("SELECT id FROM custom_agents")}
        self.assertIn("other", ids, "导入把本机独有条目删了")

    def test_acl_duplicates_skipped_not_duplicated(self):
        db.execute("INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow) "
                   "VALUES('manager','srv','read_*',1)")
        bundle = SB.collect()
        r1 = SB.apply_bundle(bundle, "t")
        r2 = SB.apply_bundle(bundle, "t")
        n = db.query("SELECT COUNT(*) c FROM mcp_acl")[0]["c"]
        self.assertEqual(n, 1, "ACL 重复导入产生了重复行")
        self.assertEqual(r2["skipped"], 1)

    def test_source_marked_import(self):
        """导入的条目标 source='import' —— 与手工注册（manual）可区分。"""
        _mk_agent("a1")
        SB.apply_bundle(SB.collect(), "t")
        row = db.query("SELECT source FROM custom_agents WHERE id='a1'")[0]
        self.assertEqual(row["source"], "import")

    def test_audit_written(self):
        """走既有记账三件套（db.log_asset_event），不另造日志。"""
        _mk_agent("a1")
        SB.apply_bundle(SB.collect(), "actor-x")
        rows = db.query("SELECT * FROM asset_audit WHERE asset_slug='a1' "
                        "AND action='import'")
        self.assertTrue(rows, "导入没记账")
        self.assertEqual(rows[0]["actor"], "actor-x")


class TestValidateBeforeWrite(_DbCase):
    """D. 校验不过 ⇒ 库一个字节都不动。"""

    def setUp(self):
        super().setUp()          # 必须调父类：否则 db._conn 仍是 None
        _fresh()
        _mk_agent("keepme")

    def _count(self):
        return db.query("SELECT COUNT(*) c FROM custom_agents")[0]["c"]

    def test_bad_bundle_touches_nothing(self):
        before = self._count()
        for bad in ({"kind": "wrong"}, {"kind": SB.BUNDLE_KIND},
                    {"kind": SB.BUNDLE_KIND, "version": 99},
                    {"kind": SB.BUNDLE_KIND, "version": SB.BUNDLE_VERSION,
                     "custom_agents": [{"name": "没有 id"}]}):
            with self.assertRaises(SB.BundleError, msg=repr(bad)):
                SB.apply_bundle(bad, "t")
        self.assertEqual(self._count(), before, "校验失败的包仍然改了库")

    def test_future_version_rejected_explicitly(self):
        """不做「尽力解析」—— 字段改名会变成静默丢配置。"""
        with self.assertRaises(SB.BundleError) as ctx:
            SB.apply_bundle({"kind": SB.BUNDLE_KIND, "version": 99}, "t")
        self.assertIn("升级", str(ctx.exception))

    def test_good_rows_before_bad_row_leaves_db_untouched(self):
        """校验是**整包前置**的：第 2 条坏 ⇒ 第 1 条也不该落库。"""
        before = self._count()
        with self.assertRaises(SB.BundleError):
            SB.apply_bundle({"kind": SB.BUNDLE_KIND, "version": SB.BUNDLE_VERSION,
                             "custom_agents": [
                                 {"id": "good", "name": "好的"},
                                 {"id": "", "name": "坏的"}]}, "t")
        self.assertEqual(self._count(), before)
        self.assertEqual(
            db.query("SELECT COUNT(*) c FROM custom_agents WHERE id='good'")[0]["c"], 0)


class TestShapeValidation(_DbCase):
    """G. 各类坏输入都要给出可读的错误，不是栈回溯。"""

    def test_oversize_text_rejected(self):
        with self.assertRaises(SB.BundleError):
            SB.apply_bundle({"kind": SB.BUNDLE_KIND, "version": SB.BUNDLE_VERSION,
                             "custom_agents": [{"id": "x", "name": "y",
                                                "description": "z" * (SB.MAX_TEXT + 1)}]}, "t")

    def test_pref_key_must_be_whitelisted(self):
        """键白名单是 prefs 的口径 —— 导入不得成为绕过它的一条路。"""
        with self.assertRaises(SB.BundleError) as ctx:
            SB.apply_bundle({"kind": SB.BUNDLE_KIND, "version": SB.BUNDLE_VERSION,
                             "prefs": [{"key": "evil.key", "value": {}}]}, "t")
        self.assertIn("白名单", str(ctx.exception))

    def test_row_cap_enforced(self):
        big = [{"id": "a%d" % i, "name": "n"} for i in range(SB.MAX_ROWS + 5)]
        with self.assertRaises(SB.BundleError) as ctx:
            SB.apply_bundle({"kind": SB.BUNDLE_KIND, "version": SB.BUNDLE_VERSION,
                             "custom_agents": big}, "t")
        self.assertIn("上限", str(ctx.exception))

    def test_not_a_dict_rejected(self):
        for bad in ("[]", "null", "123", '"x"'):
            with self.assertRaises(SB.BundleError):
                SB.apply_bundle(json.loads(bad), "t")


class TestAuthContract(unittest.TestCase):
    """F. 写门契约（静态断言行为，不是格式）。"""

    SRC = Path(__file__).resolve().parent.parent / "src" / "settings_bundle.py"

    def test_export_is_get_but_judged_as_write(self):
        """导出是 GET，但**按写方法判**——同 `audit.py::_require_read` 口径。

        红向本组：把 `_require_auth` 里的 "POST" 改成 "GET" ⇒ 转红。
        GET 不走全局 write_gate（它只拦写方法），改了等于导出变成无鉴权通道。
        """
        src = self.SRC.read_text(encoding="utf-8")
        body = src.split("def _require_auth", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"POST"', body,
                      "导出鉴权必须按写方法判（批量读＝数据外流）")

    def test_both_endpoints_call_require_auth(self):
        src = self.SRC.read_text(encoding="utf-8")
        for ep in ("/api/settings/export", "/api/settings/import"):
            self.assertIn(ep, src, "缺端点 %s" % ep)
        # 两个 handler 各调一次（`def _require_auth(request)` 是定义，不计入调用）
        self.assertEqual(src.count("_require_auth(request)"), 2,
                         "导出与导入两个端点必须各自鉴权一次")
        # 顺带钉住「鉴权在 handler 第一行」—— 放在读库之后就等于没拦
        for ep in ("/api/settings/export", "/api/settings/import"):
            tail = src.split(ep, 1)[1]
            nxt = tail.find("_require_auth(request)")
            self.assertGreater(nxt, 0, "%s 没有鉴权" % ep)
            self.assertNotIn("collect()", tail[:nxt],
                             "%s 在鉴权之前就取了库数据" % ep)

    def test_export_response_not_cacheable(self):
        """导出件含命令与环境 —— 必须 no-store，否则浏览器/中间层缓存它。"""
        src = self.SRC.read_text(encoding="utf-8")
        self.assertIn("no-store", src)
        self.assertIn("attachment; filename", src)

    def test_no_yaml_dependency(self):
        """本机硬约束：零新增第三方依赖。一律 JSON。"""
        src = self.SRC.read_text(encoding="utf-8")
        self.assertNotIn("import yaml", src)
        self.assertNotIn("BUNDLE_YAML", src)


class TestRoundTrip(_DbCase):
    """整体闭环：导出 → 改 → 导入 → 导出，两边结构一致。"""

    def setUp(self):
        super().setUp()          # 必须调父类：否则 db._conn 仍是 None
        _fresh()

    def test_roundtrip_preserves_shape(self):
        _mk_agent("a1", cmd="/usr/bin/foo")
        db.execute("INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow) "
                   "VALUES('manager','srv','read_*',1)")
        db.execute("INSERT INTO app_prefs(key,value,updated_at) VALUES(?,?,?)",
                   ("projects.gh", json.dumps({"stars": ["o/r"], "hidden": ["x/y"]}),
                    "2026-01-01T00:00:00"))
        before = SB.collect()
        before.pop("exported_at")
        _fresh()
        SB.apply_bundle(before, "t")
        after = SB.collect()
        after.pop("exported_at")
        self.assertEqual(before["custom_agents"], after["custom_agents"])
        self.assertEqual(before["mcp_acl"], after["mcp_acl"])
        self.assertEqual(before["prefs"], after["prefs"])


class TestHttpEndpoints(_DbCase):
    """HTTP 层的实弹（`TestClient` 只挂本模块 router，**不 import src.main**——
    照抄 `tests/test_memfed.py` 的口径：import src.main 会起 vitals 后台循环）。

    这一组验「路由真的能用」：`_validate`/`apply_bundle` 全绿不代表端点能通 ——
    Pydantic 模型、鉴权头解析、Content-Disposition 都在这一层。

    **凭据一律用 `mock.patch("writeauth.secrets_from_env")` 注入**，不碰真env：
    ① `secrets_from_env()` 读的是 `TERM_TOKEN`/`HUB_PASSCODE`（writeauth.py:87）；
    ② 仓内 `.env` 会被 `config.py` 的 `load_dotenv(override=True)` 灌进os.environ
       （判例 107/179），所以「pop 掉 env」根本挡不住它。第一版用 `os.environ[...]`
       赋值，结果拿到 `deny`(401) 而非 `misconfig`(503) —— **红灯不等于实现错，
       先查判据本身读的是哪**。
    """

    def _client(self):
        import fastapi
        from fastapi.testclient import TestClient
        from settings_bundle import router
        app = fastapi.FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_export_fail_closed_when_no_secret(self):
        """服务端没配凭据 ⇒ **503 而不是放行**（fail-closed，同 writeauth.decide）。

        若这条返回 200，导出就是一个无鉴权通道。
        对照组：猜 token 也得拒 —— 注意此处**仍是 503**：`decide` 在 `secrets` 为空时
        一律 `misconfig`，与客户端带没带 token 无关（没配凭据时「哪个 token」无从谈起）。
        第一版把对照组期望写成 401，是把「无凭据」与「凭据错」两种态混了。
        """
        from unittest import mock
        with mock.patch("writeauth.secrets_from_env", return_value=[]):
            with self._client() as c:
                r = c.get("/api/settings/export")
                self.assertEqual(r.status_code, 503,
                                 "未配凭据必须 503（fail-closed），实际 %s" % r.status_code)
                r2 = c.get("/api/settings/export", headers={"x-hub-token": "guess"})
                self.assertEqual(r2.status_code, 503, r2.status_code)

    def test_export_json_with_attachment_and_nostore(self):
        from unittest import mock
        _mk_agent("a1")
        with mock.patch("writeauth.secrets_from_env", return_value=["tok-A"]):
            with self._client() as c:
                r = c.get("/api/settings/export", headers={"x-hub-token": "tok-A"})
        self.assertEqual(r.status_code, 200, r.text)
        cd = r.headers.get("content-disposition", "")
        self.assertIn("attachment; filename=", cd)
        self.assertIn("_sensitive", cd)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(json.loads(r.text)["kind"], SB.BUNDLE_KIND)

    def test_export_wrong_token_denied(self):
        """错 token ⇒ 401。**样本必须纯 ASCII** —— HTTP 头带非 ASCII 会让
        httpx 在编码层抛 UnicodeEncodeError，那不是本用例要验的东西
        （第一版用 `tok-真`/`tok-错`，红因在传输层，判据根本没跑到）。"""
        from unittest import mock
        with mock.patch("writeauth.secrets_from_env", return_value=["tok-right"]):
            with self._client() as c:
                r = c.get("/api/settings/export", headers={"x-hub-token": "tok-wrong"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_import_roundtrip_over_http(self):
        from unittest import mock
        _mk_agent("a1", cmd="/usr/bin/foo")
        hdr = {"x-hub-token": "tok-B"}
        with mock.patch("writeauth.secrets_from_env", return_value=["tok-B"]):
            with self._client() as c:
                bundle = c.get("/api/settings/export", headers=hdr).json()
                db.execute("DELETE FROM custom_agents")
                r = c.post("/api/settings/import", json={"bundle": bundle}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["applied"]["custom_agents"], 1)
        rows = db.query("SELECT command FROM custom_agents WHERE id='a1'")
        self.assertEqual(rows[0]["command"], "/usr/bin/foo")

    def test_import_bad_bundle_returns_400_not_500(self):
        """形状错 ⇒ **400**（可读错误），不是 500（用户看不懂栈回溯）。"""
        from unittest import mock
        hdr = {"x-hub-token": "tok-C"}
        with mock.patch("writeauth.secrets_from_env", return_value=["tok-C"]):
            with self._client() as c:
                r = c.post("/api/settings/import", json={"bundle": {"kind": "x"}},
                           headers=hdr)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("配置包", r.text)


if __name__ == "__main__":
    unittest.main()
