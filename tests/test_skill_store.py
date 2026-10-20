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
import asyncio
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
  skill)
    if [ "${3:-}" = "reports" ]; then
      case "${4:-}" in
        bad*) echo '{"slug":"'"${4:-}"'","reports":{"yunding":{"name":"云鼎实验室","status":"suspicious","statusText":"可疑，存在潜在风险"}}}' ;;
        slow*) echo "Error: 无法获取 skill 详情 (HTTP 404): Skill not found" >&2; exit 1 ;;
        *) echo '{"slug":"'"${4:-}"'","reports":{"keen":{"name":"科恩实验室","status":"benign","statusText":"安全，无风险"}}}' ;;
      esac
      exit 0
    fi
    echo "unknown skill subcommand" >&2
    exit 7
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
        # **hermetic**：富字段搜索默认走 https://api.skillhub.cn，L0 不许联网。
        # 置空 ⇒ `_api_search` 立刻抛错、`_search_enriched` 回落夹具 CLI。
        self._saved_api = skill_store.API_SEARCH
        skill_store.API_SEARCH = ""
        self._saved_sec = dict(skill_store._sec_cache)
        skill_store._sec_cache.clear()

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
        skill_store.API_SEARCH = self._saved_api
        skill_store._sec_cache.clear()
        skill_store._sec_cache.update(self._saved_sec)
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

    def test_cli_present_matches_available(self):
        """两个字段必须同源：`available` 用绝对路径判定，`cli_present` 曾用
        `shutil.which("skillhub")`（查进程 PATH，systemd 下无 ~/.local/bin）⇒ 恒 false。
        面板一边说「可用」一边说「缺失」是自相矛盾的展示（2026-10-20 生产实测）。"""
        d = self.client.get("/api/skill/store/status", headers=self._hdr()).json()
        self.assertTrue(d["available"], d)
        self.assertTrue(d["cli_present"], "CLI 能跑就必须报 present")
        skill_store.CLI = str(self.tmp / "nope")
        d2 = self.client.get("/api/skill/store/status", headers=self._hdr()).json()
        self.assertFalse(d2["available"])
        self.assertEqual(d2["cli_present"], False, "不存在就必须报 present=false")

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
        # 装成功后 `store.ok` 必须翻绿：否则同一个响应里同时出现「store 失败」与
        # 「已落盘」（2026-10-20 实测，前端据此画成了「未装」）。
        self.assertTrue(d["store"]["ok"], d["store"])

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

    def test_plan_only_is_not_reported_as_not_found(self):
        """「已选中只是没落盘」必须与「真没找到」可区分（2026-10-20 实测曾同形）。

        两者都是 `ok=false` + `source=none`，调用方只能靠 reason/would_install 分辨；
        少了这两个字段，agent 会把「再调一次就能装」误读成「商店没有这个技能」。
        """
        for payload in ({"query": "demo", "dry_run": True},
                        {"query": "demo", "allow_install": False}):
            d = self._post(payload).json()
            self.assertFalse(d["ok"], d)
            self.assertIn("reason", d, payload)
            wi = d.get("would_install") or {}
            self.assertEqual(wi.get("slug"), "demo-skill", payload)
            self.assertEqual(wi.get("route"), "src")
            self.assertGreater(wi.get("quality") or 0, 0)
            self.assertIn("allow_install=true", wi.get("how_to_proceed", ""))

    def test_plan_only_is_not_counted_as_use(self):
        """dry_run 什么都没交付 ⇒ 不许在 `skill.invoke` 上留一条使用流水。

        它原先被记成 `skill.invoke` 且 name 为空：调用记录多一条无名行、权重账虚增
        （`use` 分子被 +2）。判据落在**事件表**而不是响应体，因为账本才是下游依据。
        """
        self._post({"query": "demo", "dry_run": True})
        rows = db.query("SELECT COUNT(*) AS n FROM profile_events"
                        " WHERE subject='skill.invoke'")
        self.assertEqual(int(rows[0]["n"]), 0, "计划态不是使用")

    def test_real_acquire_does_leave_a_use(self):
        """反向判据：真装并交付正文时必须留一条 `skill.invoke`（否则上一条是空断言）。"""
        d = self._post({"query": "demo"}).json()
        self.assertTrue(d["ok"], d)
        rows = db.query("SELECT COUNT(*) AS n FROM profile_events"
                        " WHERE subject='skill.invoke' AND detail LIKE '%demo-skill%'")
        self.assertGreaterEqual(int(rows[0]["n"]), 1)

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


