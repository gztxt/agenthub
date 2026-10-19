#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：技能商店门面（`/api/skill/store/*`，PT-20261020-02）。

钉住四条，都是「装错一次就很难回滚」的面：

1. **安装目录只来自 SKILL_DIRS**：请求体里的 route 不在白名单就 400，
   绝不接受客户端传来的路径（路径来自请求体＝把写权限交出去）。
2. **slug 白名单**：`--force` 这类前导 `-` 会被 CLI 当选项（等于改命令语义），
   `../` 会穿出目标目录——两者都必须 400。
3. **全局参数位置**：`--skip-self-upgrade` 必须在子命令**之前**。放后面 argparse 直接
   报 usage（实测），而 `head` 管道会把 rc 吞成 0——所以夹具脚本会断言位置。
4. **CLI 缺失要表态**：503 + 原因，不许返回空结果让用户以为「搜不到」。
"""
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db              # noqa: E402
import skill           # noqa: E402
import skill_store     # noqa: E402
import writeauth       # noqa: E402

from fastapi import FastAPI           # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FAKE = """#!/usr/bin/env bash
set -euo pipefail
# 位置判据：全局参数必须最先出现（真实 CLI 的 argparse 只认这个位置）
if [ "${1:-}" != "--skip-self-upgrade" ]; then
  echo "usage: --skip-self-upgrade must come first" >&2
  exit 9
fi
cmd="${2:-}"
case "$cmd" in
  --version)
    echo "fake-skillhub 1.0"
    ;;
  search)
    echo '{"query":"demo","count":1,"results":[{"slug":"demo-skill","publicSlug":"demo-skill","name":"Demo Skill","description":"demo  desc","version":"1.2.3","source":"community","namespace":{"displayName":"tester"}}]}'
    ;;
  install)
    slug="${3:-}"
    dir=""
    prev=""
    for a in "$@"; do
      if [ "$prev" = "--dir" ]; then dir="$a"; fi
      prev="$a"
    done
    if [ -z "$dir" ]; then echo "missing --dir" >&2; exit 8; fi
    mkdir -p "$dir/$slug"
    printf -- '---\\nname: %s\\ndescription: fake\\n---\\nbody\\n' "$slug" > "$dir/$slug/SKILL.md"
    printf '{"success": true, "slug": "%s", "name": "%s", "version": "9.9.9", "targetDir": "%s/%s"}\\n' "$slug" "$slug" "$dir" "$slug"
    ;;
  login)
    echo "Login successful"
    ;;
  auth)
    if [ "${3:-}" = "whoami" ]; then echo "Error: 未登录"; exit 1; fi
    echo "logged out"
    ;;
  *)
    echo "unknown command $cmd" >&2
    exit 7
    ;;
