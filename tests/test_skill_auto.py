#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：自动取用入口 `POST /api/skill/auto`（v0.13.138）。

钉住五条，都是「自动」这个词最容易长歪的地方：

1. **自动 ≠ 无条件**：本地不够像（归一化 BM25 < 0.40）就不激活，回 `ok=false` +
   候选/排序，**不装一个不相干的东西**充数。
2. **本地优先不联网**：`allow_store=false` 时连搜索都不许打（`local_only`）。
   `allow_install=false` 的语义是「只报告将会装什么」，**它仍会联网**——两者不是一回事。
3. **商店兜底可激活**：装完必须立刻回正文，且 `trust=community`；不是只回一个 slug
   让调用方自己去猜。
4. **记账分桶**：自动激活落 `skill.invoke` + `mode=auto_invoke`，权重榜进
   `auto_use` 桶（use=1），**不许混进 explicit**（use=2，那是有人点名）。
5. **失败也是 200**：对 agent 来说「没找到」是正常结果，用 404 会逼它猜是路径错
   还是真没有；只有参数错才 400。
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
import skill_weights   # noqa: E402
import writeauth       # noqa: E402

from fastapi import FastAPI           # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 与 test_skill_store.py 同形的夹具 CLI：足够跑 search / install / reports。
#: **位置判据**：`--skip-self-upgrade` 必须在子命令之前（真实 argparse 只认这个位置）。
FAKE = """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" != "--skip-self-upgrade" ]; then
  echo "usage: --skip-self-upgrade must come first" >&2
  exit 9
fi
cmd="${2:-}"
case "$cmd" in
  search)
    cat <<'JSON'
{"query":"q","count":2,"results":[
 {"slug":"pdf-kit","publicSlug":"pdf-kit","name":"PDF Kit","description":"PDF 转 Markdown 工具","version":"1.0.0","source":"community","namespace":{"displayName":"tester"}},
 {"slug":"totally-unrelated","publicSlug":"totally-unrelated","name":"Cat Memes","description":"random cats","version":"1.0.0","source":"community","namespace":{"displayName":"tester"}}
]}
JSON
    ;;
  install)
    slug="${3:-}"
    dir=""; prev=""
    for a in "$@"; do
      if [ "$prev" = "--dir" ]; then dir="$a"; fi
      prev="$a"
    done
    if [ -z "$dir" ]; then echo "missing --dir" >&2; exit 8; fi
    mkdir -p "$dir/$slug"
    printf -- '---\\nname: %s\\ndescription: store body\\n---\\nSTORE-INSTRUCTIONS\\n' "$slug" > "$dir/$slug/SKILL.md"
    printf '{"success": true, "slug": "%s", "name": "%s", "version": "3.2.1", "targetDir": "%s/%s"}\\n' "$slug" "$slug" "$dir" "$slug"
    ;;
  skill)
    if [ "${3:-}" = "reports" ]; then
      echo '{"slug":"'"${4:-}"'","reports":{"keen":{"name":"科恩实验室","status":"benign","statusText":"安全，无风险"}}}'
      exit 0
    fi
    exit 7
    ;;
  *)
    echo "unknown command $cmd" >&2
    exit 7
    ;;
esac
"""


def _mk_skill(root: pathlib.Path, name: str, desc: str, body: str = "LOCAL-INSTRUCTIONS\n") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}", encoding="utf-8")


class _AutoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0auto-"))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        for p in (self.a, self.b):
            p.mkdir()
        self._saved_dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"src": str(self.a), "other": str(self.b)}
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        # 语料里放几条 filler，避免「两条语料 ⇒ IDF 退化成常数」把归一化分数做假。
        _mk_skill(self.b, "calendar", "日历与日程管理")
        _mk_skill(self.b, "email", "邮件撰写与发送")
        _mk_skill(self.b, "notes", "笔记整理")

        self._saved_cli = skill_store.CLI
        self.cli = self.tmp / "skillhub"
        self.cli.write_text(FAKE, encoding="utf-8")
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        skill_store.CLI = str(self.cli)
        self._saved_api = skill_store.API_SEARCH
        skill_store.API_SEARCH = ""          # hermetic：富字段搜索不联网，回落夹具 CLI
        self._saved_sec = dict(skill_store._sec_cache)
        skill_store._sec_cache.clear()
        self._saved_auto = (skill_store.AUTO_ENABLED, skill_store.AUTO_TARGET_ROUTE,
                            skill_store.AUTO_MAX_PER_HOUR)
        skill_store.AUTO_ENABLED = True
        skill_store.AUTO_TARGET_ROUTE = "src"

        db.init_db(self.tmp / "t.db")
        app = FastAPI()
        app.include_router(skill.router)
        app.middleware("http")(writeauth.write_gate)
        os.environ["TERM_TOKEN"] = "l0-auto-token"
        os.environ["SKILL_LOCAL_ROOTS_JSON"] = "[]"
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
        os.environ.pop("TERM_TOKEN", None)
        os.environ.pop("SKILL_LOCAL_ROOTS_JSON", None)
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _auto(self, task="把 PDF 转成 Markdown", **kw):
        body = {"task": task}
        body.update(kw)
        return self.client.post("/api/skill/auto", json=body,
                                headers={"x-hub-token": "l0-auto-token"})

    def _events(self, subject="skill.invoke"):
        return db.query("SELECT * FROM profile_events WHERE subject = ?", (subject,))