class TestMultiDim(_StoreCase):
    """v0.13.136 同类择优：**分得开、拒得掉、说得清**。

    用户报的问题是「同类型技能很多，不能随便装又不能装完没效果」。这一组钉四件事：
      · 同分条件下，下载量高的胜（别人真用过＝最便宜的效果证据）；
      · 安全报告 `suspicious` **一票否决**，哪怕它分最高；
      · 同类有免 Key 的就别选要 Key 的（否则装完 agent 还得停下来要凭据）；
      · 「没查到报告」是 `unknown`，既不许写成 `benign`，也不许当 0 分打死。
    """

    def _it(self, slug, name, desc, **kw):
        base = {"slug": "@h/" + slug, "public_slug": slug, "handle": "h",
                "name": name, "description": desc, "version": "1.0.0",
                "downloads": 0, "needs_key": False, "verified": False,
                "updated_at": None}
        base.update(kw)
        return base

    def test_unknown_downloads_is_not_zero(self):
        """`downloads=None`（CLI 兜底路径）不能算成「没人用」——否则多维度退化成只看词。"""
        self.assertGreater(skill_store._pop_score(None), skill_store._pop_score(0))
        self.assertEqual(skill_store._pop_score(0), 0.0)

    def test_popularity_breaks_ties(self):
        items = [self._it("a", "pdf tool", "pdf 处理", downloads=10),
                 self._it("b", "pdf tool", "pdf 处理", downloads=500000)]
        rows = skill_store.score_candidates("pdf", items)
        self.assertEqual(rows[0]["slug"], "@h/b")
        self.assertGreater(rows[0]["quality"], rows[1]["quality"])
        self.assertEqual(rows[0]["veto"], "")

    def test_suspicious_is_vetoed_even_if_top(self):
        items = [self._it("bad", "pdf tool", "pdf 处理", downloads=10 ** 7),
                 self._it("ok", "pdf tool", "pdf 处理", downloads=1000)]
        sec = {"@h/bad": {"verdict": "suspicious", "providers": [
                   {"name": "云鼎实验室", "status": "suspicious", "status_text": "可疑"}]},
               "@h/ok": {"verdict": "benign", "providers": []}}
        rows = skill_store.score_candidates("pdf", items, sec)
        bad = [r for r in rows if r["slug"] == "@h/bad"][0]
        self.assertTrue(bad["veto"], "可疑必须被否决，不管分多高")
        self.assertIn("可疑", bad["veto"])
        pick, metrics = skill_store._pick_scored(rows)
        self.assertEqual(pick["slug"], "@h/ok", "否决项不许被选中")

    def test_prefers_no_key_alternative(self):
        items = [self._it("needs", "pdf tool", "pdf 处理", downloads=800000, needs_key=True),
                 self._it("free", "pdf tool", "pdf 处理", downloads=2000, needs_key=False)]
        rows = skill_store.score_candidates("pdf", items)
        pick, metrics = skill_store._pick_scored(rows)
        self.assertEqual(pick["slug"], "@h/free")
        self.assertFalse(metrics["needs_key"])
        self.assertIn("免 Key", metrics["why"])

    def test_low_quality_is_not_installed(self):
        rows = skill_store.score_candidates("量子纠缠平台", [self._it("x", "pdf tool", "pdf 处理")])
        pick, metrics = skill_store._pick_scored(rows)
        self.assertIsNone(pick)
        self.assertIn("不够像", metrics["reason"])

    def test_matched_but_weak_candidate_is_rejected(self):
        """「像但没人用、还要 Key、没更新」也会被质量底线挡住——门槛不只看关键词。

        没有这条，`STORE_QUALITY_MIN` 就是个没人验的常量（改坏不会红）。
        """
        it = self._it("weak", "pdf tool", "pdf 处理", downloads=0, needs_key=True,
                      updated_at=1_000_000_000_000)   # 2001 年 ⇒ fresh 0.2
        rows = skill_store.score_candidates("pdf ocr", [it])
        self.assertTrue(rows[0]["matched"], "关键词名称命中 0.5，属「像」")
        self.assertIn("质量分", rows[0]["veto"])
        pick, _m = skill_store._pick_scored(rows)
        self.assertIsNone(pick)

    def test_unchecked_winner_gets_checked_before_pick(self):
        """初筛前 3 名全被标记可疑时，第 4 名（原本没查）必须补查再定，不许靠未知分上位。"""
        items = [self._it("bad%d" % i, "pdf tool", "pdf 处理", downloads=10 ** 6)
                 for i in range(1, 4)]
        items.append(self._it("good", "pdf tool", "pdf 处理", downloads=1000))
        rows, row, metrics = __import__("asyncio").run(
            skill_store._rank_store("pdf", items))
        self.assertIsNotNone(row)
        self.assertEqual(row["slug"], "@h/good")
        self.assertEqual(row["security"], "benign", "中选者必须真的查过报告")
        self.assertTrue(metrics["viable"] >= 1)

    def test_security_404_is_unknown_not_benign(self):
        sec = skill_store._security("@h/slow-report")
        self.assertEqual(sec["verdict"], "unknown", "查不到不能当安全")

    def test_install_refuses_suspicious_unless_forced(self):
        r = self.client.post("/api/skill/store/install",
                             json={"slug": "bad-skill", "route": "src"}, headers=self._hdr())
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("可疑", r.text)
        self.assertFalse((self.a / "bad-skill").exists(), "拒绝时不许落盘")
        r2 = self.client.post("/api/skill/store/install",
                              json={"slug": "bad-skill", "route": "src", "force": True},
                              headers=self._hdr())
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue((self.a / "bad-skill" / "SKILL.md").is_file())

    def test_search_falls_back_to_cli_when_api_down(self):
        """富字段拿不到不许影响「搜得到」——回落 CLI，且 `via` 如实标 cli。"""
        r = self.client.get("/api/skill/store/search", params={"q": "demo"},
                            headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["via"], "cli")
        self.assertEqual(r.json()["items"][0]["slug"], "demo-skill")


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


