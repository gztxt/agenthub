"""模型清单缓存闸门（只缓存成功路径）。

【病根】`/api/models` 原先每次新建 `aiohttp.ClientSession` 打上游，
`ClientTimeout(total=8)` 且**无缓存** ⇒ CCR 一挂，模型下拉**每次卡 8 秒**。
模型清单是低频人工变更的数据 ⇒ 短 TTL 足够。

【★ 本文件最要紧的一条：失败不许进缓存】
`test_failure_is_not_cached` / `test_failure_does_not_poison_existing` 钉这个。
把一次上游抖动缓存成「没有模型」并持续 TTL，是把瞬时故障固化成稳定错误 ——
正是本仓反复消灭的「宣告能力 ≠ 实际能力」静默形态
（`tests/verify_memory_federation.py:3-19` 有完整案例记录）。

【为什么抽成 `src/models_cache.py`】
L0 禁 `import src.main`（一 import 跑 lifespan 开真库绑端口）。
本模块只吃一个「可 await 的 fetch」+ 一个「单调时钟」，不 import fastapi / aiohttp，
所以 L0 能用假 fetch 断言「上游被打了几次」—— 这个判据端到端验不出来
（真 CCR 一直在跑，压根不会失败）。
"""
import asyncio
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
from src import models_cache  # noqa: E402

MODELS_PY = _REPO / "src" / "api_models.py"   # C2：/api/models 与 _shape_models 已从 main.py 迁到这里


class TTLCacheBasics(unittest.TestCase):
    def test_miss_then_hit(self):
        c = models_cache.TTLCache(ttl_s=60)
        self.assertEqual(c.get(0.0), (False, None))
        c.put({"a": 1}, 0.0)
        hit, val = c.get(1.0)
        self.assertTrue(hit)
        self.assertEqual(val, {"a": 1})

    def test_expiry(self):
        c = models_cache.TTLCache(ttl_s=10)
        c.put({"a": 1}, 100.0)
        self.assertTrue(c.get(109.9)[0], "TTL 内应命中")
        self.assertFalse(c.get(110.1)[0], "TTL 外应失效")

    def test_boundary_is_half_open(self):
        """判据是 `now - ts < ttl`（半开区间）⇒ 恰好等于 TTL 时**不**命中。

        第一版把这条写成「恰好等于 TTL 仍算命中」，是**测试期望错了**：
        全仓另外三个缓存（skill._STATUS_CACHE / vitals / gwprobe）用的都是同
        一个 `<` 判据，写成 `<=` 反而会让本仓出现两种边界语义。
        这里钉住与兄弟缓存一致的那一种。
        """
        c = models_cache.TTLCache(ttl_s=10)
        c.put({"a": 1}, 0.0)
        self.assertTrue(c.get(9.99)[0], "TTL 内命中")
        self.assertFalse(c.get(10.0)[0], "恰好等于 TTL 不命中（半开区间，与兄弟缓存一致）")

    def test_clear(self):
        c = models_cache.TTLCache(ttl_s=60)
        c.put({"a": 1}, 0.0)
        c.clear()
        self.assertFalse(c.get(0.0)[0])

    def test_default_ttl_is_reasonable(self):
        """60s：模型清单人工变更。刻意不照搬 /status 的 600s（那是索引，变更更稀）。"""
        self.assertEqual(models_cache.DEFAULT_TTL_S, 60.0)
        self.assertLessEqual(models_cache.DEFAULT_TTL_S, 300,
                             "缓存不该长到让用户改了上游模型后半小时看不到")


