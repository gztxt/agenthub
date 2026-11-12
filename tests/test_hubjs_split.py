"""L0 · hub.js **双轨闸门**（2026-11-12 T5 裁定，替代原「只逐字节」单轨）。

两条轨道防的是不同故障，互不替代，所以是并存不是替换：

- 轨道 A（逐字节）：分片按序拼接必须逐字节等于 `static/hub.js`。防「产物漂移」——
  改了分片忘跑 `build_hubjs.sh`、或产物被手改。它**抓不到**少一个导出 / 依赖指错模块。
- 轨道 B（模块图）：分片序列就是模块清单，每个分片的顶层声明就是导出面，
  `import ... from './NN-*.js'` 构成依赖边。防「结构/契约漂移」——模块消失、导出丢失、
  依赖指向不存在的模块、依赖成环。它**抓不到**某行被误删一个字符。

T5 全量 ESM 化后两条都继续有效：`DECL_RE` 认 `export function` / `export const` 前缀，
`IMPORT_RE` 自动开始产出依赖边；轨道 A 仍守着作为**长期兼容契约**保留的
`/static/hub.js` 聚合产物（见 docs/REFLEX-FRONTEND-EVAL-20261112.md §6）。

反假绿纪律（照 tests/test_ls_guard.py 的 R5）：轨道 B 在现状「0 条 import」下，
依赖边判据的循环体不执行，属**条件不变量**而非空转 —— 另有 4 条非空判据在跑
（清单 / 导出面 / 唯一性 / 解析器自检），且 `test_parser_understands_esm_syntax`
与 `test_graph_helpers_catch_bad_imports` 用自造样本先验工具，证明解析器真认得
ESM 语法、图算法真能判出环与坏目标。T5 一旦加入 import，依赖边判据立即生效。
"""
import collections
import hashlib
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PARTS = sorted((REPO / "static" / "hub").glob("[0-9][0-9]-*.js"))
SERVED = REPO / "static" / "hub.js"
TPL = REPO / "templates" / "index.html"

# 顶层声明 = 该模块的「导出面」。认 export 前缀是为了 T5 之后不用改闸门。
DECL_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
    r"|^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M)
IMPORT_RE = re.compile(
    r"^\s*import\s+(?:[^;'\"]*?\s+from\s+)?['\"]\./([0-9][0-9]-[a-z0-9-]+\.js)['\"]",
    re.M)

# 2026-11-12 实测：17 个跨分片重名里的既存 4 个（LP / ghStars / lpStars / resLoaded）。
# 纯拼接时代它们靠「后声明覆盖前声明」共存；T5 模块作用域后各自独立，属合法。
# 按白名单制不顺手修：这里只钉「不许新增」，允许日后减少（修好了就删这个集合）。
KNOWN_CROSS_DUPS = {"LP", "ghStars", "lpStars", "resLoaded"}


def _src(p):
    return p.read_text(encoding="utf-8")


def _exports(text):
    return [m.group(1) or m.group(2) for m in DECL_RE.finditer(text)]


def _imports(text):
    return IMPORT_RE.findall(text)


def _unresolved(imports, known):
    return sorted(set(imports) - set(known))


def _has_cycle(edges):
    """三色 DFS：True 表示有环。"""
    color = {}

    def visit(n):
        color[n] = 1
        for m in edges.get(n, ()):
            if color.get(m) == 1:
                return True
            if color.get(m, 0) == 0 and visit(m):
                return True
        color[n] = 2
        return False

    return any(visit(n) for n in edges if color.get(n, 0) == 0)


MODULE_SRC = collections.OrderedDict((p.name, _src(p)) for p in PARTS)
MODULE_NAMES = list(MODULE_SRC)
# 全局拼接行号 = 前面所有分片行数之和 + 模块内行号。轨道 A 保证该拼接逐字节等于产物，
# 所以按分片定位与按产物定位逐位一致；但按分片定位不依赖「单文件行号轴」（T5 后消失）。
OFFSETS = {}
TOTAL_LINES = 0
for _name, _text in MODULE_SRC.items():
    OFFSETS[_name] = TOTAL_LINES
    TOTAL_LINES += len(_text.splitlines())


