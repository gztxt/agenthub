#!/usr/bin/env python3
"""L0 闸门：顶层 `let/const` 不得晚于「会同步读到它」的顶层语句。

为什么需要它（09-23 自研 APP 事故）：`static/hub/05-…js` 里顶层 IIFE
`histBootstrap()` 会同步走 `histLoad() → renderNav()`，而 `let _navHtml` 声明在
`renderNav` 之后 ⇒ 早期路径一旦被激活就抛
`ReferenceError: Cannot access '_navHtml' before initialization`，整个 hub.js 当场死亡。
症状是"菜单空白 + 抽屉遮住正文 + 全部点不动"，而且**只在 localStorage 有
hub.term.token 的那个 origin 出现** —— 所以本机浏览器全绿、端侧坏，前三轮都归因错了。

判据（可证伪、不靠人记）：
  1. 收集所有顶层（缩进 0）`let X` / `const X` 声明行号。
  2. 收集所有**顶层立即执行语句**：IIFE（`(function…` / `(()=>…`）与裸调用 `name(…`
     （排除 if/for/while/switch/return/function/class/try 等关键字）。
  3. 从该语句出发，沿调用图（深度 ≤ 5）展开函数体，收集它同步读到的顶层声明变量。
  4. 若某变量的声明行号 **大于** 语句起始行号 ⇒ 违规（TDZ 风险），判红。

红基线：修复前的 git 版本必须被抓出来（否则本闸门是空转的假绿）。
"""
import re
import subprocess
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUB = ROOT / "static" / "hub.js"
RED_SHA = "8067801"   # ★红基线钉**具体提交**（最后一个仍带 TDZ 的 hub.js，md5 0e6e0613）。\n# 取 HEAD 的话修复一提交 HEAD 就变干净 ⇒ 红况静默不红、闸门空转（同仓已犯两次）。
KEYWORDS = {"if", "for", "while", "switch", "return", "function", "class", "try",
            "catch", "do", "else", "typeof", "new", "delete", "void", "await", "yield"}

DECL_RE = re.compile(r"^(?:let|const)\s+([A-Za-z_$][\w$]*)")
IIFE_RE = re.compile(r"^\(\s*(?:async\s+)?(?:function|\(\)\s*=>|[A-Za-z_$])")
CALL_RE = re.compile(r"^([A-Za-z_$][\w$]*)\s*\(")
USE_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


from _js_min import strip_comments   # 见该文件 docstring：承诺与实现必须一致


def _selfcheck_strip():
    """自检：顶格注释里的 `foo()` 不得被识别为顶层语句。"""
    probe = "/*\nhistLoad() -> x\n*/\nlet _x = '';\n"
    uniq, _ = build(strip_comments(probe))
    bad = any(v == "_x" for (_, v) in uniq) or build(strip_comments(probe))[0]
    assert not bad, "去注释失效：注释被当成顶层语句"
    return True


def top_level_lines(src):
    """返回 {行号: 该行去掉缩进后的文本}，只保留缩进为 0 的非空行（顶层语句边界）。"""
    out = {}
    for i, ln in enumerate(src.split("\n"), 1):
        if ln and not ln[0].isspace():
            out[i] = ln
    return out