class TestCnSegment(unittest.TestCase):
    """中文任务的**派生短查询**（v0.13.139）。

    上游 `api.skillhub.cn/api/v1/search` 对中文是词级匹配：整句带虚词搜不到东西
    （实测整句 `把播客音频转成带说话人标注的文字稿` 返回 12 条「编程专家」，
    而 `播客音频` 能把播客类技能顶到第一）。这一组钉住派生规则本身：
    长度上限、虚词剔除、ASCII-only 不派生。
    """

    def test_short_run_kept_whole(self):
        """≤5 字本身就是词：`语音转文字` 不许截成 `语音转文`（实测截了反而搜不到）。"""
        self.assertEqual(skill_store._cn_segments("语音转文字"), ["语音转文字"])

    def test_long_run_cut_to_four(self):
        segs = skill_store._cn_segments("把播客音频转成带说话人标注的文字稿")
        self.assertTrue(segs)
        self.assertIn("播客音频转", segs)
        for x in segs:
            self.assertLessEqual(len(x), 5, "实测 6 字以上中文短语上游匹配不上")

    def test_function_words_are_separators(self):
        segs = skill_store._cn_segments("把这份 PDF 转成 Markdown")
        self.assertFalse([x for x in segs if x.startswith("把")], segs)
        self.assertFalse([x for x in segs if "的" in x], segs)

    def test_ascii_query_yields_nothing(self):
        """全英文任务已有 ASCII 关键词通道，别再造中文短查询（会多打网络）。"""
        self.assertEqual(skill_store._cn_segments("pdf to markdown"), [])


