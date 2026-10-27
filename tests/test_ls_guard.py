#!/usr/bin/env python3
"""L0 闸门：全站 localStorage 必须走 lsGet / lsSet / lsRemove 守卫（v0.13.13，09-24）。

为什么要这只闸门：前端原有 **45 处裸 `localStorage.getItem/setItem/removeItem`**，
任何一处抛异常都会打断启动链 —— 隐私模式 / WebView 禁 DOM Storage（连取
`window.localStorage` 本身都抛）、配额满（QuotaExceededError）。本项已有过两次
"顶层语句抛异常 ⇒ 整段 hub.js 当场死亡 ⇒ 端侧看起来随机坏掉"的事故（09-23 TDZ、
09-23 响应体截断），症状与"网络坏了"几乎同形，而本机对手机端零探针能力。
守卫层把这类异常收敛成计数 + 摘要（`window.__lsDiag`）并上 `?diag=1` 面板；
**但守卫本身可以被绕过**（以后谁再写一行 `localStorage.getItem` 就白干），所以判据
必须是机器可查的源码形态，而不是"我记得都改了"。

四条判据 + 一条行为层：
  R1 分片源码里裸 `localStorage.` 调用（去掉注释后）== 0，唯一允许的直接访问形式是
     守卫内部的 `window.localStorage.`；
  R2 `window.localStorage.` 恰好 3 处，且全落在 lsGet/lsSet/lsRemove 函数体内
     （堵住"在守卫外面直接摸存储"）；
  R3 定义早于使用：lsGet/lsSet/lsRemove/__lsDiag 的定义行必须早于各自第一次被使用的行
     （02/05/06 有**顶层语句**直接读存储，晚一行的使用者就是 TDZ）；
  R4 可观测性不许掉线：`?diag=1` 面板必须有打印 `__lsDiag` 的那一行（一行一键）；
  行为层（L1 host，真 node）：存储正常时语义逐字不变；存储抛异常时 get 回落 fallback、
     set/remove 返回 false、fails 必须被计数（不得静默假装成功）。

红基线钉**具体提交** `0822164`（改前状态）：闸门必须在旧字节上判红，否则属空转。
不钉 HEAD —— 本修复一提交 HEAD 就变干净，红况会静默不红（同仓已犯两次）。

★为什么不复用 `tests/_js_min.strip_comments`（实测缺陷，别踩第二遍）：它不识别正则
字面量。`01-core-boot.js` 里 `escapeHtml()` 的 `/[&<>"']/g` 那个 `"` 会让它进入"字符串
态"并一路漂到文件尾，期间把换行符替换成空格 —— 实测该文件 387 行被压成 183 行，于是
"顶层行"判定整体失真，`localStorage.` 计数也虚高（把注释里的那处数成了代码）。本文件
自带 `blank_js_comments()`：识别正则字面量、模板串可跨行，且**承诺并自检"输出行数 ==
输入行数"**（见 test_stripper_preserves_line_count）。
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARDS = sorted((ROOT / "static" / "hub").glob("[0-9][0-9]-*.js"))
# P4（v0.13.19）之后的分片在红基线 SHA 上**不存在**，逐文件取旧体时必须跳过。
# 故意列成显式名单而不是「取不到就静默 skip」：一旦有人把红基线整段注掉，
# RED_TOTAL 那道全站计数断言会立刻变空转（那正是 09-24 侦察报的假绿家族）。
# 09-local-projects.js（v0.13.30）、10-github-projects.js（v0.13.31）、11-resources.js（v0.13.52）：新分片，红基线期不存在。
# 12-activity.js（v0.13.59 P2-B 跨 Agent 活动指示）：新分片，红基线期不存在。
# 13-term-touch.js（v0.13.64 终端移动端触摸层）：新分片，红基线期不存在。
# 14-botboy.js（v0.13.170 DS Bot 团队只读投影）：新分片，红基线期不存在。
# 08-runlog.js（v0.13.27）随运行日志页于 v0.13.47 删除，名字从两处清单里一并摘掉。
SHARDS_AFTER_RED = ["07-asset-panel.js", "09-local-projects.js",
                    "10-github-projects.js", "11-resources.js", "12-activity.js",
                    "13-term-touch.js", "14-botboy.js"]
HUBJS = ROOT / "static" / "hub.js"   # 产物也一起纳入零丢行断言
HUB = ROOT / "static" / "hub.js"
TPL = ROOT / "templates" / "index.html"
RED_SHA = "0822164"          # ★改前状态（v0.13.12）：45 处裸调用、没有守卫
RED_TOTAL = 45               # 改前全站裸调用数（逐文件清点：7+9+3+15+4+7）
GUARD_FNS = ("lsGet", "lsSet", "lsRemove")
HELPER_FNS = ("lsDiagStore", "lsDiagHit", "lsDiagFail")
DIAG_OBJ = "__lsDiag"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tiers                                                  # noqa: E402


# ══════════════════════════════════════════════════════════════
#  去注释（严格保行号）
# ══════════════════════════════════════════════════════════════
_REGEX_OK_PREV = set("=([{,;:!&|?+-*%<>~^")
_REGEX_KEYWORDS = {"return", "typeof", "case", "in", "of", "do", "else",
                   "yield", "await", "delete", "void"}


def blank_js_comments(src):
    """把 `//` 与 `/* */` 注释字符换成空格，**行数一定不变**。

    与 _js_min 的区别：认得正则字面量（靠"上一个有效字符"判定 `/` 是除号还是正则），
    并且模板串里的换行原样保留。字符串本体也保留（闸门要看的是代码里的字符串常量）。
    """
    out = []
    i, n = 0, len(src)
    q = ""              # 当前字符串引号：' " `
    in_line = False     # 行注释
    in_block = False    # 块注释
    last_sig = ""       # 上一个非空白有效字符
    last_word = ""      # 上一个标识符（return 等关键字要能认出来）
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            out.append(c if c == "\n" else " ")
            if c == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                out.append("  ")
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue
        if q:
            out.append(c)
            if c == "\\":
                out.append(src[i + 1] if i + 1 < n else "")
                i += 2
                continue
            if c == q:
                q = ""
            i += 1
            continue
        if c in "\"'`":
            q = c
            out.append(c)
            i += 1
            last_sig = c
            continue
        if c == "/" and nxt == "/":
            in_line = True
            out.append("  ")
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            out.append("  ")
            i += 2
            continue
        if c == "/" and (not last_sig or last_sig in _REGEX_OK_PREV
                         or last_word in _REGEX_KEYWORDS):
            j = i + 1
            buf = [c]
            cls = False
            while j < n:
                d = src[j]
                if d == "\n":
                    break                      # 未闭合 ⇒ 当普通字符，别把整文件吞了
                buf.append(d)
                if d == "\\":
                    j += 1
                    if j < n:
                        buf.append(src[j])
                    j += 1
                    continue
                if d == "[":
                    cls = True
                elif d == "]":
                    cls = False
                elif d == "/" and not cls:
                    j += 1
                    while j < n and src[j].isalpha():
                        buf.append(src[j])
                        j += 1
                    break
                j += 1
            out.append("".join(buf))
            i = j
            last_sig = "/"
            last_word = ""
            continue
        out.append(c)
        if not c.isspace():
            if c.isalnum() or c == "_":
                last_word = (last_word + c) if (last_word and (last_word[-1].isalnum()
                            or last_word[-1] == "_")) else c
            else:
                last_word = ""
            last_sig = c
        i += 1
    return "".join(out)


def _text(p):
    return Path(p).read_text(encoding="utf-8")


def _git(rev, path):
    r = subprocess.run(["git", "-C", str(ROOT), "show", "%s:%s" % (rev, path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("取不到 %s:%s —— 红基线取不到，本闸门会退化成自证" % (rev, path))
    return r.stdout


BARE_RE = re.compile(r"(?<!window\.)localStorage\.")
WINDOW_RE = re.compile(r"window\.localStorage\.")


def bare_uses(src_no_comments):
    """每一次裸 `localStorage.` 出现 ⇒ (行号, 行内容)。按**次数**数，不按行。"""
    hits = []
    for ln, line in enumerate(src_no_comments.split("\n"), 1):
        for _m in BARE_RE.finditer(line):
            hits.append((ln, line.strip()[:110]))
    return hits


def guard_body_ranges(src):
    """{守卫函数名: (起行, 止行)}，止行取该函数后面第一个顶格 `}`。"""
    lines = src.split("\n")
    ranges = {}
    for name in GUARD_FNS:
        start = next((i + 1 for i, ln in enumerate(lines)
                      if re.match(r"^(?:async\s+)?function\s+%s\s*\(" % name, ln)), None)
        if start is None:
            continue
        end = next((j + 1 for j in range(start, len(lines)) if lines[j] == "}"), len(lines))
        ranges[name] = (start, end)
    return ranges


def def_line(src_no_comments, name):
    for ln, line in enumerate(src_no_comments.split("\n"), 1):
        if name == DIAG_OBJ:
            if re.match(r"^window\.%s\s*=" % name, line):
                return ln
        elif re.match(r"^(?:async\s+)?function\s+%s\s*\(" % name, line):
            return ln
    return None


def use_lines(src_no_comments, name):
    """name 被**使用**的行（定义行本身不算）。"""
    out = []
    for ln, line in enumerate(src_no_comments.split("\n"), 1):
        if re.search(r"function\s+%s\s*\(" % name, line):
            continue
        if re.match(r"^window\.%s\s*=" % name, line):
            continue
        if re.search(r"\b%s\b" % re.escape(name), line):
            out.append(ln)
    return sorted(set(out))


# ══════════════════════════════════════════════════════════════
#  先验工具，再拿工具验代码
# ══════════════════════════════════════════════════════════════
class TestStripperSanity(unittest.TestCase):
    def test_stripper_preserves_line_count(self):
        for p in SHARDS + [HUB]:
            raw = _text(p)
            self.assertEqual(blank_js_comments(raw).count("\n"), raw.count("\n"),
                             "%s 去注释后行数变了 ⇒ 行号类判据全部失真" % p.name)

    def test_stripper_blanks_comments_but_keeps_code(self):
        raw = _text(SHARDS[0]).split("\n")
        blanked = blank_js_comments("\n".join(raw)).split("\n")
        idx = next(i for i, ln in enumerate(raw) if "`localStorage.getItem('hub.page')`" in ln)
        self.assertNotIn("localStorage", blanked[idx], "注释里那句 localStorage 没抹干净")
        # 正则字面量那一行：字面量本体要原样保留，后面的代码不得被“字符串态”吞掉
        rx = next(i for i, ln in enumerate(raw) if "/[&<>\"']" in ln)
        self.assertRegex(blanked[rx], r"replace\(/\[", "正则字面量被改写了")
        self.assertIn("&#39;", blanked[rx], "正则字面量把后面的代码吞掉了")
        # 状态机必须从正则里复位：后面那行行注释要被抹干净
        joined = "\n".join(blanked)
        self.assertNotIn("只允许一个抽屉在开", joined,
                         "行注释没被抹掉 ⇒ 正则/字符串态飘了，行号类判据不可信")

    def test_js_min_is_lossless_now(self):
        """09-24 修好 `_js_min.strip_comments` 后，本钉子**翻向**：从"承认有缺陷"改为
        "断言不许再丢行"。（原钉子写着"若修好请同步改写本例"，现在就是那个同步。）

        为什么值得钉：丢行会让依赖它的闸门在**残缺文本**上跑（01 片曾 442→238），
        那是"看着绿其实在漏"的静默失效，比红更贵。
        """
        from _js_min import strip_comments
        for p in SHARDS + [HUBJS]:
            if not p.exists():
                continue
            raw = _text(p)
            self.assertEqual(strip_comments(raw).count("\n"), raw.count("\n"),
                             "%s 去注释后行数变了 ⇒ 又出现吞行（正则字面量/注释里的引号/跨行串）" % p.name)

    def test_regex_with_quotes_does_not_swallow_lines(self):
        """元凶样本必须单独钉：HTML 转义表 + 含引号的正则字面量。"""
        from _js_min import strip_comments
        src = ("var m = {'&':'&amp;','\"':'&quot;'};\n"
               "var re = /[&<>\"']/g;\n"
               "var keep = 1;\n")
        out = strip_comments(src)
        self.assertEqual(out.count("\n"), src.count("\n"), "含引号的正则不得开字符串态")
        self.assertIn("var keep = 1;", out, "正则之后的代码必须还在（被吞掉就等于闸门看不见）")

    def test_bare_newline_in_string_recovers(self):
        """JS 里单/双引号串不能跨行；若解析器误开串态，遇裸换行必须退出而不是吞行。"""
        from _js_min import strip_comments
        src = "var a = '没闭合的串\nvar b = 2;\nvar c = 3;\n"
        out = strip_comments(src)
        self.assertIn("var b = 2;", out, "误开串态后必须恢复，否则后面全部消失")


class TestLsGuardStatic(unittest.TestCase):
    def test_shard_set_is_what_we_counted(self):
        self.assertEqual([p.name for p in SHARDS],
                         ["01-core-boot.js", "02-nav-and-poll.js", "03-agents-cards.js",
                          "04-terminal-ws.js", "05-chat-and-history.js", "06-manager-tasks.js",
                          "07-asset-panel.js", "09-local-projects.js",
                          "10-github-projects.js", "11-resources.js", "12-activity.js",
                          "13-term-touch.js", "14-botboy.js"],
                         "分片清单变了 ⇒ 逐文件计数与红基线要一起核")

    def test_R1_no_bare_localStorage_in_shards(self):
        bad = []
        for p in SHARDS:
            for ln, txt in bare_uses(blank_js_comments(_text(p))):
                bad.append("%s:%d  %s" % (p.name, ln, txt))
        self.assertEqual(bad, [], "存在裸 localStorage 调用：\n  " + "\n  ".join(bad))

    def test_R2_direct_storage_access_only_inside_guard(self):
        blanked = blank_js_comments(_text(SHARDS[0]))
        hits = [ln for ln, line in enumerate(blanked.split("\n"), 1) if WINDOW_RE.search(line)]
        self.assertEqual(len(hits), 3,
                         "直接摸存储必须恰好 3 处（get/set/remove 各一），实得行号 %s" % hits)
        ranges = guard_body_ranges(blanked)
        self.assertEqual(sorted(ranges), sorted(GUARD_FNS), "守卫三函数没全找到")
        for ln in hits:
            self.assertTrue(any(a <= ln <= b for a, b in ranges.values()),
                            "window.localStorage. 出现在守卫外（01 第 %d 行）" % ln)
        # 守卫之外任何分片都不许出现 window.localStorage.
        for p in SHARDS[1:]:
            self.assertEqual([], [ln for ln, line in
                                  enumerate(blank_js_comments(_text(p)).split("\n"), 1)
                                  if WINDOW_RE.search(line)],
                             "%s 里不该直接摸存储" % p.name)

    def test_R3_guard_defined_before_every_use(self):
        src = blank_js_comments(_text(HUB))
        for name in GUARD_FNS + (DIAG_OBJ,):
            d = def_line(src, name)
            self.assertIsNotNone(d, "built hub.js 里找不到 %s 的定义" % name)
            early = [u for u in use_lines(src, name) if u < d]
            self.assertEqual(early, [],
                             "%s 在第 %s 行被使用，定义却在第 %d 行 ⇒ 顶层使用者踩 TDZ" % (name, early, d))
        # 硬档：守卫块必须早于**第一个顶层存储读取**（02 let chatPick / 05 const
        # navOpenStored / 06 go(lsGet('hub.page'))）。函数声明会提升，所以今天不会炸；
        # 但谁把守卫改成 const 箭头函数，这一条就是生死线。
        self.assertLess(def_line(src, "lsGet"), min(use_lines(src, "lsGet")),
                        "lsGet 定义晚于首次使用")
        self.assertLess(def_line(src, DIAG_OBJ), def_line(src, "lsGet"),
                        "__lsDiag 必须先于 lsGet 存在（lsGet 要往里计数）")

    def test_R4_diag_panel_reports_ls_failures(self):
        html = _text(TPL)
        self.assertIn("__lsDiag", html,
                      "?diag=1 面板不再打印 __lsDiag ⇒ 存储被禁时端侧无法自证")
        m = re.search(r"'(localStorage异常=[^\n']*)'", html)
        self.assertIsNotNone(m, "面板里找不到 `localStorage异常=` 那一行（一行一键）")
        self.assertTrue(m.group(1).startswith("localStorage异常="))
        # 面板是「一行一键」的纯文本，新行必须以 \n 收尾，否则会把下一键挤同行
        self.assertIn("+ '\\n'", html[html.find("localStorage异常="):html.find("localStorage异常=") + 900]
                      or html, "新增行要有换行收尾")

    def test_R5_red_baseline_is_caught(self):
        """闸门必须在改前字节上判红，否则属空转。"""
        per, total = {}, 0
        red_shards = [p for p in SHARDS if p.name not in SHARDS_AFTER_RED]
        self.assertEqual(len(red_shards), 6,
                         "红基线覆盖的分片数应为 6（11 分片 − 5 个红基线后新增）"
                         "⇒ 不能靠新增分片稀释计数基线；新增分片要登记进 SHARDS_AFTER_RED")
        for p in red_shards:
            old = blank_js_comments(_git(RED_SHA, "static/hub/" + p.name))
            n = len(bare_uses(old))
            per[p.name] = n
            total += n
        self.assertEqual(total, RED_TOTAL,
                         "改前全站裸调用应为 %d，实得 %d（%s）" % (RED_TOTAL, total, per))
        self.assertEqual(sum(len(bare_uses(blank_js_comments(_text(p)))) for p in SHARDS), 0,
                         "绿侧没清干净，红绿对照无意义")
        hub_old = blank_js_comments(_git(RED_SHA, "static/hub.js"))
        for name in GUARD_FNS + (DIAG_OBJ,):
            self.assertIsNone(def_line(hub_old, name),
                              "红基线里居然已有 %s ⇒ SHA 取错了" % name)
        self.assertEqual(guard_body_ranges(hub_old), {}, "红基线不该有守卫函数体")


# ══════════════════════════════════════════════════════════════
#  L1 host：拿真 node 跑守卫，验"抛异常时到底兜没兜住"
# ══════════════════════════════════════════════════════════════
@tiers.host_only
class TestLsGuardBehaviour(unittest.TestCase):
    """语义不变量：守卫**只加兜底、不改语义**；异常要计数，不许静默假装成功。"""

    def _run(self, stub, body):
        from _hub_extract import extract_function
        src = _text(HUB)
        parts = [extract_function(src, f) for f in HELPER_FNS + GUARD_FNS]
        missing = [f for f, p in zip(HELPER_FNS + GUARD_FNS, parts) if not p]
        self.assertFalse(missing, "抽不到 %s ⇒ 改名会让本闸门失去意义" % missing)
        js = ("global.window = %s;\n%s\n%s\n"
              "process.stdout.write(JSON.stringify(global.__out));"
              % (stub, "\n".join(parts), body))
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, "node 报错：" + r.stderr.strip()[:400])
        return json.loads(r.stdout.strip())

    def test_healthy_storage_semantics_unchanged(self):
        stub = ("{localStorage:{_d:{a:'1'},"
                "getItem(k){return Object.prototype.hasOwnProperty.call(this._d,k)?this._d[k]:null;},"
                "setItem(k,v){this._d[k]=String(v);},removeItem(k){delete this._d[k];}}}")
        got = self._run(stub,
                        "global.__out={g:lsGet('a'),miss:lsGet('zz'),missFb:lsGet('zz','d'),"
                        "s:lsSet('b','2'),rb:lsRemove('a'),missAfter:lsGet('a'),"
                        "diag:window.__lsDiag};")
        self.assertEqual(got["g"], "1")
        self.assertIsNone(got["miss"], "取不到的键必须返回 null（与浏览器原生一致）")
        self.assertEqual(got["missFb"], "d", "fallback 必须生效")
        self.assertEqual([got["s"], got["rb"], got["missAfter"]], [True, True, None])
        self.assertEqual(got["diag"]["fails"], 0, "健康路径不该记着失败")
        self.assertGreaterEqual(got["diag"]["ok"], 5)

    def test_getter_itself_throws_privacy_mode(self):
        stub = ("{get localStorage(){ const e=new Error('SecurityError'); e.name='SecurityError';"
                " throw e; }}")
        got = self._run(stub,
                        "global.__out={g:lsGet('hub.page'),g2:lsGet('hub.page','x'),"
                        "s:lsSet('k','v'),r:lsRemove('k'),diag:window.__lsDiag};")
        self.assertIsNone(got["g"])
        self.assertEqual(got["g2"], "x", "异常时必须回落到 fallback")
        self.assertEqual(got["s"], False, "写失败不得假装成功")
        self.assertEqual(got["r"], False)
        self.assertEqual(got["diag"]["fails"], 4, "四次调用四次异常，必须全计数")
        self.assertIn("SecurityError", got["diag"]["lastErr"])

    def test_quota_exceeded_is_counted_not_swallowed(self):
        stub = ("{localStorage:{getItem(){const e=new Error('full');e.name='QuotaExceededError';throw e;},"
                "setItem(){const e=new Error('full');e.name='QuotaExceededError';throw e;},"
                "removeItem(){const e=new Error('gone');e.name='QuotaExceededError';throw e;}}}")
        got = self._run(stub,
                        "lsGet('a');lsSet('b','1');lsRemove('c');"
                        "global.__out={g:lsGet('a'),s:lsSet('b','1'),"
                        "fb:lsGet('a','fallback'),diag:window.__lsDiag};")
        self.assertEqual([got["g"], got["s"], got["fb"]], [None, False, "fallback"])
        self.assertGreaterEqual(got["diag"]["fails"], 5,
                                "异常必须累计计数（供 ?diag 面板显示），实得 %s" % got["diag"])
        self.assertIn("QuotaExceededError", got["diag"]["lastErr"],
                      "lastErr 要留错误名，否则端侧分不清是配额满还是存储被禁")


if __name__ == "__main__":
    unittest.main(verbosity=2)