class CachedModelsBehaviour(unittest.TestCase):
    def setUp(self):
        self.cache = models_cache.TTLCache(ttl_s=60)
        self.now = [0.0]
        self.calls = []

    def _fetch_ok(self):
        async def fetch():
            self.calls.append(1)
            return {"models": [{"id": "a/b"}], "count": 1}
        return fetch

    def _fetch_boom(self):
        async def fetch():
            self.calls.append(1)
            raise RuntimeError("上游 8s 超时")
        return fetch

    def run_(self, fetch, **kw):
        clock = lambda: self.now[0]      # noqa: E731
        return asyncio.run(models_cache.cached_models(self.cache, fetch,
                                                      clock=clock, **kw))

    def test_two_calls_hit_upstream_once(self):
        payload1, cached1 = self.run_(self._fetch_ok())
        payload2, cached2 = self.run_(self._fetch_ok())
        self.assertEqual(len(self.calls), 1, "两次调用应只打上游 1 次")
        self.assertFalse(cached1)
        self.assertTrue(cached2, "第二次必须标 cached=True")

    def test_cached_flag_in_payload(self):
        """⚠ 必须能看出「这次没打上游」—— 默默复用会让运维无法判断缓存是否生效。"""
        self.run_(self._fetch_ok())
        payload2, _ = self.run_(self._fetch_ok())
        self.assertTrue(payload2.get("cached"))

    def test_expiry_refetches(self):
        self.run_(self._fetch_ok())
        self.now[0] = 61.0
        _, cached = self.run_(self._fetch_ok())
        self.assertFalse(cached, "TTL 过期后应重取")
        self.assertEqual(len(self.calls), 2)

    def test_force_bypasses_cache(self):
        self.run_(self._fetch_ok())
        _, cached = self.run_(self._fetch_ok(), force=True)
        self.assertFalse(cached, "force=True 必须绕缓存")
        self.assertEqual(len(self.calls), 2, "force 应真打上游")

    def test_force_also_refreshes_entry(self):
        """force 拉到的应当**成为新的缓存值** —— 否则 force 只是个绕过，不更新。"""
        self.run_(self._fetch_ok())
        self.run_(self._fetch_ok(), force=True)
        _, cached = self.run_(self._fetch_ok())
        self.assertTrue(cached, "force 之后应有新鲜的缓存值")
        self.assertEqual(len(self.calls), 2, "第三次应命中 force 拉到的值")

    # ── ★ 失败不进缓存 ────────────────────────────────────────
    def test_failure_is_not_cached(self):
        """★ 把一次上游抖动缓存成「没有模型」= 把瞬时故障固化成稳定错误。"""
        with self.assertRaises(RuntimeError):
            self.run_(self._fetch_boom())
        self.assertFalse(self.cache.get(self.now[0])[0],
                         "失败结果**不许**进缓存")

    def test_failure_does_not_poison_existing_value(self):
        """⚠ 已有好值时，一次失败**不能**把它顶掉。

        这条比上一条更隐蔽：如果失败时写了空清单，用户会在上游恢复前一直看到
        「没有模型」，而原本的缓存里其实有一份可用的。

        ⚠ 断言写法（第一版写错过）：不能靠 `cache.get(t)` 拿旧值 ——
        TTL 已过期时它返回 `(False, None)`，值根本没被保留 ⇒ 断言必然拿到 None，
        一度误判成「失败覆盖了旧值」。改为直接比对内部 `_entry` 的值。
        """
        self.run_(self._fetch_ok())              # 先有 good
        self.now[0] = 61.0                       # TTL 过期，逼它真打
        with self.assertRaises(RuntimeError):
            self.run_(self._fetch_boom())
        # 内部 entry 仍是原来那份 good（只是时间戳旧），没有被空值/失败覆盖
        self.assertIsNotNone(self.cache._entry)
        stored = self.cache._entry[1]
        self.assertEqual(stored, {"models": [{"id": "a/b"}], "count": 1},
                         "失败不该覆盖已有缓存值")

    def test_recovery_after_failure(self):
        """失败一次之后，下一次调用必须能成功（缓存没被毒化）。"""
        self.now[0] = 100.0
        with self.assertRaises(RuntimeError):
            self.run_(self._fetch_boom())
        self.now[0] = 200.0
        payload, cached = self.run_(self._fetch_ok())
        self.assertFalse(cached)
        self.assertEqual(payload["count"], 1)

    # ── ★★ 实测踩到的那个坑：error 载荷不抛异常 ──────────────────
    def test_error_payload_is_not_cached(self):
        """★ 本文件最重要的一条，由 2026-10-05 影子实测逼出来。

        `api_models.py` 的 `_fetch` 里 `async with s.get(...)` **在连接失败时不抛异常** ——
        它只是没拿到 200，于是 `out` 保持空、`_shape_models` 返回
        `{"models": [], "error": "Cannot connect to host …"}`。
        若 `cached_models` 只判「有没有抛异常」，就会把这个**失败**缓存 60s
        （实测：影子指死端口 59999，第 2~4 次 0.003s 返回「没有模型」——
        快是快了，缓存的是失败）。

        ⇒ 判据必须是 `payload["error"]` 为空，而不是「fetch 没抛异常」。
        这条断言就是把那个实测坑钉住：它红过一次，改了 `cached_models` 才绿。
        """
        calls = []

        async def fetch_returns_error_payload():
            calls.append(1)
            # 形状与 api_models.py 的 _fetch 在连接失败时**完全一致**（不抛异常）
            return {"models": [], "groups": {}, "count": 0,
                    "source": "claude",
                    "error": "Cannot connect to host 127.0.0.1:59999"}

        payload, cached = self.run_(fetch_returns_error_payload)
        self.assertFalse(cached)
        payload2, cached2 = self.run_(fetch_returns_error_payload)
        self.assertFalse(cached2, "error 载荷**不许**被当成缓存命中")
        self.assertEqual(len(calls), 2,
                         "每次都必须真打上游（错误载荷不进缓存 ⇒ 调用次数应为 2）")
        self.assertIn("error", payload2)

    def test_error_payload_does_not_poison_good_cache(self):
        """已有好值时，一次 error 载荷不能把它顶掉。"""
        self.run_(self._fetch_ok())

        async def fetch_error():
            return {"models": [], "groups": {}, "count": 0,
                    "error": "Cannot connect to host …"}

        self.now[0] = 61.0                        # TTL 过期，逼它真打
        self.run_(fetch_error)
        self.assertIsNotNone(self.cache._entry)
        self.assertEqual(self.cache._entry[1]["count"], 1,
                         "error 载荷不该覆盖已有的好缓存")


