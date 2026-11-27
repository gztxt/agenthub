"""测试分层（tier）唯一真相源 —— 约定见 tests/README.md。

三层：
  L0 hermetic  tests/test_*.py 里没打 @host_only 的用例。零宿主依赖：不读真盘 ~/.claude、
               不起服务、不打网络、不 fork pty、不 import src.main。换任何机器（含干净 CI
               runner）结论必须一模一样，且**不许出现 SKIP**（出现即分层放错，闸门判 FAIL）。
  L1 host      tests/test_*.py 里打了 @host_only 的用例。断言依赖本机真实仓库形态
               （~/.grok/sessions、~/.claude/projects、~/.jcode/sessions、~/.qoder/projects、
               ~/.hermes/state.db、~/.codex/state_5.sqlite，以及 /fs 下的真目录）。
               换机时给**显式 SKIP + 理由**，绝不静默通过。
  L2 live      tests/verify_*.py 与 tests/probe_*.py —— 需要服务在跑，天然不被
               `unittest discover -p "test_*.py"` 收进来。单跑方式见 README。

跑法（仓根）：
  HUB_HOST_TESTS=0 venv/bin/python -m unittest discover -s tests -p "test_*.py" -v   # L0
  HUB_HOST_TESTS=1 venv/bin/python -m unittest discover -s tests -p "test_*.py" -v   # L0+L1
  bash scripts/run_tests.sh hermetic|all|probe <file>                                # 同一套口径的封装
"""
from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

#: L1 判据：这六个都在，才算「像本机」——缺任何一个，对应那家仓库的断言就不可判定
HOST_REPOS = (".grok/sessions", ".claude/projects", ".jcode/sessions",
              ".qoder/projects", ".hermes/state.db", ".codex/state_5.sqlite")

#: SKIP 理由必须自带因果与解法（-v 下逐条打印，闸门汇总里也报数）
HOST_SKIP_REASON = (
    "SKIP(host-dependent): 本用例断言本机真实仓库形态（~/.grok/sessions ~/.claude/projects "
    "~/.jcode/sessions ~/.qoder/projects ~/.hermes/state.db ~/.codex/state_5.sqlite 及 /fs 真目录），"
    "换机（干净 runner）上既不能算通过也不能算失败 ⇒ 显式跳过。"
    "要在本机强制运行：HUB_HOST_TESTS=1；要跳过：HUB_HOST_TESTS=0。")