def build(src):
    tops = top_level_lines(src)
    tlines = sorted(tops)
    decls, funcs = {}, {}
    for i, ln in tops.items():
        m = DECL_RE.match(ln)
        if m and m.group(1) not in decls:
            decls[m.group(1)] = i
        fm = re.match(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", ln)
        if not fm:
            fm = re.match(r"(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|[A-Za-z_$][\w$]*\s*=>)", ln)
        if fm and fm.group(1) not in funcs:
            funcs[fm.group(1)] = i

    # 函数体范围：从声明行到下一个顶层行（简单可靠：本仓代码风格一行一语句）
    bodies = {}
    for name, start in funcs.items():
        nxt = [x for x in tlines if x > start]
        bodies[name] = "\n".join(src.split("\n")[start - 1:(nxt[0] if nxt else len(tlines))])

    def refs(name, depth, seen):
        """函数体里同步读到的 (被调函数 ∪ 被读标识符)。"""
        if depth > 5 or name in seen or name not in bodies:
            return set()
        seen.add(name)
        body = bodies[name]
        out = set()
        for tok in USE_RE.findall(body):
            if tok not in KEYWORDS and tok in bodies:
                out |= {tok} | refs(tok, depth + 1, seen)
        for tok in IDENT_RE.findall(body):
            if tok in decls:
                out |= {tok}
        return out

    violations = []
    for i, ln in tops.items():
        if DECL_RE.match(ln):
            continue
        heads = []
        if IIFE_RE.match(ln):
            heads = [x for x in USE_RE.findall(ln) if x in bodies]
            # IIFE 内部体：扫到语句块结束
            nxt = [x for x in tlines if x > i]
            blk = "\n".join(src.split("\n")[i - 1:(nxt[0] if nxt else len(tlines))])
            heads += [x for x in USE_RE.findall(blk) if x in bodies]
        else:
            m = CALL_RE.match(ln)
            if m and m.group(1) in bodies:
                heads = [m.group(1)]
        for h in set(heads):
            for v in refs(h, 0, set()):
                # refs() 混返「函数名 ∪ 顶层变量名」，只有后者才是 TDZ 对象
                if v in decls and decls[v] > i:
                    violations.append((i, v, decls[v], h))
    # 同一 (语句行, 变量) 只报一次
    uniq = {}
    for i, v, d, h in violations:
        uniq.setdefault((i, v), (d, h))
    return uniq, decls



class TestTdzOrder(unittest.TestCase):
    SRC = None
    RED = None

    @classmethod
    def setUpClass(cls):
        cls.SRC = strip_comments(HUB.read_text(encoding="utf-8"))
        cls.RED = strip_comments(subprocess.run(["git", "show", RED_SHA + ":static/hub.js"],
                                                cwd=ROOT, capture_output=True,
                                                text=True).stdout)

    def test_strip_comments_not_tricked_by_prose(self):
        """顶格注释里的 `foo()` 不得被当成顶层语句（v0.13.11 真实踩过）。"""
        uniq, _ = build(strip_comments("/*\nhistLoad() -> x\n*/\nlet _x = '';\n"))
        self.assertEqual(uniq, {}, "去注释失效：注释被当成顶层语句")

    def test_current_build_has_no_tdz(self):
        uniq, decls = build(self.SRC)
        self.assertGreater(len(decls), 20, "顶层声明数异常 ⇒ 扫描可能失效")
        bad = "; ".join("hub.js:%d 经 %s() 读到 %r 但声明在 :%d" % (i, h, v, d)
                        for (i, v), (d, h) in sorted(uniq.items()))
        self.assertEqual(len(uniq), 0, "存在 TDZ 风险:\n" + bad)

    def test_red_baseline_is_caught(self):
        """闸门对已知事故必须敏感，否则本闸门属空转。"""
        base, _ = build(self.RED)
        self.assertTrue(any(v == "_navHtml" for (_, v) in base),
                        "红基线版（修复前）未被抓住 ⇒ 判据无效")

    def test_declaration_precedes_bootstrap(self):
        """具体不变量：_navHtml 的声明必须早于 histBootstrap 这个顶层 IIFE。"""
        src = HUB.read_text(encoding="utf-8").split("\n")
        d = next(i for i, ln in enumerate(src, 1) if ln.startswith("let _navHtml = "))
        b = next(i for i, ln in enumerate(src, 1) if "(function histBootstrap" in ln)
        self.assertLess(d, b, "let _navHtml@%d 必须在 histBootstrap@%d 之前" % (d, b))

    def test_project_state_initialized_before_bootstrap_go(self):
        """v0.13.32 真事故（2026-09-26 实测抓红）：用户存了
        hub.page='localprojects' 后刷新，06 顶层 go(lsGet('hub.page')) 会同步调
        loadLocalProjects()（函数声明提升），而 LP 的 `var LP = []` 初始化在 09
        分片顶层（拼接序在 06 之后）⇒ 函数读 LP.length 抛 TypeError ⇒ promise
        reject ⇒ 列表永远停在「加载中…」。修法：LP/GH/lpStars…lpHiddenSet/
        ghStars/ghHiddenSet 的初始化前置到 01（lpLoaded 同型：声明只允许在 01
        一处，09/10 再写一份会把懒加载标志重置回 false，见 test_localprojects）。
        本钉子断言：01 里的初始化行必须早于 06 的 bootstrap go() 行。"""
        src = HUB.read_text(encoding="utf-8").split("\n")
        boot = next(i for i, ln in enumerate(src, 1)
                    if ln.startswith("go(lsGet('hub.page')"))
        for decl in ("var LP = [], GH = [];", "var lpStars = new Set()"):
            d = next(i for i, ln in enumerate(src, 1) if ln.startswith(decl))
            self.assertLess(d, boot,
                            "%s@%d 必须在 bootstrap go()@%d 之前（否则刷新回项目页即崩）"
                            % (decl, d, boot))


if __name__ == "__main__":
    unittest.main(verbosity=2)