class TestAutoLocal(_AutoCase):
    def test_local_hit_activates_without_network(self):
        _mk_skill(self.a, "pdf-tools", "PDF 转 Markdown 工具")
        r = self._auto(caller="codex")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["ok"], b)
        self.assertTrue(b["activated"])
        self.assertEqual(b["source"], "local")
        self.assertEqual(b["name"], "pdf-tools")
        self.assertIn("LOCAL-INSTRUCTIONS", b["content"])
        self.assertEqual(b["caller"], "codex")
        self.assertEqual(b["recorded_subject"], "skill.invoke")
        # 字节数必须带出来：原先只 keep `bytes_total`，而那个字段**只在截断时才有**，
        # 未截断时恒 None ⇒ 调用方拿到正文却不知道多大（与 /invoke 的 `bytes` 不同名）。
        self.assertGreater(b["bytes"], 0, b)
        # 本地命中不装任何东西
        self.assertFalse((self.a / "pdf-kit").exists())

    def test_empty_task_is_400(self):
        r = self._auto(task="   ")
        self.assertEqual(r.status_code, 400, r.text)

    def test_local_only_does_not_search_store(self):
        """allow_store=false ⇒ 不联网。注意 allow_install=false 仍会搜，两者不同。"""
        r = self._auto(task="生成一份季度财报演示", allow_store=False)
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertFalse(b["ok"])
        self.assertEqual(b["source"], "none")
        self.assertIn("local_only", b["reason"])
        self.assertEqual(b["store"]["searched"], 0)
        self.assertFalse((self.a / "pdf-kit").exists())


class TestAutoStore(_AutoCase):
    def test_store_fallback_installs_and_returns_body(self):
        """本地没有 ⇒ 商店择优（pdf-kit 命中，Cat Memes 不该被选中）⇒ 装 ⇒ 回正文。"""
        r = self._auto(task="把 PDF 转成 Markdown", caller="pi")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["ok"], json.dumps(b, ensure_ascii=False)[:800])
        self.assertEqual(b["source"], "store")
        self.assertEqual(b["slug"], "pdf-kit")
        self.assertEqual(b["trust"], "community")
        self.assertIn("STORE-INSTRUCTIONS", b["content"])
        self.assertTrue((self.a / "pdf-kit" / "SKILL.md").is_file())
        self.assertFalse((self.a / "totally-unrelated").exists(),
                         "不相关的同类候选不许因为『搜到了』就被装")
        # 两道账都要有：安装账 + 使用账（后者让僵尸榜看得到它）
        self.assertEqual(len(self._events("skill.store_auto")), 1)
        self.assertEqual(len(self._events("skill.invoke")), 1)

    def test_no_match_reports_reason_and_installs_nothing(self):
        r = self._auto(task="xylophone quantum flux")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertFalse(b["ok"])
        self.assertEqual(b["source"], "none")
        self.assertTrue(b.get("reason"))
        self.assertGreaterEqual(b["store"]["searched"], 1)
        self.assertFalse((self.a / "pdf-kit").exists(), "没通过门槛就不许落盘")

    def test_install_disabled_reports_plan_not_fake_success(self):
        """allow_store=false 是「本地 only」；要「只报告将会装什么」用 store/acquire 的
        allow_install=false。本端点不提供「装了但不回正文」的半态。"""
        r = self._auto(task="把 PDF 转成 Markdown", allow_store=False)
        b = r.json()
        self.assertFalse(b["ok"])
        self.assertNotIn("would_install", b)


class TestAutoLedger(_AutoCase):
    def test_auto_invoke_is_not_counted_as_explicit(self):
        _mk_skill(self.a, "pdf-tools", "PDF 转 Markdown 工具")
        self.assertTrue(self._auto(caller="codex").json()["ok"])
        rows = db.query("SELECT subject, detail FROM profile_events")
        det = json.loads(rows[0]["detail"])
        self.assertEqual(det["mode"], "auto_invoke")
        rep = skill_weights.build(rows)
        row = next(x for x in rep["items"] if x["name"] == "pdf-tools")
        self.assertEqual((row["explicit"], row["auto_use"], row["use"]),
                         (0, 1, 1), row)
        self.assertEqual(row["by_caller"]["auto_use"], {"codex": 1})
        self.assertEqual(row["evidence_total"]["auto_use"], 1)

    def test_explicit_invoke_still_weight_two(self):
        """分桶不能反向污染：手工点名仍是 explicit=2，不是 auto_use。"""
        _mk_skill(self.a, "pdf-tools", "PDF 转 Markdown 工具")
        r = self.client.post("/api/skill/invoke",
                             json={"name": "pdf-tools", "caller": "web"},
                             headers={"x-hub-token": "l0-auto-token"})
        self.assertEqual(r.status_code, 200, r.text)
        rep = skill_weights.build(db.query("SELECT subject, detail FROM profile_events"))
        row = next(x for x in rep["items"] if x["name"] == "pdf-tools")
        self.assertEqual((row["explicit"], row["auto_use"], row["use"]), (1, 0, 2))


class TestMcpTool(unittest.TestCase):
    def test_tool_forwards_to_auto_endpoint(self):
        import hubmcp
        seen = {}

        def fake_post(path, body, **params):
            seen.update({"path": path, "body": body})
            return {"ok": True, "name": "demo"}

        saved = hubmcp._post
        hubmcp._post = fake_post
        try:
            out = json.loads(hubmcp.hub_skill_auto(task="把 PDF 转成 Markdown",
                                                  caller="codex", allow_store=False))
        finally:
            hubmcp._post = saved
        self.assertEqual(seen["path"], "/api/skill/auto")
        self.assertEqual(seen["body"]["task"], "把 PDF 转成 Markdown")
        self.assertEqual(seen["body"]["caller"], "codex")
        self.assertFalse(seen["body"]["allow_store"])
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