esac
"""


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0store-"))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        for p in (self.a, self.b):
            p.mkdir()
        self._saved_dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"src": str(self.a), "other": str(self.b)}
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        self._saved_cli = skill_store.CLI
        self.cli = self.tmp / "skillhub"
        self.cli.write_text(FAKE, encoding="utf-8")
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        skill_store.CLI = str(self.cli)

        # 自动兜底的三项策略是模块级常量：逐例改、逐例还原，避免顺序耦合。
        self._saved_auto = (skill_store.AUTO_ENABLED, skill_store.AUTO_TARGET_ROUTE,
                            skill_store.AUTO_MAX_PER_HOUR)
        skill_store.AUTO_TARGET_ROUTE = "src"

        db.init_db(self.tmp / "t.db")
        app = FastAPI()
        app.include_router(skill_store.router)
        app.middleware("http")(writeauth.write_gate)
        self._saved_token = os.environ.get("TERM_TOKEN")
        os.environ["TERM_TOKEN"] = "l0-store-token"
        self.client = TestClient(app)
        self.addCleanup(self._restore)

    def _restore(self):
        skill.SKILL_DIRS = self._saved_dirs
        skill_store.CLI = self._saved_cli
        (skill_store.AUTO_ENABLED, skill_store.AUTO_TARGET_ROUTE,
         skill_store.AUTO_MAX_PER_HOUR) = self._saved_auto
        if self._saved_token is None:
            os.environ.pop("TERM_TOKEN", None)
        else:
            os.environ["TERM_TOKEN"] = self._saved_token
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hdr(self):
        return {"x-hub-token": "l0-store-token"}


class TestStatus(_StoreCase):
    def test_status_reports_version_and_targets(self):
        r = self.client.get("/api/skill/store/status", headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["available"], d)
        self.assertIn("1.0", d["version"])
        routes = {t["route"] for t in d["targets"]}
        self.assertEqual(routes, {"src", "other"})
        self.assertTrue(all(t["writable"] for t in d["targets"]))

    def test_missing_cli_is_503_with_reason(self):
        skill_store.CLI = str(self.tmp / "nope")
        r = self.client.get("/api/skill/store/status", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["available"])
        self.assertIn("不存在", r.json()["error"])
        s = self.client.get("/api/skill/store/search", params={"q": "demo"}, headers=self._hdr())
        self.assertEqual(s.status_code, 503, s.text)
        self.assertIn("未安装", s.text)


class TestSearch(_StoreCase):
    def test_search_normalises_items(self):
        r = self.client.get("/api/skill/store/search", params={"q": "demo"}, headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["count"], 1)
        it = d["items"][0]
        self.assertEqual((it["slug"], it["name"], it["version"]), ("demo-skill", "Demo Skill", "1.2.3"))
        self.assertEqual(it["namespace"], "tester")
        self.assertIn("targets", d, "搜索响应直接带目标，前端不必二次请求")


class TestInstall(_StoreCase):
    def test_installs_only_into_named_route(self):
        r = self.client.post("/api/skill/store/install",
                             json={"slug": "demo-skill", "route": "src"},
                             headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["route"], "src")
        self.assertTrue(d["scan_invalidated"])
        self.assertTrue((self.a / "demo-skill" / "SKILL.md").is_file(), "文件必须落到 src 根")
        self.assertFalse((self.b / "demo-skill").exists(), "不许写到未指定的路")

    def test_rejects_argument_injection_slug(self):
        for bad in ("--force", "-x", "../../etc", "a b", "x;rm -rf /", ""):
            r = self.client.post("/api/skill/store/install",
                                 json={"slug": bad, "route": "src"}, headers=self._hdr())
            self.assertEqual(r.status_code, 400, "slug=%r 应被拒，实际 %s %s"
                             % (bad, r.status_code, r.text[:120]))
        self.assertFalse((self.a / "--force").exists())

    def test_rejects_unknown_or_readonly_route(self):
        for bad in ("local", "tdai", "/etc", "../a"):
            r = self.client.post("/api/skill/store/install",
                                 json={"slug": "demo-skill", "route": bad}, headers=self._hdr())
            self.assertEqual(r.status_code, 400, "route=%r 应被拒" % bad)

    def test_install_is_audited(self):
        self.client.post("/api/skill/store/install",
                         json={"slug": "demo-skill", "route": "other"}, headers=self._hdr())
        rows = db.query("SELECT subject, detail FROM profile_events ORDER BY id DESC LIMIT 1")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["subject"], "skill.store_install")
        det = json.loads(rows[0]["detail"])
        self.assertEqual((det["slug"], det["route"], det["ok"]), ("demo-skill", "other", True))


class TestAcquire(_StoreCase):
    """v0.13.135 自动兜底：本地优先 → 商店补货 → 回正文。

    这一组钉的是**决策**不是实现细节：
      · 本地够像就不许联网/装盘（用 mock 把 search 变成断言，调了就算红）；
      · 商店候选不够像就**不许装**（否则等于「随便装一个」）；
      · 装成功必须真的回正文，且 `trust=community` 明示第三方来源；
      · 额度、开关、dry-run 各自可断言，不靠日志推断。
    """

    def _mk_skill(self, root, dirname, name, desc="", body="body"):
        d = pathlib.Path(root) / dirname
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, desc, body),
            encoding="utf-8")
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        return d

    def _post(self, body):
        return self.client.post("/api/skill/store/acquire", json=body, headers=self._hdr())

    def test_local_hit_never_touches_store(self):
        self._mk_skill(self.a, "local-pdf", "local-pdf", "本地 PDF 处理")
        # 语料只有 1 条时，同时出现在 name+desc 的词 IDF 会算成负数（df 逐字段计），
        # 这正是生产 400 条语料不会出现的边界 ⇒ 测试补一条无关技能把语料撑到 2 条。
        self._mk_skill(self.b, "other-tool", "other-tool", "别的工具")
        from unittest import mock
        with mock.patch.object(skill_store, "search",
                               side_effect=AssertionError("本地已命中却联网搜商店")):
            r = self._post({"query": "local pdf", "caller": "codex"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["source"], "local")
        self.assertEqual(d["name"], "local-pdf")
        self.assertIn("body", d["content"])
        self.assertTrue(d["local"]["accepted"])
        self.assertEqual(d["store"], {})
        self.assertFalse((self.a / "demo-skill").exists())

    def test_store_fallback_installs_and_returns_body(self):
        r = self._post({"query": "demo", "caller": "codex", "task": "试试"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["source"], "store")
        self.assertEqual(d["slug"], "demo-skill")
        self.assertEqual(d["trust"], "community")
        self.assertIn("第三方", d["warning"])
        self.assertIn("body", d["content"], "必须回正文，不是只回元信息")
        self.assertTrue((self.a / "demo-skill" / "SKILL.md").is_file())
        self.assertTrue(d["store"]["install"]["ok"])

    def test_store_fallback_is_idempotent(self):
        first = self._post({"query": "demo"}).json()
        self.assertEqual(first["source"], "store")
        r = self._post({"query": "demo"})
        d = r.json()
        self.assertTrue(d["ok"], d)
        # 第二次本地已经能扫到刚装的技能 ⇒ 走 local（这正是「本地优先」的证据）；
        # 若仍走商店，也必须报 already_present，不许重复安装。
        if d["source"] == "local":
            self.assertEqual(d["name"], "demo-skill")
        else:
            self.assertTrue(d["store"]["install"]["already_present"], d)
        rows = db.query("SELECT COUNT(*) AS n FROM profile_events"
                        " WHERE subject='skill.store_auto' AND status='success'")
        self.assertEqual(int(rows[0]["n"]), 1, "第二次是复用已装，不该再记一次安装账")

    def test_unrelated_store_candidate_is_not_installed(self):
        r = self._post({"query": "量子纠缠实验平台"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertFalse(d["ok"], d)
        self.assertEqual(d["source"], "none")
        self.assertIn("不够像", d["store"]["reason"])
        self.assertFalse((self.a / "demo-skill").exists(), "不相似的候选绝不落盘")

    def test_allow_install_false_only_reports_plan(self):
        r = self._post({"query": "demo", "allow_install": False})
        d = r.json()
        self.assertFalse(d["ok"], d)
        self.assertEqual(d["store"]["install"]["reason"], "allow_install=false")
        self.assertFalse((self.a / "demo-skill").exists())

    def test_dry_run_reports_target_without_writing(self):
        r = self._post({"query": "demo", "dry_run": True})
        d = r.json()
        self.assertFalse(d["ok"], d)
        self.assertIn("dry_run", d["store"]["install"]["reason"])
        self.assertFalse((self.a / "demo-skill").exists())

    def test_budget_exhausted_returns_429(self):
        skill_store.AUTO_MAX_PER_HOUR = 0
        r = self._post({"query": "demo"})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn("额度", r.text)
        self.assertFalse((self.a / "demo-skill").exists())

    def test_auto_disabled_skips_store(self):
        skill_store.AUTO_ENABLED = False
        r = self._post({"query": "demo"})
        d = r.json()
        self.assertFalse(d["ok"], d)
        self.assertIn("SKILL_STORE_AUTO", d["store"]["reason"])
        self.assertFalse((self.a / "demo-skill").exists())

    def test_acquire_records_skill_invoke(self):
        self._post({"query": "demo", "caller": "codex", "task": "生成报告"})
        rows = db.query("SELECT subject, detail FROM profile_events"
                        " WHERE subject='skill.invoke' ORDER BY id DESC LIMIT 1")
        self.assertTrue(rows, "取技能必须进调用流水（权重靠它）")
        det = json.loads(rows[0]["detail"])
        self.assertEqual(det.get("name"), "demo-skill")
        self.assertEqual(det.get("caller"), "codex")

    def test_empty_query_is_400(self):
        r = self._post({"query": "   "})
        self.assertEqual(r.status_code, 400, r.text)


class TestAuth(_StoreCase):
    def test_status_reports_not_logged_in_and_what_needs_token(self):
        r = self.client.get("/api/skill/store/status", headers=self._hdr())
        au = r.json()["auth"]
        self.assertFalse(au["logged_in"])
        self.assertIn("install", au["login_not_required_for"])
        self.assertIn("run", au["login_required_for"])

    def test_bad_token_is_400(self):
        for bad in ("", "abc", "pk_123456789", "skh_ short"):
            r = self.client.post("/api/skill/store/login", json={"key": bad},
                                 headers=self._hdr())
            self.assertEqual(r.status_code, 400, "key=%r 应被拒" % bad)

    def test_login_never_echoes_or_stores_key(self):
        key = "skh_l0testtoken123456"
        r = self.client.post("/api/skill/store/login", json={"key": key}, headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(key, r.text, "响应里不得回显 Token")
        rows = db.query("SELECT detail FROM profile_events WHERE subject='skill.store_login'")
        self.assertTrue(rows)
        self.assertNotIn(key, rows[0]["detail"] or "", "审计 detail 里不得出现 Token")


class TestInvokeFallback(_StoreCase):
    """`/api/skill/invoke` 的 allow_store 开关：**默认关**，MCP 才开。

    默认关是判据不是偏好：web/旧客户端打错一个名字就往发现点写第三方技能，
    等于把「手误」变成「装了一个不知道是谁的东西」。
    """

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(skill.router)
        app.include_router(skill_store.router)
        app.middleware("http")(writeauth.write_gate)
        self.client2 = TestClient(app)

    def test_default_is_no_store_write(self):
        r = self.client2.post("/api/skill/invoke", json={"name": "demo"}, headers=self._hdr())
        self.assertEqual(r.status_code, 404, r.text)
        self.assertFalse((self.a / "demo-skill").exists(), "默认不许写盘")

    def test_allow_store_installs_and_marks_provenance(self):
        r = self.client2.post("/api/skill/invoke",
                              json={"name": "demo", "caller": "codex", "allow_store": True},
                              headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["store_fallback"]["used"], d)
        self.assertEqual(d["store_fallback"]["trust"], "community")
        self.assertEqual(d["store_fallback"]["slug"], "demo-skill")
        self.assertIn("body", d["content"])
        self.assertTrue((self.a / "demo-skill" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