# ══════════════════════════════════════════════════════════════
#  轨道 A · 逐字节（原样保留，一条不删）
# ══════════════════════════════════════════════════════════════
class T(unittest.TestCase):
    def test_parts_exist(self):
        self.assertGreaterEqual(len(PARTS), 4, "part 少于 4 个说明拆分没落地")

    def test_concat_equals_served_file(self):
        cat = "".join(p.read_text(encoding="utf-8") for p in PARTS)
        served = SERVED.read_text(encoding="utf-8")
        self.assertEqual(hashlib.md5(cat.encode()).hexdigest(),
                         hashlib.md5(served.encode()).hexdigest(),
                         "拼接 != static/hub.js —— 请跑 scripts/build_hubjs.sh 重新生成")

    def test_no_part_is_empty_or_reordered(self):
        for p in PARTS:
            self.assertTrue(p.read_text(encoding="utf-8").strip(), f"{p.name} 是空的")
            self.assertRegex(p.name, r"^\d\d-[a-z0-9-]+\.js$", f"{p.name} 命名不合规范（前缀序号是顺序的唯一依据）")


# ══════════════════════════════════════════════════════════════
#  轨道 B · 模块图（新增）
# ══════════════════════════════════════════════════════════════
class TestModuleGraph(unittest.TestCase):
    """模块清单 / 导出面 / 依赖边。判据随 T5 平滑生效，不阻断施工。"""

    # —— 先验工具，再拿工具验代码（防解析器空转）——
    def test_parser_understands_esm_syntax(self):
        sample = ("export function foo() {}\n"
                  "export const bar = 1;\n"
                  "async function baz() {}\n"
                  "import { x } from './02-nav-and-poll.js';\n"
                  "import './03-agents-cards.js';\n")
        self.assertEqual(_exports(sample), ["foo", "bar", "baz"],
                         "解析器认不出 export/async 前缀 ⇒ T5 后导出面判据会静默失效")
        self.assertEqual(_imports(sample),
                         ["02-nav-and-poll.js", "03-agents-cards.js"],
                         "解析器认不出 import ⇒ T5 后依赖边判据会静默失效")

    def test_graph_helpers_catch_bad_imports(self):
        self.assertEqual(_unresolved(["99-nope.js"], MODULE_NAMES), ["99-nope.js"],
                         "坏目标没被判出 ⇒ 依赖边判据没有拦截能力")
        self.assertTrue(_has_cycle({"a": {"b"}, "b": {"a"}}), "两节点环没被判出")
        self.assertFalse(_has_cycle({"a": {"b"}, "b": set()}), "无环图被误判成有环")

    # —— 真代码上的非空判据 ——
    def test_module_list_is_stable(self):
        self.assertGreaterEqual(len(MODULE_NAMES), 4, "模块清单过短")
        self.assertEqual(len(MODULE_NAMES), len(set(MODULE_NAMES)), "模块文件名重复")
        for name in MODULE_NAMES:
            self.assertRegex(name, r"^\d\d-[a-z0-9-]+\.js$", f"{name} 命名不合规范")
            self.assertTrue(MODULE_SRC[name].strip(), f"{name} 是空的")

    def test_every_module_has_exports(self):
        empty = [name for name, text in MODULE_SRC.items() if not _exports(text)]
        self.assertEqual(empty, [], "这些分片没有顶层声明 ⇒ 模块为空或抽取器失效：%s" % empty)

    def test_export_surface_size_and_uniqueness(self):
        all_names = []
        for name, text in MODULE_SRC.items():
            exports = _exports(text)
            self.assertEqual(len(exports), len(set(exports)),
                             "%s 模块内声明重名（ESM 下同一模块重复导出会直接报错）" % name)
            all_names.extend(exports)
        self.assertGreaterEqual(len(all_names), 500,
                               "顶层声明总数 %d < 500 ⇒ 分片被截断或抽取器失效" % len(all_names))
        counts = collections.Counter(all_names)
        cross = {n for n, c in counts.items() if c > 1}
        self.assertTrue(cross <= KNOWN_CROSS_DUPS,
                        "出现新的跨分片重名 %s（已知既存集合 %s 只允许减少、不许新增）"
                        % (sorted(cross - KNOWN_CROSS_DUPS), sorted(KNOWN_CROSS_DUPS)))

    def test_import_targets_resolve_and_graph_is_acyclic(self):
        """条件不变量：现状 0 条 import 时循环体不执行，但**不是空转**——
        上面 4 条非空判据在跑，且两条工具自检已证明解析器/图算法在自造 ESM
        样本上真能工作。T5 加入 import 后本判据立即开始拦截坏目标与环。"""
        edges = {}
        for name, text in MODULE_SRC.items():
            deps = _imports(text)
            self.assertEqual(_unresolved(deps, MODULE_NAMES), [],
                             "%s 的 import 指向了清单外的模块" % name)
            edges[name] = set(deps)
        self.assertFalse(_has_cycle(edges), "模块依赖图成环：%s" % edges)


