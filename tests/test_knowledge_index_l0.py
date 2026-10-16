"""README「知识索引」的**指针有效性**闸门（PT-20260927-14 / P3）。

**为什么需要它**：README 里那张索引表是**手写**的，而 `技术文档/agent-knowledge/`
不在本仓内（本机工作区）。人一懒/一挪，编号就会指向不存在的文件，而**没有任何东西会红**——
这正是本仓反复治理的「静默失效」家族（见 `agent-knowledge/41`「四类静默失效」）。

更要说清的是**这条闸门能验什么、不能验什么**：

- ✅ 能验：索引里点名的每个编号，在 `AGENT_KB_DIR` 下**确有对应 .md**。
- ❌ **不能**验：那个文件里讲的判据是否仍与本机现状一致。索引会腐化，**判据也会过期**。
  所以 README 的索引节里必须同时写明「结论绑定取证日期，以代码与实测为准」——
  这句话不是客套，它是本闸门**结构上无法覆盖的那一半风险**的人工对冲。

`AGENT_KB_DIR` 默认取`技术文档/agent-knowledge`（相对仓父目录）；不存在时**整类skip**——
这是本仓 L0 分层约定里 `host_only` 的正当用法（见 `agent-knowledge/151`：
L0 依赖仓外未跟踪文件需要 skip 是可以的，但**必须归 host_only**，不能混进 L0 骗绿）。
"""

import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 仓外知识库根。允许 env 覆盖（测试可指向fixture 目录）。
KB_DIR = Path(os.getenv("AGENT_KB_DIR",
                        str(REPO.parent / "agent-knowledge")))

#: README 索引节里每行形如 `| **主题** | 157 静态服务深化（…）｜158 组装…`。
#: 分隔符（表格 `|`、全角 `｜`）与编号之间**允许空白** —— 真实行就是 `| 157 …`。
#: ⚠️ 第一版写成 `(?:^|[｜|])(\d{2,3})`，漏掉了分隔符后的空格 ⇒ **每行第一个编号
#: 全都没被校验**（红向自证时发现：塞一个不存在的 999 仍然全绿）。这类「悄悄少校验
#: 一部分」的洞只有在**故意破坏**时才看得见，所以红向自证不是形式主义。
#: 负向先行 `(?!\d)` 防止把 `1588` 之类截成 `158`。
_ROW = re.compile(r"(?:^|[｜|])\s*(\d{2,3})\s+(?!\d)")


