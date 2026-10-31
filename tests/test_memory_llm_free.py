#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：非会话工具调用的模型统一 `agnes/agnes-2.5-flash`（2026-10-29 用户裁定）。

口径变更史：
- 2026-10-18 裁定「非会话一律 `openrouter/free`」，理由是省钱、路由器会换掉被摘的成员。
- 2026-10-29 用户裁定「全部改为 agnes-2.5-flash，取消 deepseek，取消免费模型」。
  实测依据：免费池会把请求路由到审核/分类模型（HTTP 200 但正文是 `User Safety: safe`），
  且 deepseek 账户 402 时 CCR 按 `Router.fallback.model-chain` 静默回退到 `openrouter/free`
  ⇒ 判定链实际跑在**随机**免费模型上，结果不可复现。免费不再是「廉价可选项」，
  而是「不确定结果」的来源。

仍然成立的三条闸门：
1. 默认值必须读函数而非常量：`.env` 覆盖发生在模块导入之后，常量会把旧值钉死；
2. L2 压缩不得回落到交互式模型（`MANAGER_LLM_MODEL` 会被改指别的厂商/档位）；
3. 🚨 全场景禁 `openrouter/auto`：它自动挑最便宜的可用模型，最便宜可能是付费档。
"""
import os
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import llm  # noqa: E402

#: 2026-10-29 起的唯一默认档（经 CCR 需 provider 前缀）。
_AGNES = "agnes/agnes-2.5-flash"


def _func_body(path: pathlib.Path, name: str) -> str:
    """取源码里某个函数/路由处理器的函数体（到下一条顶层装饰器或 EOF）。"""
    src = path.read_text(encoding="utf-8")
    m = re.search(r"(?:async )?def %s\(.*?(?=\n@router\.|\nclass |\Z)" % re.escape(name), src, re.S)
    if not m:
        raise AssertionError(f"{path.name} 里找不到 {name}")
    return m.group(0)


class TestMemoryModelDefault(unittest.TestCase):
    def test_default_is_agnes(self):
        """默认值必须是 agnes-2.5-flash（经 CCR 的带前缀形态）。"""
        old = os.environ.pop("MEMORY_LLM_MODEL", None)
        try:
            self.assertEqual(llm.memory_model(), _AGNES)
        finally:
            if old is not None:
                os.environ["MEMORY_LLM_MODEL"] = old

    def test_env_override_is_honoured_at_call_time(self):
        """读函数而非常量：改 env 立即生效（`.env` 覆盖发生在模块导入之后）。"""
        old = os.environ.get("MEMORY_LLM_MODEL")
        try:
            os.environ["MEMORY_LLM_MODEL"] = "vendor/some-other-model"
            self.assertEqual(llm.memory_model(), "vendor/some-other-model")
        finally:
            if old is None:
                os.environ.pop("MEMORY_LLM_MODEL", None)
            else:
                os.environ["MEMORY_LLM_MODEL"] = old

    def test_does_not_silently_fall_back_to_manager_model(self):
        """未配置时不得跟着 `MANAGER_LLM_MODEL` 走（那是另一个可被改指的入口）。"""
        old_mem = os.environ.pop("MEMORY_LLM_MODEL", None)
        old_mgr = os.environ.get("MANAGER_LLM_MODEL")
        try:
            os.environ["MANAGER_LLM_MODEL"] = "alibaba/qwen3.8-flash"
            self.assertNotIn("qwen", llm.memory_model())
            self.assertEqual(llm.memory_model(), _AGNES)
        finally:
            if old_mem is not None:
                os.environ["MEMORY_LLM_MODEL"] = old_mem
            if old_mgr is None:
                os.environ.pop("MANAGER_LLM_MODEL", None)
            else:
                os.environ["MANAGER_LLM_MODEL"] = old_mgr

    def test_fallback_default_is_agnes(self):
        """兜底位同样不得落回免费池：实测免费路由器会返审核标签。"""
        old = os.environ.pop("MEMORY_LLM_FALLBACK_MODEL", None)
        try:
            self.assertEqual(llm.free_fallback_model(), _AGNES)
        finally:
            if old is not None:
                os.environ["MEMORY_LLM_FALLBACK_MODEL"] = old


class TestCallSitesWiring(unittest.TestCase):
    def test_l2_rebuild_passes_memory_model(self):
        """L2 压缩是**非会话**提炼：必须走 complete_free（记忆档+兜底），
        不得裸调 chat_tools_loop（那会回落到交互式模型）。"""
        body = _func_body(_REPO / "src" / "memory.py", "rebuild_l2")
        self.assertIn("llm.complete_free(", body)
        self.assertNotIn("chat_tools_loop(", body)
        # 空正文也要能降级：机械拼接分支必须保留
        self.assertIn("机械压缩", body)

    def test_decompose_keeps_manager_model(self):
        """交互式拆解（人点一下才发）不跟着走记忆档：不得改成 complete_free/memory_model。"""
        # 片3 文件级拆分：decompose 在 tasks_dag
        body = _func_body(_REPO / "src" / "tasks_dag.py", "decompose")
        self.assertNotIn("llm.memory_model()", body)
        self.assertNotIn("llm.complete_free(", body)


class TestCompleteFree(unittest.TestCase):
    """「200 但没货」必须换兜底，全失败返回空串（由调用方降级）。

    用 env 显式造出 primary≠fallback：默认值两者同为 agnes，走的是**去重后只调一次**，
    那是另一条测试（`test_same_primary_and_fallback_calls_once`）。
    """

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("MEMORY_LLM_MODEL", "MEMORY_LLM_FALLBACK_MODEL")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _call(self, mapping):
        calls = []

        async def fake(messages, tools, max_rounds, model):
            calls.append(model)
            r = mapping.get(model, "")
            if isinstance(r, Exception):
                raise r
            return r, []

        return fake, calls

    def test_empty_primary_uses_fallback(self):
        import asyncio
        os.environ["MEMORY_LLM_MODEL"] = "vendor/primary"
        os.environ["MEMORY_LLM_FALLBACK_MODEL"] = "vendor/backup"
        fake, calls = self._call({"vendor/primary": "",
                                  "vendor/backup": "这是一段正常的中文压缩结果" * 3})
        text, used = asyncio.run(llm.complete_free("sys", "user", call=fake))
        self.assertEqual(used, "vendor/backup")
        self.assertIn("正常的中文压缩结果", text)
        self.assertEqual(calls, ["vendor/primary", "vendor/backup"])

    def test_safety_label_counts_as_failure(self):
        import asyncio
        os.environ["MEMORY_LLM_MODEL"] = "vendor/primary"
        os.environ["MEMORY_LLM_FALLBACK_MODEL"] = "vendor/backup"
        fake, _ = self._call({"vendor/primary": "User Safety: safe",
                              "vendor/backup": "正常内容" * 8})
        text, used = asyncio.run(llm.complete_free("sys", "user", call=fake))
        self.assertEqual(used, "vendor/backup")
        self.assertTrue(text)

    def test_all_fail_returns_empty_not_third_model(self):
        import asyncio
        os.environ["MEMORY_LLM_MODEL"] = "vendor/primary"
        os.environ["MEMORY_LLM_FALLBACK_MODEL"] = "vendor/backup"
        fake, calls = self._call({"vendor/primary": RuntimeError("429"),
                                  "vendor/backup": "短"})
        text, used = asyncio.run(llm.complete_free("sys", "user", call=fake))
        self.assertEqual((text, used), ("", ""))
        self.assertEqual(calls, ["vendor/primary", "vendor/backup"])   # 不得再试第三个模型

    def test_same_primary_and_fallback_calls_once(self):
        """默认值时 primary == fallback：去重后只发一次，不重复烧一次长尾。"""
        import asyncio
        os.environ.pop("MEMORY_LLM_MODEL", None)
        os.environ.pop("MEMORY_LLM_FALLBACK_MODEL", None)
        fake, calls = self._call({_AGNES: "正常的中文压缩结果" * 3})
        text, used = asyncio.run(llm.complete_free("sys", "user", call=fake))
        self.assertEqual(used, _AGNES)
        self.assertTrue(text)
        self.assertEqual(calls, [_AGNES])


class TestNoAutoRouter(unittest.TestCase):
    def test_no_openrouter_auto_in_call_sites(self):
        """`openrouter/auto` 是本机唯一会直接扣款的模型 ID 写法，任何脚本/配置禁出现。"""
        hits = []
        for p in list((_REPO / "src").glob("*.py")) + [
                _REPO.parent / "scripts" / "distill-knowledge.py",
                _REPO.parent / "scripts" / "eval-q.py",
                _REPO.parent / "scripts" / "build-v3.py"]:
            if not p.exists() or p.name.startswith("."):
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if "openrouter/auto" not in line:
                    continue
                # 说明「禁用」的文案（注释/文档串/🚫）不算调用点；
                # 只抓被引号包住或赋给模型变量的字面量。
                if "禁" in line or "🚫" in line:
                    continue
                if re.search(r"[\"']openrouter/auto", line) or re.search(r"=\s*openrouter/auto", line):
                    hits.append(f"{p.name}:{i}: {line.strip()[:120]}")
        self.assertEqual(hits, [], "出现 openrouter/auto（会扣费）：\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