def host_like() -> bool:
    """当前环境是否具备跑 L1 的条件。HUB_HOST_TESTS 显式覆盖自动探测。"""
    flag = (os.getenv("HUB_HOST_TESTS") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    home = Path.home()
    return all((home / p).exists() for p in HOST_REPOS)


#: 打在 class 或 method 上：@tiers.host_only
#: 除 unittest 的 skip 外，再打一个自己的小旗 —— 否则“哪些用例是 host”这件事
#: 只能从 skip 输出里猜，而 L0 零跳过这道闸就没法机器判。
def host_only(obj):
    obj = unittest.skipUnless(host_like(), HOST_SKIP_REASON)(obj)
    try:
        obj.__hub_host_only__ = True
    except (AttributeError, TypeError):        # 不可写属性的对象（少见）：宁可不 tagging 也不报错
        pass
    return obj


def is_host(obj) -> bool:
    return bool(getattr(obj, "__hub_host_only__", False))


def missing_host_paths() -> list:
    """给闸门脚本报告「为什么跳过 L1」用，只报路径存在性，不读内容。"""
    home = Path.home()
    return [str(home / p) for p in HOST_REPOS if not (home / p).exists()]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# ── 收集器对账（2026-10-02，PT-20261002-10）──────────────────────────────
#: 事故形态：闸门写成**模块级 pytest 风格** `def test_x()` ⇒ `unittest discover`
#: **收不到** ⇒「标准套件全绿」与「新闸门真的跑过」变成两件事（本批实测：
#: `pytest tests/` 975 vs `run_tests.sh all` 961，差的 14 条正是这种写法）。
#: 下面三个函数把这条口径变成机器判：① 写出来的必须收得进 ② 每个文件都得有份
#: ③ 两个收集器例数对得上（换个 runner 不掉例）。同族前例：vitals_loop 函数头丢失、
#: 前端 TDZ、test_asset_audit 的缩进错位 —— 都是「代码存在 ≠ 会被执行」。

STANDARD_PATTERN = "test_*.py"


def standard_test_files(root: Path | None = None) -> list:
    """标准层（L0+L1）的测试文件清单。"""
    d = (root or repo_root()) / "tests"
    return sorted(p for p in d.glob(STANDARD_PATTERN) if p.is_file())


def _base_name(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return ""


def module_level_test_functions(root: Path | None = None) -> list:
    """标准层里 **unittest discover 收不到** 的 test_ 函数。

    判据只管**模块顶层的** ``def test_*`` —— 那种写法 pytest 收、unittest 不收，
    无条件成立（2026-10-02 本批事故形态：14 条 ⇒ 961 vs 975）。

    ★ 为什么**不**在语法上判「非 TestCase 类里的 test_*」：试过，**误报 216 条**。
    仓里的常规写法是 ``class TestTransport(TdaiBase)`` ——``TdaiBase`` 是本模块里
    另一个 ``unittest.TestCase`` 子类的别名，unittest 照收不误（实测 17/10/22 例）。
    基类别名一多，语法推断必然假红，而**假红的闸门比没有闸门更坏**（会逼人改断言）。
    类级差异改由**例数对账**兜（unittest vs pytest，只收不跑）——那是运行期事实，
    没有推断余地。嵌在函数里的那种由 ``tests/test_asset_audit.py`` 的
    ``TestGateSelfCheck`` 负责，三者合起来才封死。
    """
    bad = []
    for p in standard_test_files(root):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test_"):
                bad.append("%s:%d 模块级 %s()" % (p.name, node.lineno, node.name))
    return bad


def unittest_collected_count(root: Path | None = None) -> int:
    """unittest discovery 在标准层收进多少例。

    ⚠️ **只适合“没跑过任何用例”的进程**（单元自检、独立脚本）。
    全套跑完之后再调它，拿到的是**被前 4,300 例改过的 sys.modules/os.environ 之下**
    的 discovery 结果，而不是 runner 的收集口径 —— 2026-11-28 实测：同一条闸门在
    全量跑里 4420 ≠ pytest 4421，单独跑却 4421 = 4421。要跟 pytest 对账请用
    `collected_parity()`（两边都开子进程）。
    """
    d = (root or repo_root()) / "tests"
    return unittest.TestLoader().discover(str(d), pattern=STANDARD_PATTERN).countTestCases()


#: 两边都用**干净子进程**收集：进程内 discovery 会受已跑用例的 sys.modules/env 影响。
_UNIT_IDS_CODE = (
    "import sys, unittest;"
    "s = unittest.TestLoader().discover(r'%(tests)s', pattern='test_*.py');"
    "out = []\n"
    "def w(x):\n"
    "    for t in x:\n"
    "        if isinstance(t, unittest.TestSuite): w(t)\n"
    "        else: out.append(t.__class__.__module__ + '.' + t.__class__.__qualname__ + '.' + t._testMethodName)\n"
    "w(s)\n"
    "print(chr(10).join(sorted(out)))\n"
)


def unittest_collected_ids(root: Path | None = None) -> Tuple[set, str]:
    """`unittest discover` 的用例 id 集合（子进程，返回 (ids, 理由)）。"""
    import subprocess
    import sys
    base = root or repo_root()
    _t = str(base / "tests").replace("'", "\\'")
    code = _UNIT_IDS_CODE % {"tests": _t}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(base),
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return set(), "unittest 子进程 rc=%d: %s" % (r.returncode, (r.stderr or "")[-300:])
    ids = set(x.strip() for x in r.stdout.splitlines() if x.strip())
    return {x.split(".")[0] + "." + ".".join(x.split(".")[1:]) for x in ids}, ""


def pytest_collected_ids(root: Path | None = None) -> Tuple[set, str]:
    """`pytest --collect-only` 的用例 id 集合，归一化成 `mod.Class.method`。"""
    import subprocess
    import sys
    base = root or repo_root()
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
                       cwd=str(base), capture_output=True, text=True, timeout=300)
    out = set()
    for line in r.stdout.splitlines():
        if "::" not in line:
            continue
        f, rest = line.split("::", 1)
        mod = f.split("/")[-1]
        if mod.endswith(".py"):
            mod = mod[:-3]
        out.add(mod + "." + rest.replace("::", "."))
    if not out:
        return out, "pytest 收集不到任何用例（rc=%d）" % r.returncode
    return out, ""


def collected_parity(root: Path | None = None) -> Tuple[set, set, str]:
    """两个 runner 的收集集合差异（两边都是干净子进程）。返回 (only_unit, only_pytest, 理由)。"""
    u, why_u = unittest_collected_ids(root)
    p, why_p = pytest_collected_ids(root)
    why = "；".join(x for x in (why_u, why_p) if x)
    return u - p, p - u, why



def file_collected_count(fname: str, root: Path | None = None) -> int:
    """单个文件被 unittest 收进多少例（0 ⇒ 这个文件是死的）。"""
    d = (root or repo_root()) / "tests"
    return unittest.TestLoader().discover(str(d), pattern=fname).countTestCases()


def pytest_collected_count(root: Path | None = None) -> tuple:
    """``pytest --collect-only`` 数一遍，返回 ``(例数, 理由)``；跑不了就返回 (None, 理由)。

    只收集不执行（收集约 2s），所以当对账用很便宜；**不拿它跑用例** ——
    那会让同一批用例跑两遍、两个例数并排，入库时反而看不出哪个是真口径。
    """
    import re
    import shutil
    import subprocess
    import sys
    if shutil.which("pytest") is None:
        return None, "本机没装 pytest ⇒ 没法对账（装上 pytest 后这条会生效）"
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
                       cwd=str(root or repo_root()), capture_output=True, text=True, timeout=300)
    m = re.search(r"(\d+) tests? collected", r.stdout)
    if not m:
        return None, "pytest 输出里找不到 collected 计数（rc=%d）" % r.returncode
    return int(m.group(1)), "pytest --collect-only"