def _index_section() -> str:
    """取README 的「## 知识索引」到下一个 `## ` 之间的正文。

    只扫这一节：README 其它地方出现的数字（版本号、端口、行数）不是索引条目。
    """
    txt = (REPO / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^## 知识索引\s*$(.*?)(?=^## )", txt, re.S | re.M)
    if not m:
        return ""
    return m.group(1)


class TestKnowledgeIndexResolves(unittest.TestCase):
    """索引里的每个编号都必须指向真实文件。"""

    def test_index_section_exists(self):
        self.assertTrue(_index_section().strip(),
                        "README 里没有「## 知识索引」节 —— 仓内对agent-knowledge "
                        "零指针，这正是 PT-20260927-14 要补的缺口")

    def test_every_indexed_number_resolves(self):
        if not KB_DIR.is_dir():
            self.skipTest("AGENT_KB_DIR 不存在（%s）—— host_only，本机仓形态才断言"
                          % KB_DIR)
        body = _index_section()
        self.assertTrue(body, "索引节为空")
        rows = [ln for ln in body.splitlines() if ln.strip().startswith("|")]
        self.assertGreaterEqual(len(rows), 5, "索引表看起来没内容（%d 行表）" % len(rows))

        missing = []
        checked = 0
        for ln in rows:
            # 表头/分隔行要**精确**判。首版写的是 `if "篇目" in ln or "---" in ln`，
            # 后果是任何行内出现「篇目」二字的正文行被整行跳过 ⇒ 它列的编号一个都
            # 没被校验，而测试仍全绿。**这类洞只有红向自证看得见**（见文件头）。
            stripped = ln.strip()
            if stripped.startswith("|---") or stripped == "| 主题 | 篇目 |":
                continue
            for num in _ROW.findall(ln):
                checked += 1
                hits = list(KB_DIR.glob("%s-*.md" % num))
                if not hits:
                    missing.append(num)
        self.assertGreater(checked, 0, "一条编号都没抽到 —— 正则失效，"
                                       "这会让本闸门变成恒真的空跑")
        self.assertEqual(missing, [], "README 索引指向不存在的篇目：%s" % missing)

    def test_history_file_carries_original_text(self):
        """`docs/HISTORY-features.md` 是 README 瘦身时迁出的原文 —— 必须在，
        且必须指向 CHANGELOG（迁出不是删除）。"""
        p = REPO / "docs" / "HISTORY-features.md"
        self.assertTrue(p.exists(), "逐版本流水丢了 —— README 瘦身不得等于删档")
        txt = p.read_text(encoding="utf-8")
        self.assertIn("CHANGELOG.md", txt.split("---")[0],
                      "迁出说明里必须指明版本细节的正式去处是 CHANGELOG")
        # 原文体量：迁出前那段是 165 行，少于 100 行说明被截断了
        self.assertGreaterEqual(len(txt.splitlines()), 150)

    def test_readme_is_not_bloated_again(self):
        """README 保持在「速查」量级。超过 200 行说明流水账又被搬回来了。"""
        n = len((REPO / "README.md").read_text(encoding="utf-8").splitlines())
        self.assertLess(n, 200, "README %d 行 —— 逐版本流水账被搬回来了？" % n)

    def test_python_version_requirement_stated(self):
        """外部评估说对的一条：README 没写 Python 版本要求。"""
        self.assertIn("Python 3", (REPO / "README.md").read_text(encoding="utf-8"))


class TestRequirementsLocked(unittest.TestCase):
    """PT-20260927-13 / P4：`requirements.txt` 必须是**锁**版本，不是裸包名。

    理由：无 `pyproject.toml`/`uv.lock`，重建 venv 就靠这一个文件 ⇒ 裸包名意味着
    「某天重建被 breaking change 炸掉，且无人察觉」（NAS 无 CI 兜底）。
    """

    def test_every_direct_dep_pinned(self):
        req = REPO / "requirements.txt"
        self.assertTrue(req.exists())
        pins, bare = [], []
        for raw in req.read_text(encoding="utf-8").splitlines():
            ln = raw.strip()
            if not ln or ln.startswith("#") or ln.startswith("﻿"):
                continue
            if "==" in ln:
                pins.append(ln.split("==")[0].strip().lower())
            else:
                bare.append(ln)
        self.assertGreaterEqual(len(pins), 9, "直接依赖应至少 9 条")
        self.assertEqual(bare, [], "未锁版本的依赖：%s —— 重建 venv 会装到未知版本" % bare)

    def test_ctl_has_token_subcommand(self):
        """P5：随机 token 只 print 前 4 位到 journald ⇒ 首次接线得去挖日志。"""
        txt = (REPO / "agent-hubctl.sh").read_text(encoding="utf-8")
        # (?m)：assertRegex 默认不开 MULTILINE，`^` 只会匹配整个字符串开头
        self.assertRegex(txt, r"(?m)^\s*token\)", "ctl 缺 token 子命令")
        # 只读：不得写 .env / 不得 systemctl restart
        body = txt.split("\n  token)", 1)[1].split("\n  *)", 1)[0]
        for forbidden in ("systemctl --user restart", "systemctl --user start",
                          "tee ", "> $APP_DIR/.env"):
            self.assertNotIn(forbidden, body,
                             "token 子命令必须是**只读**的，不该出现 %r" % forbidden)


if __name__ == "__main__":
    unittest.main()