#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：判定缓存（`judge_cache`）—— 「同样问题不重复消耗」的落盘层。

## 为什么需要（改造前实测）

| 位置 | 缓存 | 落盘 |
|---|---|---|
| `jev_client`（外部端点已下线，2026-10-23 删除） | 内存 600s，键=query+候选名 | **否**（重启即丢） |
| `vitals` | 按证据 sha 复用 | 是（`data/vitals.json` 270 条） |
| `skill_rerank` | **无** | — |

免费池 p50 3.2~3.7s、成功率 34~44% ⇒ 同一个问题重问一次既慢又可能失败。
本层把「3 秒且有 56~66% 概率失败」换成「0ms 必然成功」。

## 四条判据（都是踩过的坑）

1. **键里必须带候选集指纹**：只拿问题做键，改一条技能描述后旧判定继续生效
   —— 「世界变了还返回旧答案」。
2. **失败绝不缓存**：缓存超时/审核标签等于把瞬时故障固化成 7 天的「事实」。
3. **落盘损坏当空缓存**，不许打挂业务。
4. **命中率必须可观测**：否则「有缓存」与「缓存从没命中」在外部看起来一样。
"""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import judge_cache  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="jc-")
        self._env = os.environ.get("JUDGE_CACHE_PATH")
        os.environ["JUDGE_CACHE_PATH"] = os.path.join(self._tmp, "jc.json")
        judge_cache.clear()
        judge_cache.reset_stats()

    def tearDown(self):
        if self._env is None:
            os.environ.pop("JUDGE_CACHE_PATH", None)
        else:
            os.environ["JUDGE_CACHE_PATH"] = self._env


class TestKeyAndFingerprint(_Base):
    ITEMS = [{"name": "a", "description": "da", "triggers": "t1"},
             {"name": "b", "description": "db", "triggers": "t2"}]

    def test_fingerprint_order_insensitive(self):
        """候选顺序不该影响指纹，否则排列一变就全失效。"""
        f1 = judge_cache.fingerprint(self.ITEMS)
        f2 = judge_cache.fingerprint(list(reversed(self.ITEMS)))
        self.assertEqual(f1, f2)

    def test_fingerprint_changes_with_description(self):
        changed = [dict(x) for x in self.ITEMS]
        changed[0]["description"] = "改过"
        self.assertNotEqual(judge_cache.fingerprint(self.ITEMS),
                            judge_cache.fingerprint(changed),
                            "描述变了指纹没变 —— 旧判定会套到新候选上")

    def test_fingerprint_changes_with_triggers(self):
        changed = [dict(x) for x in self.ITEMS]
        changed[1]["triggers"] = "改过"
        self.assertNotEqual(judge_cache.fingerprint(self.ITEMS),
                            judge_cache.fingerprint(changed))

    def test_key_separates_kind_and_model(self):
        fp = judge_cache.fingerprint(self.ITEMS)
        a = judge_cache.key("skill_rerank", "q", fp, "m1")
        b = judge_cache.key("skill_rerank", "q", fp, "m2")
        c = judge_cache.key("local_judge", "q", fp, "m1")
        self.assertEqual(len({a, b, c}), 3, "类型/模型没进键 ⇒ 不同判官的结果互相污染")


class TestPutGet(_Base):
    def test_roundtrip_persists_to_disk(self):
        judge_cache.put("k1", {"ok": True, "name": "a"}, 3600)
        # 新进程视角：清内存，只留磁盘
        judge_cache._MEM.clear()
        judge_cache._LOADED.update(at=0.0, path="")
        hit = judge_cache.get("k1")
        self.assertIsNotNone(hit, "落盘后新实例读不到 —— 重启即丢")
        self.assertEqual(hit["name"], "a")
        self.assertEqual(hit["_cache_hits"], 1)

    def test_ttl_expiry(self):
        judge_cache.put("k", {"ok": True}, 0.01)
        time.sleep(0.05)
        self.assertIsNone(judge_cache.get("k"), "过期的条目仍被返回")

    def test_hits_are_counted(self):
        judge_cache.put("k", {"ok": True}, 3600)
        judge_cache.get("k"); judge_cache.get("k")
        st = judge_cache.stats()
        self.assertEqual(st["hits"], 2)
        self.assertEqual(st["misses"], 0)
        self.assertEqual(st["hit_rate"], 1.0)

    def test_miss_counted(self):
        judge_cache.get("nope")
        self.assertEqual(judge_cache.stats()["misses"], 1)

    def test_lru_cap(self):
        old = judge_cache.MAX_ENTRIES
        try:
            judge_cache.MAX_ENTRIES = 3
            for i in range(6):
                judge_cache.put("k%d" % i, {"ok": True}, 3600)
            self.assertLessEqual(judge_cache.stats()["entries"], 3)
            self.assertGreaterEqual(judge_cache.stats()["evicted"], 3)
        finally:
            judge_cache.MAX_ENTRIES = old

    def test_corrupt_file_is_empty_cache_not_crash(self):
        p = pathlib.Path(os.environ["JUDGE_CACHE_PATH"])
        p.write_text("{not json", encoding="utf-8")
        judge_cache._MEM.clear(); judge_cache._LOADED.update(at=0.0, path="")
        self.assertIsNone(judge_cache.get("k"))     # 不抛
        self.assertEqual(judge_cache.stats()["corrupt_loads"], 1)

    def test_path_switch_does_not_leak_entries(self):
        """换路径必须清内存 —— 实测这条没做时两个用例互相污染（假红/假绿）。"""
        judge_cache.put("k", {"ok": True}, 3600)
        other = os.path.join(self._tmp, "other.json")
        os.environ["JUDGE_CACHE_PATH"] = other
        self.assertIsNone(judge_cache.get("k"), "换了缓存文件却还能读到旧条目")


class TestStatsShape(_Base):
    def test_fields_for_health(self):
        st = judge_cache.stats()
        for k in ("entries", "max_entries", "hits", "misses", "stores",
                  "evicted", "corrupt_loads", "hit_rate", "path"):
            self.assertIn(k, st)


if __name__ == "__main__":
    unittest.main()