# ── L0 夹具收尾（v0.13.58）───────────────────────────────────────────────────
#: 为什么要它：L0 夹具根默认在 ~ 下（不能用 /tmp —— /tmp 是精度闸的排除路径前缀，
#: 夹具若住那儿会被先杀光，测不到排除逻辑本身）。但**造完从不删**，实测累积到
#: 230MB / 2402 个目录（tests/test_ghsettings、test_github_projects、test_hublog、
#: test_localprojects、test_modelcfg、test_prefs 六个文件各自造）。后果不是"占磁盘"，
#: 而是每个夹具都带 .git ⇒ 项目扫描把它们当真实项目 ⇒ 本机项目数 176（应 ≈44）⇒
#: L1 精度闸与 prepush 闸门一起变红。**闸门红是真信号，不该靠改断言消掉。**
def l0_fixture_root() -> Path:
    """夹具根（与各测试文件的 _L0_TMP 同一口径，单一真相源）。"""
    return Path(os.getenv("HUB_L0_TMP", str(Path.home() / "hub-l0test-fixtures")))


def l0_fixture_sweep(testcase: unittest.TestCase) -> Path:
    """造一个夹具根下的唯一子目录，并登记「用例结束即删」。

    用 ``testcase.addCleanup`` 而不是 atexit / 模块级 tearDown：进程被杀（闸门超时、
    Ctrl-C、OOM）时 atexit 不跑，而 ``addCleanup`` 在用例正常结束时一定会跑。
    只删自己造的那个子目录，不碰别人的（并发跑多个测试文件时互不干扰）。
    """
    import shutil
    import tempfile
    root = l0_fixture_root()
    root.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="", dir=str(root)))
    testcase.addCleanup(shutil.rmtree, str(d), True)
    return d


def l0_fixture_register(d) -> Path:
    """登记一个刚造好的夹具目录，进程退出时统一删（见 l0_fixture_cleanup）。

    为什么不直接在造的地方 rmtree：这些测试的 ``_mktmp(prefix)`` 是模块级函数，
    拿不到 ``self.addCleanup``（夹具在函数里造、用例还在跑）。折中：登记 + atexit。
    进程被 kill -9 时残留最多一份 —— 比修复前「每次运行都留、无限累积到 2402 个」
    已经收敛；且残留只影响磁盘，不影响任何断言（扫描侧另有 EXCLUDE_DIR_NAMES 兜底）。
    """
    _L0_CREATED.append(Path(d))
    return Path(d)


def l0_fixture_cleanup() -> None:
    """删掉本进程造出的全部夹具目录；根目录空了也一并收掉。"""
    import shutil
    for d in list(_L0_CREATED):
        shutil.rmtree(str(d), ignore_errors=True)
    _L0_CREATED.clear()
    root = l0_fixture_root()
    try:
        next(root.iterdir())
    except StopIteration:
        try:
            root.rmdir()
        except OSError:
            pass
    except OSError:
        pass


#: 本进程造出的夹具目录（l0_fixture_register 追加）
_L0_CREATED = []

import atexit  # noqa: E402
atexit.register(l0_fixture_cleanup)
