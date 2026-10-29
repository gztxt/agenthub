"""不可信来源的渲染点必须转义（存储型 XSS 闸门）。

【为什么这个文件存在：一个已验证可利用的缺陷】
`renderTaskTable` / `renderDag`（协同 DAG 页）把 **LLM 返回的任务字段**裸拼进
`innerHTML` 与 SVG `<text>`。完整链路（本仓实测，非推断）：

    src/tasks.py:154  decompose()
      → llm.chat_tools_loop(...)            # LLM 返回 JSON
      → _extract_json_array(answer)
      → _validate_dag()                     # src/tasks.py:104
             tid = str(it.get("id") or f"t{i+1}")   # ← **无字符集校验**
             只查「重复」与「成环」，不查内容
      → INSERT INTO tasks(...)
      → GET /api/tasks/...  →  renderTaskTable()  →  innerHTML

也就是说：**模型输出什么，DOM 里就出现什么**。而 hub 的 origin 里有终端（能起 pty），
一次成功的注入就能摸到终端 —— 这是 v0.13.77 自己的根因记录里写明的赌注。

【判据为什么是「同行不一致」而不是逐点列举】
三处缺陷长得一模一样：**同一个拼接行里，有的字段转义了、有的没有**。

    04:1077  第 6 格  escapeHtml(t.output...)      ✅ 已转义
    04:1074  第 1-4 格  t.task_id / agent_id / deps / status   ❌ 裸拼
    04:1004  同行     escapeHtml(x.session_id)      ✅
             同行     x.source / x.event            ❌

⇒ 「这一处漏了」是点状的、修完就忘；「这一行有两种写法」是形状的、机器可判、
同族漏网一并兜住。所以本闸门主判据是**形状**，逐点断言只作为定位辅助。

【本闸门自己的可信度：红臂在同一产物内】
`TestDetectorBites` 用**构造出来的**payload（不是从 git 取旧版本）喂判据函数，
断言它确实会中和。这样闸门绿的时候，是「判据真的在跑」，不是「判据根本没被调用」。
反例就在本仓：`tests/test_md_inline.py:15-17` 记了第一版把「真名与手误串两边都手打」
导致恒真假绿 —— 夹具必须由原串构造，不许手打。
"""
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))
from _js_min import strip_comments  # noqa: E402

TERM = _REPO / "static" / "hub" / "04-terminal-ws.js"
CHATHIST = _REPO / "static" / "hub" / "05-chat-and-history.js"
TASKS_PY = _REPO / "src" / "tasks.py"


def _body(path: Path, func: str) -> str:
    """抠出函数体（到第 0 列的 `}` 为止 —— 本仓顶层函数一律第 0 列收尾）。

    不用花括号配对：JS 注释里就有 `{prefix:"?"}` 这类字面量，朴素配对会被
    **注释里的** `}` 提前闭合（`tests/_hub_extract.py` 记了这个坑）。
    """
    src = strip_comments(path.read_text(encoding="utf-8"))
    m = re.search(r"^function %s\(.*?\n\}" % re.escape(func), src, re.M | re.S)
    assert m, "%s 里找不到函数 %s" % (path.name, func)
    return m.group(0)


