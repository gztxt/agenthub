"""前端轮询的三条纪律闸门（2026-10-05）。

本文件守三件互相独立、但都属「后台标签页白烧」的事：

1. **A3 徽章不再自己 fetch /api/agents** —— 它与 `loadAgents` 拉同一 URL，
   30s + 60s 两轮 ⇒ 每 30s 两次重复请求。而 `/api/agents` 是全站最重的只读端点
   （25 个 agent，每次跑 `docker ps` + `systemctl`）。
2. **A4 隐藏守卫** —— 页面隐藏时轮询早退。**守卫必须在函数体内，不得动
   `setInterval` 注册**：闸门 `tests/test_activity_indicator.py:114` 断言的字面量是
   `setInterval(loadActivity, 数字)`（正则），动注册就撞红。
3. **A5 不再有 O(n²)** —— 本机项目列表原在 `map()` 里对每个项目 `LP.indexOf(p)`。

【为什么 2 的守卫要「在函数体内」而不是「条件启动」】
条件启动（`if (!document.hidden) setInterval(...)`）更省，但仍要保留那行字面量
才能过闸门，等于绕；而函数体内早退的效果一样（隐藏时回调被调用但不做事），
且唤醒路径（`visibilitychange`）可以独立补一次。两者取舍见 `12-activity.js` 的注释。
"""
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))
from _js_min import strip_comments  # noqa: E402

CORE = _REPO / "static" / "hub" / "01-core-boot.js"
NAV = _REPO / "static" / "hub" / "06-manager-tasks.js"
ACT = _REPO / "static" / "hub" / "12-activity.js"
PROJ = _REPO / "static" / "hub" / "09-local-projects.js"
HUBJS = _REPO / "static" / "hub.js"


def _func(path: Path, name: str) -> str:
    src = strip_comments(path.read_text(encoding="utf-8"))
    m = re.search(r"^(async )?function %s\(.*?\n\}" % re.escape(name), src, re.M | re.S)
    assert m, "%s 里找不到函数 %s" % (path.name, name)
    return m.group(0)


class BadgeDedup(unittest.TestCase):
    """A3：agent 徽章走 AGENTS 全局，不再打 /api/agents。"""

    def test_update_badges_does_not_fetch_agents(self):
        body = _func(NAV, "updateBadges")
        self.assertNotIn("fetch('/api/agents')", body,
                         "updateBadges 仍在 fetch /api/agents ⇒ 与 loadAgents 重复请求")
        self.assertNotIn('fetch("/api/agents")', body)

    def test_update_badges_reads_agents_global(self):
        body = _func(NAV, "updateBadges")
        self.assertIn("AGENTS", body,
                      "agent 徽章应改读 loadAgents 已填好的 AGENTS 全局")

    def test_other_badges_keep_their_own_fetch(self):
        """⚠ 别把 /mcp/servers 与 /api/jobs 也一起删了 —— 那是确属不同的数据，AGENTS 里没有。"""
        body = _func(NAV, "updateBadges")
        self.assertIn("/mcp/servers", body, "MCP 徽章的数据源被误删")
        self.assertIn("/api/jobs", body, "定时任务徽章的数据源被误删")

    def test_load_agents_still_fetches(self):
        """反过来：loadAgents 必须仍在拉 /api/agents，否则 AGENTS 永远空。"""
        body = _func(CORE, "loadAgents")
        self.assertIn("/api/agents", body,
                      "loadAgents 不拉 /api/agents ⇒ AGENTS 没人填，徽章会恒为 0")


