#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：技能清单挂 magpie 托管关系（波3 S3，2026-11-27）。

【先弄清事实再改代码】实测：magpie `library.json` 里 68 条技能的 `source.dir`
全部指向 `/fs/1000/ftp/技术文档/skills/<name>`，与 hub 扫到的是**同一批目录**
（68/68 命中）。⇒ 不是两份数据，**不需要写代理**；需要的是把「这条被哪些 agent
启用」这个只有 magpie 知道的事实暴露出来。

判据：
1. join 必须按**真实目录**，不能按名字（实测 3 条不同名同目录，按名字会漏）；
2. 读不到 library.json ⇒ 返回原因，不把「全部未托管」当正常；
3. 任何异常都不许让技能页 500。
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import magpie_client as mc  # noqa: E402
import skill                # noqa: E402


class TestLibraryMap(unittest.TestCase):
    def _lib(self, skills):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"skills": skills, "mcp": [], "instructions": {}}, f)
        f.close()
        return f.name

    def test_keyed_by_real_dir(self):
        path = self._lib([
            {"name": "a", "agents": ["pi"], "from": "x",
             "source": {"kind": "folder", "dir": "/tmp/libtest/a"}},
            {"name": "b", "agents": [], "from": "", "source": {}},
        ])
        old = os.environ.get("MAGPIE_LIBRARY")
        os.environ["MAGPIE_LIBRARY"] = path
        mc._lib_cache = (0.0, {}, "")   # 2026-11-28：读之前清缓存。
        # 旧实现只在 finally 里清 ⇒ 全量跑时若前面有用例刚读过真库（TTL 内），
        # 这里拿到的是**真库**路径列表，断言失败且报错指向「映射不对」而非缓存。
        try:
            m, why = mc.library_skills_map()
        finally:
            mc._lib_cache = (0.0, {}, "")
            if old is None:
                os.environ.pop("MAGPIE_LIBRARY", None)
            else:
                os.environ["MAGPIE_LIBRARY"] = old
            os.unlink(path)
        self.assertEqual(why, "")
        self.assertEqual(list(m), [os.path.realpath("/tmp/libtest/a")])
        self.assertEqual(m[os.path.realpath("/tmp/libtest/a")]["agents"], ["pi"])

    def test_missing_library_reports_reason(self):
        old = os.environ.get("MAGPIE_LIBRARY")
        os.environ["MAGPIE_LIBRARY"] = "/nonexistent/library.json"
        try:
            m, why = mc.library_skills_map()
        finally:
            mc._lib_cache = (0.0, {}, "")
            if old is None:
                os.environ.pop("MAGPIE_LIBRARY", None)
            else:
                os.environ["MAGPIE_LIBRARY"] = old
        self.assertEqual(m, {})
        self.assertIn("不存在", why)


class TestAttach(unittest.TestCase):
    def test_matches_by_real_dir_not_name(self):
        """同名不同名都要能贴上：hub 叫 agent-browser，magpie 叫 agent-browser-cli。"""
        d = "/fs/1000/ftp/技术文档/skills/agent-browser-cli"
        items = [{"name": "agent-browser", "realpath": d + "/SKILL.md"},
                 {"name": "other", "realpath": "/tmp/nope/SKILL.md"}]
        with mock.patch.object(mc, "library_skills_map",
                               lambda: ({os.path.realpath(d): {
                                   "name": "agent-browser-cli", "agents": ["pi"],
                                   "from": "x", "kind": "folder"}}, "")):
            out = skill._attach_magpie_managed(items)
        self.assertEqual(out["matched"], 1)
        self.assertEqual(items[0]["magpie"]["name"], "agent-browser-cli")
        self.assertIsNone(items[1]["magpie"])

    def test_library_missing_sets_error_not_silent(self):
        with mock.patch.object(mc, "library_skills_map", lambda: ({}, "读不到")):
            out = skill._attach_magpie_managed([{"name": "x", "realpath": "/tmp/x/SKILL.md"}])
        self.assertEqual(out["matched"], 0)
        self.assertIn("读不到", out["error"])

    def test_exception_does_not_raise(self):
        def boom():
            raise RuntimeError("kb down")
        with mock.patch.object(mc, "library_skills_map", boom):
            out = skill._attach_magpie_managed([])
        self.assertIn("kb down", out["error"])


if __name__ == "__main__":
    unittest.main()