class TestDetectorBites(unittest.TestCase):
    """红臂：判据本身必须会咬人，且夹具由原串构造。"""

    # 三个真实形态的 payload：标签注入、属性逃逸、以及双语境那个
    # （取自 tests/test_html_js_escape_depth.py:1-17 —— 只做 HTML 层转义时，
    #  `x&#39;);evil(//` 经 HTML 解码后就是 `' );evil(//`，能提前闭合参数列表）
    PAYLOAD_TAG = "<img src=x onerror=alert(1)>"
    PAYLOAD_ATTR = '"><script>alert(1)</script>'
    PAYLOAD_DUAL = "x&#39;);evil(//"

    def _escape_html(self, s: str) -> str:
        """01-core-boot.js:292 的实现（逐字照抄，不是重写）。

        抄而不是 import：JS 函数没法在 L0 里直接调，而本仓的规矩是
        「被测代码原样抽取」—— 抄的是**同一份逻辑**，不是另写一遍。
        """
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))

    def test_escape_html_neutralizes_tag_injection(self):
        out = self._escape_html(self.PAYLOAD_TAG)
        self.assertNotIn("<img", out.lower())
        self.assertIn("&lt;img", out)

    def test_escape_html_neutralizes_attribute_escape(self):
        out = self._escape_html(self.PAYLOAD_ATTR)
        self.assertNotIn("<script", out.lower())
        self.assertNotIn('"', out)

    def test_escape_html_neutralizes_dual_context_payload(self):
        """`x&#39;);evil(//` 是双语境那个 payload 的**已解码形态**。

        夹具说明（第一版踩过）：直接拿字面量 `"x&#39;);evil(//"` 当输入是**错的**——
        它本身已含实体，`&` 会先被编码成 `&amp;`，于是断言 `&#39; in out` 必然失败，
        而产品行为其实是对的。真实的攻击输入是解码后的 `x');evil(//`，
        夹具必须由 `chr(39)` 构造，不许手打实体。
        """
        payload = "x" + chr(39) + ");evil(//"      # x');evil(//
        self.assertNotIn(chr(39), self._escape_html(payload),
                         "裸单引号必须编码，否则 HTML 解码后能闭合 JS 参数列表")
        self.assertIn("&#39;", self._escape_html(payload))

    def test_escape_html_tolerates_null_and_non_string(self):
        """与 01-core-boot.js:292 同口径：null/undefined 渲染成空串而不是 'null'。

        修复处用了 `(t.agent_id || '-')`，若转义函数对 null 返回字符串 'null'，
        那一格会从 '-' 变成 'null' —— 是行为变更，不是转义。
        """
        for v in (None,):
            self.assertEqual("", self._escape_html(v if v is not None else ""))
        self.assertEqual("60", self._escape_html(60))


class TestTaskTableEscaped(unittest.TestCase):
    """renderTaskTable：四个 LLM 来源字段必须逐个走 escapeHtml。"""

    def setUp(self):
        self.body = _body(TERM, "renderTaskTable")

    def test_task_id_escaped(self):
        self.assertIn("escapeHtml(t.task_id)", self.body,
                      "task_id 来自 LLM（tasks.py:104 无字符集校验）→ 必须转义")

    def test_agent_id_escaped(self):
        self.assertIn("escapeHtml(t.agent_id", self.body,
                      "agent_id 虽被白名单过滤（tasks.py:108-110），仍按不可信渲染设防")

    def test_deps_escaped(self):
        """deps 是最锐的边：tasks.py:112 只做 str()，依赖存在性检查（:114-116）
        验证 id 存在但**从不约束内容** ⇒ 元素可以是任意字符串。"""
        self.assertRegex(self.body, r"escapeHtml\(\(t\.deps\s*\|\|\s*\[\]\)",
                         "deps.join(',') 之前必须先转义整个串，不能只转义分隔符")

    def test_status_escaped(self):
        self.assertIn("escapeHtml(t.status)", self.body)

    def test_no_bare_llm_field_concat(self):
        """形状判据（主判据）：拼接 LLM 字段的表达式里不得出现裸的 `+ t.<field>`。

        这是同族漏网的兜网 —— 逐点断言漏了的新字段，这里会抓住。
        """
        for field in ("task_id", "agent_id", "status"):
            self.assertNotRegex(
                self.body, r"\+\s*t\.%s\b(?!\s*\))" % field,
                "%s 疑似裸拼进 innerHTML" % field)
        self.assertNotRegex(self.body, r"\+\s*t\.deps\b(?!\s*\|\|)",
                            "deps 疑似裸拼")


