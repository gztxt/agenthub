"""L0 · hub.js 拆分必须**逐字等价**（零行为变更的可证明形式）。
   拆的是源码组织，产物形态不变：模板 /static/hub.js、缓存键、前端探针读的文件都不动
   ⇒ 手机端零风险、不需要重启。任何人只改了 part 或只改了 hub.js，这里立刻红。"""
import hashlib, pathlib, re, unittest
REPO = pathlib.Path(__file__).resolve().parents[1]
PARTS = sorted((REPO / "static" / "hub").glob("[0-9][0-9]-*.js"))


class T(unittest.TestCase):
    def test_parts_exist(self):
        self.assertGreaterEqual(len(PARTS), 4, "part 少于 4 个说明拆分没落地")

    def test_concat_equals_served_file(self):
        cat = "".join(p.read_text(encoding="utf-8") for p in PARTS)
        served = (REPO / "static" / "hub.js").read_text(encoding="utf-8")
        self.assertEqual(hashlib.md5(cat.encode()).hexdigest(),
                         hashlib.md5(served.encode()).hexdigest(),
                         "拼接 != static/hub.js —— 请跑 scripts/build_hubjs.sh 重新生成")

    def test_no_part_is_empty_or_reordered(self):
        for p in PARTS:
            self.assertTrue(p.read_text(encoding="utf-8").strip(), f"{p.name} 是空的")
            self.assertRegex(p.name, r"^\d\d-[a-z0-9-]+\.js$", f"{p.name} 命名不合规范（前缀序号是顺序的唯一依据）")


class TestLadder(unittest.TestCase):
    """端侧自检面板的「符号阶梯」清单不许过期（2026-10-30 用户报障的根因）。

    阶梯的用途只有一个：顶层 function 声明整体提升 ⇒ 某个符号 undefined 只能是**响应体被
    截断**。它一旦列了非顶层声明（改写成内嵌函数 / 局部 const / async 之外的新形态），
    绿况下那几项就恒为 0，断言的意思从「响应体截断」漂成「清单没跟上代码」——
    用户侧看到的是一条永远红、却查不出东西的判据（syncOverlayMask / tick / loadAgents
    三项就这样躺了多个版本）。
    这里把「不许过期」变成可机械核对的量：逐项在**产物**里找顶层声明、按行号升序、
    首尾覆盖文件的头与尾。清单漂了，推送前的 L0 就红，不会等到端侧才显形。
    """
    SRC = (REPO / "templates" / "index.html").read_text(encoding="utf-8")
    HUB = (REPO / "static" / "hub.js").read_text(encoding="utf-8")
    LINES = HUB.splitlines()

    def _ladder(self):
        m = re.search(r"'符号阶梯='\s*\+\s*\[(.*?)\]", self.SRC, re.S)
        self.assertIsNotNone(m, "模板里找不到 '符号阶梯=' + [...] 清单")
        return re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", m.group(1))

    def test_every_ladder_symbol_is_a_toplevel_declaration(self):
        names = self._ladder()
        self.assertGreaterEqual(len(names), 8, "阶梯少于 8 项就没有覆盖力")
        bad = []
        for n in names:
            pat = re.compile(r"^(?:async\s+)?function\s+%s\s*\(" % re.escape(n), re.M)
            if not pat.search(self.HUB):
                bad.append(n)
        self.assertEqual(bad, [], "阶梯里这些符号不是顶层 function 声明（端侧恒为 0，"
                                 "等于把「响应体被截断」判据污染成假红）：%s" % bad)

    def test_ladder_is_ordered_and_spans_head_to_tail(self):
        names = self._ladder()
        pos = []
        for n in names:
            pat = re.compile(r"^(?:async\s+)?function\s+%s\s*\(" % re.escape(n), re.M)
            m = pat.search(self.HUB)
            self.assertIsNotNone(m, n)
            pos.append(self.HUB[:m.start()].count("\n") + 1)   # 1 起行号
        self.assertEqual(pos, sorted(pos),
                         "阶梯必须按声明行升序：首个 0 才是响应体的切断位置，乱序会读错截断点")
        total = len(self.LINES)
        self.assertLess(pos[0], total * 0.01, "首项必须在文件最前 1%%内（否则头部截断查不出）")
        self.assertGreater(pos[-1], total * 0.95, "末项必须在文件最后 5%%内（否则尾部截断查不出）")
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        self.assertLess(max(gaps), total * 0.45, "阶梯有超过 45%% 的空档，中间截断会漏检")


if __name__ == "__main__":
    unittest.main(verbosity=2)
