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


if __name__ == "__main__":
    unittest.main(verbosity=2)