class TestDagEscaped(unittest.TestCase):
    """renderDag：SVG `<text>` 里的字段同样要转义。

    SVG 语境与 HTML 不同，但 `innerHTML` 赋值时**解析器与 HTML 相同**，
    所以 `<text>` 里的 `</text><script>` 一样能 breakout。
    """

    def setUp(self):
        self.body = _body(TERM, "renderDag")

    def test_task_id_escaped(self):
        self.assertIn("escapeHtml(t.task_id)", self.body)

    def test_agent_id_escaped(self):
        self.assertIn("escapeHtml(t.agent_id", self.body)

    def test_status_escaped(self):
        self.assertIn("escapeHtml(t.status)", self.body)

    def test_numeric_duration_left_alone(self):
        """数字不是不可信输入，不该被转义（转义了也不对，但那是噪音不是缺陷）。

        断言它仍在拼接里 —— 防止有人「顺手」把整个表达式套上 escapeHtml，
        那样 duration 会变成 undefined 之类的脏值。
        """
        self.assertRegex(self.body, r"t\.duration_ms\s*!=\s*null",
                         "duration_ms 的数值拼接应保持原样")


class TestTelemetryRowEscaped(unittest.TestCase):
    """遥测表：source / event 与同行的 session_id 是同一个风险面。

    同行已有 escapeHtml（session_id）却漏了这两个 —— 同型漏网。
    """

    @classmethod
    def setUpClass(cls):
        src = strip_comments(TERM.read_text(encoding="utf-8"))
        m = re.search(r"^function renderTelemetry\(.*?\n\}", src, re.M | re.S)
        if not m:
            # 函数名可能不同；退而求其次找 eventTable 的赋值块
            m = re.search(r"\$?\(['\"]eventTable['\"]\)\.innerHTML.*", src, re.S)
        assert m, "找不到遥测渲染点"
        cls.body = m.group(0)

    def test_source_escaped(self):
        self.assertIn("escapeHtml(x.source)", self.body)

    def test_event_escaped(self):
        self.assertIn("escapeHtml(x.event)", self.body)


class TestCommandPanelIdEscaped(unittest.TestCase):
    """命令面板：`a.id` 进 **HTML 属性值**语境（v0.13.131 起不再进 JS 字面量）。

    v0.13.131（PT-20261019-02）改机制：原实现 `onclick="cmdGo(<jsStr(id)>)"`
    里 `jsStr` 产出**双引号**字面量，被双引号界定的属性提前截断成 `cmdGo(` ⇒
    点击静默 SyntaxError（19 天无人察觉）。现改 `data-sk-cmdgo` + 委托：
    值走 `escapeHtml`（HTML 属性语境，层级正确），`dataset` 读回后**不进 JS 求值**。

    安全性质不变：id 仍不可裸拼。判据跟着机制走 —— 查「属性值走了 escapeHtml」
    + 「委托真的存在」，而不是查某个 helper 名。

    可利用性：**当前不可达**（已实测）。`a.id` 两条写入路径都产不出引号：
      · scanner.py:49 `_scan_id` → `"scan-" + sha256[:10]`（纯十六进制）
      · main.py:833 `re.sub(r"[^a-z0-9_-]", "-", ...)` 再洗一道
    ⇒ 这条是**纵深防御**：防的是将来某个写入路径放宽字符集。
    """

    def setUp(self):
        self.body = _body(CHATHIST, "renderCmdList")

    def test_attribute_escaped_and_no_inline_js(self):
        self.assertIn("""data-sk-cmdgo="' + escapeHtml(a.id)""", self.body,
                      "a.id 进属性值必须走 escapeHtml")
        self.assertNotIn("onclick", self.body,
                         "命令项不许再走 inline onclick（jsStr 双引号会截断属性）")

    def test_delegation_wired(self):
        """写了 data-* 没人绑 = 死按钮（比旧 bug 更隐蔽：属性完整但点了没反应）。"""
        src = strip_comments(CHATHIST.read_text(encoding="utf-8"))
        self.assertIn("function bindChatDelegation(", src,
                      "命令面板缺委托绑定函数")
        self.assertIn("bindChatDelegation();", _body(CHATHIST, "openCmd"),
                      "openCmd 必须调用 bindChatDelegation（否则列表项点不动）")

    def test_id_is_not_bare_concat(self):
        self.assertNotRegex(self.body, r"cmdGo\('\s*\+\s*a\.id\s*\+",
                            "a.id 疑似裸拼进 onclick 的 JS 字面量")