class TestCnScoring(_StoreCase):
    """中文任务的两条判据：**不因语言被系统性扣分**，**也不因短词命中就自动装**。"""

    def _it(self, slug, name, desc, **kw):
        base = {"slug": "@h/" + slug, "public_slug": slug, "handle": "h",
                "name": name, "description": desc, "version": "1.0.0",
                "downloads": 5000, "needs_key": False, "verified": False,
                "updated_at": None}
        base.update(kw)
        return base

    def test_chinese_name_coverage_is_scored(self):
        """中文技能名 + 中文任务：`kw_name` 结构性恒 0，必须由名字二字组覆盖补上。

        没有这条，`rel` 上限只有 0.35，中文技能哪怕名字完全对得上也会被
        `STORE_QUALITY_MIN` 挡在门外（实测 `播客音频转知识笔记` 六维 0.4355 < 0.45）。
        """
        it = self._it("pod", "播客音频转写", "把播客音频转成文字稿")
        rows = skill_store.score_candidates("播客音频转成文字稿", [it])
        self.assertGreaterEqual(rows[0]["dims"]["qcov_name"], 0.5, rows[0]["dims"])
        # 关键不变量：`rel` 必须越过「中文恒 0 关键词」时的上限 0.35。
        # 只断言 qcov_name 存在是不够的——公式改回只用 kw_name 时它照样非零。
        self.assertGreater(rows[0]["dims"]["rel"], 0.5, rows[0]["dims"])
        self.assertTrue(rows[0]["matched"], rows[0])
        self.assertEqual(rows[0]["veto"], "", "整句就像，不该被否决")

    def test_segment_only_match_cannot_be_picked(self):
        """只靠派生短词命中的候选**不许自动安装**：短词命中的可能是反向技能。"""
        it = self._it("gen", "播客音频生成", "把文字稿合成为播客音频")
        rows = skill_store.score_candidates("把播客音频转成带说话人标注的文字稿", [it],
                                            alts=["播客音频转"])
        row = rows[0]
        self.assertTrue(row["matched"], "短词命中 ⇒ 属于「有关」，要展示给用户")
        self.assertFalse(row["matched_main"], "但整句并不匹配")
        self.assertIn("短词", row["veto"])
        pick, metrics = skill_store._pick_scored(rows)
        self.assertIsNone(pick, "不许自动装反向技能")
        self.assertIn("短词", metrics["reason"])

    def test_acquire_retries_with_segments_and_reports(self):
        """首轮整句搜不到 ⇒ 用派生短词补候选；补到的只进排行榜，不落盘。"""
        garbage = [{"slug": "@h/dev", "public_slug": "dev", "handle": "h",
                    "name": "编程专家", "description": "写代码", "version": "1.0.0",
                    "downloads": 5_000_000, "needs_key": False, "verified": False,
                    "updated_at": None}]
        hit = [{"slug": "@h/pod", "public_slug": "pod", "handle": "h",
                "name": "播客音频转知识笔记", "description": "播客转笔记",
                "version": "1.0.0", "downloads": 900, "needs_key": False,
                "verified": False, "updated_at": None}]
        calls = []

        def fake_search(q, limit=12):
            calls.append(q)
            return {"count": len(garbage if len(calls) == 1 else hit),
                    "items": garbage if len(calls) == 1 else hit, "via": "cli"}

        from unittest import mock
        with mock.patch.object(skill_store, "_search_enriched", side_effect=fake_search):
            d = asyncio.run(skill_store.acquire("把播客音频转成带说话人标注的文字稿",
                                                caller="probe"))
        self.assertFalse(d["ok"], d)
        self.assertGreaterEqual(len(calls), 2, "首轮无候选才该派生，且必须真的再搜一次")
        self.assertTrue(any(t.get("derived") for t in d["store"]["queries"]), d["store"]["queries"])
        slugs = [r["slug"] for r in d["store"]["ranking"]]
        self.assertIn("@h/pod", slugs, "补到的候选要给用户看得见")
        self.assertIn("短词", d["reason"], d["reason"])
        self.assertFalse((self.a / "@h" / "pod").exists(), "不许自动装")


if __name__ == "__main__":
    unittest.main(verbosity=2)