# ══════════════════════════════════════════════════════════════
#  符号阶梯 · 从「单文件行号轴」改按「模块序号 + 模块内行号」
# ══════════════════════════════════════════════════════════════
class TestLadder(unittest.TestCase):
    """端侧自检面板的「符号阶梯」清单不许过期（2026-10-30 用户报障的根因）。

    阶梯的用途只有一个：顶层 function 声明整体提升 ⇒ 某个符号 undefined 只能是**响应体被
    截断**。它一旦列了非顶层声明（改写成内嵌函数 / 局部 const / async 之外的新形态），
    绿况下那几项就恒为 0，断言的意思从「响应体截断」漂成「清单没跟上代码」——
    用户侧看到的是一条永远红、却查不出东西的判据。

    T5 改造：原来在 `static/hub.js` 单文件里按行号定位，而 ESM 化后单文件行号轴消失
    ⇒ 改在**分片序列**里按「全局拼接行号」定位（OFFSETS + 模块内行号）。轨道 A 保证
    拼接逐字节等于产物，所以两种定位逐位一致，截断检测语义不变。
    """
    SRC = TPL.read_text(encoding="utf-8")

    def _ladder(self):
        m = re.search(r"'符号阶梯='\s*\+\s*\[(.*?)\]", self.SRC, re.S)
        self.assertIsNotNone(m, "模板里找不到 '符号阶梯=' + [...] 清单")
        return re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", m.group(1))

    def _pos(self, name):
        """返回 (模块序号, 全局拼接行号)，找不到返回 None。认 export 前缀以兼容 T5。"""
        pat = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+%s\s*\(" % re.escape(name), re.M)
        for idx, mod in enumerate(MODULE_NAMES):
            m = pat.search(MODULE_SRC[mod])
            if m:
                return idx, OFFSETS[mod] + MODULE_SRC[mod][:m.start()].count("\n") + 1
        return None

    def test_every_ladder_symbol_is_a_toplevel_declaration(self):
        names = self._ladder()
        self.assertGreaterEqual(len(names), 8, "阶梯少于 8 项就没有覆盖力")
        bad = [n for n in names if self._pos(n) is None]
        self.assertEqual(bad, [], "阶梯里这些符号不是分片里的顶层 function 声明（端侧恒为 0，"
                                 "等于把「响应体被截断」判据污染成假红）：%s" % bad)

    def test_ladder_is_ordered_and_spans_head_to_tail(self):
        names = self._ladder()
        pos = [self._pos(n)[1] for n in names]
        self.assertEqual(pos, sorted(pos),
                         "阶梯必须按拼接行升序：首个 0 才是响应体的切断位置，乱序会读错截断点")
        self.assertLess(pos[0], TOTAL_LINES * 0.01, "首项必须在最前 1%%内（否则头部截断查不出）")
        self.assertGreater(pos[-1], TOTAL_LINES * 0.95, "末项必须在最后 5%%内（否则尾部截断查不出）")
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        self.assertLess(max(gaps), TOTAL_LINES * 0.45, "阶梯有超过 45%% 的空档，中间截断会漏检")


if __name__ == "__main__":
    unittest.main(verbosity=2)