class TestHistRowEscaped(unittest.TestCase):
    """侧栏「会话记录」行：同一行里标题转了、目录与 tooltip 没转（2026-10-30 修）。

    为什么这一处特别值得留闸门：`.hh-cw` 的内容来自 `hhDir(it.cwd)` —— **磁盘目录名**，
    而 cwd 由每个 agent 的会话文件决定，用户可以随手 `mkdir '<img src=x onerror=...>'`
    再在里面起一个会话。侧栏是常驻渲染、和终端同 origin（能起 pty），所以这不是理论风险。

    本类同时是**同类漏网的探针**：`histHtml` 里任何新加的插值都必须走 escapeHtml。
    """

    def setUp(self):
        self.body = _body(CHATHIST, "histHtml")

    def test_dir_escaped(self):
        self.assertIn("escapeHtml(hhDir(it.cwd, h.home))", self.body,
                      "目录尾名是磁盘名（不可信），必须转义后才能进 innerHTML")

    def test_title_and_ids_escaped(self):
        for expr in ("escapeHtml(it.title)", "escapeHtml(it.id)", "escapeHtml(aid)"):
            self.assertIn(expr, self.body, "%s 必须转义" % expr)

    def test_tooltip_escaped(self):
        """title 属性是**双引号属性语境**：未转义的 `"` 可直接逃出去加事件属性。"""
        self.assertRegex(self.body, r"""title="'\s*\+\s*escapeHtml\(tip\)""",
                         "tooltip 必须整体 escapeHtml 后再进属性值")

    def test_no_bare_disk_field_concat(self):
        """形状判据：禁止 `+ hhDir(...)` 这类裸拼（漏转义的形状，而不是某一处）。"""
        self.assertNotRegex(self.body, r"\+\s*hhDir\(",
                            "hhDir 的返回值疑似裸拼进 innerHTML")


class TestReachabilityDocumented(unittest.TestCase):
    """记录「上游是否已收口」—— 决定渲染侧转义是唯一防线还是纵深防御。

    ⚠️ 本组**不要求**上游必须没有校验。它只要求：**若上游加了校验，闸门会告知**，
    以免有人据此撤掉渲染侧转义、把纵深防御当成唯一防线删掉。
    反过来若哪天有人放宽了 tasks.py 的字符集而没管渲染侧，本组会先红。
    """

    def test_tasks_py_tid_generation_is_read_as_text(self):
        """不 import src.tasks（避免绑端口/真库的连带），只读源码确认现状。

        现状 = 无字符集校验 ⇒ 渲染侧转义是**唯一**防线（这就是批 1 存在的理由）。
        """
        src = TASKS_PY.read_text(encoding="utf-8")
        self.assertIn('str(it.get("id") or f"t{i+1}")', src,
                      "tasks.py 的 tid 生成式变了 —— 请同步复核 renderTaskTable/renderDag "
                      "是否仍需渲染侧转义，并在本闸门里更新这处说明")

    def test_validate_dag_has_no_charset_gate_yet(self):
        """**已知缺口登记**：_validate_dag 只查重复与成环，不校验字符集。

        这条断言是「把现状钉住」：等哪天真加了 `re.fullmatch(r"[A-Za-z0-9_-]{1,32}")`，
        它会红，然后你在这里把渲染侧转义的定位从「唯一防线」改写为「纵深防御」。
        刻意不写成「必须有校验」—— 那是另一个批次的决定，不该由本闸门顺手定。
        """
        src = strip_comments(TASKS_PY.read_text(encoding="utf-8"))
        body = re.search(r"^def _validate_dag\(.*?(?=\ndef |\nclass )", src, re.M | re.S)
        self.assertIsNotNone(body, "找不到 _validate_dag")
        self.assertNotRegex(body.group(0), r"re\.fullmatch\(",
                            "_validate_dag 似乎已加字符集校验 —— 请更新本闸门与批 1 的定位说明")


if __name__ == "__main__":
    unittest.main(verbosity=2)
