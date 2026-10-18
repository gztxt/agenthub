#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：非会话工具调用的模型必须走免费档（2026-10-18 用户裁定）。

口径（AGENTS.md §1 第 4 条）：
- **会话**默认值用稳定模型；`:free` 只进可切换清单。
- **非会话**（L2 记忆压缩、后台提炼、定时任务）一律默认 `openrouter/free`；
  经 CCR（:3456）写 `openrouter/openrouter/free`，直连 OpenRouter 才写 `openrouter/free`。
- 🚨 全场景禁 `openrouter/auto`：它会自动挑最便宜模型，最便宜可能是付费档。

为什么这组闸门必须存在（都是可确定性命中的红向）：
1. 把 MEMORY_LLM_MODEL 写成模块常量 ⇒ `.env` 在 config 导入后覆盖，常量被钉死；
2. L2 压缩忘记传 model ⇒ 静默回落到交互式付费模型 `MANAGER_LLM_MODEL`（账户扣款风险）；
3. 有人为了「效果更好」把默认改回 `alibaba/qwen3.8-flash` ⇒ 无测试就会静默复发。
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

_FREE_CCR = "openrouter/openrouter/free"
_FREE_DIRECT = "openrouter/free"


def _func_body(path: pathlib.Path, name: str) -> str:
    """取源码里某个函数/路由处理器的函数体（到下一条顶层装饰器或 EOF）。"""
    src = path.read_text(encoding="utf-8")
    m = re.search(r"(?:async )?def %s\(.*?(?=\n@router\.|\nclass |\Z)" % re.escape(name), src, re.S)
    if not m:
        raise AssertionError(f"{path.name} 里找不到 {name}")
    return m.group(0)


class TestMemoryModelDefault(unittest.TestCase):
    def test_default_is_ccr_free_router(self):
        """默认值必须是经 CCR 的免费自动路由（带 provider 前缀）。"""
        old = os.environ.pop("MEMORY_LLM_MODEL", None)
        try:
            self.assertEqual(llm.memory_model(), _FREE_CCR)
        finally:
            if old is not None:
                os.environ["MEMORY_LLM_MODEL"] = old

    def test_env_override_is_honoured_at_call_time(self):
        """读函数而非常量：改 env 立即生效（`.env` 覆盖发生在模块导入之后）。"""
        old = os.environ.get("MEMORY_LLM_MODEL")
        try:
            os.environ["MEMORY_LLM_MODEL"] = _FREE_DIRECT
            self.assertEqual(llm.memory_model(), _FREE_DIRECT)
        finally:
            if old is None:
                os.environ.pop("MEMORY_LLM_MODEL", None)
            else:
                os.environ["MEMORY_LLM_MODEL"] = old

    def test_does_not_silently_fall_back_to_paid(self):
        """未配置时不得回落到 MANAGER_LLM_MODEL（那可能是付费档）。"""
        old_mem = os.environ.pop("MEMORY_LLM_MODEL", None)
        old_mgr = os.environ.get("MANAGER_LLM_MODEL")
        try:
            os.environ["MANAGER_LLM_MODEL"] = "alibaba/qwen3.8-flash"
            self.assertNotIn("qwen", llm.memory_model())
            self.assertEqual(llm.memory_model(), _FREE_CCR)
        finally:
            if old_mem is not None:
                os.environ["MEMORY_LLM_MODEL"] = old_mem
            if old_mgr is None:
                os.environ.pop("MANAGER_LLM_MODEL", None)
            else:
                os.environ["MANAGER_LLM_MODEL"] = old_mgr


class TestCallSitesWiring(unittest.TestCase):
    def test_l2_rebuild_passes_memory_model(self):
        """L2 压缩是**非会话**提炼：必须走 complete_free（免费档+兜底），
        不得裸调 chat_tools_loop（那会回落到交互式付费模型）。"""
        body = _func_body(_REPO / "src" / "memory.py", "rebuild_l2")
        self.assertIn("llm.complete_free(", body)
        self.assertNotIn("chat_tools_loop(", body)
        # 空正文也要能降级：机械拼接分支必须保留
        self.assertIn("机械压缩", body)

    def test_decompose_keeps_manager_model(self):
        """交互式拆解（人点一下才发）不跟着降级：不得改成 complete_free/memory_model。"""
        body = _func_body(_REPO / "src" / "tasks.py", "decompose")
        self.assertNotIn("llm.memory_model()", body)
        self.assertNotIn("llm.complete_free(", body)


class TestCompleteFree(unittest.TestCase):
    """免费池「200 但没货」必须换兜底，全失败返回空串（由调用方降级）。"""

    def _call(self, mapping):
        calls = []

        async def fake(messages, tools, max_rounds, model):
            calls.append(model)
            r = mapping.get(model, "")
            if isinstance(r, Exception):
                raise r
            return r, []

        return fake, calls

    def test_empty_primary_uses_specific_free_fallback(self):
        import asyncio
        primary, fallback = llm.memory_model(), llm.free_fallback_model()
        fake, calls = self._call({primary: "", fallback: "这是一段正常的中文压缩结果" * 3})
        text, used = asyncio.run(llm.complete_free("sys", "user", call=fake))
        self.assertEqual(used, fallback)
        self.assertIn("正常的中文压缩结果", text)
        self.assertEqual(calls, [primary, fallback])
        self.assertTrue(fallback.endswith(":free"))     # 兜底位必须是具体免费档

    def test_safety_label_counts_as_failure(self):
        import asyncio
        primary, fallback = llm.memory_model(), llm.free_fallback_model()
        fake, _ = self._call({primary: "User Safety: safe", fallback: "正常内容" * 8})
        text, used = asyncio.run(llm.complete_free("sys", "user", call=fake))
        self.assertEqual(used, fallback)
        self.assertTrue(text)

    def test_all_fail_returns_empty_not_paid_model(self):
        import asyncio
        primary, fallback = llm.memory_model(), llm.free_fallback_model()
        fake, calls = self._call({primary: RuntimeError("429"), fallback: "短"})
        text, used = asyncio.run(llm.complete_free("sys", "user", call=fake))
        self.assertEqual((text, used), ("", ""))
        self.assertEqual(calls, [primary, fallback])   # 不得再试第三个（付费）模型


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