class HiddenGuards(unittest.TestCase):
    """A4：四个轮询函数都要在页面隐藏时早退。"""

    TARGETS = [
        (CORE, "loadAgents", "/api/agents"),
        (CORE, "pollHealth", "/health"),
        (NAV, "updateBadges", None),
        (ACT, "loadActivity", None),
        # T6（2026-11-12）：6s 的 tasks/jobs/chat 轮询是最后一个缺隐藏守卫的常驻点
        (NAV, "pollPageBits", None),
    ]

    def test_each_poller_guards_on_document_hidden(self):
        for path, fn, _ in self.TARGETS:
            with self.subTest(fn=fn):
                body = _func(path, fn)
                self.assertRegex(body, r"if\s*\(\s*document\.hidden\s*\)\s*return",
                                 "%s 必须在页面隐藏时早退（后台标签页白烧）" % fn)

    def test_guard_is_inside_function_not_at_registration(self):
        """守卫必须在**函数体内**；动 setInterval 注册会撞 test_activity_indicator:114。"""
        self.assertRegex(_func(ACT, "loadActivity"), r"document\.hidden",
                         "守卫位置不对")

    def test_setinterval_literals_are_untouched(self):
        """守住那条闸门的字面量（这里是它上游的保护，不是重复）。"""
        self.assertRegex(ACT.read_text(encoding="utf-8"),
                         r"setInterval\(loadActivity,\s*\d+\)",
                         "loadActivity 的 setInterval 字面量被改动 ⇒ "
                         "tests/test_activity_indicator.py:114 会红")
        self.assertRegex(NAV.read_text(encoding="utf-8"),
                         r"setInterval\(loadAgents,\s*\d+\)", "loadAgents 的周期被改动")

    def test_wakeup_path_exists(self):
        """⚠ 隐藏守卫必须有唤醒路径，否则笔记本合盖唤醒后要等满一个周期。

        这条很容易漏：加了守卫、忘了补拉，于是「后台不烧钱」的代价是
        「回到前台先看到过期数据」。

        ⚠ 判据为什么按**全仓**搜而不是「函数所在分片」（第一版写太窄，误报 3 条）：
        `loadAgents` / `pollHealth` 定义在 01，而唤醒监听器在 06（那里是它们的
        注册处，setInterval 也都在 06）。按文件查会判「01 缺唤醒」—— 但实际
        覆盖是有的。按「任一分片里有 visibilitychange 且调用了该函数」判，
        才与运行时行为一致。
        """
        all_src = "\n".join(p.read_text(encoding="utf-8")
                            for p in (CORE, NAV, ACT))
        self.assertRegex(all_src, r"visibilitychange",
                         "全仓都没有 visibilitychange 唤醒补拉")
        for path, fn, _ in self.TARGETS:
            with self.subTest(fn=fn):
                # 该函数名必须出现在某处 visibilitychange 回调附近（同一段文本内）
                found = re.search(
                    r"addEventListener\(\s*'visibilitychange'[\s\S]{0,400}?\b%s\b"
                    % re.escape(fn), all_src)
                self.assertIsNotNone(found,
                                     "%s 没有任何 visibilitychange 回调调用它 ⇒ "
                                     "加了隐藏守卫却忘了唤醒补拉" % fn)

    def test_activity_wakeup_is_not_a_move(self):
        """唤醒是**补**不是挪：loadActivity 的首次调用与 setInterval 都得还在。"""
        src = ACT.read_text(encoding="utf-8")
        # ⚠ 勿把 re.M 当第三位置参数传给 assertRegex —— 它的签名是
        #   assertRegex(text, regex, msg)，第三位是**消息**。多传会被当 msg 之外的
        #   第四参 ⇒ TypeError（第一版踩过）。多行标志用内联 (?m)。
        self.assertRegex(src, r"(?m)^loadActivity\(\);",
                         "loadActivity() 的首次调用被挪走了")
        self.assertRegex(src, r"setInterval\(loadActivity,\s*\d+\)")


class NoQuadraticScan(unittest.TestCase):
    """A5：项目列表不得在 map/filter 里做线性查找。"""

    def test_lp_render_list_has_no_indexof_in_map(self):
        src = strip_comments(PROJ.read_text(encoding="utf-8"))
        body = re.search(r"^function lpRenderList\(.*?\n\}", src, re.M | re.S)
        self.assertIsNotNone(body, "找不到 lpRenderList")
        seg = body.group(0)
        self.assertNotIn(".indexOf(", seg,
                         "lpRenderList 里仍有 indexOf ⇒ O(n²)（应在 map 外建索引）")

    def test_lp_render_list_builds_a_lookup_map(self):
        body = _func(PROJ, "lpRenderList")
        self.assertRegex(body, r"new Map\(",
                         "应一次性建 path→下标 的 Map，而不是每个项目查一次")


class BuiltArtifactAgrees(unittest.TestCase):
    """产物同步：改分片必须跑 build_hubjs.sh（否则 test_hubjs_split 也会红，
    但那条只查 md5 相等、不查「新逻辑真的进了产物」，这里补上语义检查）。"""

    def test_guard_present_in_built_hubjs(self):
        src = HUBJS.read_text(encoding="utf-8")
        n = len(re.findall(r"if\s*\(\s*document\.hidden\s*\)\s*return", src))
        self.assertGreaterEqual(n, 4,
                                "产物里只找到 %d 处隐藏守卫（源码有 4 处）⇒ "
                                "多半忘了跑 scripts/build_hubjs.sh" % n)

    def test_badge_dedup_present_in_built_hubjs(self):
        src = HUBJS.read_text(encoding="utf-8")
        i = src.index("async function updateBadges")
        seg = src[i:i + 1200]
        self.assertNotIn("fetch('/api/agents')", seg,
                         "产物里的 updateBadges 仍在 fetch /api/agents ⇒ 忘了重建")


if __name__ == "__main__":
    unittest.main(verbosity=2)
