#!/usr/bin/env python3
"""按层跑测试，并把「L0 必须零跳过」做成**可机器判**的闸门。

为什么不用一行 `unittest discover` 就完事：
  discover 会把 host 用例的 SKIP 和"某个 hermetic 用例自己偷偷 skipTest"混在
  同一个 `skipped=N` 里 —— 而后者正是分层放错的信号（干净 runner 上它其实什么
  都没测）。本脚本按 tiers.is_host 把两层拆开，各自给数，并据此判闸。

用法：
  venv/bin/python scripts/run_tier.py hermetic   # 只 L0；**默认把 HOME 换成空目录**
  venv/bin/python scripts/run_tier.py host       # 只 L1（换机时应整层显式跳过）
  venv/bin/python scripts/run_tier.py all        # 两层都跑，L0 段隔离 HOME、L1 段还原
  venv/bin/python scripts/run_tier.py hermetic --real-home  # 排障用：保持真实 HOME（危险）
退出码：0 全绿；1 有用例失败；2 分层被放错（L0 里出现 skip）。

HOME 隔离为什么是**默认**而不是开关（2026-11-02）：
  只要跑 L0 的地方继承真实 HOME，用例里任何一次 `codebuddy --session-id X`
  都会把会话写进 `~/.codebuddy/projects/<真实项目>/X.jsonl` —— 用户历史列表里
  就多出一条测试残留（实测 cb-par-test/2/3 + 一条 UUID，四条只能手删）。
  这不是「用例写错了」，是**默认值没把宿主隔开**：同一个用例在干净 runner 上
  写入的是临时目录，在这台机上写进用户的真实历史。`--fake-home` 早就存在，
  但默认关着 ⇒ 谁都不加。现改为默认开，并把 `--fake-home` 保留为兼容别名。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import tiers  # noqa: E402

#: 闸门结果的合法取值（P0-4）。**三态而不是布尔**：
#:   pass         —— 跑完且结论成立
#:   fail         —— 跑完且结论不成立
#:   insufficient —— 跑完了但**没法下结论**（例：L0 出现 skip ⇒ 那些用例在干净 runner
#:                  上一个断言都没跑）。它**既不是 pass 也不是 fail**，退出码取 2。
#: 本仓旧口径把这种情形写成 "skipped=N" 然后继续报全绿 —— 「禁把 SKIP 当 PASS」
#: 在纸面上写了很久，这里把它变成结果对象里的一个值。
VALIDITY = ("pass", "fail", "insufficient")

#: validity → 退出码。0 绿；1 真失败；2 不可判定（沿用旧脚本对 L0-skip 的 2，不新造码）。
RC_BY_VALIDITY = {"pass": 0, "fail": 1, "insufficient": 2}


def gate_result(tier: str, *, ran: int, skipped: int = 0, failures: int = 0,
                errors: int = 0, zero_skip: bool = False) -> dict:
    """闸门产物的**四字段结果对象**（P0-4）：`exec_status + validity + evidence + quality`。

    为什么要拆四字段而不是回一个 bool：一个布尔值无法区分「没跑」「跑了挂了」与
    「跑了但不可判定」—— 而这三者的处置完全不同。特别是第三类：它看着像绿，
    实际什么都没证明（测试自己 skipTest、某层未被发现、夹具缺失都会落到这里）。

      exec_status —— **执行**层面：ran（跑了）/ empty（一个用例都没发现）
      validity    —— **结论**层面：pass / fail / insufficient
      evidence    —— 原始计数（ran/skipped/failures/errors），**不加工、不折算**
      quality     —— 证据可信度：是否要求零跳过、是否 decisive

    硬约束：`zero_skip=True`（L0）且 `skipped>0` ⇒ **insufficient**。
    它与 `evidence` 里的 skipped 计数共存 —— 「跳过几个」是证据，「因此不可判定」是结论，
    两者不许互相替代（早期版本就是只报 skipped 然后当绿）。
    """
    any_seen = bool(ran or skipped or failures or errors)
    exec_status = "ran" if any_seen else "empty"
    if failures or errors:
        validity = "fail"
    elif zero_skip and skipped:
        validity = "insufficient"
    elif not any_seen:
        validity = "insufficient"          # 一个用例都没跑过 ⇒ 什么也没证明
    else:
        validity = "pass"
    return {
        "tier": tier,
        "exec_status": exec_status,
        "validity": validity,
        "evidence": {"ran": int(ran), "skipped": int(skipped),
                     "failures": int(failures), "errors": int(errors)},
        "quality": {"zero_skip_required": bool(zero_skip),
                    "skips_were_treated_as": "insufficient" if (zero_skip and skipped) else "n/a",
                    "decisive": validity != "insufficient"},
    }


def _emit_gate(gr: dict) -> None:
    """把结果对象打成**一行机器可读**的 JSON（供 prepush/CI/人三相消费）。"""
    assert gr["validity"] in VALIDITY, gr["validity"]
    print("[gate] " + json.dumps(gr, ensure_ascii=False, sort_keys=True))


def _resolve(test: unittest.TestCase):
    """从 TestId 反查被装饰的 callable，判它是不是 host 层。"""
    parts = test.id().split(".")          # tests.test_x.TestY.test_z
    obj = None
    holder = None
    try:
        obj = sys.modules[parts[0]]
        for p in parts[1:]:
            holder = obj
            obj = getattr(obj, p, None)
            if obj is None:
                return False
    except Exception:  # noqa: BLE001
        return False
    # obj 现在是绑定方法或函数；holder 是它所属的类
    func = getattr(obj, "__func__", obj)
    if tiers.is_host(func):
        return True
    cls = holder if isinstance(holder, type) else None
    return bool(cls is not None and tiers.is_host(cls))


def _split(pattern: str):
    suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern=pattern)
    herm, host, other = [], [], []

    def walk(s):
        for x in s:
            if isinstance(x, unittest.TestSuite):
                walk(x)
            else:
                host.append(x) if _resolve(x) else herm.append(x)
    walk(suite)
    return herm, host, other


def _run(tests, label, stream=sys.stderr):
    suite = unittest.TestSuite(tests)
    res = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    skipped = [t for t, _r in res.skipped]
    return res, len(res.skipped), len(tests)


def _parity() -> int:
    """收集器对账：unittest 收进几例 vs pytest 收进几例，必须相等。

    为什么要在**分层跑完之后**再做：`run_tests.sh all` 只跑 L0+L1，而 pytest 一次
    收全标准层 —— 两边例数不等就说明有 test_ 只有某个 runner 收得到（2026-10-02 实测：
    模块级 pytest 风格闸门 14 条 ⇒ 961 vs 975，标准套件压根没跑本批闸门却报全绿）。
    只做 `--collect-only`（约 2s），**不拿 pytest 跑用例** —— 那会同一批跑两遍、
    两个例数并排，入库时反而看不出哪个是真口径。详见 tests/tiers.py 同名段。
    """
    sys.path.insert(0, str(ROOT / "tests"))
    import tiers  # noqa: E402
    bad = tiers.module_level_test_functions()
    if bad:
        print("[tier] ❌ 标准层有 unittest 收不到的 test_（标准套件里它们不会执行）：")
        for b in bad:
            print("        " + b)
        return 2
    n_unit = tiers.unittest_collected_count()
    n_pytest, why = tiers.pytest_collected_count()
    if n_pytest is None:
        print("[tier] ⚠ 收集器对账跳过：%s（unittest=%d 例已跑）" % (why, n_unit))
        return 0
    if n_unit != n_pytest:
        print("[tier] ❌ 收集器对账：unittest=%d ≠ pytest=%d ⇒ 有用例只被一个 runner 收进来"
              % (n_unit, n_pytest))
        return 2
    print("[tier] ✅ 收集器对账：unittest=%d = pytest=%d（%s）" % (n_unit, n_pytest, why))
    return 0


#: 生产 unit 通过 `Environment=` 注入的档位切换器，**不得泄进 L0**。
#: 2026-11-13 实测：重启后会话继承 `CHAIN_JUDGE=shadow`，L0 里 3 条「默认 off」断言假红
#: （主仓同样红，与改动无关）。L0 的默认值必须由代码决定，不由宿主 env 决定。
L0_SCRUBBED = ("CHAIN_JUDGE",)
_SAVED_SCRUB: dict[str, str] = {}

# 必须在**模块加载时**剥离，早于任何 tests import —— 否则 `chain_judge` 这类
# 「import 时读 env」的模块会先把生产档位固化下来，`isolate_home` 再清也来不及
# （2026-11-13 实测：清理只放在 isolate_home 里，失败数从 3 只降到 2）。
for _k in L0_SCRUBBED:
    if _k in os.environ:
        _SAVED_SCRUB[_k] = os.environ.pop(_k)


def isolate_home(saved_home: str | None, log=print) -> str:
    """把 HOME 换成一次性的空目录，返回该目录。

    假 HOME 必须落在 `<真实 HOME>/hub-l0test-fixtures` 下、**不能落 /tmp**：
    v0.13.31 P1 扫描闸把 /tmp 整前缀剔除（EXCLUDE_PATH_PREFIXES），假 HOME
    一旦在 /tmp，test_localprojects/test_github_projects 的夹具全被排除
    ⇒ hermetic-clean 12 例假红（2026-09-26 排障定案：换台机器/换 TMPDIR
    就时绿时红，正是这种环境耦合）。与其它 L0 夹具同域即可。

    `HUB_TEST_HOME` 是给子进程的自述标记：用例/探针据此断言「我在隔离档里」，
    而不是靠 `HOME != /home/xxx` 这种碰运气的比较。
    """
    _base = Path(saved_home or str(Path.home())) / "hub-l0test-fixtures"
    _base.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="hub-clean-home-", dir=str(_base))
    os.environ["HOME"] = tmp
    os.environ["HUB_TEST_HOME"] = tmp
    os.environ["HUB_HOST_TESTS"] = "0"      # 干净 runner 上 host 层必须整层跳过
    log(f"[tier] 已把 HOME 换成空目录 {tmp}（L0 不写真实 ~/.codebuddy / ~/.claude）")
    return tmp


def restore_home(saved_home: str | None, saved_host_tests: str | None) -> None:
    if saved_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = saved_home
    os.environ.pop("HUB_TEST_HOME", None)
    for k, v in _SAVED_SCRUB.items():
        os.environ[k] = v
    _SAVED_SCRUB.clear()
    if saved_host_tests is None:
        os.environ.pop("HUB_HOST_TESTS", None)
    else:
        os.environ["HUB_HOST_TESTS"] = saved_host_tests


def main(argv):
    tier = (argv[0] if argv else "all").lower()
    if tier == "pytest":
        # `run_tests.sh pytest` 的直通档：不走分层闸门，但**同样不许碰真实 HOME**
        # ——否则「只想快速看全绿」的人会用这条路径把测试会话写进用户历史。
        isolate_home(os.environ.get("HOME"))
        sys.stdout.flush()      # execv 不 flush：不打这一行，调用者就看不到自己已进隔离档
        os.execv(sys.executable, [sys.executable, "-m", "pytest", "tests", "-v", *argv[1:]])
        return 2    # execv 成功即进程已被替换；只有它被替身接管（测试）才走得到这行
    real_home = "--real-home" in argv
    if "--fake-home" in argv and not real_home:
        # 默认已是隔离档；旧脚本/文档还在传这个开关，保留别名继续可用
        print("[tier] --fake-home 已是默认行为（保留为兼容别名）")
    saved_home = os.environ.get("HOME")
    saved_host_tests = os.environ.get("HUB_HOST_TESTS")
    saved_memdb = os.environ.get("MEMINDEX_DB")

    # 2026-10-02：L0 hermetic 层的**投影隔离**，对所有会跑 L0 的档位生效
    # （不只 --fake-home）。原先 memindex.DB_PATH 是仓库相对硬编码、无注入口，
    # 于是 L0 用例会去读**生产那份 1.6 GiB 真实索引**——用例把临时语料根
    # rmtree 掉后投影照样答得出来（ok=True），隔离性被静默破坏，而且是假绿：
    # 红会逼人看，假绿只会沉淀成“已通过”。real-HOME 档同样中招（6 例失败）。
    # 钉到不存在的路径后 probe 返回 no_index，_search_proj 按 fail-closed
    # 契约回退 rg ⇒ 6 例恢复原语义，且 L0 重新满足“无宿主依赖”的定义。
    if tier in ("hermetic", "all"):
        _anchor = Path(saved_home or str(Path.home()))
        os.environ["MEMINDEX_DB"] = str(_anchor / "hub-l0test-fixtures" / "no-such-memindex.db")
        print("[tier] 已把 memindex 投影钉到不存在的路径（L0 不读生产索引）")
        # L0 身份替身（2026-11-07）：真实 users.identity_of 需要会话行 + pbkdf2 校验，
        # 而 L0 是空 HOME/临时库，150 条既有夹具的语义是「带凭据头 = 已登录」。
        # 替身只活在 tests/authfix.py，不引入任何生产侧 env 开关。
        try:
            # 必须先把 src 放进 sys.path：authfix.install() 要 import users，
            # 而本脚本只有 ROOT（仓根），users.py 在 src/ 下。少了这一行，
            # 异常会被下面的 except 吞成一行警告，随后全是 401 —— 假红灾难。
            sys.path.insert(0, str(ROOT / "src"))
            sys.path.insert(0, str(ROOT / "tests"))
            import authfix  # noqa: E402
            authfix.install()
        except Exception as e:  # noqa: BLE001
            print("[tier] ⚠ authfix 未装上：%s（身份相关用例会红）" % e)

    # 2026-10-05：摘掉 FORCE_COLOR（同一个「L0 不该继承调用者环境」的关切）。
    # 本机 shell 里 FORCE_COLOR=3 是常态，于是 node 把 console.log(数字) 染成 ANSI：
    #   node -e "console.log(60)" → '\x1b[33m60\x1b[39m'
    # 依赖「node 输出一个纯数字」的用例用 int(...) 解析它，直接 ValueError ——
    # test_search_fuzzy / test_skill_list_q_parity 实测 22 例假红（2026-10-05）。
    # ⚠ NO_COLOR / NODE_DISABLE_COLORS 在 FORCE_COLOR 存在时**无效**：node 自己会打印
    #   "Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set"
    # 然后照样染色（实测）⇒ 唯一可靠解是摘掉变量本身，不是再叠一个反向开关。
    # 为什么这里能修：测试是 `unittest.TestSuite().run()` **进程内**跑的（本文件），
    # os.environ 的改动会传播到每个 subprocess.run(["node", ...])。
    # L0 的定义是「结论必须一模一样」——继承调用者的终端着色显然不在这个定义内。
    os.environ.pop("FORCE_COLOR", None)

    herm, host, other = _split("test_*.py")
    pairs = {"hermetic": [("L0-hermetic", herm)],
             "host": [("L1-host", host)],
             "all": [("L0-hermetic", herm), ("L1-host", host)]}.get(
        tier if tier in ("hermetic", "host", "all") else "all")
    # 判据：**只要这一趟会跑 L0，就默认隔离 HOME**；L1 需要真实安装，跑之前还原。
    isolate = (not real_home) and any(lbl.startswith("L0") for lbl, _ in pairs)
    if isolate:
        isolate_home(saved_home)
    elif real_home:
        print("[tier] ⚠ --real-home：HOME 保持真实值 —— L0 会读写 ~/.codebuddy / ~/.claude，"
              "只用于排障，别当默认")
    try:
        rc = 0
        print(f"[tier] L0 hermetic={len(herm)} 例   L1 host={len(host)} 例")
        for label, tests in pairs:
            if label.startswith("L1") and isolate:
                # L1 断言的是真实安装面（CLI 在不在、能不能拉起），隔离 HOME 会把它跑成
                # 假红；还原只在这一段生效，L0 已经跑完、结果已落盘。
                restore_home(saved_home, saved_host_tests)
                print("[tier] L1 段：HOME 已还原为真实值（host 用例要真实安装面）")
            if not tests:
                print(f"[tier] {label}: 0 例（本层无用例）")
                # P0-4：「0 例」不是「全绿」——它是**什么都没证明**（判据没被发现=
                # 发现器坏了 / 文件不在）。旧版只打一行就 continue、rc 不变 ⇒ 静默放行。
                gr0 = gate_result(label, ran=0, skipped=0, zero_skip=label.startswith("L0"))
                _emit_gate(gr0)
                rc = max(rc, RC_BY_VALIDITY[gr0["validity"]])
                continue
            stream = sys.stdout
            res, nskip, ntot = _run(tests, label, stream)
            print(f"[tier] {label}: ran={ntot - nskip} skipped={nskip} failures={len(res.failures)} "
                  f"errors={len(res.errors)}")
            gr = gate_result(label, ran=ntot - nskip, skipped=nskip,
                             failures=len(res.failures), errors=len(res.errors),
                             zero_skip=label.startswith("L0"))
            _emit_gate(gr)
            rc = max(rc, RC_BY_VALIDITY[gr["validity"]])
            if gr["validity"] == "insufficient" and nskip:
                print(f"[tier] ❌ 分层放错：L0 出现 {nskip} 个 skip —— 这些用例在干净 runner 上"
                      f"什么都没测（validity=insufficient，**不是** pass），"
                      f"请打 @tiers.host_only 或去掉 skipTest：")
                for t, _r in res.skipped:
                    print(f"        {t.id()}")
        if tier == "all" and host:
            print("[tier] （L1 已随 all 一起跑过；单独看：run_tier.py host）")
        rc = max(rc, _parity())
        return rc
    finally:
        # 幂等：L1 段可能已经还原过一次；这里再收一次，保证本进程后续逻辑不继承夹具值
        if isolate:
            restore_home(saved_home, saved_host_tests)
        # 还原投影落位，避免本进程后续逻辑（如被当库 import）继承了夹具值
        if saved_memdb is None:
            os.environ.pop("MEMINDEX_DB", None)
        else:
            os.environ["MEMINDEX_DB"] = saved_memdb


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
