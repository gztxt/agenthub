#!/usr/bin/env python3
"""按「改了哪些文件」挑出该跑的测试子集 —— **开发迭代用，不是闸门**。

为什么要有它（实测非推断）：本仓 L0 全量 **1561 例 / ~54s**，而耗时是**均摊**的
（最慢一例仅 5.49s，只占 10%）⇒ 想靠"跳过慢用例"提速是错的方向，唯一有效的是
**少跑不相关的用例**。改一个 `src/db.py` 却跑全量，是每次迭代白付 54s。

⚠️ **三条边界，缺一条这个工具就会害人**：
  1. 它**不能**替代 `scripts/prepush.sh`。子集绿 ≠ 闸门绿 —— 映射漏了谁，这里不会知道
     （与 `test_tier_collector_parity` 防的"标准套件全绿但它没跑"是同一个病）。
  2. **映射不到的改动一律显式报出并退出码 2**，不装看不见（见 `unmapped`）。
  3. 与 `run_tier.py` 同口径摘掉 `FORCE_COLOR`：本机 shell 自带 `FORCE_COLOR=3`，
     会让依赖 node 输出纯数字的 22 例**假红**（09-23 实测）。

用法：
  bash scripts/run_tests.sh changed              # 取 git 工作区改动
  bash scripts/run_tests.sh changed src/db.py    # 指定文件
  bash scripts/run_tests.sh changed --allow-partial   # 有映射不到的文件也照跑
退出码：0 子集全绿；1 有用例失败；2 有改动映射不到测试（子集不完整）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def index_tests() -> dict:
    """test 文件名 → 正文。映射靠"测试正文提到这个模块名"，不靠命名约定（本仓无约定）。"""
    out = {}
    for p in sorted(TESTS.glob("test_*.py")):
        out[p.name] = p.read_text(encoding="utf-8", errors="replace")
    return out


def select(changed: list[str], index: dict) -> tuple[list[str], list[str]]:
    """→ (要跑的 test 文件名, 映射不到的改动文件)。纯函数，可单测。"""
    picked: set[str] = set()
    unmapped: list[str] = []
    for f in changed:
        name = Path(f).name
        if name.startswith("test_") and name.endswith(".py"):
            if name in index:
                picked.add(name)
            continue
        if f.startswith("src/"):
            mod = name[:-3] if name.endswith(".py") else name
            pat = re.compile(r"\b(import\s+%s\b|from\s+%s\s+import|%s\.)" % (re.escape(mod), re.escape(mod), re.escape(mod)))
            hit = {t for t, body in index.items() if pat.search(body)}
        elif f.startswith("static/") or f.startswith("templates/"):
            hit = {t for t, body in index.items() if name in body}
        else:
            hit = set()
        if hit:
            picked |= hit
        else:
            unmapped.append(f)
    return sorted(picked), unmapped


def changed_files(argv: list[str]) -> list[str]:
    args = [a for a in argv if not a.startswith("--")]
    if args:
        return args
    files: set[str] = set()
    # 两个来源分别取，**不解析 porcelain**：porcelain 的首字符就是状态位，
    # 一次 `line.strip()` 就会把 " M path" 的列位吃掉，切出 "cripts/xxx" 这种坏路径
    # （本脚本第一版实测踩到）。改用 --name-only（已跟踪改动）+ --others（未跟踪）。
    for cmd in (["git", "diff", "--name-only", "HEAD"],
                ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=20)
        except Exception:  # noqa: BLE001
            continue
        for path in r.stdout.splitlines():
            path = path.strip()
            if not path or path.endswith(".bak") or ".bak-" in path:
                continue
            files.add(path)
    return sorted(f for f in files if not f.startswith(("venv/", ".git/")))


def main(argv: list[str]) -> int:
    allow_partial = "--allow-partial" in argv
    changed = changed_files(argv)
    if not changed:
        print("[changed] 工作区无改动（或未指定文件）⇒ 无可跑子集。")
        print("          改了但没在 git 里？显式传路径：run_tests.sh changed src/db.py")
        return 0
    index = index_tests()
    picked, unmapped = select(changed, index)

    print(f"[changed] 改动 {len(changed)} 个文件 ⇒ 选中 {len(picked)} 个测试文件")
    for f in changed:
        print(f"          改动: {f}")
    if unmapped:
        print(f"[changed] ⚠ 有 {len(unmapped)} 个改动**映射不到任何测试**：")
        for f in unmapped:
            print(f"            {f}")
        print("          ⇒ 子集不完整。要么补测试，要么跑全量：run_tests.sh hermetic")
    if not picked:
        print("[changed] 无测试可跑。")
        return 2 if (unmapped and not allow_partial) else 0

    env = dict(os.environ)
    env.pop("FORCE_COLOR", None)          # 与 run_tier.py 同口径（22 例假红坑）
    cmd = [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", "-q",
           *[f"tests/{t}" for t in picked]]
    print("[changed] + " + " ".join(cmd[1:]))
    print("[changed] ⚠ 这是子集，**不是闸门**。提交/推送前请跑 scripts/prepush.sh（L0 全量 + L1 + 六步扫描）。")
    rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    if rc == 0 and unmapped and not allow_partial:
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