class MainWiring(unittest.TestCase):
    def test_endpoint_uses_cache(self):
        src = MODELS_PY.read_text(encoding="utf-8")
        self.assertIn("models_cache.cached_models", src)
        self.assertIn("_models_cache", src)

    def test_endpoint_has_force_param(self):
        """`force` 沿用 skill_status(skill.py:857) 的既有惯例，不另造第二套形状。"""
        import ast
        tree = ast.parse(MODELS_PY.read_text(encoding="utf-8"))
        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_models":
                fn = n
                break
        self.assertIsNotNone(fn, "找不到 list_models")
        args = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        self.assertIn("force", args, "/api/models 应有 force 参数可绕缓存")

    def test_endpoint_still_returns_error_key_on_failure(self):
        """改前语义：上游异常 ⇒ 返回 `error` 键而非抛 500。不得改变。

        ⚠ 窗口从 2000 放宽到 4000：加了缓存层与 docstring 后端点变长，
        2000 字符已经截不到那行 —— 闸门因为**实现变详细**而红，不是行为变了。
        """
        src = MODELS_PY.read_text(encoding="utf-8")
        i = src.index("async def list_models")
        seg = src[i:i + 4000]
        self.assertIn('"error": str(e)[:200]', seg,
                      "上游异常仍须回 error 键（与改前逐字一致）")

    def test_models_cache_has_no_heavy_imports(self):
        """L0 可测前提：不 import fastapi / aiohttp。

        ⚠ 用 AST 而不是扫文本 —— 本模块 docstring 里就写着「不 import fastapi /
        aiohttp」，扫文本会命中自己的自述（第一批闸门踩过这个坑，已记进注释）。
        """
        import ast
        tree = ast.parse((_REPO / "src" / "models_cache.py")
                         .read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("fastapi", "aiohttp"):
            self.assertNotIn(banned, imported)


class ShapeModels(unittest.TestCase):
    """`_shape_models` 的分组/去重口径必须与改前逐字一致。"""

    def test_dedup_by_id(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_m", _REPO / "src" / "models_cache.py")
        # 不能 import src.main（L0 铁律），所以这里只测纯逻辑的等价实现口径：
        # 直接从 api_models.py 抠出 _shape_models 的源码文本用 exec 跑（不 import 模块）。
        src = MODELS_PY.read_text(encoding="utf-8")
        import ast
        tree = ast.parse(src)
        # 2026-10-21：`_shape_models` 起调用同模块的 `model_billing` 给每项标免费/付费。
        # 必须把它一起 exec 进同一个命名空间 —— 只抠 `_shape_models` 会得到
        # `NameError: model_billing is not defined`（这里红过一次，别再拆开）。
        want = ("model_billing", "_shape_models")
        fns = [n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name in want]
        self.assertEqual(sorted(n.name for n in fns), sorted(want),
                         "api_models.py 里少了 %s" % (set(want) - {n.name for n in fns},))
        ns: dict = {}
        exec(compile(ast.Module(body=fns, type_ignores=[]), "<shape>", "exec"), ns)
        shape = ns["_shape_models"]
        out = [{"id": "a/b", "display_name": "B"}, {"id": "a/b"}, {"id": "c"}]
        r = shape(out, "claude")
        self.assertEqual(r["count"], 2, "同 id 只保留一条")
        self.assertEqual(sorted(r["groups"]), ["a", "default"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
